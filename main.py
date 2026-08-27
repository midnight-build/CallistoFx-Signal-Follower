"""
Telegram -> MT5 signal-copying bot.

Listens for 'Buy/Sell Now' + range/SL/TP message pairs across two signal
sources (the main CallistoFx Premium channel, and the 'Institutional
Trader' topic in the CallistoFx Grind Room forum group), opens the
corresponding trade in MT5, and manages it with a fixed pip-based partial
close ladder and automatic breakeven.

Requires TEL_API_ID and TEL_API_HASH in the environment (.env), a running
MT5 terminal reachable via mt5linux on 127.0.0.1:8001, and the symbol
configured below (XAUUSD.s) enabled with AutoTrading on.
"""

import os
import re
import json
import time
import asyncio
import logging
import atexit
import fcntl

from telethon import TelegramClient, events
from mt5linux import MetaTrader5

import config

# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('signal_bot')

# ==================================================
# SINGLE-INSTANCE LOCK
# ==================================================
# Prevents two copies of this bot running at once (e.g. forgetting one's
# already running elsewhere), which would double-trade every signal. Held
# for as long as this process is alive; released automatically on exit,
# clean or not, since it's tied to the file descriptor.

_lock_file_handle = None


def acquire_lock():
    global _lock_file_handle

    _lock_file_handle = open(config.LOCK_FILE, 'w')

    try:
        fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.critical(
            'Another instance of this bot appears to already be running '
            '(lock file %s is held) - exiting to avoid double-trading '
            'every signal.', config.LOCK_FILE)
        raise SystemExit(1)

    _lock_file_handle.write(str(os.getpid()))
    _lock_file_handle.flush()
    logger.info('Acquired single-instance lock (%s)', config.LOCK_FILE)


def release_lock():
    global _lock_file_handle

    if _lock_file_handle is None:
        return

    try:
        fcntl.flock(_lock_file_handle, fcntl.LOCK_UN)
        _lock_file_handle.close()
    except Exception:
        pass

    _lock_file_handle = None


atexit.register(release_lock)
acquire_lock()

# ==================================================
# TELEGRAM / MT5 SETUP
# ==================================================

client = TelegramClient(
    'trade_session',
    config.API_ID,
    config.API_HASH
)

mt5 = MetaTrader5(host='127.0.0.1', port=8001)

logger.info('Initializing MT5...')

if not mt5.initialize():
    logger.error('MT5 initialization failed')
    quit()

logger.info('MT5 Connected')

terminal_info = mt5.terminal_info()
if terminal_info:
    logger.info('Trade Allowed: %s', terminal_info.trade_allowed)
    if not terminal_info.trade_allowed:
        logger.warning('AutoTrading is DISABLED in MT5!')
        logger.warning('Enable it in MT5 GUI and restart the service')


def ensure_mt5_connected():
    """
    Verifies the MT5 bridge connection is alive and tries to restore it if
    not (Wine/MT5 crashing or the bridge server dropping are the usual
    causes). After config.MT5_RECONNECT_ATTEMPTS failed tries, gives up
    and exits the whole process - systemd (see callistofx.service) then
    restarts it from a clean state rather than the bot limping along
    unable to manage trades.
    """

    try:
        if mt5.terminal_info() is not None:
            return
    except Exception:
        pass

    logger.warning('MT5 connection appears to be down - attempting to reconnect')

    for attempt in range(1, config.MT5_RECONNECT_ATTEMPTS + 1):
        try:
            if mt5.initialize() and mt5.terminal_info() is not None:
                logger.info('MT5 reconnected (attempt %d)', attempt)
                return
        except Exception:
            logger.exception('MT5 reconnect attempt %d raised an exception', attempt)

        logger.warning(
            'MT5 reconnect attempt %d/%d failed - retrying in %ds',
            attempt, config.MT5_RECONNECT_ATTEMPTS, config.MT5_RECONNECT_DELAY_SECONDS)
        time.sleep(config.MT5_RECONNECT_DELAY_SECONDS)

    logger.critical(
        'Could not reconnect to MT5 after %d attempts - exiting so '
        'systemd can restart the process', config.MT5_RECONNECT_ATTEMPTS)
    raise SystemExit(1)

# --------------------------------------------------
# Derived TP/lot settings (built from config.py - see that file to change
# any of the values used here)
# --------------------------------------------------

SYMBOL = config.SYMBOL
PIP_VALUE = config.PIP_VALUE
MAX_ACTIVE_TRADES = config.MAX_ACTIVE_TRADES
RISK_PERCENT = config.RISK_PERCENT
MAX_LOT = config.MAX_LOT
MONITOR_INTERVAL_SECONDS = config.MONITOR_INTERVAL_SECONDS

NOTIFY_CHANNEL = config.NOTIFY_CHANNEL
GRIND_ROOM_CHAT_ID = config.GRIND_ROOM_CHAT_ID
INSTITUTIONAL_TOPIC_ID = config.INSTITUTIONAL_TOPIC_ID
INSTITUTE_UPDATE_CHAT_ID = config.INSTITUTE_UPDATE_CHAT_ID

TP_PIPS = [p for p, _ in config.MAIN_TP_STRUCTURE]
TP_FRACTIONS = [f for _, f in config.MAIN_TP_STRUCTURE]
BREAKEVEN_DISTANCE = TP_PIPS[0] * PIP_VALUE

# When set (via a 'lot size X' message), this overrides automatic risk-based
# sizing and becomes the new default for all future trades until changed
# again, or reset with a 'lot size auto' message.
CURRENT_LOT_OVERRIDE = None

INSTITUTIONAL_RISK_PERCENT = config.INSTITUTIONAL_RISK_PERCENT
INSTITUTIONAL_LOT_OVERRIDE = None

INSTITUTIONAL_TP_PIPS = [p for p, _ in config.INSTITUTIONAL_TP_STRUCTURE]
INSTITUTIONAL_TP_FRACTIONS = [f for _, f in config.INSTITUTIONAL_TP_STRUCTURE]
INSTITUTIONAL_BREAKEVEN_DISTANCE = INSTITUTIONAL_TP_PIPS[0] * PIP_VALUE

# ==================================================
# STATE
# ==================================================

# waiting_for_info[(chat_id, topic_id)] = 'buy' / 'sell' once a 'Buy/Sell
# Now' message has armed that chat/topic, waiting for the follow-up
# range/SL/TP message. topic_id is None for non-forum chats.
waiting_for_info = {}

active_trades = {}

# When True, the bot ignores new 'Buy/Sell Now' signals (both sources) but
# keeps managing any trades already open - set via /pause and /resume.
TRADING_PAUSED = False


def save_active_trades():
    """
    Writes active_trades to config.TRADE_STATE_FILE so a crash or restart
    doesn't 'forget' about positions that are still open in MT5. Called
    right after a trade opens/closes and once per monitor cycle, so it
    also captures TP-ladder and breakeven progress as it happens.
    """

    try:
        with open(config.TRADE_STATE_FILE, 'w') as f:
            json.dump({str(ticket): trade for ticket, trade in active_trades.items()}, f)
    except Exception:
        logger.exception('Failed to save trade state to %s', config.TRADE_STATE_FILE)


def load_active_trades():
    """
    Restores active_trades from config.TRADE_STATE_FILE on startup. Each
    saved ticket is checked against MT5's actual open positions first -
    anything that closed while the bot was offline is logged and dropped
    rather than recovered, since its TP/breakeven progress can no longer
    be trusted and there's nothing left to manage.
    """

    global active_trades

    if not os.path.exists(config.TRADE_STATE_FILE):
        return

    try:
        with open(config.TRADE_STATE_FILE) as f:
            raw = json.load(f)
    except Exception:
        logger.exception(
            'Failed to read %s - starting with no recovered trades',
            config.TRADE_STATE_FILE)
        return

    recovered = 0

    for ticket_str, trade in raw.items():
        ticket = int(ticket_str)

        if mt5.positions_get(ticket=ticket):
            active_trades[ticket] = trade
            recovered += 1
        else:
            logger.info(
                'Saved trade %s is no longer open in MT5 - not recovering '
                '(closed while the bot was offline)', ticket)

    if recovered:
        logger.info('Recovered %d open trade(s) from %s', recovered, config.TRADE_STATE_FILE)

    save_active_trades()  # drop the no-longer-open tickets from the file too

# ==================================================
# PATTERNS
# ==================================================

# 'buy now' / 'sell now' - the FIRST of the two messages required to open a
# trade. Arms the chat and waits for the follow-up range/SL/TP message.
action_pattern = re.compile(
    r'^\s*(buy|sell)\s+now\b[.!]?\s*$',
    re.IGNORECASE
)

# The SECOND message, e.g.:
#
#   🟢XAUUSD🟢
#   BUY RANGE: 4123 - 4129
#   SL 4119
#   TP : 4203
#
# Captures the trade type, the entry range (low/high, informational only -
# the trade still executes at current market price), the SL, and one or
# more TP values (e.g. '4015/4005/3995' - the last one is used as the
# broker's own TP field).
trade_pattern = re.compile(
    r'(?P<type>buy|sell)\s*range\W*'
    r'(?P<range_low>\d+\.?\d*)\s*-\s*(?P<range_high>\d+\.?\d*).*?'
    r'sl\W*(?P<sl>\d+\.?\d*).*?'
    r'tp\W*(?P<tp>[\d./\s]+)',
    re.IGNORECASE | re.DOTALL
)

# --------------------------------------------------
# Patterns that react to the SIGNAL PROVIDER's own messages, automatically.
# These stay as loose phrase matches - the provider's wording isn't
# something we control, so we watch for the phrases they actually use.
# --------------------------------------------------

# Matches messages that back out of an armed signal, e.g.:
# 'no longer looking', 'no longer looking for trade', 'cancel', 'cancelled trade'
cancel_pattern = re.compile(
    r'(no\s+longer\s+looking|cancel(led|ed)?\s*(trade|signal)?|scrap\s+(that|trade))',
    re.IGNORECASE)

# Sent when a trade should be fully closed out, e.g. 'POSITION CLOSED ...
# CLOSE ALL OPEN POSITIONS NOW'. Used by both the main channel and the
# Institutional Trader topic - each only closes its own source's trades.
close_all_pattern = re.compile(
    r'close\s+all\s+open\s+positions',
    re.IGNORECASE
)

# Sent when the provider wants an open trade's SL moved to breakeven, e.g.
# 'take your partials set SL TO BE & Take partials NOW.'
breakeven_phrase_pattern = re.compile(
    r'sl\s+to\s+be',
    re.IGNORECASE
)

# --------------------------------------------------
# Slash commands - these are things YOU type to control the bot directly,
# as opposed to the phrase patterns above which react to the signal
# provider. All are case-insensitive and ignore surrounding whitespace.
# --------------------------------------------------

lot_command_pattern = re.compile(r'^/lot\s+(?P<lot>\d+\.?\d*)\s*$', re.IGNORECASE)
lot_auto_command_pattern = re.compile(r'^/lotauto\s*$', re.IGNORECASE)
close_all_command_pattern = re.compile(r'^/closeall\s*$', re.IGNORECASE)
cancel_command_pattern = re.compile(r'^/cancel\s*$', re.IGNORECASE)
breakeven_command_pattern = re.compile(r'^/be\s*$', re.IGNORECASE)
status_command_pattern = re.compile(r'^/status\s*$', re.IGNORECASE)
help_command_pattern = re.compile(r'^/help\s*$', re.IGNORECASE)
pause_command_pattern = re.compile(r'^/pause\s*$', re.IGNORECASE)
resume_command_pattern = re.compile(r'^/resume\s*$', re.IGNORECASE)

HELP_TEXT = (
    '🤖 CallistoFx Bot Commands\n'
    '\n'
    'Trading control:\n'
    '/lot <size> - set a fixed lot size for future trades (this chat\'s source)\n'
    '/lotauto - reset to automatic risk-based sizing (this chat\'s source)\n'
    '/pause - stop opening new trades (existing trades still managed normally)\n'
    '/resume - resume opening new trades\n'
    '\n'
    'Trade management:\n'
    '/closeall - close all open trades from this source\n'
    '/cancel - cancel a signal that\'s armed but not yet completed\n'
    '/be - move SL to breakeven for open trades from this source\n'
    '\n'
    'Info:\n'
    '/status - open trades, lot settings, and pause state\n'
    '/help - show this message\n'
    '\n'
    'Note: /closeall, /cancel and /be also happen automatically when the '
    'signal provider posts a matching message in the channel.'
)


def get_topic_id(event):
    """
    Returns the forum topic id a message belongs to, or None if the chat
    isn't a forum (or the message is in the default 'General' topic).
    Needed because forum groups deliver every topic's messages under the
    same chat_id - this is what tells 'Institutional Trader' apart from
    the group's other topics.
    """

    reply = event.message.reply_to

    if reply is None or not getattr(reply, 'forum_topic', False):
        return None

    return reply.reply_to_top_id or reply.reply_to_msg_id


# ==================================================
# MT5 FUNCTIONS
# ==================================================


def get_min_lot(symbol):
    info = mt5.symbol_info(symbol)
    return info.volume_min if info else 0.01


def clamp_lot(symbol, lot):
    info = mt5.symbol_info(symbol)

    if info is None:
        return round(min(lot, MAX_LOT), 2)

    step = info.volume_step or 0.01
    lot = round(lot / step) * step

    lot = max(info.volume_min, min(lot, info.volume_max, MAX_LOT))

    return round(lot, 2)


def calculate_lot_size(symbol, entry_price, sl_price, order_type, risk_percent=RISK_PERCENT):
    """
    Risk-based position sizing: sizes the trade so that if SL is hit,
    the account loses approximately risk_percent of current balance.
    Falls back to the symbol's minimum lot if inputs are invalid, so a
    calculation failure can never silently produce a zero-lot order.
    """

    account = mt5.account_info()

    if account is None:
        logger.warning('Could not read account info - using minimum lot')
        return get_min_lot(symbol)

    risk_amount = account.balance * (risk_percent / 100.0)

    # Loss (in account currency) for exactly 1.0 lot moving from entry to SL
    loss_per_lot = mt5.order_calc_profit(
        order_type, symbol, 1.0, entry_price, sl_price)

    if not loss_per_lot:
        logger.warning('Could not calculate risk-based lot size - using minimum lot')
        return get_min_lot(symbol)

    raw_lot = risk_amount / abs(loss_per_lot)

    return clamp_lot(symbol, raw_lot)


def place_trade(symbol, signal, sl, tp, lot=None, risk_percent=RISK_PERCENT,
                 magic=config.MAIN_MAGIC_NUMBER, comment=config.MAIN_ORDER_COMMENT):

    logger.info('PLACE TRADE CALLED - signal=%s sl=%s tp=%s', signal, sl, tp)

    if not mt5.symbol_select(symbol, True):
        logger.error('Could not select %s', symbol)
        return None

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        logger.warning('No tick data for %s', symbol)
        return None

    if signal == 'buy':
        price = tick.ask
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        order_type = mt5.ORDER_TYPE_SELL

    symbol_info = mt5.symbol_info(symbol)

    if lot is None:
        lot = calculate_lot_size(symbol, price, sl, order_type, risk_percent)
        logger.info('Lot size (auto, %s%% risk): %s', risk_percent, lot)
    else:
        logger.info('Lot size (manual override): %s', lot)

    request = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': symbol,
        'volume': lot,
        'type': order_type,
        'price': round(price, symbol_info.digits),
        'sl': round(sl, symbol_info.digits),
        'tp': round(tp, symbol_info.digits),
        'deviation': 20,
        'magic': magic,
        'comment': comment,
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }

    logger.debug('Sending order: %s', request)

    result = mt5.order_send(request)

    logger.debug('MT5 response: %s', result)

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error('Order failed: %s', result.comment)
        return None

    logger.info('Order successful - ticket %s', result.order)

    return {'ticket': result.order, 'lot': lot}


def modify_sl(ticket, new_sl):

    pos = mt5.positions_get(ticket=ticket)

    if not pos:
        logger.warning('Cannot modify SL - position %s not found', ticket)
        return False

    pos = pos[0]

    request = {
        'action': mt5.TRADE_ACTION_SLTP,
        'position': ticket,
        'sl': new_sl,
        'tp': pos.tp
    }

    result = mt5.order_send(request)

    logger.debug('Modify SL result (ticket %s -> %s): %s', ticket, new_sl, result)

    return result.retcode == mt5.TRADE_RETCODE_DONE


def modify_tp(ticket, new_tp):
    """
    Changes ONLY the tp field on a position, keeping its current sl.
    Not currently called anywhere in the handler flow - kept as a utility
    for anyone extending the bot with manual TP adjustments.
    """

    pos = mt5.positions_get(ticket=ticket)

    if not pos:
        logger.warning('Cannot modify TP - position %s not found', ticket)
        return False

    pos = pos[0]

    request = {
        'action': mt5.TRADE_ACTION_SLTP,
        'position': ticket,
        'sl': pos.sl,
        'tp': new_tp
    }

    result = mt5.order_send(request)

    logger.debug('Modify TP result (ticket %s -> %s): %s', ticket, new_tp, result)

    return result.retcode == mt5.TRADE_RETCODE_DONE


def close_partial(ticket, volume):

    positions = mt5.positions_get(ticket=ticket)

    if not positions:
        logger.warning('Cannot close partial - position %s not found', ticket)
        return False

    pos = positions[0]

    tick = mt5.symbol_info_tick(pos.symbol)

    if pos.type == mt5.ORDER_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

    request = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': pos.symbol,
        'volume': volume,
        'type': order_type,
        'position': ticket,
        'price': price,
        'deviation': 20,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    logger.debug('Partial close result (ticket %s, vol %s): %s', ticket, volume, result)

    return result.retcode == mt5.TRADE_RETCODE_DONE


def check_tp1_and_move_to_breakeven(ticket, trade):
    """
    Checks whether price has moved BREAKEVEN_DISTANCE (= TP1's fixed pip
    distance) in favor of the trade. If so, moves SL to entry (breakeven)
    and marks it done so we don't keep re-sending the same modify request
    every monitor cycle.

    Each trade's 'auto_breakeven' flag is set when it's opened, based on
    AUTO_BREAKEVEN_MAIN / INSTITUTIONAL_AUTO_BREAKEVEN in config.py. When
    it's False for a source, breakeven only happens via /be or the
    provider's own 'sl to be' message, handled elsewhere in the handler.
    """

    if not trade.get('auto_breakeven', False):
        return

    if trade.get('breakeven_done'):
        return

    symbol = trade['symbol']
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        return

    entry = trade['entry']
    breakeven_distance = trade.get('breakeven_distance', BREAKEVEN_DISTANCE)

    if trade['signal'] == 'buy':
        # closing a buy uses bid price
        profit_distance = tick.bid - entry
    else:
        # closing a sell uses ask price
        profit_distance = entry - tick.ask

    if profit_distance >= breakeven_distance:
        logger.info(
            'Breakeven reached for ticket %s (+%.2f >= %.2f)',
            ticket, profit_distance, breakeven_distance)

        success = modify_sl(ticket, entry)

        if success:
            trade['breakeven_done'] = True
            logger.info('SL moved to breakeven (%s) for ticket %s', entry, ticket)
        else:
            logger.warning('Failed to move SL to breakeven for ticket %s', ticket)


def get_remaining_lot(ticket):
    pos = mt5.positions_get(ticket=ticket)
    return pos[0].volume if pos else None


def calculate_partial_close_volume(
        symbol,
        original_lot,
        remaining_lot,
        fraction):

    info = mt5.symbol_info(symbol)
    step = info.volume_step if info else 0.01
    min_lot = info.volume_min if info else 0.01

    raw = original_lot * fraction
    raw = round(raw / step) * step

    # A small lot / fraction combo can round DOWN to zero (e.g. 0.02 lot *
    # 20% = 0.004, rounds to 0 at a 0.01 step) - that would silently skip
    # the level entirely instead of taking a partial, since the caller
    # still marks it as hit either way. Take the smallest tradable step
    # instead, as long as there's enough position left to do so.
    if raw <= 0 and fraction > 0 and remaining_lot >= min_lot:
        raw = min_lot

    raw = min(raw, remaining_lot)

    # Don't leave an unclosable sliver behind
    if remaining_lot - raw < min_lot:
        raw = remaining_lot

    return round(raw, 2)


def check_tp_levels_and_partial_close(ticket, trade):
    """
    Checks each of this trade's TP levels in order and, once price reaches
    one, closes the configured fraction of the ORIGINAL lot at that level.
    A level that fails to close is left unmarked so it's retried on the
    next monitor cycle instead of being silently skipped.
    """

    levels = trade.get('computed_tp_levels')
    hit_flags = trade.get('computed_tp_hit')
    tp_pips = trade.get('tp_pips', TP_PIPS)
    tp_fractions = trade.get('tp_fractions', TP_FRACTIONS)

    if not levels or not hit_flags:
        return

    symbol = trade['symbol']
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        return

    for i, target in enumerate(levels):

        if hit_flags[i]:
            continue

        if trade['signal'] == 'buy':
            reached = tick.bid >= target
        else:
            reached = tick.ask <= target

        if not reached:
            continue

        remaining_lot = get_remaining_lot(ticket)

        if remaining_lot is None or remaining_lot <= 0:
            hit_flags[i] = True
            continue

        close_fraction = tp_fractions[i]
        close_volume = calculate_partial_close_volume(
            symbol, trade['original_lot'], remaining_lot, close_fraction
        )

        if close_volume <= 0:
            # Nothing meaningful left to close at this level - done with it.
            hit_flags[i] = True
            continue

        logger.info(
            'TP%d (%s pips, %.2f) reached for ticket %s - closing %s lots (%.0f%%)',
            i + 1, tp_pips[i], target, ticket, close_volume, close_fraction * 100)

        success = close_partial(ticket, close_volume)

        if success:
            new_remaining = get_remaining_lot(ticket)
            if new_remaining is not None:
                trade['remaining_lot'] = new_remaining
                logger.info('Partial close successful - remaining lot: %s', new_remaining)
            hit_flags[i] = True
        else:
            logger.warning('Partial close failed for ticket %s - will retry next cycle', ticket)


def get_closed_trade_pnl(ticket):
    """
    Looks up the historical deals for a closed position and sums their
    profit/swap/commission to get the final realized PnL.
    Returns None if history isn't available for some reason.
    """

    deals = mt5.history_deals_get(position=ticket)

    if not deals:
        return None

    return sum(d.profit + d.swap + d.commission for d in deals)


def format_tp_levels(trade):
    computed = trade.get('computed_tp_levels') or []
    fractions = trade.get('tp_fractions', TP_FRACTIONS)
    parts = [
        f'TP{i + 1}: {lvl:.2f} ({fractions[i] * 100:.0f}%)'
        for i, lvl in enumerate(computed)
    ]
    parts.append(
        f"TP{len(computed) + 1}: {trade.get('tp')} (broker's TP - run to full TP)")
    return ' | '.join(parts)


async def send_trade_open_notification(ticket, trade):

    msg = (
        f'🟢 TRADE OPENED\n'
        f"Type: {trade['signal'].upper()}\n"
        f"Symbol: {trade['symbol']}\n"
        f"Lot: {trade['lot']}\n"
        f"Entry: {trade['entry']}\n"
        f"SL: {trade['sl']}\n"
        f'TP Structure: {format_tp_levels(trade)}\n'
        f'PnL: $0.00'
    )

    try:
        await client.send_message(trade.get('notify_channel', NOTIFY_CHANNEL), msg)
    except Exception:
        logger.exception('Failed to send open notification for ticket %s', ticket)


async def send_trade_close_notification(ticket, trade, pnl):

    pnl_str = f'${pnl:.2f}' if pnl is not None else 'unknown'

    msg = (
        f'🔴 TRADE CLOSED\n'
        f"Type: {trade['signal'].upper()}\n"
        f"Symbol: {trade['symbol']}\n"
        f"Lot: {trade['lot']}\n"
        f"Entry: {trade['entry']}\n"
        f"SL: {trade['sl']}\n"
        f'TP Structure: {format_tp_levels(trade)}\n'
        f'Final PnL: {pnl_str}'
    )

    try:
        await client.send_message(trade.get('notify_channel', NOTIFY_CHANNEL), msg)
    except Exception:
        logger.exception('Failed to send close notification for ticket %s', ticket)


def _get_trade_pnl(ticket):
    """Live floating PnL for an open position, or None if it can't be read."""
    pos = mt5.positions_get(ticket=ticket)
    return pos[0].profit if pos else None


def build_status_message():
    """
    Builds the /status reply: pause state, lot settings for both sources,
    and a line per open trade with its live floating PnL.
    """

    lines = ['📊 CallistoFx Status', '']

    lines.append('⏸ Paused' if TRADING_PAUSED else '▶️ Running')
    lines.append('')

    main_lot = (
        f'{CURRENT_LOT_OVERRIDE} (manual)' if CURRENT_LOT_OVERRIDE is not None
        else f'auto ({RISK_PERCENT}% risk)'
    )
    institutional_lot = (
        f'{INSTITUTIONAL_LOT_OVERRIDE} (manual)' if INSTITUTIONAL_LOT_OVERRIDE is not None
        else f'auto ({INSTITUTIONAL_RISK_PERCENT}% risk)'
    )
    lines.append(f'Main lot size: {main_lot}')
    lines.append(f'Institutional lot size: {institutional_lot}')
    lines.append('')

    lines.append(f'Active trades: {len(active_trades)}/{MAX_ACTIVE_TRADES}')

    if not active_trades:
        lines.append('(none open)')
    else:
        for ticket, trade in active_trades.items():
            pnl = _get_trade_pnl(ticket)
            pnl_str = f'${pnl:.2f}' if pnl is not None else 'unknown'
            be_str = 'BE done' if trade.get('breakeven_done') else 'BE pending'
            lines.append(
                f"- #{ticket} [{trade.get('source', 'main')}] "
                f"{trade['signal'].upper()} {trade['symbol']} "
                f"lot {trade['lot']} @ {trade['entry']} | PnL {pnl_str} | {be_str}"
            )

    return '\n'.join(lines)


# ==================================================
# TELEGRAM SIGNALS
# ==================================================

# Chat IDs are all set in config.py - see MAIN_CHANNEL_ID, TEST_CHANNEL_ID,
# GRIND_ROOM_CHAT_ID, and INSTITUTE_UPDATE_CHAT_ID.

_listened_chats = [
    config.MAIN_CHANNEL_ID,
    config.TEST_CHANNEL_ID,
]

if GRIND_ROOM_CHAT_ID is not None:
    _listened_chats += [GRIND_ROOM_CHAT_ID, INSTITUTE_UPDATE_CHAT_ID]


@client.on(events.NewMessage(chats=_listened_chats))
async def close_all_trades(source):
    """Closes every active trade belonging to `source`. Returns closed tickets."""

    closed = []

    for ticket in list(active_trades.keys()):

        trade = active_trades.get(ticket)

        if not trade or trade.get('source') != source:
            continue

        remaining_lot = get_remaining_lot(ticket)

        if remaining_lot and remaining_lot > 0:
            await asyncio.to_thread(close_partial, ticket, remaining_lot)

        pos = mt5.positions_get(ticket=ticket)

        if not pos:
            closed_trade = active_trades.pop(ticket, None)
            if closed_trade:
                pnl = await asyncio.to_thread(get_closed_trade_pnl, ticket)
                await send_trade_close_notification(ticket, closed_trade, pnl)
                closed.append(ticket)

    if closed:
        save_active_trades()

    return closed


def apply_breakeven_for_source(source):
    """Moves SL to breakeven for every open trade of `source`. Returns moved tickets."""

    moved = []

    for ticket, trade in active_trades.items():

        if trade.get('source', 'main') != source:
            continue

        if trade.get('breakeven_done'):
            continue

        success = modify_sl(ticket, trade['entry'])

        if success:
            trade['breakeven_done'] = True
            moved.append(ticket)

    return moved


async def handler(event):

    global CURRENT_LOT_OVERRIDE, INSTITUTIONAL_LOT_OVERRIDE, TRADING_PAUSED

    text = (event.raw_text or '').lower()
    chat_id = event.chat_id
    topic_id = get_topic_id(event)
    institutional = chat_id == GRIND_ROOM_CHAT_ID and topic_id == INSTITUTIONAL_TOPIC_ID

    # A message can target the institutional source either from its forum
    # topic directly, or from the Institute update chat set up to control it.
    command_source = 'institutional' if (
        institutional or chat_id == INSTITUTE_UPDATE_CHAT_ID) else 'main'

    logger.debug('New message (chat %s, topic %s): %s', chat_id, topic_id, text)

    # -------------------------
    # GLOBAL COMMANDS - work from any chat the bot is listening to,
    # regardless of source, since they don't need trade-source context.
    # -------------------------

    if status_command_pattern.match(text):
        await event.reply(await asyncio.to_thread(build_status_message))
        return

    if help_command_pattern.match(text):
        await event.reply(HELP_TEXT)
        return

    if pause_command_pattern.match(text):
        TRADING_PAUSED = True
        logger.info('Trading paused via /pause')
        await event.reply('⏸ Trading paused - no new trades will be opened. '
                           'Existing trades are still managed normally. Send /resume to continue.')
        return

    if resume_command_pattern.match(text):
        TRADING_PAUSED = False
        logger.info('Trading resumed via /resume')
        await event.reply('▶️ Trading resumed.')
        return

    # -------------------------
    # Ignore other topics in the Grind Room group - only 'Institutional
    # Trader' (topic 515) is a signal source, everything else there is
    # just chatter we don't want feeding the trade patterns below.
    # -------------------------

    if chat_id == GRIND_ROOM_CHAT_ID and not institutional:
        return

    # -------------------------
    # 'Institute update' chat - command + notifications only, for
    # Institutional Trader trades. No signals are ever parsed here.
    # -------------------------

    if chat_id == INSTITUTE_UPDATE_CHAT_ID:

        if lot_auto_command_pattern.match(text):
            INSTITUTIONAL_LOT_OVERRIDE = None
            logger.info('Institutional lot sizing reset to automatic risk-based calculation')
            await event.reply('✅ Institutional lot sizing reset to automatic (risk-based) calculation')
            return

        lot_match = lot_command_pattern.match(text)

        if lot_match:
            INSTITUTIONAL_LOT_OVERRIDE = float(lot_match.group('lot'))
            logger.info('Institutional lot size override set -> %s', INSTITUTIONAL_LOT_OVERRIDE)
            await event.reply(f'✅ Institutional lot size set to {INSTITUTIONAL_LOT_OVERRIDE}')
            return

        if close_all_command_pattern.match(text):
            closed = await close_all_trades('institutional')
            await event.reply(f'✅ Closed {len(closed)} institutional trade(s).'
                               if closed else 'ℹ️ No open institutional trades to close.')
            return

        if breakeven_command_pattern.match(text):
            moved = apply_breakeven_for_source('institutional')
            await event.reply(f'✅ Moved {len(moved)} trade(s) to breakeven.'
                               if moved else 'ℹ️ No institutional trades needed breakeven.')
            return

        return

    state_key = (chat_id, topic_id)

    # -------------------------
    # LOT SIZE COMMANDS (main channel only - independent of trade state).
    # Institutional lot size is controlled from the 'Institute update'
    # chat instead, handled above.
    # -------------------------

    if not institutional:

        if lot_auto_command_pattern.match(text):
            CURRENT_LOT_OVERRIDE = None
            logger.info('Lot sizing reset to automatic risk-based calculation')
            await event.reply('✅ Lot sizing reset to automatic (risk-based) calculation')
            return

        lot_match = lot_command_pattern.match(text)

        if lot_match:
            CURRENT_LOT_OVERRIDE = float(lot_match.group('lot'))
            logger.info(
                'Lot size override set -> %s (this is now the default for future trades)',
                CURRENT_LOT_OVERRIDE)
            await event.reply(f'✅ Lot size set to {CURRENT_LOT_OVERRIDE}')
            return

    # -------------------------
    # MANUAL TRADE-MANAGEMENT COMMANDS (/closeall, /cancel, /be) - the
    # slash-command equivalents of the automatic phrase detection below,
    # for when you want to trigger them yourself rather than waiting for
    # the signal provider to post a matching message.
    # -------------------------

    if close_all_command_pattern.match(text):
        closed = await close_all_trades(command_source)
        waiting_for_info.pop(state_key, None)
        await event.reply(f'✅ Closed {len(closed)} {command_source} trade(s).'
                           if closed else f'ℹ️ No open {command_source} trades to close.')
        return

    if cancel_command_pattern.match(text):
        had_waiting = waiting_for_info.pop(state_key, None) is not None
        await event.reply('✅ Armed signal cancelled.' if had_waiting
                           else 'ℹ️ Nothing was armed to cancel.')
        return

    if breakeven_command_pattern.match(text):
        moved = apply_breakeven_for_source(command_source)
        await event.reply(f'✅ Moved {len(moved)} trade(s) to breakeven.'
                           if moved else f'ℹ️ No {command_source} trades needed breakeven.')
        return

    # -------------------------
    # CANCELLATION (signal provider's own phrasing - checked first, always
    # wins over other patterns)
    # -------------------------

    if cancel_pattern.search(text):

        had_waiting = waiting_for_info.pop(state_key, None) is not None

        if had_waiting:
            logger.info('Signal cancelled - state cleared for %s', state_key)
        else:
            logger.debug('Cancellation message received, but nothing was armed')

        return

    # -------------------------
    # CLOSE ALL - e.g. 'POSITION CLOSED ... CLOSE ALL OPEN POSITIONS NOW'.
    # Fully closes every trade sourced from whichever chat/topic sent the
    # message (main channel or Institutional Trader); the other source's
    # trades are untouched.
    # -------------------------

    if close_all_pattern.search(text):

        source = 'institutional' if institutional else 'main'

        logger.info('Close-all (%s) detected from provider message', source)

        await close_all_trades(source)

        waiting_for_info.pop(state_key, None)
        return

    # -------------------------
    # BUY NOW / SELL NOW
    # This is the FIRST of the two required messages. It arms the
    # chat/topic directly and waits for the follow-up range/SL/TP message.
    # -------------------------

    if state_key not in waiting_for_info and action_pattern.search(text):

        if TRADING_PAUSED:
            logger.info('Ignoring signal - trading is paused (%s)', state_key)
            await event.reply('⏸ Bot is paused - ignoring this signal. Send /resume to continue.')
            return

        signal = 'buy' if 'buy' in text else 'sell'

        waiting_for_info[state_key] = signal

        logger.info('Armed for %s (%s) - waiting for range/SL/TP details', signal, state_key)

        return

    # -------------------------
    # RANGE / SL / TP MESSAGE (the SECOND required message)
    # -------------------------

    if state_key in waiting_for_info:

        match = trade_pattern.search(text)

        if match:

            trade_type = match.group('type').lower()

            range_low = float(match.group('range_low'))
            range_high = float(match.group('range_high'))
            sl = float(match.group('sl'))
            tp_raw = match.group('tp')

            # Parse '4015/4005/3995' into a list, use the last as broker TP
            tp_values = [float(x.strip())
                         for x in tp_raw.split('/') if x.strip()]
            broker_tp = tp_values[-1]

            logger.info(
                'Trade found - source=%s type=%s range=%s-%s sl=%s tp=%s',
                'institutional' if institutional else 'main',
                trade_type, range_low, range_high, sl, broker_tp)

            if len(active_trades) >= MAX_ACTIVE_TRADES:
                logger.warning(
                    'Max active trades (%s) already open - skipping this signal',
                    MAX_ACTIVE_TRADES)
                waiting_for_info.pop(state_key, None)
                return

            lot_for_trade = INSTITUTIONAL_LOT_OVERRIDE if institutional else CURRENT_LOT_OVERRIDE
            risk_percent_for_trade = INSTITUTIONAL_RISK_PERCENT if institutional else RISK_PERCENT
            magic_for_trade = config.INSTITUTIONAL_MAGIC_NUMBER if institutional else config.MAIN_MAGIC_NUMBER
            comment_for_trade = config.INSTITUTIONAL_ORDER_COMMENT if institutional else config.MAIN_ORDER_COMMENT

            await asyncio.to_thread(ensure_mt5_connected)

            trade_result = await asyncio.to_thread(
                place_trade,
                SYMBOL,
                trade_type,
                sl,
                broker_tp,
                lot_for_trade,
                risk_percent_for_trade,
                magic_for_trade,
                comment_for_trade
            )

            if trade_result:

                ticket = trade_result['ticket']

                pos = mt5.positions_get(ticket=ticket)

                if pos:

                    entry_price = pos[0].price_open
                    # authoritative, broker may adjust rounding
                    actual_lot = pos[0].volume

                    tp_pips_for_trade = INSTITUTIONAL_TP_PIPS if institutional else TP_PIPS
                    tp_fractions_for_trade = INSTITUTIONAL_TP_FRACTIONS if institutional else TP_FRACTIONS
                    breakeven_distance_for_trade = INSTITUTIONAL_BREAKEVEN_DISTANCE if institutional else BREAKEVEN_DISTANCE
                    notify_channel_for_trade = INSTITUTE_UPDATE_CHAT_ID if institutional else NOTIFY_CHANNEL

                    # Compute TP1-N as fixed pip distances from the ACTUAL entry
                    if trade_type == 'buy':
                        computed_tp_levels = [
                            entry_price + (pips * PIP_VALUE) for pips in tp_pips_for_trade]
                    else:
                        computed_tp_levels = [
                            entry_price - (pips * PIP_VALUE) for pips in tp_pips_for_trade]

                    active_trades[ticket] = {
                        'symbol': SYMBOL,
                        'signal': trade_type,
                        'entry': entry_price,
                        'sl': sl,
                        'tp': broker_tp,  # broker's own tp field, closes the runner
                        'source': 'institutional' if institutional else 'main',
                        'signal_range': (range_low, range_high),  # for logging only
                        'computed_tp_levels': computed_tp_levels,
                        'computed_tp_hit': [False] * len(computed_tp_levels),
                        'tp_pips': tp_pips_for_trade,
                        'tp_fractions': tp_fractions_for_trade,
                        'breakeven_distance': breakeven_distance_for_trade,
                        'notify_channel': notify_channel_for_trade,
                        'original_lot': actual_lot,
                        'lot': actual_lot,
                        'breakeven_done': False,
                        # Whether this trade auto-moves to breakeven at its
                        # first TP level, per source (see config.py). When
                        # False, breakeven only happens via /be or the
                        # provider's own 'sl to be' message.
                        'auto_breakeven': (
                            config.INSTITUTIONAL_AUTO_BREAKEVEN if institutional
                            else config.AUTO_BREAKEVEN_MAIN
                        ),
                    }

                    logger.info('Trade stored: %s', active_trades[ticket])
                    save_active_trades()

                    await send_trade_open_notification(ticket, active_trades[ticket])

            waiting_for_info.pop(state_key, None)
            return

        else:
            waiting_for_info.pop(state_key, None)
            logger.warning(
                'Chat %s (topic %s) was waiting for info but got a non-matching '
                'message - state reset', chat_id, topic_id)
            return

    # =================================================
    # BREAK EVEN - the signal provider's own phrasing, e.g. a bare 'sl to
    # be' or the full 'take your partials set SL TO BE & Take partials
    # NOW.' message (which contains 'sl to be' as a substring, so it's
    # already matched here). Scoped to the source it came from, so a
    # breakeven message in one signal chat doesn't touch the other
    # source's open trades.
    # =================================================

    if breakeven_phrase_pattern.search(text):

        source = 'institutional' if institutional else 'main'

        logger.info('Breakeven phrase detected from provider message (%s)', source)

        apply_breakeven_for_source(source)


# ==================================================
# TRADE MONITOR
# ==================================================

async def monitor_trades():

    while True:

        await asyncio.to_thread(ensure_mt5_connected)

        if active_trades:
            logger.debug('Checking %s active trade(s)...', len(active_trades))

        for ticket in list(active_trades.keys()):

            pos = mt5.positions_get(ticket=ticket)

            if not pos:

                trade = active_trades.pop(ticket, None)

                logger.info('Trade closed: %s', ticket)

                if trade:
                    pnl = await asyncio.to_thread(get_closed_trade_pnl, ticket)
                    await send_trade_close_notification(ticket, trade, pnl)

                continue

            await asyncio.to_thread(
                check_tp1_and_move_to_breakeven,
                ticket,
                active_trades[ticket]
            )

            await asyncio.to_thread(
                check_tp_levels_and_partial_close,
                ticket,
                active_trades[ticket]
            )

        save_active_trades()

        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

# ==================================================
# MAIN
# ==================================================


async def main():

    load_active_trades()

    logger.info('Bot started - waiting for Telegram messages...')

    await asyncio.gather(
        client.run_until_disconnected(),
        monitor_trades()
    )

with client:
    client.loop.run_until_complete(main())
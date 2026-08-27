"""
All settings for the signal bot live here. Copy .env.example to .env and
fill in your Telegram API credentials, then edit the values below to match
your own channels, symbol, and risk preferences.

Nothing in this file needs to be secret except what lives in .env - this
file is safe to share or commit.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ==================================================
# TELEGRAM CREDENTIALS
# ==================================================
# Get these from https://my.telegram.org -> API Development Tools.
# Set them in a .env file next to this one (see .env.example) - never put
# them directly in this file if you plan to share or publish it.

API_ID = int(os.getenv('TEL_API_ID'))
API_HASH = os.getenv('TEL_API_HASH')

# ==================================================
# SIGNAL SOURCES
# ==================================================
# Every chat ID below must be in the numeric form Telegram uses internally
# (e.g. -1001623437581), not a @username. The easiest way to find a chat's
# ID is to forward one of its messages to a bot like @JsonDumpBot, or check
# the logs after the bot has been listening to it for a moment - Telethon
# will print the chat_id of any message it sees.

# The main signal channel. Every 'Buy/Sell Now' + range/SL/TP message pair
# posted here is acted on automatically.
MAIN_CHANNEL_ID = -1001623437581

# A secondary chat you can use for testing. The bot will respond to admin
# commands here (lot size, close all) but will NOT open trades from it.
TEST_CHANNEL_ID = -1004291999680

# A second, independent signal source: a specific topic inside a Telegram
# forum group. Same message format as the main channel, but tracked and
# sized completely separately (see INSTITUTIONAL_* settings below).
# Set GRIND_ROOM_CHAT_ID to None to disable this second source entirely.
GRIND_ROOM_CHAT_ID = -1004371878551
INSTITUTIONAL_TOPIC_ID = 515

# Where the bot sends "lot size" replies and trade notifications for the
# second signal source above.
INSTITUTE_UPDATE_CHAT_ID = -1003871499701

# Where the bot sends trade open/close notifications for the MAIN channel's
# trades. Can be the same as TEST_CHANNEL_ID or any chat the bot can post to.
NOTIFY_CHANNEL = -1004291999680

# ==================================================
# SYMBOL / SIZING
# ==================================================

# The exact symbol name as it appears in your broker's MT5 terminal. Many
# brokers suffix gold with '.s', '.raw', etc. - check your Market Watch
# window if trades fail to open.
SYMBOL = 'XAUUSD.s'

# 'Pip' size for this symbol, in price units. Check the symbol's Digits in
# MT5 (right-click the symbol in Market Watch -> Specification):
#   Digits = 2  ->  PIP_VALUE = 0.10
#   Digits = 3  ->  PIP_VALUE = 0.01
PIP_VALUE = 0.10

# The bot will not open a new trade while this many are already active,
# across both signal sources combined. STARTING VALUE ONLY - once the bot
# has run, /maxtrades in Telegram is the source of truth; see
# RUNTIME_SETTINGS_FILE above.
MAX_ACTIVE_TRADES = 5

# --- Main channel lot sizing (risk-based by default) ---

# % of account balance risked per trade if no manual lot size is set. The
# lot size is calculated so a stop-loss hit loses approximately this % of
# balance. STARTING VALUE ONLY - change it any time with '/risk main X' in
# Telegram (persists across restarts); '/lot X' / '/lotauto' switch
# between manual and automatic sizing without touching this %.
RISK_PERCENT = 25.0

# Hard cap on lot size, regardless of the risk calculation or any manual
# override. This is your last line of defense against a mis-typed lot size
# or an unexpectedly large risk calculation. STARTING VALUE ONLY - change
# with '/maxlot X' in Telegram.
MAX_LOT = 1.5

# --- Institutional Trader (second source) lot sizing ---
# Same two modes as the main channel: automatic risk-based sizing (this %
# of balance) by default, or a manual override set with '/lot X' sent in
# the Institute update chat ('/lotauto' there resets back to automatic).
# STARTING VALUE ONLY - change the % any time with '/risk institutional X'.
INSTITUTIONAL_RISK_PERCENT = 25.0

# ==================================================
# TAKE-PROFIT LADDERS
# ==================================================
# Each entry is (pips_from_entry, fraction_of_original_lot_to_close).
# Fractions don't need to add up to 100% - whatever's left after the last
# level keeps running until it hits the signal's own broker TP or the SL.
#
# STARTING VALUES ONLY - both ladders below can be changed any time from
# Telegram with '/tp main 20:0.2,40:0.15,...' or '/tp institutional ...'
# (persists across restarts). Only trades opened after the change use the
# new ladder; anything already open keeps the ladder it started with.
# The first level in each ladder is also the trigger point for that
# source's auto-breakeven, when auto-breakeven is enabled for it (see
# AUTO_BREAKEVEN_MAIN / INSTITUTIONAL_AUTO_BREAKEVEN below).

# Main channel: 75% closed across 5 levels, ~25% runs to the broker TP.
MAIN_TP_STRUCTURE = [
    (20, 0.20),   # TP1: move SL to breakeven, close 20%
    (40, 0.15),   # TP2: close 15%
    (60, 0.10),   # TP3: close 10%
    (80, 0.10),   # TP4: close 10%
    (100, 0.20),  # TP5: close 20%
]

# Institutional Trader: percentages are of whatever the current
# institutional lot is (auto or manual), so they stay proportional
# whichever mode you're in.
INSTITUTIONAL_TP_STRUCTURE = [
    (20, 0.40),
    (50, 0.20),
    (80, 0.20),
]

# If True, the main channel's SL auto-moves to breakeven once price
# reaches the first TP level above (20 pips by default). STARTING VALUE
# ONLY - toggle any time with '/breakeven main on' or '/breakeven main off'.
AUTO_BREAKEVEN_MAIN = True

# If True, the Institutional Trader source ALSO auto-moves to breakeven
# at its own first TP level (20 pips by default) - same mechanism as the
# main channel. If False (the default), it only moves to breakeven when
# you send /be, or the provider posts a message containing 'sl to be'.
# STARTING VALUE ONLY - toggle with '/breakeven institutional on'/'off'.
INSTITUTIONAL_AUTO_BREAKEVEN = False

# ==================================================
# MONITORING
# ==================================================

# How often (in seconds) the bot checks open trades against their TP
# levels and breakeven condition. Lower = more responsive, more MT5 calls.
MONITOR_INTERVAL_SECONDS = 0.5

# ==================================================
# RELIABILITY
# ==================================================

# Where the bot saves its list of currently-open trades, so a crash or
# restart doesn't "forget" about positions still open in MT5. Relative
# paths are relative to wherever you run main.py from. This file is
# runtime state, not settings - don't commit it (it's in .gitignore).
TRADE_STATE_FILE = 'active_trades.json'

# Where the bot saves any settings changed via Telegram (/tp, /risk,
# /breakeven, /maxlot, /maxtrades) so they survive a restart without you
# needing to edit this file or log into the VPS again. Overrides whatever
# is set below for the settings it covers; if this file doesn't exist yet
# (or a particular setting was never changed), the values below stand.
# Runtime state, not settings - don't commit it (it's in .gitignore).
RUNTIME_SETTINGS_FILE = 'runtime_settings.json'

# Prevents two copies of the bot running at once (e.g. forgetting one's
# already running in another terminal), which would double-trade every
# signal. The lock is held for as long as the process is alive and
# released automatically when it exits, cleanly or not.
LOCK_FILE = 'callistofx.lock'

# If an MT5 call fails (bridge dropped, Wine crashed, etc.), the bot
# tries to reconnect this many times, waiting this many seconds between
# attempts, before giving up and shutting down (systemd will then restart
# it after RestartSec, per callistofx.service).
MT5_RECONNECT_ATTEMPTS = 5
MT5_RECONNECT_DELAY_SECONDS = 10

# Magic numbers tag every order so you (or another EA on the same
# account) can tell at a glance in MT5's history which trades came from
# which source. Comments show up next to the trade in the MT5 terminal.
MAIN_MAGIC_NUMBER = 123456
INSTITUTIONAL_MAGIC_NUMBER = 123457
MAIN_ORDER_COMMENT = 'CallistoFx Main'
INSTITUTIONAL_ORDER_COMMENT = 'CallistoFx Institutional'
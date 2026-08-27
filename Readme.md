# CallistoFx — Telegram → MT5 Signal Bot

A bot that reads trading signals posted in Telegram (buy/sell + range/SL/TP
messages) and automatically opens, manages, and closes the matching trades
in MetaTrader 5 — with a fixed take-profit ladder, automatic breakeven, and
support for two independent signal sources at once (🚀 CallistoFx Premium Channel 🚀, and Institutional Trader).

I have not set up Aarons channel.

---

## ⚠️ Read this before you do anything else

This bot places **real, automatic, leveraged trades with no confirmation
step.** Once it's running, it will act on every matching signal without
asking you first.

- **Test on a demo account first, for atleast a week** Don't point this at a live account
  until you've watched it handle several signals correctly on a demo.
- **You are responsible for what this bot does.** The Trades are performed based 
  on signals given in CallistoFx, MT5/Wine can misbehave, and bugs happen. 
  I am involved in this project is liable for losses it causes you.
- **Past performance of the signal provider is not a guarantee of
  anything.** This bot doesn't evaluate whether a signal is good — it just
  executes it.

If you're not comfortable with that, stop here.

---

## What this bot actually does

When Nick or Institutional Trader send
trading signals in a specific two-message format:

```
Message 1:  "Buy Now"
Message 2:  🟢XAUUSD🟢
            BUY RANGE: 4123 - 4129
            SL 4119
            TP: 4203
```

When the bot sees that pair, it:

1. Opens a trade in MT5 at current market price (not at the signal's
   range — the range is informational only).
2. Sets the broker's own take-profit and stop-loss from the signal.
3. Watches the trade and closes it in pieces as price moves in your
   favor, according to a fixed pip ladder (see below) — instead of
   waiting for one all-or-nothing TP.
4. Moves the stop-loss to breakeven once the first ladder level is hit
   (if enabled for that source — see below), so a winning trade can no
   longer turn into a loss.
5. Sends you a Telegram message when a trade opens and when it closes,
   with the final profit/loss.
6. Understands a set of `/slash` commands you can send yourself to
   control it directly (lot size, pause, status, and more — full list
   below), and separately reacts automatically when the signal provider
   posts their own cancel/close-all/breakeven messages.

### Two independent signal sources

The bot can watch two separate places at once, each with its own lot
sizing, breakeven behavior, and take-profit ladder:

| | Main channel | "Institutional Trader" topic |
|---|---|---|
| Lot sizing | Auto risk-based (% of balance) by default, or `/lot X` for a fixed override | Same as main: auto risk-based by default, or `/lot X` (sent in the Institute update chat) for a fixed override |
| Breakeven | Automatic by default, at the first TP level — toggle with `/breakeven main on\|off` | Manual by default — `/be`, the provider's "sl to be" message, or `/breakeven institutional on` to make it automatic too |
| Notifications | Sent to `NOTIFY_CHANNEL` | Sent to `INSTITUTE_UPDATE_CHAT_ID` |

You can disable the second source entirely by setting
`GRIND_ROOM_CHAT_ID = None` in `config.py` if you only want the main
channel.

### No split/scaled entries

Every signal opens as **one single trade at full lot size**, the moment
the range/SL/TP message arrives. The bot does not scale into a position
in pieces as price moves through the signal's range — the range in the
signal is informational only (see above). Partial *closes* happen via the
TP ladder below, but the *entry* is always one trade, one lot amount.

### The take-profit ladders

Instead of one TP, the bot closes a portion of the trade at each of several
fixed pip distances from your entry price. Each source has its own ladder.
These are the values a fresh install starts with — see "Managing the bot
from Telegram" below for changing them without touching the VPS.

**Main channel** — 5 levels, 75% closed, ~25% runs to the broker TP:

| Level | Distance | Closes | Notes |
|---|---|---|---|
| TP1 | 20 pips | 20% | Also moves SL to breakeven (if main auto-breakeven is on) |
| TP2 | 40 pips | 15% | |
| TP3 | 60 pips | 10% | |
| TP4 | 80 pips | 10% | |
| TP5 | 100 pips | 20% | Remaining ~25% runs to the signal's own TP |

**Institutional Trader** — 3 levels, 80% closed, ~20% runs to the broker TP:

| Level | Distance | Closes | Notes |
|---|---|---|---|
| TP1 | 20 pips | 40% | Also moves SL to breakeven, only if institutional auto-breakeven is on |
| TP2 | 50 pips | 20% | |
| TP3 | 80 pips | 20% | Remaining ~20% runs to the signal's own TP |

### Managing the bot from Telegram — no VPS access needed

Once it's running, you don't need to SSH back in for most changes. TP
ladders, risk %, max lot, max simultaneous trades, and breakeven mode are
all changeable live from Telegram (`/tp`, `/risk`, `/maxlot`,
`/maxtrades`, `/breakeven` — full list in "Commands you send yourself"
below), and every change is saved to `runtime_settings.json` so it
survives a restart. `config.py`'s values for these are just the starting
defaults for a brand-new install.

**What can't move to Telegram, and has to stay in `config.py` on the
VPS:** the channel/topic IDs, your Telegram API credentials, `SYMBOL`,
and `PIP_VALUE`. The bot subscribes to specific chats and connects to a
specific broker symbol at startup, so changing any of these needs a
restart with new config regardless of where you set them — there's no
"live" version of them to expose.

---

## Requirements

- A VPS running **Ubuntu 22.04 or 24.04 LTS**, at least 2GB RAM
- An MT5 broker account — **use a demo account until you trust the setup**
- A Telegram account that's already a member of the signal channel(s)
- About 45–60 minutes for the initial setup (mostly waiting for
  downloads/installs)

You don't need to know how to code to follow this guide — just how to
copy/paste commands into a terminal.

---

## Setup guide

This installs everything on a fresh Ubuntu VPS. Run every command below
over SSH, one at a time, waiting for each to finish.

### 1. Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 2. Install Wine

MetaTrader 5 only exists for Windows. Wine is a compatibility layer that
lets it run on Linux — this is the single most important piece of the
whole setup.

```bash
sudo dpkg --add-architecture i386
sudo apt update
sudo mkdir -pm755 /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
sudo wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/$(lsb_release -cs)/winehq-$(lsb_release -cs).sources
sudo apt update
sudo apt install --install-recommends winehq-stable -y
```

Check it installed:

```bash
wine --version
```

### 3. Install a virtual display

Your VPS has no monitor, but MT5 is a graphical Windows program and needs
somewhere to "draw" — even if nobody's looking at it. `Xvfb` provides a
fake display for exactly this.

```bash
sudo apt install xvfb x11vnc -y
Xvfb :1 -screen 0 1280x800x16 &
export DISPLAY=:1
```

`x11vnc` lets you actually *see* that virtual display from your own
computer for the one-time steps later (installing MT5, logging in). Start
it whenever you need to look at the screen:

```bash
x11vnc -display :1 -nopw -listen localhost -xkb &
```

Then, on your own computer, open an SSH tunnel and connect a VNC viewer
(e.g. [TigerVNC](https://tigervnc.org/), [RealVNC Viewer](https://www.realvnc.com/en/connect/download/viewer/)) to `localhost:5900`:

```bash
ssh -L 5900:localhost:5900 your_user@your_vps_ip
```

You only need this VNC connection for the one-off installation/login
steps below — the bot itself runs headless afterwards.

### 4. Install MT5 under Wine

Download the installer from your broker's website (they all provide a
Windows `.exe`, usually called `mt5setup.exe`), then:

```bash
wine mt5setup.exe
```

Follow the on-screen installer (visible over your VNC connection). Once
done, MT5 will offer to launch itself — let it, and **log into your
broker account** (demo account first!). Tick "save account information"
so it reconnects automatically on future headless launches.

While you're in there:
- Go to **Tools → Options → Expert Advisors** and tick **"Allow automated
  trading"**.
- Click the **AutoTrading** button in the toolbar so it shows green/on.

### 5. Install Python for Windows, inside Wine

The bridge that connects your Linux bot to MT5 needs a Windows Python
installation living inside Wine (separate from the normal Linux Python
you'll use for the bot itself).

Download a Windows Python installer (e.g. `python-3.11.8-amd64.exe`) from
[python.org](https://www.python.org/downloads/windows/), then:

```bash
wine python-3.11.8-amd64.exe
```

Follow the installer, and tick **"Add python.exe to PATH"** if offered.

Find where it installed to (you'll need this path shortly):

```bash
find ~/.wine -iname "python.exe"
```

### 6. Install the MT5 bridge

Inside the Wine Python you just installed, add the official MetaTrader5
package and `rpyc` (the two packages the bridge server needs to talk to
MT5):

```bash
wine "PATH_TO_WINE_PYTHON/python.exe" -m pip install MetaTrader5 "rpyc==6.0.0"
```

(Replace `PATH_TO_WINE_PYTHON` with the path `find` gave you in step 5.)

### 7. Install the bot's own Python dependencies (Linux side)

```bash
sudo apt install python3-pip -y
cd ~/callstoFx
pip3 install -r requirements.txt
```

### 8. Get your Telegram API credentials

1. Go to https://my.telegram.org and log in with the Telegram account
   that's a member of the signal channel(s).
2. Click **API Development Tools**.
3. Fill in any app name and submit — it immediately shows you an
   `api_id` and `api_hash`.
4. Copy `.env.example` to `.env` and paste them in:

```bash
cp .env.example .env
nano .env
```

### 9. Set up config.py

Open `config.py` and fill in your channel IDs, symbol name, and lot
sizing — every setting has a comment explaining what it does and how to
find the right value. The defaults match the CallistoFx channels; change
`MAIN_CHANNEL_ID`, `TEST_CHANNEL_ID`, etc. if you're using different
signal sources.

### 10. First run (by hand)

With MT5 still open under Wine (from step 4), start the bridge server:

```bash
wine "PATH_TO_WINE_PYTHON/python.exe" -m mt5linux --host localhost --port 8001 "PATH_TO_WINE_PYTHON/python.exe"
```

Leave that running, open a second terminal, and start the bot:

```bash
cd ~/callstoFx
python3 main.py
```

The first time it runs, Telethon will ask for the phone number of the
Telegram account from step 8, then a login code sent to that account via
Telegram. After that one-time login, it saves a `trade_session` file and
won't ask again.

If you see `MT5 Connected` and `Bot started - waiting for Telegram
messages...`, everything is working. Send a test signal or wait for a
real one.

### 11. Make it permanent

Right now, the bot and the bridge only run while your SSH session is
open. `callistofx.service` (included in this repo) turns the bot itself
into a background service that survives reboots and restarts itself if it
crashes:

```bash
sudo nano /etc/systemd/system/callistofx.service   # edit YOUR_USERNAME, then save
sudo systemctl daemon-reload
sudo systemctl enable callistofx
sudo systemctl start callistofx
```

You'll want the same treatment for the Xvfb display, MT5 itself, and the
bridge server so the *whole* stack — not just `main.py` — survives a
reboot. The simplest approach is one more systemd service that starts
Xvfb, then MT5, then the bridge, in order, before `callistofx.service`
starts. If you're not comfortable writing that yourself, open an issue on
this repo and we can add an example.

---

## Day-to-day use

```bash
sudo systemctl status callistofx     # is it running?
journalctl -u callistofx -f          # watch its logs live
sudo systemctl restart callistofx    # restart it
```

### Commands you send yourself

`/status`, `/help`, `/pause`, `/resume`, `/settings`, `/maxlot`, and
`/maxtrades` work from anywhere the bot is listening. Everything else
applies to whichever source that chat belongs to (main channel/test
channel → main; Institutional Trader topic or the Institute update chat →
institutional) — unless you specify the source explicitly, which works
from any chat.

| Command | Effect |
|---|---|
| `/status` | Shows pause state, both sources' lot settings, and every open trade with live PnL |
| `/help` | Lists all commands |
| `/pause` | Stops new trades from opening (existing trades keep being managed normally) |
| `/resume` | Resumes opening new trades |
| `/lot <size>` | Sets a fixed lot size for that source (e.g. `/lot 0.5`) |
| `/lotauto` | Resets that source back to automatic risk-based sizing |
| `/closeall` | Closes every open trade from that source, with a confirmation reply |
| `/cancel` | Cancels a signal that's armed but hasn't received its range/SL/TP message yet |
| `/be` | Manually moves SL to breakeven for that source's open trades |

**Settings — change these any time, no VPS or restart needed. Changes
persist and only affect trades opened after the change:**

| Command | Effect |
|---|---|
| `/settings` | Shows current risk %, max lot, max trades, both TP ladders, and breakeven mode |
| `/tp [main\|institutional] <pips:fraction,...>` | Sets that source's TP ladder, e.g. `/tp main 20:0.2,40:0.15,60:0.1,80:0.1,100:0.2` |
| `/risk [main\|institutional] <percent>` | Sets the risk % used when that source's lot sizing is on auto |
| `/breakeven [main\|institutional] <on\|off>` | Toggles auto-breakeven for that source |
| `/maxlot <size>` | Sets the hard lot cap (both sources) |
| `/maxtrades <n>` | Sets the max number of simultaneous trades (both sources) |

The `main`/`institutional` part of `/tp`, `/risk`, and `/breakeven` is
optional — leave it out and it's inferred from which chat you send the
command in, same as `/lot`.

### Automatic reactions to the signal provider's own messages

The bot also watches for these phrases *in the provider's own posts* and
reacts without you doing anything — separate from the slash commands
above, and these don't reply, since the provider isn't reading the bot's
output:

| Provider's phrase (examples) | Effect |
|---|---|
| "no longer looking", "cancel", "cancelled trade", "scrap that" | Cancels an armed signal, same as `/cancel` |
| "close all open positions" (e.g. "POSITION CLOSED... CLOSE ALL OPEN POSITIONS NOW") | Closes that source's trades, same as `/closeall` |
| "sl to be" (e.g. "take your partials set SL TO BE & Take partials NOW.") | Moves SL to breakeven, same as `/be` |

**This is a narrower match than it might look.** The close-all trigger
needs the exact phrase **"close all open positions"** — those four words,
in that order. A message that just says "Position closed", "all
positions closed", or even "close all positions" (missing the word
"open") does **nothing** — the bot won't touch the trade. If the signal
provider ever changes their exact wording for closing a trade, use
`/closeall` yourself rather than relying on the automatic match, or open
an issue/PR to add the new phrasing to `close_all_pattern` in `main.py`.

---

## Troubleshooting

**"MT5 initialization failed"** — MT5 isn't running under Wine, or the
bridge server (step 10) isn't running, or the port doesn't match. Check
`config.py` and the bridge command both say the same port.

**"AutoTrading is DISABLED in MT5!"** — click the AutoTrading button in
the MT5 toolbar (see step 4) — it needs to be green.

**Trades never open even though signals arrive** — check
`MAX_ACTIVE_TRADES` in `config.py` hasn't been reached, that the bot
isn't paused (`/status` shows this), and that your broker's symbol name
matches `SYMBOL` exactly (many brokers suffix gold with `.s`, `.raw`,
etc. — check Market Watch in MT5).

**Bridge connection refused** — make sure you started the bridge server
(step 10/11) *before* the bot, and that MT5 itself is open under Wine.

**Wine/MT5 randomly stops responding** — Wine running MT5 for weeks at a
time can get flaky. A scheduled weekly restart of the whole stack (Xvfb →
MT5 → bridge → bot) is common practice for this kind of setup.

---

## Reliability features

A few things the bot does on its own to make unattended operation safer:

- **Single-instance lock** — the bot won't start a second time if it's
  already running (prevents accidentally double-trading every signal from
  two copies). Uses `callistofx.lock` next to `main.py`.
- **Trade state survives restarts** — open trades are saved to
  `active_trades.json` as they change, and reloaded on startup. If the
  bot crashes or systemd restarts it, it picks back up managing whatever
  positions are still genuinely open in MT5, rather than "forgetting"
  them. Anything that actually closed while the bot was offline is
  logged and not recovered.
- **MT5 reconnect attempts** — if the Wine/MT5 bridge connection drops,
  the bot retries a few times (`MT5_RECONNECT_ATTEMPTS` /
  `MT5_RECONNECT_DELAY_SECONDS` in `config.py`) before giving up and
  exiting, so systemd can restart it cleanly rather than it running on in
  a half-broken state.

Both `active_trades.json`, `runtime_settings.json`, and `callistofx.lock`
are runtime state, not settings — make sure your `.gitignore` includes
them alongside `.env` and `trade_session.session`.


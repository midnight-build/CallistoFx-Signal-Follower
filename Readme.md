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
4. Moves the stop-loss to breakeven once the first ladder level is hit,
   so a winning trade can no longer turn into a loss.
5. Sends you a Telegram message when a trade opens and when it closes,
   with the final profit/loss.

It also understands a few plain-English admin messages sent in the same
chats — `lot size 0.5`, `lot size auto`, `close all open positions`,
`cancel` — so you can control it without touching the code.

### Two independent signal sources

The bot can watch two separate places at once, each with its own lot
sizing and its own take-profit ladder:

| | Main channel | "Institutional Trader" topic |
|---|---|---|
| Lot sizing | Risk-based (% of balance) or manual override | Always a fixed lot |
| Breakeven | Automatic, at the first TP level | Manual only (an explicit "sl to be" message) |
| Notifications | Sent to `NOTIFY_CHANNEL` | Sent to `INSTITUTE_UPDATE_CHAT_ID` |

You can disable the second source entirely by setting
`GRIND_ROOM_CHAT_ID = None` in `config.py` if you only want the main
channel.

### The take-profit ladder

Instead of one TP, the bot closes a portion of the trade at each of five
fixed pip distances from your entry price. By default (main channel):

| Level | Distance | Closes | Notes |
|---|---|---|---|
| TP1 | 20 pips | 20% | Also moves SL to breakeven |
| TP2 | 40 pips | 15% | |
| TP3 | 60 pips | 10% | |
| TP4 | 80 pips | 10% | |
| TP5 | 100 pips | 20% | Remaining ~25% runs to the signal's own TP |

All of this — pip distances, percentages, lot sizing, symbol, channel
IDs — is adjustable in `config.py`. See that file for a full explanation
of every setting.

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

Controlling it from Telegram (send these in the relevant chat):

| Message | Effect |
|---|---|
| `lot size 0.5` | Sets a fixed lot size for future trades |
| `lot size auto` | Goes back to risk-based sizing (main channel only) |
| `close all open positions` | Fully closes every trade from that source |
| `cancel` / `no longer looking` | Cancels a signal that was armed but not yet completed |
| `sl to be` | Manually moves SL to breakeven for open trades from that source |

---

## Troubleshooting

**"MT5 initialization failed"** — MT5 isn't running under Wine, or the
bridge server (step 10) isn't running, or the port doesn't match. Check
`config.py` and the bridge command both say the same port.

**"AutoTrading is DISABLED in MT5!"** — click the AutoTrading button in
the MT5 toolbar (see step 4) — it needs to be green.

**Trades never open even though signals arrive** — check
`MAX_ACTIVE_TRADES` in `config.py` hasn't been reached, and that your
broker's symbol name matches `SYMBOL` exactly (many brokers suffix gold
with `.s`, `.raw`, etc. — check Market Watch in MT5).

**Bridge connection refused** — make sure you started the bridge server
(step 10/11) *before* the bot, and that MT5 itself is open under Wine.

**Wine/MT5 randomly stops responding** — Wine running MT5 for weeks at a
time can get flaky. A scheduled weekly restart of the whole stack (Xvfb →
MT5 → bridge → bot) is common practice for this kind of setup.

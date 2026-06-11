# Telegram Research Bot

A Telegram bot powered by Claude (Anthropic) that acts as a structured research agent. Send it any question or topic and it will decompose it into sub-questions, analyze each one, and return a well-structured summary with explicit caveats.

> **Tutorial credit:** Based on the guide by [@AnatoliKopadze](https://x.com/AnatoliKopadze/status/2063985608381362576)

---

## Features

- Breaks any question into 3–5 sub-questions and answers each systematically
- Maintains per-user conversation history across restarts (stored as JSON files)
- `/clear` command to reset a user's history
- Splits long responses to respect Telegram's 4096-character message limit
- Optionally runs as a `systemd` service for always-on deployment

---

## Prerequisites

- Python 3.10+
- A Telegram bot token — create one via [@BotFather](https://t.me/BotFather)
- An Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/pantherstock/telegram-research-bot.git
cd telegram-research-bot
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your keys:

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC-...
```

---

## Running the bot

### Directly (foreground)

```bash
source venv/bin/activate
python main.py
```

The bot will start polling for messages. Press `Ctrl+C` to stop.

---

## Running as a systemd service (Linux)

This keeps the bot running in the background and restarts it automatically on failure.

### 1. Create the logs directory

```bash
mkdir -p /home/$USER/telegram-research-bot/logs
```

### 2. Edit the service file

Open `telegram-research-bot.service` and update the `User` and `WorkingDirectory` / `EnvironmentFile` / `ExecStart` paths to match your username and install location if they differ from the defaults.

### 3. Install and enable the service

```bash
sudo cp telegram-research-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-research-bot
sudo systemctl start telegram-research-bot
```

### 4. Check status and logs

```bash
sudo systemctl status telegram-research-bot
tail -f /home/$USER/telegram-research-bot/logs/bot.log
```

### Stopping or restarting

```bash
sudo systemctl stop telegram-research-bot
sudo systemctl restart telegram-research-bot
```

---

## Bot commands

| Command  | Description                        |
|----------|------------------------------------|
| `/start` | Show the welcome / help message    |
| `/clear` | Reset your conversation history    |

Any other text message is treated as a research query.

---

## Project structure

```
telegram-research-bot/
├── main.py                        # Bot logic
├── requirements.txt               # Python dependencies
├── .env.example                   # Environment variable template
├── telegram-research-bot.service  # systemd unit file
└── history/                       # Per-user conversation history (auto-created, git-ignored)
```

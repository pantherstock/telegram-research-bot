# Telegram Research Bot

A Telegram bot powered by Claude (Anthropic) that acts as a structured research agent. Send it any question or topic and it will decompose it into sub-questions, analyse each one, and return a well-structured summary with explicit caveats.

> **Tutorial credit:** Based on the guide by [@AnatoliKopadze](https://x.com/AnatoliKopadze/status/2063985608381362576)

---

## Features

- Breaks any question into 3–5 sub-questions and answers each systematically
- **Web search** via [Tavily](https://tavily.com) — automatically searches for recent or time-sensitive topics
- **Notes system** — save and retrieve personal notes via chat messages
- **Daily briefing** — Claude generates a morning briefing sent at 08:00 in your timezone
- **Cost tracking** — per-day and all-time token usage with estimated dollar cost
- **Access control** — optionally restrict the bot to a single Telegram user ID
- **Automatic context management** — conversation history is compressed into a rolling summary at 12 messages; the summary persists to disk so the next session picks up seamlessly
- Maintains per-user conversation history across restarts (stored as JSON files)
- Splits long responses to respect Telegram's 4096-character message limit
- Optionally runs as a `systemd` service for always-on deployment

---

## Prerequisites

- Python 3.10+
- A Telegram bot token — create one via [@BotFather](https://t.me/BotFather)
- An Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com)
- *(Optional)* A Tavily API key for web search — get one at [tavily.com](https://tavily.com)

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

# Optional — enables web search
TAVILY_API_KEY=tvly-...

# Optional — restricts bot access to one user (get your ID with /myid)
ALLOWED_USER_ID=123456789

# Timezone for the daily briefing (e.g. America/New_York, Europe/London)
BRIEFING_TIMEZONE=UTC
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

| Command       | Description                                              |
|---------------|----------------------------------------------------------|
| `/start`      | Show the welcome / help message                          |
| `/clear`      | Reset your conversation history                          |
| `/notes`      | Show your last 10 saved notes                            |
| `/clearnotes` | Delete all your saved notes                              |
| `/costs`      | Show token usage and estimated cost (today + all time)   |
| `/briefing`   | Send today's morning briefing immediately                |
| `/compress`   | Compress conversation history into a rolling summary     |
| `/checkpoint` | Structured status: what's done, decided, and still needed|
| `/myid`       | Show your Telegram user ID (useful for `ALLOWED_USER_ID`)|

Any other text message is treated as a research query, unless it starts with a note trigger (see below).

### Saving notes

Start a message with any of these prefixes to save a note instead of querying the AI:

```
note: <text>
save this: <text>
remember this: <text>
```

---

## Web search

When `TAVILY_API_KEY` is set, the bot automatically decides whether to search the web before answering. It searches for recent news, current statistics, or any topic where up-to-date information matters, and skips searching for stable facts it can answer from training data.

---

## Context management

Long research sessions are handled automatically — no manual context pasting required.

- **Auto-compress** — when a conversation reaches 12 messages, the bot compresses the full history into a terse rolling summary and replaces the stored history with it. The summary is saved to disk immediately, so the next session (even after a bot restart) loads the summary as its starting context.
- **`/compress`** — trigger compression manually at any time, e.g. before switching to a new subtopic.
- **`/checkpoint`** — ask Claude for a structured status report (under 200 words): what's been completed, key decisions, what's still open, and what context a new session would need.

---

## Project structure

```
telegram-research-bot/
├── main.py                        # Bot logic
├── requirements.txt               # Python dependencies
├── .env.example                   # Environment variable template
├── telegram-research-bot.service  # systemd unit file
├── history/                       # Per-user conversation history (auto-created, git-ignored)
├── notes/                         # Per-user notes (auto-created, git-ignored)
└── costs.json                     # Token usage log (auto-created, git-ignored)
```

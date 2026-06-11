import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY is not set in environment")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set in environment")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HISTORY_DIR = Path(__file__).parent / "history"
HISTORY_DIR.mkdir(exist_ok=True)
MAX_HISTORY = 20  # messages per user (each exchange = 2 messages)

SYSTEM_PROMPT = """You are a research agent with rigorous analytical habits. When given a question or topic:

1. Decompose the request into 3-5 distinct sub-questions that must be answered to fully address the topic.
2. Analyze each sub-question systematically, weighing source quality and reliability.
3. Synthesize findings into a structured response with clear sections.

Response format:
- Use headers (##) and bullet points for clarity
- Lead with the most important findings
- Cite uncertainty explicitly ("evidence is limited", "sources conflict on this")
- End with a **Gaps & Caveats** section noting what remains unknown or requires deeper investigation

Tone: direct, precise, no filler. Do not pad responses with affirmations or summaries of what you just said."""

def _history_path(user_id: int) -> Path:
    return HISTORY_DIR / f"{user_id}.json"


def load_all_histories() -> dict[int, list[dict]]:
    histories = {}
    for path in HISTORY_DIR.glob("*.json"):
        try:
            user_id = int(path.stem)
            histories[user_id] = json.loads(path.read_text())
        except (ValueError, json.JSONDecodeError):
            logger.warning("Could not load history file %s — skipping", path)
    logger.info("Loaded histories for %d user(s)", len(histories))
    return histories


def save_history(user_id: int, messages: list[dict]) -> None:
    _history_path(user_id).write_text(json.dumps(messages, ensure_ascii=False))


def trim(messages: list[dict]) -> list[dict]:
    """Keep only the most recent MAX_HISTORY messages."""
    return messages[-MAX_HISTORY:] if len(messages) > MAX_HISTORY else messages


# per-user conversation history: {user_id: [{"role": ..., "content": ...}]}
conversation_histories: dict[int, list[dict]] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Research agent online.\n\n"
        "Send me any question or topic and I'll break it down into sub-questions, "
        "analyze the evidence, and deliver a structured summary.\n\n"
        "Commands:\n"
        "/start — show this message\n"
        "/clear — reset conversation history"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversation_histories.pop(user_id, None)
    path = _history_path(user_id)
    if path.exists():
        path.unlink()
    await update.message.reply_text("Conversation history cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": user_text})
    conversation_histories[user_id] = trim(conversation_histories[user_id])

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=conversation_histories[user_id],
        )

        assistant_text = response.content[0].text
        conversation_histories[user_id].append(
            {"role": "assistant", "content": assistant_text}
        )
        save_history(user_id, conversation_histories[user_id])

        # Telegram messages max out at 4096 chars; split if needed
        if len(assistant_text) <= 4096:
            await update.message.reply_text(assistant_text)
        else:
            for chunk_start in range(0, len(assistant_text), 4096):
                await update.message.reply_text(
                    assistant_text[chunk_start : chunk_start + 4096]
                )

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        conversation_histories[user_id].pop()  # remove the unanswered user message
        await update.message.reply_text(
            "API error — please try again in a moment."
        )


def main() -> None:
    global conversation_histories
    conversation_histories = load_all_histories()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

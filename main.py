import os
import json
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot, Update
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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
_raw_allowed = os.getenv("ALLOWED_USER_ID", "").strip()
ALLOWED_USER_ID: int | None = int(_raw_allowed) if _raw_allowed.isdigit() else None
BRIEFING_TIMEZONE = os.getenv("BRIEFING_TIMEZONE", "UTC")

# claude-sonnet-4-6 pricing
INPUT_PRICE_PER_TOKEN = 3.00 / 1_000_000
OUTPUT_PRICE_PER_TOKEN = 15.00 / 1_000_000

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
NOTES_DIR = Path(__file__).parent / "notes"
NOTES_DIR.mkdir(exist_ok=True)
COSTS_FILE = Path(__file__).parent / "costs.json"
MAX_HISTORY = 20
MAX_TOOL_ITERATIONS = 5  # prevent runaway search loops
AUTO_COMPRESS_THRESHOLD = 12

SYSTEM_PROMPT = """You are a research agent with rigorous analytical habits. You have access to a web_search tool for finding current information.

Use web_search when the question involves:
- Recent news, events, or developments (past year or so)
- Current prices, statistics, or live data
- Topics where your training knowledge may be outdated or incomplete

Do NOT search for:
- Well-established historical facts or scientific principles
- Stable definitions or concepts that don't change over time
- Questions you can confidently answer from training data

When given a question or topic:
1. Decide whether web search is needed. If yes, search before composing your answer.
2. Decompose the request into 3-5 distinct sub-questions that must be answered to fully address the topic.
3. Analyze each sub-question systematically, weighing source quality and reliability.
4. Synthesize findings into a structured response with clear sections.

Response format:
- Use headers (##) and bullet points for clarity
- Lead with the most important findings
- Cite uncertainty explicitly ("evidence is limited", "sources conflict on this")
- End with a **Gaps & Caveats** section noting what remains unknown or requires deeper investigation

Tone: direct, precise, no filler. Do not pad responses with affirmations or summaries of what you just said."""

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this for recent news, current "
        "prices or statistics, recent events, or any topic where up-to-date "
        "information matters."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query. Be specific and concise.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (1-10). Default is 5.",
            },
        },
        "required": ["query"],
    },
}


def search_tavily(query: str, max_results: int = 5) -> str:
    """Call the Tavily search API and return formatted results as a string."""
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": min(max(1, max_results), 10),
                "include_answer": False,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error("Tavily HTTP %s for query %r", e.response.status_code, query)
        return f"Search failed (HTTP {e.response.status_code})."
    except Exception as e:
        logger.error("Tavily search error: %s", e)
        return "Search failed due to a network error."

    results = data.get("results", [])
    if not results:
        return "No results found for that query."

    parts = []
    for r in results:
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("content", "")[:600].strip()
        parts.append(f"**{title}**\n{url}\n{snippet}")

    return "\n\n---\n\n".join(parts)


def blocks_to_dicts(content) -> list[dict]:
    """Convert SDK content blocks to plain dicts for JSON serialisation."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    result = []
    for block in content:
        if isinstance(block, dict):
            result.append(block)
        elif block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result


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


def load_costs() -> dict:
    if COSTS_FILE.exists():
        try:
            return json.loads(COSTS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"all_time": {"input_tokens": 0, "output_tokens": 0}, "daily": {}}


def record_usage(input_tokens: int, output_tokens: int) -> None:
    data = load_costs()
    today = datetime.now().strftime("%Y-%m-%d")
    data["all_time"]["input_tokens"] += input_tokens
    data["all_time"]["output_tokens"] += output_tokens
    if today not in data["daily"]:
        data["daily"][today] = {"input_tokens": 0, "output_tokens": 0}
    data["daily"][today]["input_tokens"] += input_tokens
    data["daily"][today]["output_tokens"] += output_tokens
    COSTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    logger.info("Tokens — input: %d, output: %d", input_tokens, output_tokens)


_BRIEFING_SYSTEM = (
    "You write terse, direct morning briefings. "
    "No clichés. Avoid: embrace, journey, potential, unlock, strive, passion."
)

_BRIEFING_PROMPT = (
    "Write a 3-part morning briefing, each part on its own paragraph (blank line between them):\n\n"
    "1. A one-sentence motivational thought — specific and direct, not generic\n"
    "2. A one-sentence reminder to name the single most important task for today\n"
    "3. One specific, interesting fact about productivity research or AI\n\n"
    "Plain text only. No headers, no numbers, no bullet points."
)


def generate_briefing() -> str:
    """Call Claude to generate today's briefing. Returns the text, empty string on failure."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=_BRIEFING_SYSTEM,
        messages=[{"role": "user", "content": _BRIEFING_PROMPT}],
    )
    if response.usage:
        record_usage(response.usage.input_tokens, response.usage.output_tokens)
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            return block.text.strip()
    return ""


async def send_daily_briefing(bot: Bot) -> None:
    if ALLOWED_USER_ID is None:
        logger.warning("Daily briefing: ALLOWED_USER_ID not set, skipping")
        return
    try:
        text = generate_briefing()
        if not text:
            logger.error("Daily briefing: empty response from Claude")
            return
        await bot.send_message(chat_id=ALLOWED_USER_ID, text=f"Good morning.\n\n{text}")
        logger.info("Daily briefing sent to %d", ALLOWED_USER_ID)
    except Exception as e:
        logger.error("Daily briefing failed: %s", e)


_scheduler = AsyncIOScheduler()


async def _scheduler_post_init(application: Application) -> None:
    _scheduler.add_job(
        send_daily_briefing,
        CronTrigger(hour=8, minute=0, timezone=BRIEFING_TIMEZONE),
        args=[application.bot],
        id="daily_briefing",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Daily briefing scheduled at 08:00 %s", BRIEFING_TIMEZONE)


async def _scheduler_post_shutdown(application: Application) -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


def _notes_path(user_id: int) -> Path:
    return NOTES_DIR / f"{user_id}.txt"


_COMPRESS_PROMPT = (
    "Compress this conversation into a rolling summary. Include: original goal, "
    "what was done and found, key decisions made, what still needs to happen. "
    "Be terse — this is a context hand-off, not a report."
)

_CHECKPOINT_PROMPT = (
    "In under 200 words provide a structured checkpoint:\n"
    "1. What has been completed so far?\n"
    "2. Key decisions or findings from this session?\n"
    "3. What still needs to be done?\n"
    "4. What context would be needed to continue in a new session?"
)


def run_compression(user_id: int, messages: list[dict]) -> str:
    """Call Claude to compress messages into a summary. Returns summary text."""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=messages + [{"role": "user", "content": _COMPRESS_PROMPT}],
        )
        if response.usage:
            record_usage(response.usage.input_tokens, response.usage.output_tokens)
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                return block.text.strip()
    except Exception as e:
        logger.error("Compression failed for user %d: %s", user_id, e)
    return ""


def detect_note_trigger(text: str) -> str | None:
    """Return note content if text starts with a save trigger phrase, else None."""
    stripped = text.strip()
    lower = stripped.lower()
    for prefix in ("note:", "save this:", "remember this:"):
        if lower.startswith(prefix):
            return stripped[len(prefix):].strip()
    for prefix in ("save this", "remember this"):
        if lower.startswith(prefix):
            remainder = stripped[len(prefix):].strip()
            return remainder if remainder else stripped
    return None


def save_note(user_id: int, content: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(_notes_path(user_id), "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {content}\n")


def load_notes(user_id: int) -> list[str]:
    path = _notes_path(user_id)
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# per-user conversation history: {user_id: [{"role": ..., "content": ...}]}
conversation_histories: dict[int, list[dict]] = {}

# tracks unauthorized users who have already received the rejection message
notified_unauthorized: set[int] = set()


async def _check_authorized(update: Update) -> bool:
    """Return True if the user is allowed. On first unauthorized attempt, reply once."""
    if ALLOWED_USER_ID is None:
        return True
    user_id = update.effective_user.id
    if user_id == ALLOWED_USER_ID:
        return True
    if user_id not in notified_unauthorized:
        notified_unauthorized.add(user_id)
        await update.message.reply_text("This bot is private.")
    return False


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram user ID is: {update.effective_user.id}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    search_status = "Web search: enabled" if TAVILY_API_KEY else "Web search: disabled (no TAVILY_API_KEY)"
    await update.message.reply_text(
        "Research agent online.\n\n"
        "Send me any question or topic and I'll break it down into sub-questions, "
        "analyse the evidence, and deliver a structured summary.\n\n"
        f"{search_status}\n\n"
        "Commands:\n"
        "/start — show this message\n"
        "/clear — reset conversation history\n"
        "/notes — show your last 10 saved notes\n"
        "/clearnotes — delete all your saved notes\n"
        "/costs — show token usage and estimated cost\n"
        "/briefing — send today's briefing now\n"
        "/compress — compress conversation history to a summary\n"
        "/checkpoint — structured status: what's done, decided, and next\n\n"
        "To save a note, start your message with:\n"
        "  note: <text>\n"
        "  save this: <text>\n"
        "  remember this: <text>"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    user_id = update.effective_user.id
    conversation_histories.pop(user_id, None)
    path = _history_path(user_id)
    if path.exists():
        path.unlink()
    await update.message.reply_text("Conversation history cleared.")


async def notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    user_id = update.effective_user.id
    all_notes = load_notes(user_id)
    if not all_notes:
        await update.message.reply_text("No notes saved yet.")
        return
    recent = all_notes[-10:]
    lines = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(recent))
    await update.message.reply_text(f"Your last {len(recent)} note(s):\n\n{lines}")


async def clearnotes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    user_id = update.effective_user.id
    path = _notes_path(user_id)
    if path.exists():
        path.unlink()
    await update.message.reply_text("All notes cleared.")


async def costs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    data = load_costs()
    today = datetime.now().strftime("%Y-%m-%d")
    today_data = data["daily"].get(today, {"input_tokens": 0, "output_tokens": 0})
    all_time = data["all_time"]

    today_in = today_data["input_tokens"]
    today_out = today_data["output_tokens"]
    today_cost = today_in * INPUT_PRICE_PER_TOKEN + today_out * OUTPUT_PRICE_PER_TOKEN

    total_in = all_time["input_tokens"]
    total_out = all_time["output_tokens"]
    total_cost = total_in * INPUT_PRICE_PER_TOKEN + total_out * OUTPUT_PRICE_PER_TOKEN

    msg = (
        f"Token usage (claude-sonnet-4-6)\n\n"
        f"Today ({today}):\n"
        f"  Input:  {today_in:,}\n"
        f"  Output: {today_out:,}\n"
        f"  Cost:   ${today_cost:.4f}\n\n"
        f"All time:\n"
        f"  Input:  {total_in:,}\n"
        f"  Output: {total_out:,}\n"
        f"  Cost:   ${total_cost:.4f}"
    )
    await update.message.reply_text(msg)


async def briefing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await send_daily_briefing(context.bot)


async def compress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    user_id = update.effective_user.id
    messages = conversation_histories.get(user_id, [])
    if len(messages) < 2:
        await update.message.reply_text("Nothing to compress yet.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    summary = run_compression(user_id, messages)
    if not summary:
        await update.message.reply_text("Compression failed — history unchanged.")
        return
    compressed = [
        {"role": "user", "content": "[Session compressed — prior context below]"},
        {"role": "assistant", "content": summary},
    ]
    conversation_histories[user_id] = compressed
    save_history(user_id, compressed)
    await update.message.reply_text(f"[Context compressed]\n\n{summary}")


async def checkpoint_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    user_id = update.effective_user.id
    messages = conversation_histories.get(user_id, [])
    if len(messages) < 2:
        await update.message.reply_text("No conversation history to checkpoint yet.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=messages + [{"role": "user", "content": _CHECKPOINT_PROMPT}],
        )
        if response.usage:
            record_usage(response.usage.input_tokens, response.usage.output_tokens)
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                await update.message.reply_text(block.text.strip())
                return
    except Exception as e:
        logger.error("Checkpoint failed for user %d: %s", user_id, e)
        await update.message.reply_text("Checkpoint failed — please try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_authorized(update):
        return
    user_id = update.effective_user.id
    user_text = update.message.text

    note_content = detect_note_trigger(user_text)
    if note_content is not None:
        save_note(user_id, note_content)
        await update.message.reply_text("Note saved.")
        return

    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": user_text})
    will_trim = len(conversation_histories[user_id]) > MAX_HISTORY
    conversation_histories[user_id] = trim(conversation_histories[user_id])

    if len(conversation_histories[user_id]) >= AUTO_COMPRESS_THRESHOLD:
        summary = run_compression(user_id, conversation_histories[user_id])
        if summary:
            conversation_histories[user_id] = [
                {"role": "user", "content": "[Session compressed — prior context below]"},
                {"role": "assistant", "content": summary},
            ]
            conversation_histories[user_id].append({"role": "user", "content": user_text})
            save_history(user_id, conversation_histories[user_id])
            await update.message.reply_text("[Context compressed to keep conversation focused]")
            will_trim = False

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # Only offer the tool when a Tavily key is configured
    tools = [WEB_SEARCH_TOOL] if TAVILY_API_KEY else []

    # Work on a local copy so we can include tool_use/tool_result turns without
    # polluting the stored per-user history (which stays text-only).
    working_messages = conversation_histories[user_id].copy()
    assistant_text = ""
    response = None
    total_input_tokens = 0
    total_output_tokens = 0

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=working_messages,
            )

            if response.usage:
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "type") and block.type == "text":
                        assistant_text = block.text
                        break
                break

            if response.stop_reason == "tool_use":
                # Add the assistant turn (including tool_use blocks) to the working context
                working_messages.append({
                    "role": "assistant",
                    "content": blocks_to_dicts(response.content),
                })

                # Execute each tool call
                tool_results = []
                for block in response.content:
                    if not (hasattr(block, "type") and block.type == "tool_use"):
                        continue

                    if block.name == "web_search":
                        query = block.input.get("query", "")
                        max_results = block.input.get("max_results", 5)
                        logger.info("web_search: %r", query)
                        result = search_tavily(query, max_results)
                    else:
                        result = f"Unknown tool: {block.name}"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                working_messages.append({"role": "user", "content": tool_results})

                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action="typing"
                )
            else:
                logger.warning("Unexpected stop_reason: %s", response.stop_reason)
                break

        # Fallback: pull text from the last response if we somehow missed it
        if not assistant_text and response is not None:
            for block in response.content:
                if hasattr(block, "type") and block.type == "text":
                    assistant_text = block.text
                    break
        if not assistant_text:
            assistant_text = "I couldn't generate a response. Please try again."

        record_usage(total_input_tokens, total_output_tokens)

        # Store only the final text reply in the persistent per-user history
        conversation_histories[user_id].append(
            {"role": "assistant", "content": assistant_text}
        )
        save_history(user_id, conversation_histories[user_id])

        # Telegram caps messages at 4096 chars
        if len(assistant_text) <= 4096:
            await update.message.reply_text(assistant_text)
        else:
            for chunk_start in range(0, len(assistant_text), 4096):
                await update.message.reply_text(
                    assistant_text[chunk_start : chunk_start + 4096]
                )

        if will_trim:
            await update.message.reply_text(
                "Note: the oldest messages were dropped from context "
                f"(history limit is {MAX_HISTORY} messages). Use /clear to reset."
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

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_scheduler_post_init)
        .post_shutdown(_scheduler_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("notes", notes))
    app.add_handler(CommandHandler("clearnotes", clearnotes))
    app.add_handler(CommandHandler("costs", costs_cmd))
    app.add_handler(CommandHandler("briefing", briefing_cmd))
    app.add_handler(CommandHandler("compress", compress_cmd))
    app.add_handler(CommandHandler("checkpoint", checkpoint_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting (web search %s)...", "enabled" if TAVILY_API_KEY else "disabled")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

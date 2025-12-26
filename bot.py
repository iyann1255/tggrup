import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN kosong")

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "group_guard")

SPAM_WINDOW = int(os.getenv("SPAM_WINDOW", "30"))
SPAM_REPEAT = int(os.getenv("SPAM_REPEAT", "2"))

def parse_ids(raw: str) -> set[int]:
    return {int(x) for x in (raw or "").split(",") if x.strip().isdigit()}

SUDO_IDS = parse_ids(os.getenv("SUDO_IDS", ""))

# ================= REGEX =================
LINK_RE = re.compile(
    r"(?i)\b("
    r"https?://\S+|"
    r"www\.\S+|"
    r"t\.me/\S+|telegram\.me/\S+|"
    r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?"
    r")\b"
)
AT_RE = re.compile(r"(?<!\w)@\w{2,32}\b")

# ================= SPAM CACHE =================
@dataclass
class SeenMsg:
    last_ts: float
    count: int

SEEN: Dict[Tuple[int, int, str], SeenMsg] = {}

def normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"\s+", " ", s)

def prune_old(now: float):
    for k in list(SEEN.keys()):
        if now - SEEN[k].last_ts > SPAM_WINDOW:
            del SEEN[k]

# ================= MONGO =================
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[MONGO_DB]
col = db.badwords

BADWORD_RE_CACHE: Dict[int, Optional[re.Pattern]] = {}

def build_re(words: List[str]) -> Optional[re.Pattern]:
    if not words:
        return None
    escaped = [re.escape(w) for w in sorted(set(words), key=len, reverse=True)]
    return re.compile(r"(?i)(?<!\w)(" + "|".join(escaped) + r")(?!\w)")

async def refresh_cache(chat_id: int):
    words = [d["word"] async for d in col.find({"chat_id": chat_id})]
    BADWORD_RE_CACHE[chat_id] = build_re(words)

async def get_badword_re(chat_id: int):
    if chat_id not in BADWORD_RE_CACHE:
        await refresh_cache(chat_id)
    return BADWORD_RE_CACHE.get(chat_id)

# ================= PERMISSION =================
async def is_admin_or_sudo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    if not msg or not msg.from_user:
        return False

    if msg.from_user.id in SUDO_IDS:
        return True

    if msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False

    try:
        m = await context.bot.get_chat_member(msg.chat.id, msg.from_user.id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# ================= COMMANDS =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡 Group Guard aktif\n\n"
        "/bad_add <kata>\n"
        "/bad_del <kata>\n"
        "/bad_list\n"
        "/bad_clear"
    )

async def bad_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_admin_or_sudo(update, context):
        return

    if not context.args:
        return await msg.reply_text("Format: /bad_add <kata>")

    word = normalize_text(" ".join(context.args))
    await col.update_one(
        {"chat_id": msg.chat.id, "word": word},
        {"$setOnInsert": {"created_at": int(time.time())}},
        upsert=True
    )
    await refresh_cache(msg.chat.id)
    await msg.reply_text(f"✅ Ditambah: `{word}`")

async def bad_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_admin_or_sudo(update, context):
        return

    word = normalize_text(" ".join(context.args))
    r = await col.delete_one({"chat_id": msg.chat.id, "word": word})
    await refresh_cache(msg.chat.id)
    await msg.reply_text("🗑 Dihapus" if r.deleted_count else "❌ Tidak ada")

async def bad_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    words = [d["word"] async for d in col.find({"chat_id": msg.chat.id})]
    if not words:
        return await msg.reply_text("Badwords kosong.")
    await msg.reply_text("Badwords:\n" + "\n".join(f"- {w}" for w in words))

async def bad_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_admin_or_sudo(update, context):
        return

    r = await col.delete_many({"chat_id": msg.chat.id})
    BADWORD_RE_CACHE.pop(msg.chat.id, None)
    await msg.reply_text(f"🔥 Cleared {r.deleted_count} badwords")

# ================= GUARD =================
async def maybe_delete(update: Update):
    try:
        await update.message.delete()
    except Exception:
        pass

async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    if msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    text = msg.text

    if AT_RE.search(text):
        return await maybe_delete(update)

    if LINK_RE.search(text):
        return await maybe_delete(update)

    bw = await get_badword_re(msg.chat.id)
    if bw and bw.search(text):
        return await maybe_delete(update)

    now = time.time()
    prune_old(now)

    norm = normalize_text(text)
    key = (msg.chat.id, msg.from_user.id, norm)

    seen = SEEN.get(key)
    if not seen:
        SEEN[key] = SeenMsg(now, 1)
    elif now - seen.last_ts <= SPAM_WINDOW:
        seen.count += 1
        seen.last_ts = now
        if seen.count >= SPAM_REPEAT:
            return await maybe_delete(update)
    else:
        SEEN[key] = SeenMsg(now, 1)

# ================= MAIN =================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("bad_add", bad_add))
    app.add_handler(CommandHandler("bad_del", bad_del))
    app.add_handler(CommandHandler("bad_list", bad_list))
    app.add_handler(CommandHandler("bad_clear", bad_clear))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guard))

    print("Mongo Guard Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()

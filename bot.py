import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN belum diisi di .env")

SPAM_WINDOW = int(os.getenv("SPAM_WINDOW", "30"))  # detik
SPAM_REPEAT = int(os.getenv("SPAM_REPEAT", "2"))   # jumlah pengulangan yang dianggap spam

# Detect link (http/https/www/t.me/telegram.me) + domain sederhana
LINK_RE = re.compile(
    r"(?i)\b("
    r"https?://\S+|"
    r"www\.\S+|"
    r"t\.me/\S+|telegram\.me/\S+|"
    r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?"
    r")\b"
)

# Detect @mention / @username
AT_RE = re.compile(r"(?<!\w)@\w{2,32}\b")

HELP_TEXT = (
    "Bot Grup Guard aktif.\n\n"
    "Fitur:\n"
    "- `.start` / `/start`: cek bot + help\n"
    "- Auto delete pesan berisi `@`\n"
    "- Auto delete pesan berisi link\n"
    f"- Auto delete spam teks sama (>= {SPAM_REPEAT}x dalam {SPAM_WINDOW}s)\n"
)

@dataclass
class SeenMsg:
    last_ts: float
    count: int

# Key: (chat_id, user_id, normalized_text)
SEEN: Dict[Tuple[int, int, str], SeenMsg] = {}


def normalize_text(s: str) -> str:
    # Normalisasi biar "SPAM  " == "spam"
    s = (s or "").strip().lower()
    # rapihin spasi berlebih
    s = re.sub(r"\s+", " ", s)
    return s


def prune_old(now: float):
    # bersihin cache yang udah lewat window biar nggak bengkak
    to_del = []
    for k, v in SEEN.items():
        if now - v.last_ts > SPAM_WINDOW:
            to_del.append(k)
    for k in to_del:
        SEEN.pop(k, None)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Mendukung .start jika dikirim sebagai teks, tapi command handler hanya menangkap /start.
    # Untuk ".start", kita tangkap di message handler dan panggil fungsi ini.
    if update.message:
        await update.message.reply_text(HELP_TEXT)


async def maybe_delete(update: Update, reason: str):
    """Delete message safely; fail silently if no permission."""
    msg = update.message
    if not msg:
        return

    try:
        await msg.delete()
    except Exception:
        # biasanya karena bot bukan admin / tidak punya izin delete
        return


async def guard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # Hanya berlaku di grup/supergroup
    if msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    text = msg.text

    # 0) dukung ".start" (tanpa slash)
    if text.strip().lower() == ".start":
        return await start_cmd(update, context)

    # 1) hapus kalau ada @mention
    if AT_RE.search(text):
        return await maybe_delete(update, "contains @")

    # 2) hapus kalau ada link
    if LINK_RE.search(text):
        return await maybe_delete(update, "contains link")

    # 3) hapus spam teks yang sama (repeat dalam window)
    now = time.time()
    prune_old(now)

    norm = normalize_text(text)
    if not norm:
        return

    user_id = msg.from_user.id if msg.from_user else 0
    key = (msg.chat.id, user_id, norm)

    seen = SEEN.get(key)
    if not seen:
        SEEN[key] = SeenMsg(last_ts=now, count=1)
        return

    # masih dalam window
    if now - seen.last_ts <= SPAM_WINDOW:
        seen.count += 1
        seen.last_ts = now
        SEEN[key] = seen

        if seen.count >= SPAM_REPEAT:
            return await maybe_delete(update, "repeat spam")
    else:
        # lewat window, reset
        SEEN[key] = SeenMsg(last_ts=now, count=1)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start dan /help
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))

    # Guard semua teks non-command (kecuali /start dll)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guard_handler))

    print("Bot jalan... pastiin bot admin di grup + izin delete.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

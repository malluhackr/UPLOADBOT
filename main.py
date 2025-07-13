
# == main.py ==
import os
import sys
import time
import asyncio
import threading
import logging
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from instagrapi import Client as InstaClient
from dotenv import load_dotenv

# === Setup Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UploaderBot")

load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL_ID = -1002750394644

INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")

AUTHORIZED_USERS = [7898534200]  # Add your Telegram user ID
user_states = {}

app = Client("insta_bot", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, bot_token=TELEGRAM_BOT_TOKEN)
insta = InstaClient()

# === Insta login ===
def login_instagram():
    try:
        insta.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        return True
    except Exception as e:
        logger.error(f"Instagram login failed: {e}")
        return False

# === Keyboards ===
def main_menu():
    return ReplyKeyboardMarkup([
        ["📤 Upload Reel", "⚙️ Settings"],
        ["📈 Stats", "♻️ Restart"]
    ], resize_keyboard=True)

def settings_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
        [InlineKeyboardButton("📐 Aspect Ratio", callback_data="aspect_ratio")],
        [InlineKeyboardButton("📝 Caption", callback_data="caption")],
        [InlineKeyboardButton("🏷️ Hashtags", callback_data="hashtags")],
        [InlineKeyboardButton("🔐 Login", callback_data="login")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])

# === /start ===
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    if message.from_user.id not in AUTHORIZED_USERS:
        await message.reply("⛔ You are not authorized.")
        return
    await message.reply("👋 Welcome to the Reels Uploader Bot!", reply_markup=main_menu())

# === /settings ===
@app.on_message(filters.command("settings") | filters.regex("⚙️ Settings"))
async def settings_handler(client, message):
    await message.reply("🔧 Bot Settings:", reply_markup=settings_buttons())

# === Callback Buttons ===
@app.on_callback_query()
async def callback_handler(client, callback: CallbackQuery):
    data = callback.data
    if data == "login":
        success = login_instagram()
        await callback.message.edit_text("✅ Login successful!" if success else "❌ Login failed.")
    elif data == "back":
        await callback.message.edit_text("Main Menu", reply_markup=settings_buttons())
    else:
        await callback.answer(f"Selected: {data}")

# === /restart ===
@app.on_message(filters.command("restart") | filters.regex("♻️ Restart"))
async def restart_handler(client, message):
    if message.from_user.id not in AUTHORIZED_USERS:
        return
    await message.reply("🔄 Restarting bot...")
    os.execv(sys.executable, ['python'] + sys.argv)

# === /stats ===
@app.on_message(filters.command("stats") | filters.regex("📈 Stats"))
async def stats_handler(client, message):
    stats = "📊 Bot Stats:\n"
    stats += f"Authorized Users: {len(AUTHORIZED_USERS)}\n"
    stats += f"Logged into Instagram: {'Yes' if insta.user_id else 'No'}"
    await message.reply(stats)

# === Upload Reel Flow ===
@app.on_message(filters.regex("📤 Upload Reel"))
async def upload_reel(client, message):
    user_id = message.from_user.id
    if user_id not in AUTHORIZED_USERS:
        await message.reply("⛔ Not allowed.")
        return
    user_states[user_id] = {"step": "video"}
    await message.reply("🎥 Send your video now")

@app.on_message(filters.video)
async def handle_video(client, message):
    user_id = message.from_user.id
    if user_states.get(user_id, {}).get("step") != "video":
        return
    file = await message.download()
    progress_msg = await message.reply("⬆️ Uploading...\n" + generate_progress_bar(0))

    for i in range(1, 11):
        await asyncio.sleep(0.3)
        await progress_msg.edit_text(f"⬆️ Uploading...\n" + generate_progress_bar(i * 10))

    result = insta.clip_upload(file, caption="Uploaded from bot")
    await progress_msg.edit_text("✅ Upload complete!")

# === Progress Bar Generator ===
def generate_progress_bar(percent):
    filled = int(percent / 5)
    empty = 20 - filled
    return f"┃ [{'■' * filled}{'▦' if percent % 5 > 0 else ''}{'□' * empty}] {percent:.2f}%"

# === Health check server ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    httpd = HTTPServer(('0.0.0.0', 8080), Handler)
    httpd.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

logger.info("Bot starting...")
app.run()
```

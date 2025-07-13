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
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from instagrapi import Client as InstaClient
from dotenv import load_dotenv

# === Setup Logging ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === Configuration ===
load_dotenv()

class Config:
    TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
    LOG_CHANNEL_ID = -1002750394644
    DB_CHANNEL_ID = -1002750394644
    INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
    INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
    DATA_DIR = Path("data")
    DATA_DIR.mkdir(exist_ok=True)
    AUTHORIZED_USERS_FILE = DATA_DIR / "authorized_users.txt"
    SESSION_FILE = DATA_DIR / "insta_session.json"
    if not AUTHORIZED_USERS_FILE.exists():
        with open(AUTHORIZED_USERS_FILE, "w") as f:
            f.write("7898534200\n")

config = Config()

# === Instagram Client ===
class InstagramUploader:
    def __init__(self):
        self.client = InstaClient()
        self.load_settings()

    def load_settings(self):
        if config.INSTAGRAM_PROXY:
            self.client.set_proxy(config.INSTAGRAM_PROXY)
        if config.SESSION_FILE.exists():
            self.client.load_settings(config.SESSION_FILE)

    def login(self):
        try:
            self.client.login(config.INSTAGRAM_USERNAME, config.INSTAGRAM_PASSWORD)
            self.client.dump_settings(config.SESSION_FILE)
            return True
        except Exception as e:
            logger.error(f"Instagram login failed: {e}")
            return False

    def upload_reel(self, video_path: str, caption: str, aspect_ratio: str = "9:16", upload_type: str = "reel"):
        try:
            extra_data = {
                'configure_mode': 'REELS' if upload_type == "reel" else 'DEFAULT',
                'like_and_view_counts_disabled': False,
                'disable_comments': False
            }
            result = self.client.clip_upload(video_path, caption=caption, extra_data=extra_data)
            return True, result.code
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False, str(e)

insta_uploader = InstagramUploader()
app = Client("upload_bot", api_id=config.TELEGRAM_API_ID, api_hash=config.TELEGRAM_API_HASH, bot_token=config.TELEGRAM_BOT_TOKEN)

user_states = {}

def get_main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("\ud83d\udcc4 Upload Reel"), KeyboardButton("\u2699\ufe0f Settings")],
            [KeyboardButton("\ud83d\udcca Stats"), KeyboardButton("\ud83d\udd04 Restart Bot")]
        ], resize_keyboard=True
    )

def get_settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\ud83d\udccc Upload Type", callback_data="set_upload_type")],
        [InlineKeyboardButton("\ud83d\udd00 Aspect Ratio", callback_data="set_aspect_ratio")],
        [InlineKeyboardButton("\ud83d\udcdd Default Caption", callback_data="set_caption")],
        [InlineKeyboardButton("\ud83c\udff7\ufe0f Default Hashtags", callback_data="set_hashtags")],
        [InlineKeyboardButton("\ud83d\udd19 Back", callback_data="main_menu")]
    ])

def is_authorized(user_id: int) -> bool:
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r") as file:
            return str(user_id) in file.read().splitlines()
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        return False

@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(f"\u274c Unauthorized.\nYour ID: {user_id}")
        return
    await message.reply("\ud83d\udc4b Welcome to Instagram Reels Bot!", reply_markup=get_main_menu())

@app.on_message(filters.command("restart"))
async def restart(client, message):
    if not is_authorized(message.from_user.id):
        await message.reply("⛔ Unauthorized.")
        return
    await message.reply("♻️ Restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

@app.on_message(filters.command("settings"))
async def settings_menu(client, message):
    if not is_authorized(message.from_user.id):
        await message.reply("⛔ Unauthorized.")
        return
    await message.reply("⚙️ Bot Settings:", reply_markup=get_settings_menu())

@app.on_message(filters.text & filters.regex("^\ud83d\udcc4 Upload Reel$"))
async def upload_prompt(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply("⛔ Unauthorized.")
        return
    user_states[user_id] = {"step": "awaiting_video"}
    await message.reply("\ud83c\udfa5 Send your reel video.", reply_markup=ReplyKeyboardRemove())

@app.on_message(filters.video)
async def receive_video(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply("⛔ Unauthorized.")
        return
    state = user_states.get(user_id)
    if not state or state.get("step") != "awaiting_video":
        return
    video_path = await message.download()
    user_states[user_id] = {
        "step": "awaiting_caption",
        "video_path": video_path
    }
    await message.reply("✍️ Now send the caption for your reel.")

@app.on_message(filters.text & filters.create(lambda _, __, m: user_states.get(m.chat.id, {}).get("step") == "awaiting_caption"))
async def receive_caption(client, message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state:
        return
    video_path = state.get("video_path")
    caption = message.text
    success, result = insta_uploader.upload_reel(video_path, caption)
    if success:
        await message.reply(f"✅ Reel uploaded: https://instagram.com/reel/{result}")
    else:
        await message.reply(f"❌ Upload failed: {result}")
    user_states.pop(user_id, None)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    httpd = HTTPServer(('0.0.0.0', 8080), Handler)
    httpd.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Bot running...")
    app.run()

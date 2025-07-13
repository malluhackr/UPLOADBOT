import os
import sys
import time
import asyncio
import threading
import logging
from pathlib import Path
from datetime import datetime
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

# ===== INITIALIZATION =====
load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== CONFIGURATION =====
class Config:
    # Telegram
    TELEGRAM_API_ID = 24026226
    TELEGRAM_API_HASH = "76b243b66cf12f8b7a603daef8859837"
    TELEGRAM_BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
    LOG_CHANNEL_ID = -1002750394644
    
    # Instagram
    INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME")
    INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY")
    
    # Paths (Docker compatible)
    BASE_DIR = Path(__file__).parent
    DATA_DIR = BASE_DIR / "data"
    DATA_DIR.mkdir(exist_ok=True)
    
    AUTHORIZED_USERS_FILE = DATA_DIR / "authorized_users.txt"
    SESSION_FILE = DATA_DIR / "insta_session.json"
    
    # Initialize authorized users
    if not AUTHORIZED_USERS_FILE.exists():
        with open(AUTHORIZED_USERS_FILE, "w") as f:
            f.write("7898534200\n")  # Your admin ID

config = Config()

# ===== INSTAGRAM CLIENT =====
class InstagramManager:
    def __init__(self):
        self.client = InstaClient()
        self._load_session()
    
    def _load_session(self):
        try:
            if config.INSTAGRAM_PROXY:
                self.client.set_proxy(config.INSTAGRAM_PROXY)
            if config.SESSION_FILE.exists():
                self.client.load_settings(config.SESSION_FILE)
            if not self.client.login(config.INSTAGRAM_USERNAME, config.INSTAGRAM_PASSWORD):
                raise Exception("Login failed")
            self.client.dump_settings(config.SESSION_FILE)
        except Exception as e:
            logger.error(f"Instagram init error: {e}")

    def upload_reel(self, video_path: str, caption: str = "", aspect_ratio: str = "9:16") -> bool:
        try:
            extra_data = {
                'configure_mode': 'REELS' if aspect_ratio == "9:16" else 'DEFAULT',
                'like_and_view_counts_disabled': False
            }
            self.client.clip_upload(video_path, caption=caption, extra_data=extra_data)
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

# ===== TELEGRAM BOT =====
app = Client(
    "reels_bot",
    api_id=config.TELEGRAM_API_ID,
    api_hash=config.TELEGRAM_API_HASH,
    bot_token=config.TELEGRAM_BOT_TOKEN
)
insta_manager = InstagramManager()
user_states = {}

# ===== UTILITIES =====
def is_authorized(user_id: int) -> bool:
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r") as f:
            return str(user_id) in [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        return False

async def send_progress(message, current: int, total: int):
    percent = current / total * 100
    progress_bar = (
        f"Upload Task Reels\n"
        f"┃[{'■' * int(percent/10)}{'□' * (10 - int(percent/10))}] {percent:.2f}%"
    )
    await message.edit_text(progress_bar)

# ===== HANDLERS =====
@app.on_message(filters.command("start"))
async def start(_, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(
            f"⛔ Unauthorized (Admin ID: 7898534200)\n"
            f"Your ID: {user_id}"
        )
        return
    
    await message.reply(
        "👋 Admin Access Granted!\n"
        "Send a video to upload as Reel.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📤 Upload Reel")]],
            resize_keyboard=True
        )
    )

@app.on_message(filters.video)
async def handle_video(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return
    
    try:
        # Step 1: Download
        progress_msg = await message.reply("⬇️ Downloading... 0%")
        video_path = await message.download(
            progress=lambda c, t: asyncio.run(send_progress(progress_msg, c, t))
        
        # Step 2: Get caption
        await progress_msg.edit_text("📝 Send caption (or /skip)")
        caption_msg = await client.listen(message.chat.id, filters.text, timeout=60)
        caption = caption_msg.text if not caption_msg.text.startswith("/skip") else ""
        
        # Step 3: Upload with progress
        await progress_msg.edit_text("⏫ Uploading to Instagram...")
        if insta_manager.upload_reel(video_path, caption):
            await progress_msg.edit_text("✅ Uploaded successfully!")
            await client.send_message(
                config.LOG_CHANNEL_ID,
                f"New Reel uploaded by {user_id}"
            )
        else:
            await progress_msg.edit_text("❌ Upload failed")
        
    except Exception as e:
        await message.reply(f"⚠️ Error: {str(e)}")
        logger.error(f"Handler crashed: {e}")
    finally:
        if 'video_path' in locals():
            os.remove(video_path)

# ===== HEALTH CHECK =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    HTTPServer(('0.0.0.0', 8080), HealthHandler).serve_forever()

# ===== MAIN =====
if __name__ == "__main__":
    # Verify critical files
    logger.info(f"Admin ID confirmed: 7898534200")
    logger.info(f"Authorized users: {open(config.AUTHORIZED_USERS_FILE).read()}")
    
    # Start services
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Starting bot...")
    app.run()

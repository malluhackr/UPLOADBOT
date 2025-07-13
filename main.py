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
    ReplyKeyboardRemove
)
from instagrapi import Client as InstaClient
from dotenv import load_dotenv

# === Setup ===
load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Configuration ===
class Config:
    TELEGRAM_API_ID = 24026226
    TELEGRAM_API_HASH = "76b243b66cf12f8b7a603daef8859837"
    TELEGRAM_BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
    LOG_CHANNEL_ID = -1002750394644
    INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME")
    INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD")
    
    # Paths
    DATA_DIR = Path("/app/data")
    DATA_DIR.mkdir(exist_ok=True, parents=True)
    AUTHORIZED_USERS_FILE = DATA_DIR / "authorized_users.txt"
    SESSION_FILE = DATA_DIR / "insta_session.json"
    
    # Initialize authorized users
    if not AUTHORIZED_USERS_FILE.exists():
        with open(AUTHORIZED_USERS_FILE, "w") as f:
            f.write("7898534200\n")  # Your admin ID

config = Config()

# === Instagram Client ===
insta_client = InstaClient()
if config.INSTAGRAM_USERNAME and config.INSTAGRAM_PASSWORD:
    try:
        if config.SESSION_FILE.exists():
            insta_client.load_settings(config.SESSION_FILE)
        insta_client.login(config.INSTAGRAM_USERNAME, config.INSTAGRAM_PASSWORD)
        insta_client.dump_settings(config.SESSION_FILE)
    except Exception as e:
        logger.error(f"Instagram login failed: {e}")

# === Telegram Bot ===
app = Client(
    "reels_bot",
    api_id=config.TELEGRAM_API_ID,
    api_hash=config.TELEGRAM_API_HASH,
    bot_token=config.TELEGRAM_BOT_TOKEN
)

# === Utilities ===
def is_authorized(user_id: int) -> bool:
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r") as f:
            return str(user_id) in [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        return False

async def upload_with_progress(message, video_path: str, caption: str = ""):
    try:
        progress_msg = await message.reply("⏫ Uploading to Instagram... 0%")
        
        def progress_callback(current, total):
            percent = current / total * 100
            progress_bar = f"┃[{'■' * int(percent/10)}{'□' * (10 - int(percent/10))}] {percent:.2f}%"
            asyncio.run_coroutine_threadsafe(
                progress_msg.edit_text(f"⏫ Uploading to Instagram...\n{progress_bar}"),
                app.loop
            )
        
        insta_client.clip_upload(
            video_path,
            caption=caption,
            extra_data={"configure_mode": "REELS"},
            progress=progress_callback
        )
        
        await progress_msg.edit_text("✅ Uploaded successfully!")
        await app.send_message(config.LOG_CHANNEL_ID, f"New Reel uploaded by {message.from_user.id}")
        return True
    except Exception as e:
        await message.reply(f"❌ Upload failed: {str(e)}")
        logger.error(f"Upload error: {e}")
        return False
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)

# === Handlers ===
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(
            f"⛔ Unauthorized access\n"
            f"Your ID: {user_id}\n"
            f"Admin ID: 7898534200"
        )
        return
    
    await message.reply(
        "👋 Welcome to Instagram Reels Uploader!\n"
        "Send me a video to get started.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📤 Upload Reel")]],
            resize_keyboard=True
        )
    )

@app.on_message(filters.video)
async def video_handler(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return
    
    try:
        # Download video
        download_msg = await message.reply("⬇️ Downloading video...")
        video_path = await message.download()
        await download_msg.delete()
        
        # Get caption
        caption_msg = await message.reply(
            "📝 Please send your caption (or /skip):",
            reply_markup=ReplyKeyboardRemove()
        )
        
        try:
            caption_response = await client.listen(message.chat.id, timeout=60)
            caption = caption_response.text if not caption_response.text.startswith("/skip") else ""
        except asyncio.TimeoutError:
            caption = ""
        
        # Upload
        await upload_with_progress(message, video_path, caption)
        
    except Exception as e:
        await message.reply(f"⚠️ Error: {str(e)}")
        logger.error(f"Handler error: {e}")

# === Health Check ===
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    httpd = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    httpd.serve_forever()

# === Main ===
if __name__ == "__main__":
    # Verify configuration
    logger.info("Starting bot with configuration:")
    logger.info(f"Admin ID: 7898534200")
    logger.info(f"Authorized users: {open(config.AUTHORIZED_USERS_FILE).read()}")
    
    # Start services
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Bot starting...")
    app.run()

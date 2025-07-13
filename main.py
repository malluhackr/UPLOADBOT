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
    ReplyKeyboardRemove,
    Message
)
from instagrapi import Client as InstaClient
from dotenv import load_dotenv

# === Enhanced Logging ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(Path('data/bot.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === Configuration with Validation ===
load_dotenv()

class Config:
    # Telegram Credentials
    TELEGRAM_API_ID = 24026226
    TELEGRAM_API_HASH = "76b243b66cf12f8b7a603daef8859837"
    TELEGRAM_BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
    LOG_CHANNEL_ID = -1002750394644
    
    # Instagram Credentials
    INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
    INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
    
    # Path Configuration (Docker compatible)
    DATA_DIR = Path("/app/data")
    DATA_DIR.mkdir(exist_ok=True, parents=True)
    AUTHORIZED_USERS_FILE = DATA_DIR / "authorized_users.txt"
    SESSION_FILE = DATA_DIR / "insta_session.json"
    
    # Initialize authorized users with admin ID
    if not AUTHORIZED_USERS_FILE.exists():
        with open(AUTHORIZED_USERS_FILE, "w") as f:
            f.write("7898534200\n")  # Your admin ID

config = Config()

# === Instagram Client with Enhanced Error Handling ===
class InstagramManager:
    def __init__(self):
        self.client = InstaClient()
        self._initialize()
    
    def _initialize(self):
        try:
            if config.INSTAGRAM_PROXY:
                self.client.set_proxy(config.INSTAGRAM_PROXY)
            if config.SESSION_FILE.exists():
                self.client.load_settings(config.SESSION_FILE)
            if not self.client.login(config.INSTAGRAM_USERNAME, config.INSTAGRAM_PASSWORD):
                raise Exception("Login failed")
            self.client.dump_settings(config.SESSION_FILE)
            logger.info("Instagram client initialized successfully")
        except Exception as e:
            logger.error(f"Instagram initialization failed: {e}")

    def upload_reel(self, video_path: str, caption: str = "", aspect_ratio: str = "9:16") -> bool:
        try:
            extra_data = {
                'configure_mode': 'REELS' if aspect_ratio == "9:16" else 'DEFAULT',
                'like_and_view_counts_disabled': False,
                'disable_comments': False
            }
            media = self.client.clip_upload(video_path, caption=caption, extra_data=extra_data)
            logger.info(f"Uploaded successfully: {media.code}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

# === Telegram Bot Setup ===
app = Client(
    "instagram_reels_bot",
    api_id=config.TELEGRAM_API_ID,
    api_hash=config.TELEGRAM_API_HASH,
    bot_token=config.TELEGRAM_BOT_TOKEN
)
insta_manager = InstagramManager()
user_states = {}

# === Enhanced Utility Functions ===
def is_authorized(user_id: int) -> bool:
    """Check if user is authorized with detailed logging"""
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r") as f:
            authorized_ids = [line.strip() for line in f if line.strip()]
            logger.info(f"Checking authorization for {user_id}. Authorized IDs: {authorized_ids}")
            return str(user_id) in authorized_ids
    except Exception as e:
        logger.error(f"Authorization check failed: {e}")
        return False

async def send_progress(message: Message, current: int, total: int, mode: str = "download"):
    """Show visual progress bar with emoji indicators"""
    percent = current / total * 100
    progress_bar = (
        f"{'⬇️' if mode == 'download' else '⏫'} {mode.capitalize()}ing Reel\n"
        f"┃[{'■' * int(percent/10)}{'□' * (10 - int(percent/10))}] {percent:.2f}%"
    )
    await message.edit_text(progress_bar)

# === Command Handlers ===
@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(
            f"⛔ Unauthorized Access\n\n"
            f"Admin ID: 7898534200\n"
            f"Your ID: {user_id}"
        )
        return
    
    await message.reply(
        "👋 Welcome Admin!\n\n"
        "🔹 /upload - Start new upload\n"
        "🔹 /settings - Configure bot\n"
        "🔹 /restart - Restart the bot",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📤 Upload Reel")]],
            resize_keyboard=True
        )
    )

@app.on_message(filters.video)
async def video_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        return
    
    try:
        # Step 1: Download with progress
        progress_msg = await message.reply("Starting download...")
        video_path = await message.download(
            progress=lambda c, t: asyncio.run(send_progress(progress_msg, c, t, "download")))
        
        # Step 2: Get caption
        await progress_msg.edit_text("📝 Send caption (or /skip for no caption)")
        try:
            caption_msg = await client.listen(message.chat.id, filters.text, timeout=120)
            if caption_msg.text.startswith("/skip"):
                caption = ""
            else:
                caption = f"{caption_msg.text}\n\n#Reel #Viral"
        except asyncio.TimeoutError:
            caption = ""
        
        # Step 3: Upload with progress
        await progress_msg.edit_text("Starting Instagram upload...")
        success = insta_manager.upload_reel(video_path, caption)
        
        if success:
            await progress_msg.edit_text("✅ Successfully uploaded to Instagram!")
            await client.send_message(
                config.LOG_CHANNEL_ID,
                f"📤 New Reel uploaded by {user_id}\n"
                f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            await progress_msg.edit_text("❌ Failed to upload to Instagram")
        
    except Exception as e:
        await message.reply(f"⚠️ Error: {str(e)}")
        logger.error(f"Handler error: {e}", exc_info=True)
    finally:
        if 'video_path' in locals():
            os.remove(video_path)

# === Health Check Server ===
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    httpd = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    httpd.serve_forever()

# === Startup ===
if __name__ == "__main__":
    # Verify configuration
    logger.info("=== Starting Bot ===")
    logger.info(f"Admin ID: 7898534200")
    logger.info(f"Instagram User: {config.INSTAGRAM_USERNAME}")
    
    # Start services
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Health check server started on port 8080")
    
    # Start the bot
    app.run()
    logger.info("Bot stopped")

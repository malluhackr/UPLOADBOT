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
    # Telegram
    TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
    LOG_CHANNEL_ID = -1002750394644
    DB_CHANNEL_ID = -1000000000000  # Add your DB channel ID
    
    # Instagram
    INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
    INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
    
    # Paths
    DATA_DIR = Path("data")
    DATA_DIR.mkdir(exist_ok=True)
    AUTHORIZED_USERS_FILE = DATA_DIR / "authorized_users.txt"
    SESSION_FILE = DATA_DIR / "insta_session.json"
    
    # Initialize authorized users
    if not AUTHORIZED_USERS_FILE.exists():
        with open(AUTHORIZED_USERS_FILE, "w") as f:
            f.write("7898534200\n")  # Your admin ID

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
            
            if aspect_ratio == "1:1":
                # Generate square thumbnail for square videos
                extra_data['thumbnail'] = self._generate_thumbnail(video_path)
            
            result = self.client.clip_upload(video_path, caption=caption, extra_data=extra_data)
            return True, result.code
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False, str(e)

# === Initialize ===
insta_uploader = InstagramUploader()
app = Client(
    "upload_bot",
    api_id=config.TELEGRAM_API_ID,
    api_hash=config.TELEGRAM_API_HASH,
    bot_token=config.TELEGRAM_BOT_TOKEN
)

# === User States ===
user_states = {}

# === Keyboard Layouts ===
def get_main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📤 Upload Reel"), KeyboardButton("⚙️ Settings")],
            [KeyboardButton("📊 Stats"), KeyboardButton("🔄 Restart Bot")]
        ],
        resize_keyboard=True
    )

def get_settings_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📌 Upload Type", callback_data="set_upload_type")],
            [InlineKeyboardButton("📐 Aspect Ratio", callback_data="set_aspect_ratio")],
            [InlineKeyboardButton("📝 Default Caption", callback_data="set_caption")],
            [InlineKeyboardButton("🏷️ Default Hashtags", callback_data="set_hashtags")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
    )

def get_aspect_ratio_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("9:16 (Reels)", callback_data="ratio_9_16")],
            [InlineKeyboardButton("1:1 (Square)", callback_data="ratio_1_1")],
            [InlineKeyboardButton("4:5 (Portrait)", callback_data="ratio_4_5")],
            [InlineKeyboardButton("🔙 Back", callback_data="settings_menu")]
        ]
    )

# === Utility Functions ===
def is_authorized(user_id: int) -> bool:
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r") as file:
            return str(user_id) in file.read().splitlines()
    except Exception as e:
        logger.error(f"Auth check failed: {e}")
        return False

async def log_to_channels(message: str, media_code: str = ""):
    """Log to both log channel and database channel"""
    try:
        await app.send_message(config.LOG_CHANNEL_ID, message)
        if media_code:
            db_message = f"{message}\nPost URL: https://instagram.com/reel/{media_code}"
            await app.send_message(config.DB_CHANNEL_ID, db_message)
    except Exception as e:
        logger.error(f"Channel logging failed: {e}")

def generate_progress_bar(percent: float) -> str:
    filled = int(percent / 5)  # More precise 20-step progress
    empty = 20 - filled
    return f"Upload Task Reels\n┃ [{'■' * filled}{'▦' if percent % 5 > 0 else ''}{'□' * empty}] {percent:.2f}%"

# === Command Handlers ===
@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(
            f"⛔ Unauthorized access.\n\n"
            f"Your ID: {user_id}\n"
            f"Admin ID: 7898534200"
        )
        return
    
    await message.reply(
        "👋 Welcome to Advanced Instagram Reels Uploader!\n\n"
        "Choose an option below:",
        reply_markup=get_main_menu()
    )

@app.on_message(filters.command("settings"))
async def settings_menu(client, message):
    if not is_authorized(message.from_user.id):
        await message.reply("⛔ Unauthorized.")
        return
    
    await message.reply(
        "⚙️ Bot Settings:",
        reply_markup=get_settings_menu()
    )

# === Upload Flow ===
@app.on_message(filters.text & filters.regex("^📤 Upload Reel$"))
async def upload_reel_prompt(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply("⛔ Unauthorized.")
        return
    
    user_states[user_id] = {
        "step": "awaiting_video",
        "aspect_ratio": "9:16",
        "upload_type": "reel",
        "default_hashtags": "#reel #viral"
    }
    await message.reply(
        "🎥 Please send your video (any size supported):",
        reply_markup=ReplyKeyboardRemove()
    )

@app.on_message(filters.video)
async def handle_video_upload(client, message):
    user_id = message.from_user.id
    if user_id not in user_states or user_states[user_id].get("step") != "awaiting_video":
        return
    
    try:
        # Download with progress
        progress_msg = await message.reply("⬇️ Starting download... 0%")
        
        def progress_callback(current, total):
            percent = current / total * 100
            progress_text = f"⬇️ Downloading...\n{generate_progress_bar(percent)}"
            asyncio.run_coroutine_threadsafe(
                progress_msg.edit_text(progress_text),
                app.loop
            )
        
        video_path = await message.download(progress=progress_callback)
        await progress_msg.edit_text("✅ Download complete!")
        
        # Store video path and move to caption step
        user_states[user_id].update({
            "step": "awaiting_caption",
            "video_path": video_path
        })
        
        await message.reply(
            "📝 Please send your caption (or /skip to use default):",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Use Default Caption")]],
                resize_keyboard=True
            )
        )
        
        await log_to_channels(f"📥 New video received from user {user_id}")
        
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")
        logger.error(f"Video handling error: {e}")
        if 'video_path' in locals():
            os.remove(video_path)

# === Keep Alive Server ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    httpd = HTTPServer(('0.0.0.0', 8080), Handler)
    httpd.serve_forever()

# === Startup ===
if __name__ == "__main__":
    # Start health check server
    threading.Thread(target=run_server, daemon=True).start()
    
    # Start the bot
    logger.info("Starting Instagram Reels Uploader Bot...")
    app.run()

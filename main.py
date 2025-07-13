import os
import time
import threading
import logging
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from instagrapi import Client as InstaClient
from dotenv import load_dotenv

# === Setup logging ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Configuration ===
load_dotenv()

class Config:
    TELEGRAM_API_ID = 24026226
    TELEGRAM_API_HASH = "76b243b66cf12f8b7a603daef8859837"
    TELEGRAM_BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
    LOG_CHANNEL_ID = -1002750394644
    INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
    INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
    INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
    
    # File paths
    DATA_DIR = Path("data")
    AUTHORIZED_USERS_FILE = DATA_DIR / "authorized_users.txt"
    SESSION_FILE = DATA_DIR / "insta_session.json"
    SETTINGS_FILE = DATA_DIR / "user_settings.json"
    
    # Create data directory if not exists
    DATA_DIR.mkdir(exist_ok=True)

config = Config()

# === Instagram Client ===
class InstagramUploader:
    def __init__(self):
        self.client = InstaClient()
        self.load_settings()
    
    def load_settings(self):
        if config.INSTAGRAM_PROXY:
            self.client.set_proxy(config.INSTAGRAM_PROXY)
        if os.path.exists(config.SESSION_FILE):
            self.client.load_settings(config.SESSION_FILE)
    
    def login(self):
        try:
            self.client.login(config.INSTAGRAM_USERNAME, config.INSTAGRAM_PASSWORD)
            self.client.dump_settings(config.SESSION_FILE)
            return True
        except Exception as e:
            logger.error(f"Instagram login failed: {e}")
            return False
    
    def upload_reel(self, video_path: str, caption: str, aspect_ratio: str = "9:16"):
        try:
            if not self.client.user_id:
                if not self.login():
                    return False
            
            # Progress callback function
            def progress_callback(current, total):
                percent = (current / total) * 100
                print(f"Upload progress: {percent:.2f}%")
            
            # Set aspect ratio
            if aspect_ratio == "9:16":
                extra_data = {'configure_mode': 'REELS'}
            else:
                extra_data = {'configure_mode': 'DEFAULT'}
            
            self.client.clip_upload(
                path=video_path,
                caption=caption,
                extra_data=extra_data,
                progress=progress_callback
            )
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

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
user_settings = {}

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

def get_upload_type_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Single Reel", callback_data="upload_single")],
            [InlineKeyboardButton("Multiple Reels", callback_data="upload_multiple")],
            [InlineKeyboardButton("Back", callback_data="settings_menu")]
        ]
    )

def get_aspect_ratio_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("9:16 (Reels)", callback_data="ratio_9_16")],
            [InlineKeyboardButton("1:1 (Square)", callback_data="ratio_1_1")],
            [InlineKeyboardButton("4:5 (Portrait)", callback_data="ratio_4_5")],
            [InlineKeyboardButton("Back", callback_data="settings_menu")]
        ]
    )

# === Utility Functions ===
def is_authorized(user_id: int) -> bool:
    try:
        with open(config.AUTHORIZED_USERS_FILE, "r") as file:
            return str(user_id) in file.read().splitlines()
    except FileNotFoundError:
        return False

async def log_to_channel(message: str):
    try:
        await app.send_message(config.LOG_CHANNEL_ID, message)
    except Exception as e:
        logger.error(f"Failed to log to channel: {e}")

async def cleanup_file(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.error(f"Error cleaning up file {path}: {e}")

# === Command Handlers ===
@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(
            f"⛔ Unauthorized access.\n\n"
            f"Your ID: {user_id}\n\n"
            f"Contact admin to get access."
        )
        return
    
    await message.reply(
        "👋 Welcome to Advanced Instagram Reels Uploader!\n\n"
        "Choose an option below:",
        reply_markup=get_main_menu()
    )

@app.on_message(filters.command("restart"))
async def restart_bot(client, message):
    if not is_authorized(message.from_user.id):
        await message.reply("⛔ Unauthorized.")
        return
    
    await message.reply("🔄 Restarting bot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command("settings"))
async def settings_menu(client, message):
    if not is_authorized(message.from_user.id):
        await message.reply("⛔ Unauthorized.")
        return
    
    await message.reply(
        "⚙️ Bot Settings:",
        reply_markup=get_settings_menu()
    )

# === Callback Handlers ===
@app.on_callback_query(filters.regex("^settings_menu$"))
async def settings_menu_callback(client, callback_query):
    await callback_query.message.edit_text(
        "⚙️ Bot Settings:",
        reply_markup=get_settings_menu()
    )

@app.on_callback_query(filters.regex("^set_upload_type$"))
async def set_upload_type(client, callback_query):
    await callback_query.message.edit_text(
        "📌 Select Upload Type:",
        reply_markup=get_upload_type_menu()
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio$"))
async def set_aspect_ratio(client, callback_query):
    await callback_query.message.edit_text(
        "📐 Select Aspect Ratio:",
        reply_markup=get_aspect_ratio_menu()
    )

# === Upload Handlers ===
@app.on_message(filters.text & filters.regex("^📤 Upload Reel$"))
async def upload_reel_prompt(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply("⛔ Unauthorized.")
        return
    
    user_states[user_id] = {"step": "awaiting_video"}
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
        # Download video with progress
        download_msg = await message.reply("⬇️ Downloading video... 0%")
        
        def progress(current, total):
            asyncio.run_coroutine_threadsafe(
                download_msg.edit_text(f"⬇️ Downloading video... {current * 100 / total:.1f}%"),
                app.loop
            )
        
        video_path = await message.download(progress=progress)
        await download_msg.delete()
        
        user_states[user_id].update({
            "step": "awaiting_caption",
            "video_path": video_path
        })
        
        await message.reply(
            "📝 Please send your caption for the reel:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Use Default Caption")]],
                resize_keyboard=True
            )
        )
        
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")
        if 'video_path' in locals():
            await cleanup_file(video_path)

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

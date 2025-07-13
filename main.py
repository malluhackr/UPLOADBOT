import os
import sys
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime
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

# === Initialize Environment ===
load_dotenv()
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# === Logging Setup ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(DATA_DIR / "bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ReelsBotPro")

# === Configuration ===
class Config:
    # Telegram
    API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
    API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
    LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002750394644"))
    ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))
    
    # Files
    PREMIUM_USERS_FILE = DATA_DIR / "premium_users.txt"
    
    # Initialize premium users
    if not PREMIUM_USERS_FILE.exists():
        with open(PREMIUM_USERS_FILE, "w") as f:
            f.write(f"{ADMIN_ID}\n")  # Add admin as default

config = Config()

# === Instagram Manager ===
class InstagramManager:
    def __init__(self):
        self.client = InstaClient()
        self.user_sessions = {}  # {user_id: {username, password}}

# === Bot Setup ===
app = Client(
    "reels_bot_pro",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)
insta_manager = InstagramManager()

# === Keyboard Layouts ===
def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("📤 Upload Reel")],
        [KeyboardButton("⚙️ Settings")]
    ]
    if user_id == config.ADMIN_ID:
        buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Upload Type", callback_data="set_upload_type")],
        [InlineKeyboardButton("📐 Aspect Ratio", callback_data="set_aspect_ratio")],
        [InlineKeyboardButton("📝 Default Caption", callback_data="set_caption")],
        [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")]
    ])

def get_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Premium", callback_data="add_premium")],
        [InlineKeyboardButton("➖ Remove Premium", callback_data="remove_premium")],
        [InlineKeyboardButton("📊 Stats", callback_data="view_stats")]
    ])

# === Utility Functions ===
def is_premium_user(user_id: int) -> bool:
    try:
        with open(config.PREMIUM_USERS_FILE, "r") as f:
            return str(user_id) in [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error(f"Premium check failed: {e}")
        return False

async def log_activity(action: str, user_id: int, details: str = ""):
    log_msg = (
        f"#{action.replace(' ', '')}\n"
        f"👤 User: {user_id}\n"
        f"🕒 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📝 Details: {details}"
    )
    try:
        await app.send_message(config.LOG_CHANNEL, log_msg)
    except Exception as e:
        logger.error(f"Logging failed: {e}")

def generate_progress_bar(percent: float) -> str:
    filled = int(percent / 5)  # 20-step progress
    return f"┃ [{'■' * filled}{'▦' if percent % 5 > 0 else ''}{'□' * (20 - filled)}] {percent:.2f}%"

# === Command Handlers ===
@app.on_message(filters.command("start"))
async def start_handler(_, message):
    user_id = message.from_user.id
    welcome_msg = (
        "👋 Welcome to Reels Uploader Pro!\n\n"
        f"🆔 Your ID: <code>{user_id}</code>\n"
        f"🔑 Status: {'Premium User ✅' if is_premium_user(user_id) else 'Standard User'}"
    )
    await message.reply(welcome_msg, reply_markup=get_main_menu(user_id))

@app.on_message(filters.command("restart") & filters.user(config.ADMIN_ID))
async def restart_handler(_, message):
    restart_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    restart_msg = (
        "<b>Bot Restarted Successfully!</b>\n\n"
        f"📅 Date: <code>{datetime.now().strftime('%Y-%m-%d')}</code>\n"
        f"⏰ Time: <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
        f"🌐 Timezone: <code>UTC+5:30</code>\n"
        f"🛠️ Version: <code>v2.8.0 [Stable]</code>"
    )
    await message.reply(restart_msg)
    await log_activity("Bot Restart", message.from_user.id)
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command("addpremium") & filters.user(config.ADMIN_ID))
async def add_premium_handler(_, message):
    try:
        new_user_id = int(message.text.split()[1])
        with open(config.PREMIUM_USERS_FILE, "a") as f:
            f.write(f"{new_user_id}\n")
        await message.reply(f"✅ User <code>{new_user_id}</code> added as premium!")
        await log_activity("Add Premium", message.from_user.id, f"New premium user: {new_user_id}")
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

# === Upload Flow ===
@app.on_message(filters.video & filters.create(lambda _, __, m: is_premium_user(m.from_user.id)))
async def video_handler(client, message):
    user_id = message.from_user.id
    try:
        # Step 1: Download with progress
        progress_msg = await message.reply("⬇️ Starting download... 0%")
        
        def progress_callback(current, total):
            percent = current / total * 100
            progress_text = f"⬇️ Downloading...\n{generate_progress_bar(percent)}"
            asyncio.run_coroutine_threadsafe(
                progress_msg.edit_text(progress_text),
                client.loop
            )
        
        video_path = await message.download(progress=progress_callback)
        
        # Step 2: Check Instagram login
        if user_id not in insta_manager.user_sessions:
            await progress_msg.edit_text("🔑 Please login first with /login username password")
            return
        
        # Step 3: Upload to Instagram
        await progress_msg.edit_text("⏫ Starting Instagram upload...")
        insta_client = InstaClient()
        insta_client.login(
            insta_manager.user_sessions[user_id]["username"],
            insta_manager.user_sessions[user_id]["password"]
        )
        media = insta_client.clip_upload(video_path)
        
        # Step 4: Finalize
        success_msg = (
            "✅ Upload Successful!\n\n"
            f"🔗 View Reel: https://instagram.com/reel/{media.code}\n"
            f"🕒 Upload Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        await progress_msg.edit_text(success_msg)
        await log_activity("New Upload", user_id, f"Reel: {media.code}")
        
    except Exception as e:
        await message.reply(f"❌ Upload failed: {str(e)}")
        logger.error(f"Upload error: {e}", exc_info=True)
    finally:
        if 'video_path' in locals():
            os.remove(video_path)

if __name__ == "__main__":
    logger.info("Starting Reels Uploader Pro Bot...")
    app.run()

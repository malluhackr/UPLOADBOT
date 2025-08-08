import os
import sys
import asyncio
import threading
import logging
import subprocess
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import signal
from functools import wraps

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# MongoDB
from pymongo import MongoClient

# Pyrogram (Telegram Bot)
from pyrogram import Client, filters, enums
from pyrogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)

# Instagram Client
from instagrapi import Client as InstaClient
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    BadPassword,
    PleaseWaitFewMinutes,
    ClientError
)

# Logging to Telegram Channel
from log_handler import send_log_to_channel

# System Utilities
import psutil
import GPUtil
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Load env ===
API_ID = int(os.getenv("TELEGRAM_API_ID", "27356561"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "efa4696acce7444105b02d82d0b2e381")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002544142397"))
MONGO_URI = os.getenv("MONGO_DB", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6644681404"))

# Instagram Client Credentials (for the bot's own primary account, if any)
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
PROXY_SETTINGS = os.getenv("PROXY_SETTINGS", "")

# === Global Bot Settings ===
DEFAULT_GLOBAL_SETTINGS = {
    "onam_toggle": False,
    "max_concurrent_uploads": 15,
    "max_file_size_mb": 250,
    "payment_settings": {
        "google_play": "",
        "upi": "",
        "ust": "",
        "btc": "",
        "others": ""
    },
    "no_compression_admin": False
}

# Initialize MongoDB Client
try:
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo.NowTok
    mongo.admin.command('ismaster')
    logging.info("Connected to MongoDB successfully.")
except Exception as e:
    logging.critical(f"Failed to connect to MongoDB: {e}")
    sys.exit(1)

# Configure logging to console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger("InstaUploadBot")

# --- Global State Management ---
global_settings = db.settings.find_one({"_id": "global_settings"}) or DEFAULT_GLOBAL_SETTINGS
db.settings.update_one({"_id": "global_settings"}, {"$set": global_settings}, upsert=True)
logger.info(f"Global settings loaded: {global_settings}")

MAX_CONCURRENT_UPLOADS = global_settings.get("max_concurrent_uploads", DEFAULT_GLOBAL_SETTINGS["max_concurrent_uploads"])
upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
user_upload_locks = {}
# New state management for tasks and timeouts
user_tasks = {}
TIMEOUT_SECONDS = 60

# FFMpeg timeout constant
FFMPEG_TIMEOUT_SECONDS = 600

# Max file size
MAX_FILE_SIZE_BYTES = global_settings.get("max_file_size_mb", DEFAULT_GLOBAL_SETTINGS["max_file_size_mb"]) * 1024 * 1024

# Pyrogram Client
app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
insta_client = InstaClient()
insta_client.delay_range = [1, 3]

# Create collections if not exists
required_collections = ["users", "settings", "sessions", "uploads", "scheduled_posts"]
for collection_name in required_collections:
    if collection_name not in db.list_collection_names():
        db.create_collection(collection_name)
        logger.info(f"Collection '{collection_name}' created.")

# State management for sequential user input
user_states = {}
upload_tasks = {}

# Scheduled jobs
scheduler = AsyncIOScheduler(timezone='UTC')

# --- PREMIUM DEFINITIONS ---
PREMIUM_PLANS = {
    "3_hour_trial": {"duration": timedelta(hours=3), "price": "Free / Free"},
    "3_days": {"duration": timedelta(days=3), "price": "₹10 / $0.40"},
    "7_days": {"duration": timedelta(days=7), "price": "₹25 / $0.70"},
    "15_days": {"duration": timedelta(days=15), "price": "₹35 / $0.90"},
    "1_month": {"duration": timedelta(days=30), "price": "₹60 / $2.50"},
    "3_months": {"duration": timedelta(days=90), "price": "₹150 / $4.50"},
    "1_year": {"duration": timedelta(days=365), "price": "Negotiable / Negotiable"},
    "lifetime": {"duration": None, "price": "Negotiable / Negotiable"}
}

PREMIUM_PLATFORMS = ["instagram"]

# Keyboards
def get_main_keyboard(user_id):
    buttons = [
        [KeyboardButton("⚙️ ꜱᴇᴛᴛɪɴɢꜱ"), KeyboardButton("📊 ꜱᴛᴀᴛꜱ")]
    ]
    is_instagram_premium = is_premium_for_platform(user_id, "instagram")

    upload_buttons_row = []
    if is_instagram_premium:
        upload_buttons_row.extend([KeyboardButton("📸 ɪɴꜱᴛᴀ ᴩʜᴏᴛᴏ"), KeyboardButton("📤 ɪɴꜱᴛᴀ ʀᴇᴇʟ")])
    

    if upload_buttons_row:
        buttons.insert(0, upload_buttons_row)

    buttons.append([KeyboardButton("⭐ ᴩʀᴇᴍɪᴜᴍ"), KeyboardButton("/premiumdetails")])
    if is_admin(user_id):
        buttons.append([KeyboardButton("🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ"), KeyboardButton("🔄 ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)


user_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 ᴜᴩʟᴏᴀᴅ ᴛyᴩᴇ", callback_data="upload_type")],
    [InlineKeyboardButton("📝 ᴄᴀᴩᴛɪᴏɴ", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ ʜᴀꜱʜᴛᴀɢꜱ", callback_data="set_hashtags")],
    [InlineKeyboardButton("📐 ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ (ᴠɪᴅᴇᴏ)", callback_data="set_aspect_ratio")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 ᴜꜱᴇʀꜱ ʟɪꜱᴛ", callback_data="users_list")],
    [InlineKeyboardButton("➕ ᴍᴀɴᴀɢᴇ ᴩʀᴇᴍɪᴜᴍ", callback_data="manage_premium")],
    [InlineKeyboardButton("📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ", callback_data="broadcast_message")],
    [InlineKeyboardButton("⚙️ ɢʟᴏʙᴀʟ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="global_settings_panel")],
    [InlineKeyboardButton("📊 ꜱᴛᴀᴛꜱ ᴩᴀɴᴇʟ", callback_data="admin_stats_panel")],
    [InlineKeyboardButton("💰 ᴩᴀyᴍᴇɴᴛ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="payment_settings_panel")],
    [InlineKeyboardButton("➕ ᴀᴅᴅ ғᴇᴀᴛᴜʀᴇ", callback_data="add_feature_request")], # New button
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴍᴇɴᴜ", callback_data="back_to_main_menu")]
])

admin_global_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("ᴏɴᴀᴍ ᴛᴏɢɢʟᴇ", callback_data="toggle_onam")],
    [InlineKeyboardButton("ᴍᴀx ᴜᴩʟᴏᴀᴅ ᴜꜱᴇʀꜱ", callback_data="set_max_uploads")],
    [InlineKeyboardButton("ʀᴇꜱᴇᴛ ꜱᴛᴀᴛꜱ", callback_data="reset_stats")],
    [InlineKeyboardButton("ꜱʜᴏᴡ ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ", callback_data="show_system_stats")],
    [InlineKeyboardButton("🌐 ᴩʀᴏxʏ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="set_proxy_url")],
    [InlineKeyboardButton("🗜️ ᴛᴏɢɢʟᴇ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ", callback_data="toggle_compression_admin")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴀᴅᴍɪɴ", callback_data="admin_panel")]
])

payment_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("ɢᴏᴏɢʟᴇ ᴩʟᴀy ǫʀ ᴄᴏᴅᴇ", callback_data="set_payment_google_play_qr")],
    [InlineKeyboardButton("ᴜᴩɪ", callback_data="set_payment_upi")],
    [InlineKeyboardButton("ᴜꜱᴛ", callback_data="set_payment_ust")],
    [InlineKeyboardButton("ʙᴛᴄ", callback_data="set_payment_btc")],
    [InlineKeyboardButton("ᴏᴛʜᴇʀꜱ", callback_data="set_payment_others")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴀᴅᴍɪɴ", callback_data="admin_panel")]
])

upload_type_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 ʀᴇᴇʟ", callback_data="set_type_reel")],
    [InlineKeyboardButton("📷 ᴩᴏꜱᴛ", callback_data="set_type_post")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_settings")]
])

aspect_ratio_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("ᴏʀɪɢɪɴᴀʟ ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ", callback_data="set_ar_original")],
    [InlineKeyboardButton("9:16 (ᴄʀᴏᴩ/ғɪᴛ)", callback_data="set_ar_9_16")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_settings")]
])

def get_platform_selection_markup(user_id, current_selection=None):
    if current_selection is None:
        current_selection = {}
    buttons = []
    for platform in PREMIUM_PLATFORMS:
        emoji = "✅" if current_selection.get(platform) else "⬜"
        buttons.append([InlineKeyboardButton(f"{emoji} {platform.capitalize()}", callback_data=f"select_platform_{platform}")])
    buttons.append([InlineKeyboardButton("➡️ ᴄᴏɴᴛɪɴᴜᴇ ᴛᴏ ᴩʟᴀɴꜱ", callback_data="confirm_platform_selection")])
    buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴀᴅᴍɪɴ", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_premium_plan_markup(user_id):
    buttons = []
    for key, value in PREMIUM_PLANS.items():
        buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()}", callback_data=f"show_plan_details_{key}")])
    buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(buttons)

def get_premium_details_markup(plan_key, price_multiplier):
    plan_details = PREMIUM_PLANS[plan_key]
    buttons = []
    
    price_string = plan_details['price']
    if '₹' in price_string:
        try:
            base_price = float(price_string.replace('₹', '').split('/')[0].strip())
            calculated_price = base_price * price_multiplier
            price_string = f"₹{int(calculated_price)}"
        except ValueError:
            pass
            
    buttons.append([InlineKeyboardButton(f"💰 ʙᴜy ɴᴏᴡ ({price_string})", callback_data=f"buy_now")])
    buttons.append([InlineKeyboardButton("➡️ ᴄʜᴇᴄᴋ ᴩᴀyᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ", callback_data="show_payment_methods")])
    buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴩʟᴀɴꜱ", callback_data="back_to_premium_plans")])
    return InlineKeyboardMarkup(buttons)


def get_payment_methods_markup():
    payment_buttons = []
    settings = global_settings.get("payment_settings", {})
    if settings.get("google_play_qr_file_id"):
        payment_buttons.append([InlineKeyboardButton("ɢᴏᴏɢʟᴇ ᴩʟᴀy ǫʀ ᴄᴏᴅᴇ", callback_data="show_payment_qr_google_play")])
    if settings.get("upi"):
        payment_buttons.append([InlineKeyboardButton("ᴜᴩɪ", callback_data="show_payment_details_upi")])
    if settings.get("ust"):
        payment_buttons.append([InlineKeyboardButton("ᴜꜱᴛ", callback_data="show_payment_details_ust")])
    if settings.get("btc"):
        payment_buttons.append([InlineKeyboardButton("ʙᴛᴄ", callback_data="show_payment_details_btc")])
    if settings.get("others"):
        payment_buttons.append([InlineKeyboardButton("ᴏᴛʜᴇʀꜱ", callback_data="show_payment_details_others")])

    payment_buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴩʀᴇᴍɪᴜᴍ ᴩʟᴀɴꜱ", callback_data="back_to_premium_plans")])
    return InlineKeyboardMarkup(payment_buttons)


def get_upload_buttons(user_id):
    buttons = [
        [InlineKeyboardButton("➡️ ᴜꜱᴇ ᴅᴇғᴀᴜʟᴛ ᴄᴀᴩᴛɪᴏɴ", callback_data="skip_caption")],
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ ᴜᴩʟᴏᴀᴅ", callback_data="cancel_upload")],
    ]
    return InlineKeyboardMarkup(buttons)

def get_progress_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_upload")]
    ])

def get_caption_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ꜱᴋɪᴩ (ᴜꜱᴇ ᴅᴇғᴀᴜʟᴛ)", callback_data="skip_caption")],
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_upload")]
    ])

# === Helper Functions ===

def is_admin(user_id):
    return user_id == ADMIN_ID

def _get_user_data(user_id):
    return db.users.find_one({"_id": user_id})

def _save_user_data(user_id, data_to_update):
    db.users.update_one(
        {"_id": user_id},
        {"$set": data_to_update},
        upsert=True
    )

def _update_global_setting(key, value):
    db.settings.update_one({"_id": "global_settings"}, {"$set": {key: value}}, upsert=True)
    global_settings[key] = value

def is_premium_for_platform(user_id, platform):
    user = _get_user_data(user_id)
    if not user:
        return False
    if user_id == ADMIN_ID:
        return True

    platform_premium = user.get("premium", {}).get(platform, {})
    premium_type = platform_premium.get("type")
    premium_until = platform_premium.get("until")

    if premium_type == "lifetime":
        return True

    if premium_until and isinstance(premium_until, datetime) and premium_until > datetime.utcnow():
        return True

    if premium_type and premium_until and premium_until <= datetime.utcnow():
        db.users.update_one(
            {"_id": user_id},
            {"$unset": {f"premium.{platform}.type": "", f"premium.{platform}.until": ""}}
        )
        logger.info(f"Premium for {platform} expired for user {user_id}. Status updated in DB.")

    return False

def get_current_datetime():
    now = datetime.utcnow()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "UTC"
    }

async def save_instagram_session(user_id, session_data):
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"instagram_session": session_data}},
        upsert=True
    )
    logger.info(f"Instagram session saved for user {user_id}")

async def load_instagram_session(user_id):
    session = db.sessions.find_one({"user_id": user_id})
    return session.get("instagram_session") if session else None


async def save_user_settings(user_id, settings):
    db.settings.update_one(
        {"_id": user_id},
        {"$set": settings},
        upsert=True
    )
    logger.info(f"User settings saved for user {user_id}")

async def get_user_settings(user_id):
    settings = db.settings.find_one({"_id": user_id}) or {}
    if "aspect_ratio" not in settings:
        settings["aspect_ratio"] = "original"
    if "no_compression" not in settings:
        settings["no_compression"] = False
    return settings

async def safe_edit_message(message, text, reply_markup=None, parse_mode=enums.ParseMode.MARKDOWN):
    """
    Safely edits a message, avoiding the MESSAGE_NOT_MODIFIED error.
    """
    try:
        current_text = message.text if message.text else ""
        if current_text.strip() != text.strip():
            await message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
    except Exception as e:
        logger.warning(f"Couldn't edit message: {e}")

async def restart_bot(msg):
    dt = get_current_datetime()
    restart_msg_log = (
        "🔄 ʙᴏᴛ ʀᴇꜱᴛᴀʀᴛ ɪɴɪᴛɪᴀᴛᴇᴅ!\n\n"
        f"📅 ᴅᴀᴛᴇ: {dt['date']}\n"
        f"⏰ ᴛɪᴍᴇ: {dt['time']}\n"
        f"🌐 ᴛɪᴍᴇᴢᴏɴᴇ: {dt['timezone']}\n"
        f"👤 ʙy: {msg.from_user.mention} (ɪᴅ: {msg.from_user.id})"
    )
    logger.info(f"User {msg.from_user.id} attempting restart command.")
    await send_log_to_channel(app, LOG_CHANNEL, restart_msg_log)
    await msg.reply("✅ ʙᴏᴛ ɪꜱ ʀᴇꜱᴛᴀʀᴛɪɴɢ...")
    await asyncio.sleep(2)
    try:
        logger.info("Executing os.execv to restart process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execute restart via os.execv: {e}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ ʀᴇꜱᴛᴀʀᴛ ғᴀɪʟᴇᴅ ғᴏʀ {msg.from_user.id}: {str(e)}")
        await msg.reply(f"❌ ғᴀɪʟᴇᴅ ᴛᴏ ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ: {str(e)}")

def load_instagram_client_session(user_id=None):
    proxy_url = global_settings.get("proxy_url")
    if proxy_url:
        insta_client.set_proxy(proxy_url)
        logger.info(f"Global proxy set to: {proxy_url}")
    elif INSTAGRAM_PROXY:
        insta_client.set_proxy(INSTAGRAM_PROXY)
        logger.info(f"Default Instagram proxy set to: {INSTAGRAM_PROXY}")
    else:
        logger.info("No Instagram proxy configured.")
    return True

async def progress_callback(current, total, ud_type, msg, start_time):
    percentage = current * 100 / total
    speed = current / (time.time() - start_time)
    eta = (total - current) / speed
    
    progress_bar = f"[{'█' * int(percentage / 5)}{' ' * (20 - int(percentage / 5))}]"
    
    progress_text = (
        f"{ud_type} ᴩʀᴏɢʀᴇꜱꜱ: `{progress_bar}`\n"
        f"📊 ᴩᴇʀᴄᴇɴᴛᴀɢᴇ: `{percentage:.2f}%`\n"
        f"✅ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ: `{current / (1024 * 1024):.2f}` ᴍʙ\n"
        f"📦 ᴛᴏᴛᴀʟ ꜱɪᴢᴇ: `{total / (1024 * 1024):.2f}` ᴍʙ\n"
        f"🚀 ꜱᴩᴇᴇᴅ: `{speed / (1024 * 1024):.2f}` ᴍʙ/ꜱ\n"
        f"⏳ ᴇᴛᴀ: `{timedelta(seconds=eta)}`"
    )
    
    # We only edit the message at 5% intervals to avoid rate limiting
    if int(percentage) % 5 == 0:
        try:
            await safe_edit_message(msg, progress_text, reply_markup=get_progress_markup(), parse_mode=enums.ParseMode.MARKDOWN)
        except Exception:
            pass

def cleanup_temp_files(files_to_delete):
    for file_path in files_to_delete:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"ᴅᴇʟᴇᴛᴇᴅ ʟᴏᴄᴀʟ ғɪʟᴇ: {file_path}")
            except Exception as e:
                logger.error(f"ᴇʀʀᴏʀ ᴅᴇʟᴇᴛɪɴɢ ғɪʟᴇ {file_path}: {e}")

def with_user_lock(func):
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        if user_id not in user_upload_locks:
            user_upload_locks[user_id] = asyncio.Lock()

        if user_upload_locks[user_id].locked():
            return await message.reply("⚠️ ᴀɴᴏᴛʜᴇʀ ᴏᴩᴇʀᴀᴛɪᴏɴ ɪꜱ ᴀʟʀᴇᴀᴅy ɪɴ ᴩʀᴏɢʀᴇꜱꜱ. ᴩʟᴇᴀꜱᴇ ᴡᴀɪᴛ ᴜɴᴛɪʟ ɪᴛ'ꜱ ғɪɴɪꜱʜᴇᴅ ᴏʀ ᴜꜱᴇ ᴛʜᴇ `❌ ᴄᴀɴᴄᴇʟ` ʙᴜᴛᴛᴏɴ.")

        async with user_upload_locks[user_id]:
            return await func(client, message, *args, **kwargs)
    return wrapper

# --- Message Handlers ---

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    if is_admin(user_id):
        welcome_msg = "🤖 **ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴜᴩʟᴏᴀᴅ ʙᴏᴛ!**\n\n"
        welcome_msg += "🛠️ yᴏᴜ ʜᴀᴠᴇ **ᴀᴅᴍɪɴ ᴩʀɪᴠɪʟᴇɢᴇꜱ**."
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = _get_user_data(user_id)
    is_new_user = not user
    if is_new_user:
        _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 ɴᴇᴡ ᴜꜱᴇʀ ꜱᴛᴀʀᴛᴇᴅ ʙᴏᴛ: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")
        
        welcome_msg = (
            f"👋 **ʜɪ {user_first_name}!**\n\n"
            "ᴛʜɪꜱ ʙᴏᴛ ʟᴇᴛꜱ yᴏᴜ ᴜᴩʟᴏᴀᴅ ᴀɴy ꜱɪᴢᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ & ᴩᴏꜱᴛꜱ ᴅɪʀᴇᴄᴛʟy ғʀᴏᴍ ᴛᴇʟᴇɢʀᴀᴍ.\n\n"
            "ᴛᴏ ɢᴇᴛ ᴀ ᴛᴀꜱᴛᴇ ᴏғ ᴛʜᴇ ᴩʀᴇᴍɪᴜᴍ ғᴇᴀᴛᴜʀᴇꜱ, yᴏᴜ ᴄᴀɴ ᴀᴄᴛɪᴠᴀᴛᴇ ᴀ **ғʀᴇᴇ 3-ʜᴏᴜʀ ᴛʀɪᴀʟ** ғᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ ʀɪɢʜᴛ ɴᴏᴡ!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ᴀᴄᴛɪᴠᴀᴛᴇ ғʀᴇᴇ 3-ʜᴏᴜʀ", callback_data="activate_trial")],
            [InlineKeyboardButton("➡️ ᴩʀᴇᴍɪᴜᴍ", callback_data="buypypremium")]
        ])
        await msg.reply(welcome_msg, reply_markup=trial_markup, parse_mode=enums.ParseMode.MARKDOWN)
        return
    else:
        _save_user_data(user_id, {"last_active": datetime.utcnow()})

    onam_toggle = global_settings.get("onam_toggle", False)
    if onam_toggle:
        onam_text = (
            f"🎉 **ʜᴀᴩᴩy ᴏɴᴀᴍ!** 🎉\n\n"
            f"ᴡɪꜱʜɪɴɢ yᴏᴜ ᴀ ꜱᴇᴀꜱᴏɴ ᴏғ ᴩʀᴏꜱᴩᴇʀɪᴛy ᴀɴᴅ ʜᴀᴩᴩɪɴᴇꜱꜱ. ᴇɴᴊᴏy ᴛʜᴇ ғᴇꜱᴛɪᴠɪᴛɪᴇꜱ ᴡɪᴛʜ ᴏᴜʀ ᴇxᴄʟᴜꜱɪᴠᴇ **ᴏɴᴀᴍ ʀᴇᴇʟ ᴜᴩʟᴏᴀᴅꜱ** ғᴇᴀᴛᴜʀᴇ!\n\n"
            f"ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ꜱᴛᴀʀᴛ ᴜᴩʟᴏᴀᴅɪɴɢ yᴏᴜʀ ғᴇꜱᴛɪᴠᴀʟ ᴄᴏɴᴛᴇɴᴛ!"
        )
        await msg.reply(onam_text, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user_premium = _get_user_data(user_id).get("premium", {})
    instagram_premium_data = user_premium.get("instagram", {})

    welcome_msg = f"🚀 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛᴇʟᴇɢʀᴀᴍ ➜ ɪɴꜱᴛᴀɢʀᴀᴍ ᴅɪʀᴇᴄᴛ ᴜᴩʟᴏᴀᴅᴇʀ\n\n"
    premium_details_text = ""
    is_admin_user = is_admin(user_id)
    if is_admin_user:
        premium_details_text += "🛠️ yᴏᴜ ʜᴀᴠᴇ **ᴀᴅᴍɪɴ ᴩʀɪᴠɪʟᴇɢᴇꜱ**.\n\n"

    ig_premium_until = instagram_premium_data.get("until")

    if is_premium_for_platform(user_id, "instagram"):
        if ig_premium_until:
            remaining_time = ig_premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ ᴇxᴩɪʀᴇꜱ ɪɴ: `{days} ᴅᴀyꜱ, {hours} ʜᴏᴜʀꜱ`.\n"
    
    if not is_admin_user and not premium_details_text.strip():
        premium_details_text = (
            "🔥 **ᴋᴇy ғᴇᴀᴛᴜʀᴇꜱ:**\n"
            "✅ ᴅɪʀᴇᴄᴛ ʟᴏɢɪɴ (ɴᴏ ᴛᴏᴋᴇɴꜱ ɴᴇᴇᴅᴇᴅ)\n"
            "✅ ᴜʟᴛʀᴀ-ғᴀꜱᴛ ᴜᴩʟᴏᴀᴅɪɴɢ\n"
            "✅ ʜɪɢʜ ǫᴜᴀʟɪᴛy / ғᴀꜱᴛ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ\n"
            "✅ ɴᴏ ғɪʟᴇ ꜱɪᴢᴇ ʟɪᴍɪᴛ\n"
            "✅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴜᴩʟᴏᴀᴅꜱ\n"
            "✅ ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴜᴩᴩᴏʀᴛ\n"
            "✅ ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ ᴀғᴛᴇʀ ᴜᴩʟᴏᴀᴅ (ᴏᴩᴛɪᴏɴᴀʟ)\n\n"
            "👤 ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ ᴛᴏᴍ → [ᴄʟɪᴄᴋ ʜᴇʀᴇ](t.me/CjjTom) ᴛᴏ ɢᴇᴛ ᴩʀᴇᴍɪᴜᴍ ɴᴏᴡ\n"
            "🔐 yᴏᴜʀ ᴅᴀᴛᴀ ɪꜱ ғᴜʟʟy ✅ ᴇɴᴅ ᴛᴏ ᴇɴᴅ ᴇɴᴄʀyᴩᴛᴇᴅ\n\n"
            f"🆔 yᴏᴜʀ ɪᴅ: `{user_id}`"
        )
    
    welcome_msg += premium_details_text
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ.")
    restarting_msg = await msg.reply("♻️ ʀᴇꜱᴛᴀʀᴛɪɴɢ ʙᴏᴛ...")
    await asyncio.sleep(1)
    await restart_bot(msg)

# Redesigned login flow to be conversational and handle state
@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʟᴇᴀꜱᴇ ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ ᴡɪᴛʜ /buypypremium.")
    
    # Check if already logged in
    user_data = _get_user_data(user_id)
    session = await load_instagram_session(user_id)
    if session and user_data and user_data.get("instagram_username"):
        last_login_date = user_data.get("last_login_timestamp")
        login_info = ""
        if last_login_date:
            days_ago = (datetime.utcnow() - last_login_date).days
            login_info = f" (ʟᴏɢɢᴇᴅ ɪɴ {days_ago} ᴅᴀyꜱ ᴀɢᴏ)" if days_ago > 0 else " (ʟᴏɢɢᴇᴅ ɪɴ ᴛᴏᴅᴀy)"
        return await msg.reply(f"🔐 yᴏᴜ ᴀʀᴇ ᴀʟʀᴇᴀᴅy ʟᴏɢɢᴇᴅ ɪɴ ᴀꜱ @{user_data['instagram_username']}{login_info}")

    user_states[user_id] = {"action": "waiting_for_instagram_username"}
    await msg.reply("👤 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ **ᴜꜱᴇʀɴᴀᴍᴇ**.")

@app.on_message(filters.command("buypypremium"))
@app.on_message(filters.regex("⭐ ᴩʀᴇᴍɪᴜᴍ"))
async def show_premium_options(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    premium_plans_text = (
        "⭐ **ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ᴩʀᴇᴍɪᴜᴍ!** ⭐\n\n"
        "ᴜɴʟᴏᴄᴋ ғᴜʟʟ ғᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜᴩʟᴏᴀᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ ғᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ!\n\n"
        "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩʟᴀɴꜱ:**"
    )
    await msg.reply(premium_plans_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user = _get_user_data(user_id)
    if not user:
        return await msg.reply("yᴏᴜ ᴀʀᴇ ɴᴏᴛ ʀᴇɢɪꜱᴛᴇʀᴇᴅ ᴡɪᴛʜ ᴛʜᴇ ʙᴏᴛ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ /start.")

    if is_admin(user_id):
        return await msg.reply("👑 yᴏᴜ ᴀʀᴇ ᴛʜᴇ **ᴀᴅᴍɪɴ**. yᴏᴜ ʜᴀᴠᴇ ᴩᴇʀᴍᴀɴᴇɴᴛ ғᴜʟʟ ᴀᴄᴄᴇꜱꜱ ᴛᴏ ᴀʟʟ ғᴇᴀᴛᴜʀᴇꜱ!", parse_mode=enums.ParseMode.MARKDOWN)

    status_text = "⭐ **yᴏᴜʀ ᴩʀᴇᴍɪᴜᴍ ꜱᴛᴀᴛᴜꜱ:**\n\n"
    has_premium_any = False

    for platform in PREMIUM_PLATFORMS:
        platform_premium = user.get("premium", {}).get(platform, {})
        premium_type = platform_premium.get("type")
        premium_until = platform_premium.get("until")

        status_text += f"**{platform.capitalize()} ᴩʀᴇᴍɪᴜᴍ:** "
        if premium_type == "lifetime":
            status_text += "🎉 **ʟɪғᴇᴛɪᴍᴇ!**\n"
            has_premium_any = True
        elif premium_until and premium_until > datetime.utcnow():
            remaining_time = premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            minutes = (remaining_time.seconds % 3600) // 60
            status_text += (
                f"`{premium_type.replace('_', ' ').title()}` ᴇxᴩɪʀᴇꜱ ᴏɴ: "
                f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`\n"
                f"ᴛɪᴍᴇ ʀᴇᴍᴀɪɴɪɴɢ: `{days} ᴅᴀyꜱ, {hours} ʜᴏᴜʀꜱ, {minutes} ᴍɪɴᴜᴛᴇꜱ`\n"
            )
            has_premium_any = True
        else:
            status_text += "😔 **ɴᴏᴛ ᴀᴄᴛɪᴠᴇ.**\n"
        status_text += "\n"

    if not has_premium_any:
        status_text = (
            "😔 **yᴏᴜ ᴄᴜʀʀᴇɴᴛʟy ʜᴀᴠᴇ ɴᴏ ᴀᴄᴛɪᴠᴇ ᴩʀᴇᴍɪᴜᴍ.**\n\n"
            "ᴛᴏ ᴜɴʟᴏᴄᴋ ᴀʟʟ ғᴇᴀᴛᴜʀᴇꜱ, ᴩʟᴇᴀꜱᴇ ᴄᴏɴᴛᴀᴄᴛ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ᴛᴏ ʙᴜy ᴀ ᴩʀᴇᴍɪᴜᴍ ᴩʟᴀɴ."
        )

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("reset_profile"))
@with_user_lock
async def reset_profile_cmd(_, msg):
    user_id = msg.from_user.id
    await msg.reply("⚠️ **ᴡᴀʀɴɪɴɢ!** ᴛʜɪꜱ ᴡɪʟʟ ᴄʟᴇᴀʀ ᴀʟʟ yᴏᴜʀ ꜱᴀᴠᴇᴅ ꜱᴇꜱꜱɪᴏɴꜱ ᴀɴᴅ ꜱᴇᴛᴛɪɴɢꜱ. ᴀʀᴇ yᴏᴜ ꜱᴜʀᴇ yᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴩʀᴏᴄᴇᴇᴅ?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ yᴇꜱ, ʀᴇꜱᴇᴛ ᴍy ᴩʀᴏғɪʟᴇ", callback_data="confirm_reset_profile")],
            [InlineKeyboardButton("❌ ɴᴏ, ᴄᴀɴᴄᴇʟ", callback_data="back_to_main_menu")]
        ]),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_reset_profile$"))
@with_user_lock
async def confirm_reset_profile_cb(_, query):
    user_id = query.from_user.id
    db.users.delete_one({"_id": user_id})
    db.settings.delete_one({"_id": user_id})
    db.sessions.delete_one({"user_id": user_id})
    
    if user_id in user_states:
        del user_states[user_id]
    
    await query.answer("✅ yᴏᴜʀ ᴩʀᴏғɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ʀᴇꜱᴇᴛ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ /start ᴛᴏ ʙᴇɢɪɴ ᴀɢᴀɪɴ.", show_alert=True)
    await safe_edit_message(query.message, "✅ yᴏᴜʀ ᴩʀᴏғɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ʀᴇꜱᴇᴛ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ /start ᴛᴏ ʙᴇɢɪɴ ᴀɢᴀɪɴ.")

@app.on_message(filters.regex("⚙️ ꜱᴇᴛᴛɪɴɢꜱ"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ ᴛᴏ ᴀᴄᴄᴇꜱꜱ ꜱᴇᴛᴛɪɴɢꜱ.")
    
    current_settings = await get_user_settings(user_id)
    compression_status = "ᴏɴ (ᴏʀɪɢɪɴᴀʟ ǫᴜᴀʟɪᴛy)" if current_settings.get("no_compression") else "ᴏғғ (ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)"
    
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = "ɴᴏɴᴇ"
    if proxy_url:
        proxy_status_text = f"`{proxy_url}`"

    settings_text = "⚙️ ꜱᴇᴛᴛɪɴɢꜱ ᴩᴀɴᴇʟ\n\n" \
                    f"🗜️ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟy: **{compression_status}**\n" \
                    f"🌐 ʙᴏᴛ ᴩʀᴏxʏ ꜱᴛᴀᴛᴜꜱ: {proxy_status_text}\n\n" \
                    "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ yᴏᴜʀ ᴩʀᴇғᴇʀᴇɴᴄᴇꜱ."

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ ᴜꜱᴇʀ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="user_settings_personal")]
        ])
    else:
        markup = user_settings_markup

    await msg.reply(settings_text, reply_markup=markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("📤 ɪɴꜱᴛᴀ ʀᴇᴇʟ"))
@with_user_lock
async def initiate_instagram_reel_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ yᴏᴜʀ ᴀᴄᴄᴇꜱꜱ ʜᴀꜱ ʙᴇᴇɴ ᴅᴇɴɪᴇᴅ. ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ ᴛᴏ ᴜɴʟᴏᴄᴋ ʀᴇᴇʟꜱ ᴜᴩʟᴏᴀᴅ. /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("✅ ꜱᴇɴᴅ ᴠɪᴅᴇᴏ ғɪʟᴇ - ʀᴇᴇʟ ʀᴇᴀᴅy!!")
    user_states[user_id] = {"action": "waiting_for_instagram_reel_video", "platform": "instagram", "upload_type": "reel"}

@app.on_message(filters.regex("📸 ɪɴꜱᴛᴀ ᴩʜᴏᴛᴏ"))
@with_user_lock
async def initiate_instagram_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("🚫 ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴜᴩʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʜᴏᴛᴏꜱ ᴩʟᴇᴀꜱᴇ ᴜᴩɢʀᴀᴅᴇ ᴩʀᴇᴍɪᴜᴍ /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ ꜱᴇɴᴅ ᴩʜᴏᴛᴏ ғɪʟᴇ - ʀᴇᴀᴅy ғᴏʀ ɪɢ!.")
    user_states[user_id] = {"action": "waiting_for_instagram_photo_image", "platform": "instagram", "upload_type": "post"}

@app.on_message(filters.regex("📊 ꜱᴛᴀᴛꜱ"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLANS):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. yᴏᴜ ɴᴇᴇᴅ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ғᴏʀ ᴀᴛ ʟᴇᴀꜱᴛ ᴏɴᴇ ᴩʟᴀᴛғᴏʀᴍ ᴛᴏ ᴠɪᴇᴡ ꜱᴛᴀᴛꜱ.")

    total_users = db.users.count_documents({})
    premium_counts = {platform: 0 for platform in PREMIUM_PLATFORMS}
    total_premium_users = 0
    for user in db.users.find({}):
        is_any_premium = False
        for platform in PREMIUM_PLATFORMS:
            if is_premium_for_platform(user["_id"], platform):
                premium_counts[platform] += 1
                is_any_premium = True
        if is_any_premium:
            total_premium_users += 1

    total_uploads = db.uploads.count_documents({})
    total_instagram_reel_uploads = db.uploads.count_documents({"platform": "instagram", "upload_type": "reel"})
    total_instagram_post_uploads = db.uploads.count_documents({"platform": "instagram", "upload_type": "post"})
    
    stats_text = (
        "📊 **ʙᴏᴛ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ:**\n\n"
        f"**ᴜꜱᴇʀꜱ**\n"
        f"👥 ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ: `{total_users}`\n"
        f"👑 ᴀᴅᴍɪɴ ᴜꜱᴇʀꜱ: `{db.users.count_documents({'_id': ADMIN_ID})}`\n"
        f"⭐ ᴩʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ: `{total_premium_users}` (`{total_premium_users / total_users * 100:.2f}%`)\n"
        f"     - ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ: `{premium_counts['instagram']}` (`{premium_counts['instagram'] / total_users * 100:.2f}%`)\n"
    )

    stats_text += (
        f"\n**ᴜᴩʟᴏᴀᴅꜱ**\n"
        f"📈 ᴛᴏᴛᴀʟ ᴜᴩʟᴏᴀᴅꜱ: `{total_uploads}`\n"
        f"🎬 ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ: `{total_instagram_reel_uploads}`\n"
        f"📸 ɪɴꜱᴛᴀɢʀᴀᴍ ᴩᴏꜱᴛꜱ: `{total_instagram_post_uploads}`\n"
    )
    await msg.reply(stats_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_cmd(_, msg):
    if len(msg.text.split(maxsplit=1)) < 2:
        return await msg.reply("ᴜꜱᴀɢᴇ: `/broadcast <your message>`", parse_mode=enums.ParseMode.MARKDOWN)
    broadcast_message = msg.text.split(maxsplit=1)[1]
    users = db.users.find({})
    sent_count = 0
    failed_count = 0
    status_msg = await msg.reply("📢 ꜱᴛᴀʀᴛɪɴɢ ʙʀᴏᴀᴅᴄᴀꜱᴛ...")
    for user in users:
        try:
            if user["_id"] == ADMIN_ID:
                continue
            await app.send_message(user["_id"], broadcast_message, parse_mode=enums.ParseMode.MARKDOWN)
            sent_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to send broadcast to user {user['_id']}: {e}")
    await status_msg.edit_text(f"✅ ʙʀᴏᴀᴅᴄᴀꜱᴛ ғɪɴɪꜱʜᴇᴅ!\nꜱᴇɴᴛ ᴛᴏ `{sent_count}` ᴜꜱᴇʀꜱ, ғᴀɪʟᴇᴅ ғᴏʀ `{failed_count}` ᴜꜱᴇʀꜱ.")
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ ɪɴɪᴛɪᴀᴛᴇᴅ ʙy ᴀᴅᴍɪɴ `{msg.from_user.id}`\n"
        f"ꜱᴇɴᴛ: `{sent_count}`, ғᴀɪʟᴇᴅ: `{failed_count}`"
    )

# Updated handle_text_input to manage the login flow and timeouts
@app.on_message(filters.text & filters.private & ~filters.command(""))
@with_user_lock
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not state_data:
        return

    action = state_data.get("action")
    
    if action == "waiting_for_instagram_username":
        user_states[user_id]["username"] = msg.text
        user_states[user_id]["action"] = "waiting_for_instagram_password"
        return await msg.reply("🔑 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ **ᴩᴀꜱꜱᴡᴏʀᴅ**.", reply_markup=ReplyKeyboardRemove())
    
    if action == "waiting_for_instagram_password":
        username = user_states[user_id]["username"]
        password = msg.text
        
        if user_id in user_states:
            del user_states[user_id]
        
        login_msg = await msg.reply("🔐 ᴀᴛᴛᴇᴍᴩᴛɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ...")
        
        async def login_task():
            try:
                user_insta_client = InstaClient()
                user_insta_client.delay_range = [1, 3]
                
                proxy_url = global_settings.get("proxy_url")
                if proxy_url:
                    user_insta_client.set_proxy(proxy_url)
                elif INSTAGRAM_PROXY:
                    user_insta_client.set_proxy(INSTAGRAM_PROXY)
                
                await asyncio.to_thread(user_insta_client.login, username, password)

                session_data = user_insta_client.get_settings()
                await save_instagram_session(user_id, session_data)
                _save_user_data(user_id, {"instagram_username": username, "last_login_timestamp": datetime.utcnow()})
                
                await safe_edit_message(login_msg, "✅ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ꜱᴜᴄᴄᴇꜱꜱғᴜʟ!")
                await send_log_to_channel(app, LOG_CHANNEL,
                    f"📝 ɴᴇᴡ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ\nᴜꜱᴇʀ: `{user_id}`\n"
                    f"ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
                    f"ɪɴꜱᴛᴀɢʀᴀᴍ: `{username}`"
                )
                logger.info(f"Instagram login successful for user {user_id} ({username}).")
            except ChallengeRequired:
                await safe_edit_message(login_msg, "🔐 ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇǫᴜɪʀᴇꜱ ᴄʜᴀʟʟᴇɴɢᴇ ᴠᴇʀɪғɪᴄᴀᴛɪᴏɴ. ᴩʟᴇᴀꜱᴇ ᴄᴏᴍᴩʟᴇᴛᴇ ɪᴛ ɪɴ ᴛʜᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀᴩᴩ ᴀɴᴅ ᴛʀy ᴀɢᴀɪɴ.")
                await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ ɪɴꜱᴛᴀɢʀᴀᴍ ᴄʜᴀʟʟᴇɴɢᴇ ʀᴇǫᴜɪʀᴇᴅ ғᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`).")
                logger.warning(f"Instagram Challenge Required for user {user_id} ({username}).")
            except (BadPassword, LoginRequired) as e:
                await safe_edit_message(login_msg, f"❌ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ғᴀɪʟᴇᴅ: {e}. ᴩʟᴇᴀꜱᴇ ᴄʜᴇᴄᴋ yᴏᴜʀ ᴄʀᴇᴅᴇɴᴛɪᴀʟꜱ.")
                await send_log_to_channel(app, LOG_CHANNEL, f"❌ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ғᴀɪʟᴇᴅ ғᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`): {e}")
                logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
            except PleaseWaitFewMinutes:
                await safe_edit_message(login_msg, "⚠️ ɪɴꜱᴛᴀɢʀᴀᴍ ɪꜱ ᴀꜱᴋɪɴɢ ᴛᴏ ᴡᴀɪᴛ ᴀ ғᴇᴡ ᴍɪɴᴜᴛᴇꜱ ʙᴇғᴏʀᴇ ᴛʀyɪɴɢ ᴀɢᴀɪɴ. ᴩʟᴇᴀꜱᴇ ᴛʀy ᴀғᴛᴇʀ ꜱᴏᴍᴇ ᴛɪᴍᴇ.")
                await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ ɪɴꜱᴛᴀɢʀᴀᴍ 'ᴩʟᴇᴀꜱᴇ ᴡᴀɪᴛ' ғᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`).")
                logger.warning(f"Instagram 'Please Wait' for user {user_id} ({username}).")
            except Exception as e:
                await safe_edit_message(login_msg, f"❌ ᴀɴ ᴜɴᴇxᴩᴇᴄᴛᴇᴅ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ ᴅᴜʀɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ: {str(e)}")
                logger.error(f"ᴜɴʜᴀɴᴅʟᴇᴅ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ғᴏʀ {user_id} ({username}): {str(e)}")
                await send_log_to_channel(app, LOG_CHANNEL, f"🔥 ᴄʀɪᴛɪᴄᴀʟ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ᴇʀʀᴏʀ ғᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`): {str(e)}")
        
        login_task_id = f"login_task_{user_id}"
        if login_task_id in user_tasks:
            # Cancel old task to prevent stacking
            user_tasks[login_task_id].cancel()
        
        # Start the login process in a separate task
        task = asyncio.create_task(login_task())
        user_tasks[login_task_id] = task
        return
        
    if action == "waiting_for_caption":
        caption = msg.text
        settings = await get_user_settings(user_id)
        settings["caption"] = caption
        await save_user_settings(user_id, settings)
        
        # Save caption to user history
        db.users.update_one(
            {"_id": user_id},
            {"$push": {"caption_history": {"$each": [caption], "$slice": -5}}}
        )
        
        await safe_edit_message(msg.reply_to_message, f"✅ ᴄᴀᴩᴛɪᴏɴ ꜱᴇᴛ ᴛᴏ: `{caption}`", reply_markup=user_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states:
            del user_states[user_id]
        
    elif action == "waiting_for_hashtags":
        hashtags = msg.text
        settings = await get_user_settings(user_id)
        settings["hashtags"] = hashtags
        await save_user_settings(user_id, settings)
        await safe_edit_message(msg.reply_to_message, f"✅ ʜᴀꜱʜᴛᴀɢꜱ ꜱᴇᴛ ᴛᴏ: `{hashtags}`", reply_markup=user_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states:
            del user_states[user_id]
        
    elif action.startswith("waiting_for_payment_details_"):
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        
        payment_method = action.replace("waiting_for_payment_details_", "")
        details = msg.text
        
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings[payment_method] = details
        _update_global_setting("payment_settings", new_payment_settings)
        
        await msg.reply(f"✅ ᴩᴀyᴍᴇɴᴛ ᴅᴇᴛᴀɪʟꜱ ғᴏʀ **{payment_method.upper()}** ᴜᴩᴅᴀᴛᴇᴅ.", reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states:
            del user_states[user_id]

    elif action.startswith("waiting_for_google_play_qr"):
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        
        await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀɴ ɪᴍᴀɢᴇ ғɪʟᴇ ᴄᴏɴᴛᴀɪɴɪɴɢ ᴛʜᴇ ɢᴏᴏɢʟᴇ ᴩᴀy ǫʀ ᴄᴏᴅᴇ.")
        if user_id in user_states:
            del user_states[user_id]
        
    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ ᴜꜱᴇʀ ɪᴅ `{target_user_id}` ʀᴇᴄᴇɪᴠᴇᴅ. ꜱᴇʟᴇᴄᴛ ᴩʟᴀᴛғᴏʀᴍꜱ ғᴏʀ ᴩʀᴇᴍɪᴜᴍ:",
                reply_markup=get_platform_selection_markup(user_id, user_states[user_id]["selected_platforms"]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ ɪɴᴠᴀʟɪᴅ ᴜꜱᴇʀ ɪᴅ. ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ.")
            if user_id in user_states:
                del user_states[user_id]

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_max_uploads":
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        try:
            new_limit = int(msg.text)
            if new_limit <= 0:
                return await msg.reply("❌ ᴛʜᴇ ʟɪᴍɪᴛ ᴍᴜꜱᴛ ʙᴇ ᴀ ᴩᴏꜱɪᴛɪᴠᴇ ɪɴᴛᴇɢᴇʀ.")
            _update_global_setting("max_concurrent_uploads", new_limit)
            global upload_semaphore
            upload_semaphore = asyncio.Semaphore(new_limit)
            await msg.reply(f"✅ ᴍᴀxɪᴍᴜᴍ ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴩʟᴏᴀᴅꜱ ꜱᴇᴛ ᴛᴏ `{new_limit}`.", reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
            if user_id in user_states:
                del user_states[user_id]
        except ValueError:
            await msg.reply("❌ ɪɴᴠᴀʟɪᴅ ɪɴᴩᴜᴛ. ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ.")
            if user_id in user_states:
                del user_states[user_id]
    
    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_proxy_url":
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        proxy_url = msg.text
        if proxy_url.lower() == "none" or proxy_url.lower() == "remove":
            _update_global_setting("proxy_url", "")
            await msg.reply("✅ ʙᴏᴛ ᴩʀᴏxʏ ʜᴀꜱ ʙᴇᴇɴ ʀᴇᴍᴏᴠᴇᴅ.")
            logger.info(f"Admin {user_id} removed the global proxy.")
        else:
            _update_global_setting("proxy_url", proxy_url)
            await msg.reply(f"✅ ʙᴏᴛ ᴩʀᴏxʏ ꜱᴇᴛ ᴛᴏ: `{proxy_url}`.")
            logger.info(f"Admin {user_id} set the global proxy to: {proxy_url}")
        if user_id in user_states:
            del user_states[user_id]
        if msg.reply_to_message:
            await safe_edit_message(msg.reply_to_message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_global_settings_markup)

    elif isinstance(state_data, dict) and state_data.get("action") == "awaiting_post_title":
        caption = msg.text
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = caption
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await start_upload_task(msg, file_info)
    
    else:
        await msg.reply("ɪ ᴅᴏɴ'ᴛ ᴜɴᴅᴇʀꜱᴛᴀɴᴅ ᴛʜᴀᴛ ᴄᴏᴍᴍᴀɴᴅ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ ᴛʜᴇ ᴍᴇɴᴜ ʙᴜᴛᴛᴏɴꜱ ᴛᴏ ɪɴᴛᴇʀᴀᴄᴛ ᴡɪᴛʜ ᴍᴇ.")

@app.on_callback_query(filters.regex("^activate_trial$"))
async def activate_trial_cb(_, query):
    user_id = query.from_user.id
    user = _get_user_data(user_id)
    user_first_name = query.from_user.first_name or "there"

    if user and is_premium_for_platform(user_id, "instagram"):
        await query.answer("yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ ɪꜱ ᴀʟʀᴇᴀᴅy ᴀᴄᴛɪᴠᴇ! ᴇɴᴊᴏy yᴏᴜʀ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ.", show_alert=True)
        welcome_msg = f"🤖 **ᴡᴇʟᴄᴏᴍᴇ ʙᴀᴄᴋ, {user_first_name}!**\n\n"
        premium_details_text = ""
        user_premium = user.get("premium", {})
        ig_expiry = user_premium.get("instagram", {}).get("until")
        if ig_expiry:
            remaining_time = ig_expiry - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ ᴇxᴩɪʀᴇꜱ ɪɴ: `{days} ᴅᴀyꜱ, {hours} ʜᴏᴜʀꜱ`.\n"
        welcome_msg += premium_details_text
        await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    trial_duration = timedelta(hours=3)
    premium_until = datetime.utcnow() + trial_duration

    premium_data = {
        "instagram": {
            "type": "3_hour_trial",
            "added_by": "callback_trial",
            "added_at": datetime.utcnow(),
            "until": premium_until
        }
    }
    _save_user_data(user_id, {"premium": premium_data})
    logger.info(f"User {user_id} activated a 3-hour Instagram trial.")
    await send_log_to_channel(app, LOG_CHANNEL, f"✨ ᴜꜱᴇʀ `{user_id}` ᴀᴄᴛɪᴠᴀᴛᴇᴅ ᴀ 3-ʜᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ.")

    await query.answer("✅ ғʀᴇᴇ 3-ʜᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ ᴀᴄᴛɪᴠᴀᴛᴇᴅ! ᴇɴᴊᴏy!", show_alert=True)
    welcome_msg = (
        f"🎉 **ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ, {user_first_name}!**\n\n"
        f"yᴏᴜ ʜᴀᴠᴇ ᴀᴄᴛɪᴠᴀᴛᴇᴅ yᴏᴜʀ **3-ʜᴏᴜʀ ᴩʀᴇᴍɪᴜᴍ ᴛʀɪᴀʟ** ғᴏʀ **ɪɴꜱᴛᴀɢʀᴀᴍ**.\n\n"
        "yᴏᴜ ɴᴏᴡ ʜᴀᴠᴇ ᴀᴄᴄᴇꜱꜱ ᴛᴏ ᴜᴩʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴄᴏɴᴛᴇɴᴛ!\n\n"
        "ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ, ᴩʟᴇᴀꜱᴇ ʟᴏɢ ɪɴ ᴛᴏ yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀᴄᴄᴏᴜɴᴛ ᴡɪᴛʜ:\n"
        "`/login`\n\n"
        "ᴡᴀɴᴛ ᴍᴏʀᴇ ғᴇᴀᴛᴜʀᴇꜱ ᴀғᴛᴇʀ ᴛʜᴇ ᴛʀɪᴀʟ ᴇɴᴅꜱ? ᴄʜᴇᴄᴋ ᴏᴜᴛ ᴏᴜʀ ᴩᴀɪᴅ ᴩʟᴀɴꜱ ᴡɪᴛʜ /buypypremium."
    )
    await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buypypremium$"))
async def buypypremium_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    premium_plans_text = (
        "⭐ **ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ᴩʀᴇᴍɪᴜᴍ!** ⭐\n\n"
        "ᴜɴʟᴏᴄᴋ ғᴜʟʟ ғᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜᴩʟᴏᴀᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ ғᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ!\n\n"
        "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩʟᴀɴꜱ:**"
    )
    await safe_edit_message(query.message, premium_plans_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_plan_details_"))
async def show_plan_details_cb(_, query):
    user_id = query.from_user.id
    plan_key = query.data.split("show_plan_details_")[1]
    
    price_multiplier = 1
    
    plan_details = PREMIUM_PLANS[plan_key]
    
    plan_text = (
        f"**{plan_key.replace('_', ' ').title()} ᴩʟᴀɴ ᴅᴇᴛᴀɪʟꜱ**\n\n"
        f"**ᴅᴜʀᴀᴛɪᴏɴ**: "
    )
    if plan_details['duration']:
        plan_text += f"{plan_details['duration'].days} ᴅᴀyꜱ\n"
    else:
        plan_text += "ʟɪғᴇᴛɪᴍᴇ\n"
    
    price_string = plan_details['price']
    if '₹' in price_string:
        try:
            base_price = float(price_string.replace('₹', '').split('/')[0].strip())
            calculated_price = base_price * price_multiplier
            price_string = f"₹{int(calculated_price)} / {round(calculated_price * 0.012, 2)}$"
        except ValueError:
            pass

    plan_text += f"**ᴩʀɪᴄᴇ**: {price_string}\n\n"
    plan_text += "ᴛᴏ ᴩᴜʀᴄʜᴀꜱᴇ, ᴄʟɪᴄᴋ 'ʙᴜy ɴᴏᴡ' ᴏʀ ᴄʜᴇᴄᴋ ᴛʜᴇ ᴀᴠᴀɪʟᴀʙʟᴇ ᴩᴀyᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ."

    await safe_edit_message(query.message, plan_text, reply_markup=get_premium_details_markup(plan_key, price_multiplier), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_methods$"))
async def show_payment_methods_cb(_, query):
    user_id = query.from_user.id
    
    payment_methods_text = "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩᴀyᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ**\n\n"
    payment_methods_text += "ᴄʜᴏᴏꜱᴇ yᴏᴜʀ ᴩʀᴇғᴇʀʀᴇᴅ ᴍᴇᴛʜᴏᴅ ᴛᴏ ᴩʀᴏᴄᴇᴇᴅ ᴡɪᴛʜ ᴩᴀyᴍᴇɴᴛ."
    
    await safe_edit_message(query.message, payment_methods_text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_qr_google_play$"))
async def show_payment_qr_google_play_cb(_, query):
    user_id = query.from_user.id
    qr_file_id = global_settings.get("payment_settings", {}).get("google_play_qr_file_id")

    if not qr_file_id:
        await query.answer("ɢᴏᴏɢʟᴇ ᴩᴀy ǫʀ ᴄᴏᴅᴇ ɪꜱ ɴᴏᴛ ꜱᴇᴛ ʙy ᴛʜᴇ ᴀᴅᴍɪɴ yᴇᴛ.", show_alert=True)
        return
    
    await query.message.reply_photo(
        photo=qr_file_id,
        caption="**ꜱᴄᴀɴ & ᴩᴀy ᴜꜱɪɴɢ ɢᴏᴏɢʟᴇ ᴩᴀy**\n\n"
                "ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ꜱᴄʀᴇᴇɴꜱʜᴏᴛ ᴏғ ᴛʜᴇ ᴩᴀyᴍᴇɴᴛ ᴛᴏ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ғᴏʀ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ.",
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=get_payment_methods_markup()
    )
    await safe_edit_message(query.message, "ᴄʜᴏᴏꜱᴇ yᴏᴜʀ ᴩʀᴇғᴇʀʀᴇᴅ ᴍᴇᴛʜᴏᴅ ᴛᴏ ᴩʀᴏᴄᴇᴇᴅ ᴡɪᴛʜ ᴩᴀyᴍᴇɴᴛ.", reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)
    
@app.on_callback_query(filters.regex("^show_payment_details_"))
async def show_payment_details_cb(_, query):
    user_id = query.from_user.id
    method = query.data.split("show_payment_details_")[1]
    
    payment_details = global_settings.get("payment_settings", {}).get(method, "ɴᴏ ᴅᴇᴛᴀɪʟꜱ ᴀᴠᴀɪʟᴀʙʟᴇ.")
    
    text = (
        f"**{method.upper()} ᴩᴀyᴍᴇɴᴛ ᴅᴇᴛᴀɪʟꜱ**\n\n"
        f"{payment_details}\n\n"
        f"ᴩʟᴇᴀꜱᴇ ᴩᴀy ᴛʜᴇ ʀᴇǫᴜɪʀᴇᴅ ᴀᴍᴏᴜɴᴛ ᴀɴᴅ ᴄᴏɴᴛᴀᴄᴛ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ᴡɪᴛʜ ᴀ ꜱᴄʀᴇᴇɴꜱʜᴏᴛ ᴏғ ᴛʜᴇ ᴩᴀyᴍᴇɴᴛ ғᴏʀ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ."
    )
    
    await safe_edit_message(query.message, text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buy_now"))
async def buy_now_cb(_, query):
    user_id = query.from_user.id
    text = (
        f"**ᴩᴜʀᴄʜᴀꜱᴇ ᴄᴏɴғɪʀᴍᴀᴛɪᴏɴ**\n\n"
        f"ᴩʟᴇᴀꜱᴇ ᴄᴏɴᴛᴀᴄᴛ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ᴛᴏ ᴄᴏᴍᴩʟᴇᴛᴇ ᴛʜᴇ ᴩᴀyᴍᴇɴᴛ ᴩʀᴏᴄᴇꜱꜱ."
    )
    await safe_edit_message(query.message, text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^premiumdetails$"))
async def premium_details_cb(_, query):
    await query.message.reply("ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ ᴛʜᴇ `/premiumdetails` ᴄᴏᴍᴍᴀɴᴅ ɪɴꜱᴛᴇᴀᴅ ᴏғ ᴛʜɪꜱ ʙᴜᴛᴛᴏɴ.")


@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def user_settings_personal_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if is_admin(user_id) or any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        current_settings = await get_user_settings(user_id)
        compression_status = "ᴏɴ (ᴏʀɪɢɪɴᴀʟ ǫᴜᴀʟɪᴛy)" if current_settings.get("no_compression") else "ᴏғғ (ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)"
        settings_text = "⚙️ yᴏᴜʀ ᴩᴇʀꜱᴏɴᴀʟ ꜱᴇᴛᴛɪɴɢꜱ\n\n" \
                        f"🗜️ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟy: **{compression_status}**\n\n" \
                        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ yᴏᴜʀ ᴩʀᴇғᴇʀᴇɴᴄᴇꜱ."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=user_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    else:
        await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ.", show_alert=True)
        return

# New handler for the 'admin_panel' callback query
@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    await safe_edit_message(
        query.message,
        "🛠 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛʜᴇ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ!\n\n"
        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴍᴀɴᴀɢᴇ ᴛʜᴇ ʙᴏᴛ.",
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^global_settings_panel$"))
async def global_settings_panel_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    
    onam_status = "ᴏɴ" if global_settings.get("onam_toggle") else "ᴏғғ"
    max_uploads = global_settings.get("max_concurrent_uploads")
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = f"`{proxy_url}`" if proxy_url else "ɴᴏɴᴇ"
    
    compression_status = "ᴅɪꜱᴀʙʟᴇᴅ" if global_settings.get("no_compression_admin") else "ᴇɴᴀʙʟᴇᴅ"
    
    settings_text = (
        "⚙️ **ɢʟᴏʙᴀʟ ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        f"**ᴏɴᴀᴍ ꜱᴩᴇᴄɪᴀʟ ᴇᴠᴇɴᴛ:** `{onam_status}`\n"
        f"**ᴍᴀx ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴩʟᴏᴀᴅꜱ:** `{max_uploads}`\n"
        f"**ɢʟᴏʙᴀʟ ᴩʀᴏxʏ:** {proxy_status_text}\n"
        f"**ɢʟᴏʙᴀʟ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ:** `{compression_status}`\n"
    )
    
    await safe_edit_message(query.message, settings_text, reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_callback_query(filters.regex("^payment_settings_panel$"))
async def payment_settings_panel_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    await safe_edit_message(
        query.message,
        "💰 **ᴩᴀyᴍᴇɴᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴍᴀɴᴀɢᴇ ᴩᴀyᴍᴇɴᴛ ᴅᴇᴛᴀɪʟꜱ ғᴏʀ ᴩʀᴇᴍɪᴜᴍ ᴩᴜʀᴄʜᴀꜱᴇꜱ.",
        reply_markup=payment_settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^back_to_"))
async def back_to_cb(_, query):
    data = query.data
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    # Check for and cancel any active user task
    user_task_id = f"user_task_{user_id}"
    if user_task_id in user_tasks:
        user_tasks[user_task_id].cancel()
        if user_task_id in user_tasks:
            del user_tasks[user_task_id]
        
    if user_id in user_states:
        del user_states[user_id]

    if data == "back_to_main_menu":
        await query.message.delete()
        await app.send_message(
            query.message.chat.id,
            "🏠 ᴍᴀɪɴ ᴍᴇɴᴜ",
            reply_markup=get_main_keyboard(user_id)
        )
    elif data == "back_to_settings":
        current_settings = await get_user_settings(user_id)
        compression_status = "ᴏɴ (ᴏʀɪɢɪɴᴀʟ ǫᴜᴀʟɪᴛy)" if current_settings.get("no_compression") else "ᴏғғ (ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)"
        settings_text = "⚙️ yᴏᴜʀ ᴩᴇʀꜱᴏɴᴀʟ ꜱᴇᴛᴛɪɴɢꜱ\n\n" \
                        f"🗜️ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟy: **{compression_status}**\n\n" \
                        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ yᴏᴜʀ ᴩʀᴇғᴇʀᴇɴᴄᴇꜱ."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=user_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "back_to_admin":
        await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    elif data == "back_to_premium_plans":
        premium_text = (
            "⭐ **ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ᴩʀᴇᴍɪᴜᴍ!** ⭐\n\n"
            "ᴜɴʟᴏᴄᴋ ғᴜʟʟ ғᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ ғᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ!\n\n"
            "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩʟᴀɴꜱ:**"
        )
        await safe_edit_message(query.message, premium_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)
    else:
        await query.answer("❌ ᴜɴᴋɴᴏᴡɴ ʙᴀᴄᴋ ᴀᴄᴛɪᴏɴ", show_alert=True)

@app.on_callback_query(filters.regex("^toggle_compression_admin$"))
async def toggle_compression_admin_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    
    current_status = global_settings.get("no_compression_admin", False)
    new_status = not current_status
    _update_global_setting("no_compression_admin", new_status)
    status_text = "ᴅɪꜱᴀʙʟᴇᴅ" if new_status else "ᴇɴᴀʙʟᴇᴅ"
    
    await query.answer(f"ɢʟᴏʙᴀʟ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴛᴏɢɢʟᴇᴅ ᴛᴏ: {status_text}.", show_alert=True)

    onam_status = "ᴏɴ" if global_settings.get("onam_toggle") else "ᴏғғ"
    max_uploads = global_settings.get("max_concurrent_uploads")
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = f"`{proxy_url}`" if proxy_url else "ɴᴏɴᴇ"
    
    compression_status = "ᴅɪꜱᴀʙʟᴇᴅ" if global_settings.get("no_compression_admin") else "ᴇɴᴀʙʟᴇᴅ"
    
    settings_text = (
        "⚙️ **ɢʟᴏʙᴀʟ ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        f"**ᴏɴᴀᴍ ꜱᴩᴇᴄɪᴀʟ ᴇᴠᴇɴᴛ:** `{onam_status}`\n"
        f"**ᴍᴀx ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴩʟᴏᴀᴅꜱ:** `{max_uploads}`\n"
        f"**ɢʟᴏʙᴀʟ ᴩʀᴏxʏ:** {proxy_status_text}\n"
        f"**ɢʟᴏʙᴀʟ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ:** `{compression_status}`\n"
    )
    
    await safe_edit_message(query.message, settings_text, reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_callback_query(filters.regex("^set_max_uploads$"))
@with_user_lock
async def set_max_uploads_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    user_states[user_id] = {"action": "waiting_for_max_uploads"}
    current_limit = global_settings.get("max_concurrent_uploads")
    await safe_edit_message(
        query.message,
        f"🔄 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ ɴᴇᴡ ᴍᴀxɪᴍᴜᴍ ɴᴜᴍʙᴇʀ ᴏғ ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴩʟᴏᴀᴅꜱ.\n\n"
        f"ᴄᴜʀʀᴇɴᴛ ʟɪᴍɪᴛ ɪꜱ: `{current_limit}`"
    )

@app.on_callback_query(filters.regex("^set_proxy_url$"))
@with_user_lock
async def set_proxy_url_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    user_states[user_id] = {"action": "waiting_for_proxy_url"}
    current_proxy = global_settings.get("proxy_url", "ɴᴏ ᴩʀᴏxʏ ꜱᴇᴛ.")
    await safe_edit_message(
        query.message,
        f"🌐 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ ɴᴇᴡ ᴩʀᴏxʏ ᴜʀʟ (e.g., `http://user:pass@ip:port`).\n"
        f"ᴛyᴩᴇ 'ɴᴏɴᴇ' ᴏʀ 'ʀᴇᴍᴏᴠᴇ' ᴛᴏ ᴅɪꜱᴀʙʟᴇ ᴛʜᴇ ᴩʀᴏxʏ.\n\n"
        f"ᴄᴜʀʀᴇɴᴛ ᴩʀᴏxʏ: `{current_proxy}`"
    )

@app.on_callback_query(filters.regex("^reset_stats$"))
@with_user_lock
async def reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    await safe_edit_message(query.message, "⚠️ **ᴡᴀʀɴɪɴɢ!** ᴀʀᴇ yᴏᴜ ꜱᴜʀᴇ yᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʀᴇꜱᴇᴛ ᴀʟʟ ᴜᴩʟᴏᴀᴅ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ? ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ ɪꜱ ɪʀʀᴇᴠᴇʀꜱɪʙʟᴇ.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ yᴇꜱ, ʀᴇꜱᴇᴛ ꜱᴛᴀᴛꜱ", callback_data="confirm_reset_stats")],
            [InlineKeyboardButton("❌ ɴᴏ, ᴄᴀɴᴄᴇʟ", callback_data="admin_panel")]
        ]), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^confirm_reset_stats$"))
@with_user_lock
async def confirm_reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    result = db.uploads.delete_many({})
    await query.answer(f"✅ ᴀʟʟ ᴜᴩʟᴏᴀᴅ ꜱᴛᴀᴛꜱ ʜᴀᴠᴇ ʙᴇᴇɴ ʀᴇꜱᴇᴛ! ᴅᴇʟᴇᴛᴇᴅ {result.deleted_count} ᴇɴᴛʀɪᴇꜱ.", show_alert=True)
    await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    await send_log_to_channel(app, LOG_CHANNEL, f"📊 ᴀᴅᴍɪɴ `{user_id}` ʜᴀꜱ ʀᴇꜱᴇᴛ ᴀʟʟ ʙᴏᴛ ᴜᴩʟᴏᴀᴅ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ.")

@app.on_callback_query(filters.regex("^show_system_stats$"))
async def show_system_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        system_stats_text = (
            "💻 **ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ**\n\n"
            f"**ᴄᴩᴜ:** `{cpu_usage}%`\n"
            f"**ʀᴀᴍ:** `{ram.percent}%` (ᴜꜱᴇᴅ: `{ram.used / (1024**3):.2f}` ɢʙ / ᴛᴏᴛᴀʟ: `{ram.total / (1024**3):.2f}` ɢʙ)\n"
            f"**ᴅɪꜱᴋ:** `{disk.percent}%` (ᴜꜱᴇᴅ: `{disk.used / (1024**3):.2f}` ɢʙ / ᴛᴏᴛᴀʟ: `{disk.total / (1024**3):.2f}` ɢʙ)\n\n"
        )
        gpu_info = "ɴᴏ ɢᴩᴜ ғᴏᴜɴᴅ ᴏʀ ɢᴩᴜᴛɪʟ ɪꜱ ɴᴏᴛ ɪɴꜱᴛᴀʟʟᴇᴅ."
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu_info = "**ɢᴩᴜ ɪɴғᴏ:**\n"
                for i, gpu in enumerate(gpus):
                    gpu_info += (
                        f"     - **ɢᴩᴜ {i}:** `{gpu.name}`\n"
                        f"     - ʟᴏᴀᴅ: `{gpu.load*100:.1f}%`\n"
                        f"     - ᴍᴇᴍᴏʀy: `{gpu.memoryUsed}/{gpu.memoryTotal}` ᴍʙ\n"
                        f"     - ᴛᴇᴍᴩ: `{gpu.temperature}°ᴄ`\n"
                    )
            else:
                gpu_info = "ɴᴏ ɢᴩᴜ ғᴏᴜɴᴅ."
        except Exception:
            gpu_info = "ᴄᴏᴜʟᴅ ɴᴏᴛ ʀᴇᴛʀɪᴇᴠᴇ ɢᴩᴜ ɪɴғᴏ."
        system_stats_text += gpu_info
        await safe_edit_message(
            query.message,
            system_stats_text,
            reply_markup=admin_global_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await query.answer("❌ ғᴀɪʟᴇᴅ ᴛᴏ ʀᴇᴛʀɪᴇᴠᴇ ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ.", show_alert=True)
        logger.error(f"ᴇʀʀᴏʀ ʀᴇᴛʀɪᴇᴠɪɴɢ ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ ғᴏʀ ᴀᴅᴍɪɴ {user_id}: {e}")
        await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    users = list(db.users.find({}))
    if not users:
        await safe_edit_message(
            query.message,
            "👥 ɴᴏ ᴜꜱᴇʀꜱ ғᴏᴜɴᴅ ɪɴ ᴛʜᴇ ᴅᴀᴛᴀʙᴀꜱᴇ.",
            reply_markup=admin_markup
        )
        return
    user_list_text = "👥 **ᴀʟʟ ᴜꜱᴇʀꜱ:**\n\n"
    for user in users:
        user_id = user["_id"]
        instagram_username = user.get("instagram_username", "ɴ/ᴀ")
        added_at = user.get("added_at", "ɴ/ᴀ").strftime("%Y-%m-%d") if isinstance(user.get("added_at"), datetime) else "ɴ/ᴀ"
        last_active = user.get("last_active", "ɴ/ᴀ").strftime("%Y-%m-%d %H:%M") if isinstance(user.get("last_active"), datetime) else "ɴ/ᴀ"
        platform_statuses = []
        if user_id == ADMIN_ID:
            platform_statuses.append("👑 ᴀᴅᴍɪɴ")
        else:
            for platform in PREMIUM_PLATFORMS:
                if is_premium_for_platform(user_id, platform):
                    platform_data = user.get("premium", {}).get(platform, {})
                    premium_type = platform_data.get("type")
                    premium_until = platform_data.get("until")
                    if premium_type == "lifetime":
                        platform_statuses.append(f"⭐ {platform.capitalize()}: ʟɪғᴇᴛɪᴍᴇ")
                    elif premium_until:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: ᴇxᴩɪʀᴇꜱ `{premium_until.strftime('%Y-%m-%d')}`")
                    else:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: ᴀᴄᴛɪᴠᴇ")
                else:
                    platform_statuses.append(f"❌ {platform.capitalize()}: ғʀᴇᴇ")
        status_line = " | ".join(platform_statuses)
        user_list_text += (
            f"ɪᴅ: `{user_id}` | {status_line}\n"
            f"ɪɢ: `{instagram_username}`\n"
            f"ᴀᴅᴅᴇᴅ: `{added_at}` | ʟᴀꜱᴛ ᴀᴄᴛɪᴠᴇ: `{last_active}`\n"
            "-----------------------------------\n"
        )
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "ᴜꜱᴇʀ ʟɪꜱᴛ ɪꜱ ᴛᴏᴏ ʟᴏɴɢ. ꜱᴇɴᴅɪɴɢ ᴀꜱ ᴀ ғɪʟᴇ...")
        with open("users.txt", "w") as f:
            f.write(user_list_text.replace("`", ""))
        await app.send_document(query.message.chat.id, "users.txt", caption="👥 ᴀʟʟ ᴜꜱᴇʀꜱ ʟɪꜱᴛ")
        os.remove("users.txt")
        await safe_edit_message(
            query.message,
            "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ",
            reply_markup=admin_markup
        )
    else:
        await safe_edit_message(
            query.message,
            user_list_text,
            reply_markup=admin_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )

@app.on_callback_query(filters.regex("^manage_premium$"))
@with_user_lock
async def manage_premium_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    user_states[query.from_user.id] = {"action": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(
        query.message,
        "➕ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ **ᴜꜱᴇʀ ɪᴅ** ᴛᴏ ᴍᴀɴᴀɢᴇ ᴛʜᴇɪʀ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ."
    )

@app.on_callback_query(filters.regex("^select_platform_"))
async def select_platform_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("ᴇʀʀᴏʀ: ᴜꜱᴇʀ ꜱᴇʟᴇᴄᴛɪᴏɴ ʟᴏꜱᴛ. ᴩʟᴇᴀꜱᴇ ᴛʀy 'ᴍᴀɴᴀɢᴇ ᴩʀᴇᴍɪᴜᴍ' ᴀɢᴀɪɴ.", show_alert=True)
        if user_id in user_states:
            del user_states[user_id]
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    platform_to_toggle = query.data.split("_")[-1]
    selected_platforms = state_data.get("selected_platforms", {})
    if platform_to_toggle in selected_platforms:
        selected_platforms.pop(platform_to_toggle)
    else:
        selected_platforms[platform_to_toggle] = True
    state_data["selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    await safe_edit_message(
        query.message,
        f"✅ ᴜꜱᴇʀ ɪᴅ `{state_data['target_user_id']}` ʀᴇᴄᴇɪᴠᴇᴅ. ꜱᴇʟᴇᴄᴛ ᴩʟᴀᴛғᴏʀᴍꜱ ғᴏʀ ᴩʀᴇᴍɪᴜᴍ:",
        reply_markup=get_platform_selection_markup(user_id, selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_platform_selection$"))
async def confirm_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("ᴇʀʀᴏʀ: ᴩʟᴀᴛғᴏʀᴍ ꜱᴇʟᴇᴄᴛɪᴏɴ ʟᴏꜱᴛ. ᴩʟᴇᴀꜱᴇ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ᴩʀᴇᴍɪᴜᴍ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ᴩʀᴏᴄᴇꜱꜱ.", show_alert=True)
        if user_id in user_states:
            del user_states[user_id]
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = [p for p, selected in state_data.get("selected_platforms", {}).items() if selected]
    if not selected_platforms:
        return await query.answer("ᴩʟᴇᴀꜱᴇ ꜱᴇʟᴇᴄᴛ ᴀᴛ ʟᴇᴀꜱᴛ ᴏɴᴇ ᴩʟᴀᴛғᴏʀᴍ!", show_alert=True)
    state_data["action"] = "select_premium_plan_for_platforms"
    state_data["final_selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    await safe_edit_message(
        query.message,
        f"✅ ᴩʟᴀᴛғᴏʀᴍꜱ ꜱᴇʟᴇᴄᴛᴇᴅ: `{', '.join(platform.capitalize() for platform in selected_platforms)}`. ɴᴏᴡ, ꜱᴇʟᴇᴄᴛ ᴀ ᴩʀᴇᴍɪᴜᴍ ᴩʟᴀɴ ғᴏʀ ᴜꜱᴇʀ `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(user_id),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_plan_"))
async def select_plan_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_premium_plan_for_platforms":
        await query.answer("ᴇʀʀᴏʀ: ᴩʟᴀɴ ꜱᴇʟᴇᴄᴛɪᴏɴ ʟᴏꜱᴛ. ᴩʟᴇᴀꜱᴇ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ᴩʀᴇᴍɪᴜᴍ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ᴩʀᴏᴄᴇꜱꜱ.", show_alert=True)
        if user_id in user_states:
            del user_states[user_id]
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    
    target_user_id = state_data["target_user_id"]
    selected_platforms = state_data["final_selected_platforms"]
    premium_plan_key = query.data.split("select_plan_")[1]
    
    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("ɪɴᴠᴀʟɪᴅ ᴩʀᴇᴍɪᴜᴍ ᴩʟᴀɴ ꜱᴇʟᴇᴄᴛᴇᴅ.", show_alert=True)
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    
    plan_details = PREMIUM_PLANS[premium_plan_key]
    update_query = {}
    for platform in selected_platforms:
        new_premium_until = None
        if plan_details["duration"] is not None:
            new_premium_until = datetime.utcnow() + plan_details["duration"]
        platform_premium_data = {
            "type": premium_plan_key,
            "added_by": user_id,
            "added_at": datetime.utcnow()
        }
        if new_premium_until:
            platform_premium_data["until"] = new_premium_until
        update_query[f"premium.{platform}"] = platform_premium_data
    
    # Corrected logic to apply premium directly from admin panel
    db.users.update_one({"_id": target_user_id}, {"$set": update_query}, upsert=True)
    
    admin_confirm_text = f"✅ ᴩʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ᴛᴏ ᴜꜱᴇʀ `{target_user_id}` ғᴏʀ:\n"
    for platform in selected_platforms:
        updated_user = _get_user_data(target_user_id)
        platform_data = updated_user.get("premium", {}).get(platform, {})
        confirm_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if platform_data.get("until"):
            confirm_line += f" (ᴇxᴩɪʀᴇꜱ: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`)"
        admin_confirm_text += f"- {confirm_line}\n"
    
    await safe_edit_message(
        query.message,
        admin_confirm_text,
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer("ᴩʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ!", show_alert=False)
    if user_id in user_states:
        del user_states[user_id]
    
    try:
        user_msg = (
            f"🎉 **ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ!** 🎉\n\n"
            f"yᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ɢʀᴀɴᴛᴇᴅ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ғᴏʀ ᴛʜᴇ ғᴏʟʟᴏᴡɪɴɢ ᴩʟᴀᴛғᴏʀᴍꜱ:\n"
        )
        for platform in selected_platforms:
            updated_user = _get_user_data(target_user_id)
            platform_data = updated_user.get("premium", {}).get(platform, {})
            msg_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
            if platform_data.get("until"):
                msg_line += f" (ᴇxᴩɪʀᴇꜱ: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`)"
            user_msg += f"- {msg_line}\n"
        user_msg += "\nᴇɴᴊᴏy yᴏᴜʀ ɴᴇᴡ ғᴇᴀᴛᴜʀᴇꜱ! ✨"
        await app.send_message(target_user_id, user_msg, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 ᴩʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ɴᴏᴛɪғɪᴄᴀᴛɪᴏɴ ꜱᴇɴᴛ ᴛᴏ `{target_user_id}` ʙy ᴀᴅᴍɪɴ `{user_id}`. ᴩʟᴀᴛғᴏʀᴍꜱ: `{', '.join(selected_platforms)}`, ᴩʟᴀɴ: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"ғᴀɪʟᴇᴅ ᴛᴏ ɴᴏᴛɪғy ᴜꜱᴇʀ {target_user_id} ᴀʙᴏᴜᴛ ᴩʀᴇᴍɪᴜᴍ: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ ғᴀɪʟᴇᴅ ᴛᴏ ɴᴏᴛɪғy ᴜꜱᴇʀ `{target_user_id}` ᴀʙᴏᴜᴛ ᴩʀᴇᴍɪᴜᴍ. ᴇʀʀᴏʀ: `{str(e)}`"
        )

@app.on_callback_query(filters.regex("^back_to_platform_selection$"))
async def back_to_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") not in ["select_platforms_for_premium", "select_premium_plan_for_platforms"]:
        await query.answer("ᴇʀʀᴏʀ: ɪɴᴠᴀʟɪᴅ ꜱᴛᴀᴛᴇ ғᴏʀ ʙᴀᴄᴋ ᴀᴄᴛɪᴏɴ. ᴩʟᴇᴀꜱᴇ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ᴩʀᴇᴍɪᴜᴍ ᴩʀᴏᴄᴇꜱꜱ.", show_alert=True)
        if user_id in user_states:
            del user_states[user_id]
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    current_selected_platforms = state_data.get("selected_platforms", {})
    user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": current_selected_platforms}
    await safe_edit_message(
        query.message,
        f"✅ ᴜꜱᴇʀ ɪᴅ `{target_user_id}` ʀᴇᴄᴇɪᴠᴇᴅ. ꜱᴇʟᴇᴄᴛ ᴩʟᴀᴛғᴏʀᴍꜱ ғᴏʀ ᴩʀᴇᴍɪᴜᴍ:",
        reply_markup=get_platform_selection_markup(user_id, current_selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "📢 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ ᴍᴇꜱꜱᴀɢᴇ yᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴛᴏ ᴀʟʟ ᴜꜱᴇʀꜱ.\n\n"
        "ᴜꜱᴇ `/broadcast <message>` ᴄᴏᴍᴍᴀɴᴅ ɪɴꜱᴛᴇᴀᴅ."
    )

@app.on_callback_query(filters.regex("^admin_stats_panel$"))
async def admin_stats_panel_cb(_, query):
    if not is_admin(query.from_user.id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    
    total_users = db.users.count_documents({})
    total_uploads = db.uploads.count_documents({})
    
    stats_text = (
        "📊 **ᴀᴅᴍɪɴ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ ᴩᴀɴᴇʟ**\n\n"
        f"**ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ**: `{total_users}`\n"
        f"**ᴛᴏᴛᴀʟ ᴜᴩʟᴏᴀᴅꜱ**: `{total_uploads}`\n\n"
        "ᴜꜱᴇ `/stats` ᴄᴏᴍᴍᴀɴᴅ ғᴏʀ ᴍᴏʀᴇ ᴅᴇᴛᴀɪʟᴇᴅ ꜱᴛᴀᴛꜱ."
    )
    
    await safe_edit_message(query.message, stats_text, reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)

# Fix for user-facing Settings buttons
@app.on_callback_query(filters.regex("^upload_type$"))
async def upload_type_cb(_, query):
    user_id = query.from_user.id
    if not is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ.", show_alert=True)

    await safe_edit_message(
        query.message,
        "📌 ꜱᴇʟᴇᴄᴛ ᴛʜᴇ ᴅᴇғᴀᴜʟᴛ ᴜᴩʟᴏᴀᴅ ᴛyᴩᴇ:",
        reply_markup=upload_type_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_caption$"))
async def set_caption_cb(_, query):
    user_id = query.from_user.id
    if not is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ.", show_alert=True)
    
    user_states[user_id] = {"action": "waiting_for_caption"}
    await safe_edit_message(
        query.message,
        "📝 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɴᴇᴡ ᴅᴇғᴀᴜʟᴛ ᴄᴀᴩᴛɪᴏɴ.",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_hashtags$"))
async def set_hashtags_cb(_, query):
    user_id = query.from_user.id
    if not is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ.", show_alert=True)

    user_states[user_id] = {"action": "waiting_for_hashtags"}
    await safe_edit_message(
        query.message,
        "🏷️ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɴᴇᴡ ᴅᴇғᴀᴜʟᴛ ʜᴀꜱʜᴛᴀɢꜱ. (e.g., `#hashtag1 #hashtag2`)",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio$"))
async def set_aspect_ratio_cb(_, query):
    user_id = query.from_user.id
    if not is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ.", show_alert=True)

    await safe_edit_message(
        query.message,
        "📐 ꜱᴇʟᴇᴄᴛ ᴛʜᴇ ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ ғᴏʀ yᴏᴜʀ ᴠɪᴅᴇᴏꜱ:",
        reply_markup=aspect_ratio_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_ar_"))
async def set_aspect_ratio_value_cb(_, query):
    user_id = query.from_user.id
    if not is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ.", show_alert=True)

    aspect_ratio = query.data.split("_")[-1]
    settings = await get_user_settings(user_id)
    settings["aspect_ratio"] = aspect_ratio
    await save_user_settings(user_id, settings)

    await query.answer(f"✅ ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ ꜱᴇᴛ ᴛᴏ {aspect_ratio}.", show_alert=True)
    
    current_settings = await get_user_settings(user_id)
    compression_status = "ᴏɴ (ᴏʀɪɢɪɴᴀʟ ǫᴜᴀʟɪᴛy)" if current_settings.get("no_compression") else "ᴏғғ (ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)"
    settings_text = "⚙️ yᴏᴜʀ ᴩᴇʀꜱᴏɴᴀʟ ꜱᴇᴛᴛɪɴɢꜱ\n\n" \
                    f"🗜️ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟy: **{compression_status}**\n\n" \
                    "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ yᴏᴜʀ ᴩʀᴇғᴇʀᴇɴᴄᴇꜱ."
    await safe_edit_message(query.message, settings_text, reply_markup=user_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)

# Timeout function to cancel user tasks
async def timeout_task(user_id, message_id):
    await asyncio.sleep(TIMEOUT_SECONDS)
    if user_id in user_states:
        del user_states[user_id]
        logger.info(f"Task for user {user_id} timed out and was canceled.")
        try:
            await app.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text="⚠️ ᴛɪᴍᴇᴏᴜᴛ! ᴛʜᴇ ᴏᴩᴇʀᴀᴛɪᴏɴ ᴡᴀꜱ ᴄᴀɴᴄᴇʟᴇᴅ ᴅᴜᴇ ᴛᴏ ɪɴᴀᴄᴛɪᴠɪᴛy. ᴩʟᴇᴀꜱᴇ ꜱᴛᴀʀᴛ ᴀɢᴀɪɴ."
            )
        except Exception as e:
            logger.warning(f"Could not send timeout message to user {user_id}: {e}")

# Modified handle_media_upload to handle timeouts
@app.on_message(filters.media & filters.private)
@with_user_lock
async def handle_media_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    state_data = user_states.get(user_id)
    
    if is_admin(user_id) and state_data and state_data.get("action") == "waiting_for_google_play_qr" and msg.photo:
        qr_file_id = msg.photo.file_id
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings["google_play_qr_file_id"] = qr_file_id
        _update_global_setting("payment_settings", new_payment_settings)
        if user_id in user_states:
            del user_states[user_id]
        return await msg.reply("✅ ɢᴏᴏɢʟᴇ ᴩᴀy ǫʀ ᴄᴏᴅᴇ ɪᴍᴀɢᴇ ꜱᴜᴄᴄᴇꜱꜱғᴜʟʟy ꜱᴀᴠᴇᴅ!")
    
    if not state_data or state_data.get("action") not in [
        "waiting_for_instagram_reel_video", "waiting_for_instagram_photo_image"
    ]:
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ ᴏɴᴇ ᴏғ ᴛʜᴇ ᴜᴩʟᴏᴀᴅ ʙᴜᴛᴛᴏɴꜱ ғɪʀꜱᴛ.")

    platform = state_data["platform"]
    upload_type = state_data["upload_type"]
    
    if msg.video and (upload_type in ["reel", "video"]):
        if msg.video.file_size > MAX_FILE_SIZE_BYTES:
            if user_id in user_states:
                del user_states[user_id]
            return await msg.reply(f"❌ ғɪʟᴇ ꜱɪᴢᴇ ᴇxᴄᴇᴇᴅꜱ ᴛʜᴇ ʟɪᴍɪᴛ ᴏғ `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` ᴍʙ.")
        file_info = {
            "file_id": msg.video.file_id,
            "platform": platform,
            "upload_type": upload_type,
            "file_size": msg.video.file_size,
            "processing_msg": await msg.reply("⏳ ꜱᴛᴀʀᴛɪɴɢ ᴅᴏᴡɴʟᴏᴀᴅ...")
        }
    elif msg.photo and (upload_type in ["post", "photo"]):
        file_info = {
            "file_id": msg.photo.file_id,
            "platform": platform,
            "upload_type": upload_type,
            "file_size": msg.photo.file_size,
            "processing_msg": await msg.reply("⏳ ꜱᴛᴀʀᴛɪɴɢ ᴅᴏᴡɴʟᴏᴀᴅ...")
        }
    elif msg.document:
        if user_id in user_states:
            del user_states[user_id]
        return await msg.reply("⚠️ ᴅᴏᴄᴜᴍᴇɴᴛꜱ ᴀʀᴇ ɴᴏᴛ ꜱᴜᴩᴩᴏʀᴛᴇᴅ ғᴏʀ ᴜᴩʟᴏᴀᴅ yᴇᴛ. ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠɪᴅᴇᴏ ᴏʀ ᴩʜᴏᴛᴏ.")
    else:
        if user_id in user_states:
            del user_states[user_id]
        return await msg.reply("❌ ᴛʜᴇ ғɪʟᴇ ᴛyᴩᴇ ᴅᴏᴇꜱ ɴᴏᴛ ᴍᴀᴛᴄʜ ᴛʜᴇ ʀᴇǫᴜᴇꜱᴛᴇᴅ ᴜᴩʟᴏᴀᴅ ᴛyᴩᴇ.")

    file_info["downloaded_path"] = None
    
    # Start download and set timeout
    try:
        start_time = time.time()
        file_info["processing_msg"].is_progress_message_updated = False
        file_info["downloaded_path"] = await asyncio.to_thread(app.download_media,
            msg,
            progress=lambda current, total: asyncio.run(progress_callback(current, total, "ᴅᴏᴡɴʟᴏᴀᴅ", file_info["processing_msg"], start_time))
        )
        
        caption_msg = await safe_edit_message(file_info["processing_msg"], "✅ ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴏᴍᴩʟᴇᴛᴇ. ᴡʜᴀᴛ ᴛɪᴛʟᴇ ᴅᴏ yᴏᴜ ᴡᴀɴᴛ ғᴏʀ yᴏᴜʀ ᴩᴏꜱᴛ?", reply_markup=get_caption_markup())
        user_states[user_id] = {"action": "awaiting_post_title", "file_info": file_info}
        
        # Start a timeout task for user input
        user_task_id = f"user_task_{user_id}"
        if user_task_id in user_tasks:
            user_tasks[user_task_id].cancel()
        user_tasks[user_task_id] = asyncio.create_task(timeout_task(user_id, caption_msg.id))

    except asyncio.CancelledError:
        logger.info(f"ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴀɴᴄᴇʟʟᴇᴅ ʙy ᴜꜱᴇʀ {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ғɪʟᴇ ᴅᴏᴡɴʟᴏᴀᴅ ғᴏʀ ᴜꜱᴇʀ {user_id}: {e}")
        await safe_edit_message(file_info["processing_msg"], f"❌ ᴅᴏᴡɴʟᴏᴀᴅ ғᴀɪʟᴇᴅ: {str(e)}")
        cleanup_temp_files([file_info.get("downloaded_path")])
        if user_id in user_states:
            del user_states[user_id]

# Updated process_and_upload to fix the caption bug
async def process_and_upload(msg, file_info):
    user_id = msg.from_user.id
    platform = file_info["platform"]
    upload_type = file_info["upload_type"]
    file_path = file_info["downloaded_path"]
    
    processing_msg = file_info["processing_msg"]

    # Cancel the timeout task if it's still running
    user_task_id = f"user_task_{user_id}"
    if user_task_id in user_tasks:
        user_tasks[user_task_id].cancel()
        if user_task_id in user_tasks:
            del user_tasks[user_task_id]

    try:
        video_to_upload = file_path
        transcoded_video_path = None
        
        # Get admin compression setting
        no_compression_admin = global_settings.get("no_compression_admin", False)
        
        file_extension = os.path.splitext(file_path)[1].lower()
        is_video = file_extension in ['.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv']
        
        if is_video and not no_compression_admin:
            await safe_edit_message(processing_msg, "🔄 ᴏᴩᴛɪᴍɪᴢɪɴɢ ᴠɪᴅᴇᴏ (ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ)... ᴛʜɪꜱ ᴍᴀy ᴛᴀᴋᴇ ᴀ ᴍᴏᴍᴇɴᴛ.")
            transcoded_video_path = f"{file_path}_transcoded.mp4"
            ffmpeg_command = ["ffmpeg", "-i", file_path, "-map_chapters", "-1", "-y"]
            ffmpeg_command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "23",
                                     "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                                     "-pix_fmt", "yuv420p", "-movflags", "faststart", transcoded_video_path])
            
            logger.info(f"Running FFmpeg command: {' '.join(ffmpeg_command)}")
            try:
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
                if process.returncode != 0:
                    logger.error(f"FFmpeg transcoding failed for {file_path}: {stderr.decode()}")
                    raise Exception(f"ᴠɪᴅᴇᴏ ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ғᴀɪʟᴇᴅ: {stderr.decode()}")
                else:
                    logger.info(f"FFmpeg transcoding successful. ᴏᴜᴛᴩᴜᴛ: {transcoded_video_path}")
                    video_to_upload = transcoded_video_path
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    logger.info(f"ᴅᴇʟᴇᴛᴇᴅ ᴏʀɪɢɪɴᴀʟ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ ᴠɪᴅᴇᴏ ғɪʟᴇ: {file_path}")
            except asyncio.TimeoutError:
                process.kill()
                logger.error(f"FFmpeg process timed out for user {user_id}")
                raise Exception("ᴠɪᴅᴇᴏ ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ᴛɪᴍᴇᴅ ᴏᴜᴛ.")
        elif is_video and no_compression_admin:
            await safe_edit_message(processing_msg, "✅ ɴᴏ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ. ᴜᴩʟᴏᴀᴅɪɴɢ ᴏʀɪɢɪɴᴀʟ ғɪʟᴇ.")
            video_to_upload = file_path
        else:
            await safe_edit_message(processing_msg, "✅ ɴᴏ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴀᴩᴩʟɪᴇᴅ ғᴏʀ ɪᴍᴀɢᴇꜱ.")

        settings = await get_user_settings(user_id)
        default_caption = settings.get("caption", f"ᴄʜᴇᴄᴋ ᴏᴜᴛ ᴍy ɴᴇᴡ ɪɴꜱᴛᴀɢʀᴀᴍ ᴄᴏɴᴛᴇɴᴛ! 🎥")
        hashtags = settings.get("hashtags", "")
        
        # Fixed caption logic
        final_caption = file_info.get("custom_caption")
        if not final_caption:
            final_caption = default_caption
        if hashtags:
            final_caption = f"{final_caption}\n\n{hashtags}"

        url = "ɴ/ᴀ"
        media_id = "ɴ/ᴀ"
        media_type_value = ""

        await safe_edit_message(processing_msg, "🚀 **ᴜᴩʟᴏᴀᴅɪɴɢ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ...**", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup())
        start_time = time.time()

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            proxy_url = global_settings.get("proxy_url")
            if proxy_url:
                user_upload_client.set_proxy(proxy_url)
            elif INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
            session = await load_instagram_session(user_id)
            if not session:
                raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴩɪʀᴇᴅ.")
            user_upload_client.set_settings(session)
            
            try:
                await asyncio.to_thread(user_upload_client.get_timeline_feed)
            except LoginRequired:
                raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴩɪʀᴇᴅ.")

            if upload_type == "reel":
                result = await asyncio.to_thread(user_upload_client.clip_upload, video_to_upload, caption=final_caption)
                url = f"https://instagram.com/reel/{result.code}"
                media_id = result.pk
                media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type
            elif upload_type == "post":
                result = await asyncio.to_thread(user_upload_client.photo_upload, video_to_upload, caption=final_caption)
                url = f"https://instagram.com/p/{result.code}"
                media_id = result.pk
                media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type
        
        db.uploads.insert_one({
            "user_id": user_id,
            "media_id": media_id,
            "media_type": media_type_value,
            "platform": platform,
            "upload_type": upload_type,
            "timestamp": datetime.utcnow(),
            "url": url,
            "caption": final_caption
        })

        log_msg = (
            f"📤 ɴᴇᴡ {platform.capitalize()} {upload_type.capitalize()} ᴜᴩʟᴏᴀᴅ\n\n"
            f"👤 ᴜꜱᴇʀ: `{user_id}`\n"
            f"📛 ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 ᴜʀʟ: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await safe_edit_message(processing_msg, f"✅ ᴜᴩʟᴏᴀᴅᴇᴅ ꜱᴜᴄᴄᴇꜱꜱғᴜʟʟy!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except asyncio.CancelledError:
        logger.info(f"ᴜᴩʟᴏᴀᴅ ᴩʀᴏᴄᴇꜱꜱ ғᴏʀ ᴜꜱᴇʀ {user_id} ᴡᴀꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
        await safe_edit_message(processing_msg, "❌ ᴜᴩʟᴏᴀᴅ ᴩʀᴏᴄᴇꜱꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
    except LoginRequired:
        await safe_edit_message(processing_msg, f"❌ {platform.capitalize()} ʟᴏɢɪɴ ʀᴇǫᴜɪʀᴇᴅ. yᴏᴜʀ ꜱᴇꜱꜱɪᴏɴ ᴍɪɢʜᴛ ʜᴀᴠᴇ ᴇxᴩɪʀᴇᴅ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ `/login` ᴀɢᴀɪɴ.")
        logger.error(f"ʟᴏɢɪɴʀᴇǫᴜɪʀᴇᴅ ᴅᴜʀɪɴɢ {platform} ᴜᴩʟᴏᴀᴅ ғᴏʀ ᴜꜱᴇʀ {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ (ʟᴏɢɪɴ ʀᴇǫᴜɪʀᴇᴅ)\nᴜꜱᴇʀ: `{user_id}`")
    except ClientError as ce:
        await safe_edit_message(processing_msg, f"❌ {platform.capitalize()} ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ᴜᴩʟᴏᴀᴅ: {ce}. ᴩʟᴇᴀꜱᴇ ᴛʀy ᴀɢᴀɪɴ ʟᴀᴛᴇʀ.")
        logger.error(f"ᴄʟɪᴇɴᴛᴇʀʀᴏʀ ᴅᴜʀɪɴɢ {platform} ᴜᴩʟᴏᴀᴅ ғᴏʀ ᴜꜱᴇʀ {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ (ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ)\nᴜꜱᴇʀ: `{user_id}`\nᴇʀʀᴏʀ: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ: {str(e)}"
        if processing_msg:
            await safe_edit_message(processing_msg, error_msg)
        else:
            await msg.reply(error_msg)
        logger.error(f"{platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ ғᴏʀ {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ\nᴜꜱᴇʀ: `{user_id}`\nᴇʀʀᴏʀ: `{error_msg}`")
    finally:
        cleanup_temp_files([file_path, transcoded_video_path])
        if user_id in user_states:
            del user_states[user_id]
        upload_tasks.pop(user_id, None)

# === HTTP Server ===
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

def run_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    server.serve_forever()

# Main entry point
if __name__ == "__main__":
    os.makedirs("sessions", exist_ok=True)
    logger.info("Session directory ensured.")
    
    load_instagram_client_session()
    
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Health check server started on port 8080.")

    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}")
        sys.exit(1)

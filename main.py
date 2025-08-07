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
    "no_compression_admin": False # New admin-only switch for compression
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
        [KeyboardButton("⚙️ 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀"), KeyboardButton("📊 𝗦𝘁𝗮𝘁𝘀")]
    ]
    is_instagram_premium = is_premium_for_platform(user_id, "instagram")

    upload_buttons_row = []
    if is_instagram_premium:
        upload_buttons_row.extend([KeyboardButton("📸 𝗜𝗻𝘀𝘁𝗮 𝗣𝗵𝗼𝘁𝗼"), KeyboardButton("📤 𝗜𝗻𝘀𝘁𝗮 𝗥𝗲𝗲𝗹")])
    

    if upload_buttons_row:
        buttons.insert(0, upload_buttons_row)

    buttons.append([KeyboardButton("⭐ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺"), KeyboardButton("/premiumdetails")])
    if is_admin(user_id):
        buttons.append([KeyboardButton("🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹"), KeyboardButton("🔄 𝗥𝗲𝘀𝘁𝗮𝗿𝘁 𝗕𝗼𝘁")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)


# User settings markup now only includes relevant buttons. The compression toggle is removed.
user_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 𝗨𝗽𝗹𝗼𝗮𝗱 𝗧𝘆𝗽𝗲", callback_data="upload_type")],
    [InlineKeyboardButton("📝 𝗖𝗮𝗽𝘁𝗶𝗼𝗻", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ 𝗛𝗮𝘀𝗵𝘁𝗮𝗴𝘀", callback_data="set_hashtags")],
    [InlineKeyboardButton("📐 𝗔𝘀𝗽𝗲𝗰𝘁 𝗥𝗮𝘁𝗶𝗼 (𝗩𝗶𝗱𝗲𝗼)", callback_data="set_aspect_ratio")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 𝗨𝘀𝗲𝗿𝘀 𝗟𝗶𝘀𝘁", callback_data="users_list")],
    [InlineKeyboardButton("➕ 𝗠𝗮𝗻𝗮𝗴𝗲 𝗣𝗿𝗲𝗺𝗶𝘂𝗺", callback_data="manage_premium")],
    [InlineKeyboardButton("📢 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁", callback_data="broadcast_message")],
    [InlineKeyboardButton("⚙️ 𝗚𝗹𝗼𝗯𝗮𝗹 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀", callback_data="global_settings_panel")],
    [InlineKeyboardButton("📊 𝗦𝘁𝗮𝘁𝘀 𝗣𝗮𝗻𝗲𝗹", callback_data="admin_stats_panel")],
    [InlineKeyboardButton("💰 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀", callback_data="payment_settings_panel")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝗠𝗲𝗻𝘂", callback_data="back_to_main_menu")]
])

admin_global_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("𝗢𝗻𝗮𝗺 𝗧𝗼𝗴𝗴𝗹𝗲", callback_data="toggle_onam")],
    [InlineKeyboardButton("𝗠𝗮𝘅 𝗨𝗽𝗹𝗼𝗮𝗱 𝗨𝘀𝗲𝗿𝘀", callback_data="set_max_uploads")],
    [InlineKeyboardButton("𝗥𝗲𝘀𝗲𝘁 𝗦𝘁𝗮𝘁𝘀", callback_data="reset_stats")],
    [InlineKeyboardButton("𝗦𝗵𝗼𝘄 𝗦𝘆𝘀𝘁𝗲𝗺 𝗦𝘁𝗮𝘁𝘀", callback_data="show_system_stats")],
    [InlineKeyboardButton("🌐 𝗣𝗿𝗼𝘅𝘆 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀", callback_data="set_proxy_url")],
    [InlineKeyboardButton("🗜️ 𝗧𝗼𝗴𝗴𝗹𝗲 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻", callback_data="toggle_compression_admin")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝘁𝗼 𝗔𝗱𝗺𝗶𝗻", callback_data="admin_panel")]
])

payment_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗹𝗮𝘆 𝗤𝗥 𝗖𝗼𝗱𝗲", callback_data="set_payment_google_play_qr")],
    [InlineKeyboardButton("𝗨𝗣𝗜", callback_data="set_payment_upi")],
    [InlineKeyboardButton("𝗨𝗦𝗧", callback_data="set_payment_ust")],
    [InlineKeyboardButton("𝗕𝗧𝗖", callback_data="set_payment_btc")],
    [InlineKeyboardButton("𝗢𝘁𝗵𝗲𝗿𝘀", callback_data="set_payment_others")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝘁𝗼 𝗔𝗱𝗺𝗶𝗻", callback_data="admin_panel")]
])

upload_type_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 𝗥𝗲𝗲𝗹", callback_data="set_type_reel")],
    [InlineKeyboardButton("📷 𝗣𝗼𝘀𝘁", callback_data="set_type_post")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_settings")]
])

aspect_ratio_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("𝗢𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗔𝘀𝗽𝗲𝗰𝘁 𝗥𝗮𝘁𝗶𝗼", callback_data="set_ar_original")],
    [InlineKeyboardButton("𝟵:𝟭𝟲 (𝗖𝗿𝗼𝗽/𝗙𝗶𝘁)", callback_data="set_ar_9_16")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_settings")]
])

def get_platform_selection_markup(user_id, current_selection=None):
    if current_selection is None:
        current_selection = {}
    buttons = []
    for platform in PREMIUM_PLATFORMS:
        emoji = "✅" if current_selection.get(platform) else "⬜"
        buttons.append([InlineKeyboardButton(f"{emoji} {platform.capitalize()}", callback_data=f"select_platform_{platform}")])
    buttons.append([InlineKeyboardButton("➡️ 𝗖𝗼𝗻𝘁𝗶𝗻𝘂𝗲 𝘁𝗼 𝗣𝗹𝗮𝗻𝘀", callback_data="confirm_platform_selection")])
    buttons.append([InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝘁𝗼 𝗔𝗱𝗺𝗶𝗻", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_premium_plan_markup(selected_platforms):
    buttons = []
    for key, value in PREMIUM_PLANS.items():
        buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()}", callback_data=f"show_plan_details_{key}")])
    buttons.append([InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_main_menu")])
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
            
    buttons.append([InlineKeyboardButton(f"💰 𝗕𝗨𝗬 𝗡𝗢𝗪 ({price_string})", callback_data=f"buy_now")])
    buttons.append([InlineKeyboardButton("➡️ 𝗖𝗵𝗲𝗰𝗸 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗠𝗲𝘁𝗵𝗼𝗱𝘀", callback_data="show_payment_methods")])
    buttons.append([InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝘁𝗼 𝗣𝗹𝗮𝗻𝘀", callback_data="buypypremium")])
    return InlineKeyboardMarkup(buttons)


def get_payment_methods_markup():
    payment_buttons = []
    settings = global_settings.get("payment_settings", {})
    if settings.get("google_play_qr_file_id"):
        payment_buttons.append([InlineKeyboardButton("𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗹𝗮𝘆 𝗤𝗥 𝗖𝗼𝗱𝗲", callback_data="show_payment_qr_google_play")])
    if settings.get("upi"):
        payment_buttons.append([InlineKeyboardButton("𝗨𝗣𝗜", callback_data="show_payment_details_upi")])
    if settings.get("ust"):
        payment_buttons.append([InlineKeyboardButton("𝗨𝗦𝗧", callback_data="show_payment_details_ust")])
    if settings.get("btc"):
        payment_buttons.append([InlineKeyboardButton("𝗕𝗧𝗖", callback_data="show_payment_details_btc")])
    if settings.get("others"):
        payment_buttons.append([InlineKeyboardButton("𝗢𝘁𝗵𝗲𝗿 𝗠𝗲𝘁𝗵𝗼𝗱𝘀", callback_data="show_payment_details_others")])

    payment_buttons.append([InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝘁𝗼 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗣𝗹𝗮𝗻𝘀", callback_data="buypypremium")])
    return InlineKeyboardMarkup(payment_buttons)


def get_upload_buttons(user_id):
    buttons = [
        [InlineKeyboardButton("➡️ 𝗨𝘀𝗲 𝗱𝗲𝗳𝗮𝘂𝗹𝘁 𝗰𝗮𝗽𝘁𝗶𝗼𝗻", callback_data="skip_caption")],
        [InlineKeyboardButton("❌ 𝗖𝗮𝗻𝗰𝗲𝗹 𝗨𝗽𝗹𝗼𝗮𝗱", callback_data="cancel_upload")],
    ]
    return InlineKeyboardMarkup(buttons)

def get_progress_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 𝗖𝗮𝗻𝗰𝗲𝗹", callback_data="cancel_upload")]
    ])

def get_caption_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 𝗦𝗸𝗶𝗽 (𝘂𝘀𝗲 𝗱𝗲𝗳𝗮𝘂𝗹𝘁)", callback_data="skip_caption")],
        [InlineKeyboardButton("❌ 𝗖𝗮𝗻𝗰𝗲𝗹", callback_data="cancel_upload")]
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
        "🔄 𝗕𝗼𝘁 𝗥𝗲𝘀𝘁𝗮𝗿𝘁 𝗜𝗻𝗶𝘁𝗶𝗮𝘁𝗲𝗱!\n\n"
        f"📅 𝗗𝗮𝘁𝗲: {dt['date']}\n"
        f"⏰ 𝗧𝗶𝗺𝗲: {dt['time']}\n"
        f"🌐 𝗧𝗶𝗺𝗲𝘇𝗼𝗻𝗲: {dt['timezone']}\n"
        f"👤 𝗕𝘆: {msg.from_user.mention} (𝗜𝗗: {msg.from_user.id})"
    )
    logger.info(f"User {msg.from_user.id} attempting restart command.")
    await send_log_to_channel(app, LOG_CHANNEL, restart_msg_log)
    await msg.reply("✅ 𝗕𝗼𝘁 𝗶𝘀 𝗿𝗲𝘀𝘁𝗮𝗿𝘁𝗶𝗻𝗴...")
    await asyncio.sleep(2)
    try:
        logger.info("Executing os.execv to restart process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execute restart via os.execv: {e}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ 𝗥𝗲𝘀𝘁𝗮𝗿𝘁 𝗳𝗮𝗶𝗹𝗲𝗱 𝗳𝗼𝗿 {msg.from_user.id}: {str(e)}")
        await msg.reply(f"❌ 𝗙𝗮𝗶𝗹𝗲𝗱 𝘁𝗼 𝗿𝗲𝘀𝘁𝗮𝗿𝘁 𝗯𝗼𝘁: {str(e)}")

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

def progress_callback(current, total, ud_type, msg, start_time):
    percentage = current * 100 / total
    speed = current / (time.time() - start_time)
    elapsed_time = time.time() - start_time
    eta = (total - current) / speed
    
    progress_bar = f"[{'█' * int(percentage / 5)}{' ' * (20 - int(percentage / 5))}]"
    
    progress_text = (
        f"{ud_type} 𝗽𝗿𝗼𝗴𝗿𝗲𝘀𝘀: `{progress_bar}`\n"
        f"📊 𝗣𝗲𝗿𝗰𝗲𝗻𝘁𝗮𝗴𝗲: `{percentage:.2f}%`\n"
        f"✅ 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱𝗲𝗱: `{current / (1024 * 1024):.2f}` 𝗠𝗕\n"
        f"📦 𝗧𝗼𝘁𝗮𝗹 𝘀𝗶𝘇𝗲: `{total / (1024 * 1024):.2f}` 𝗠𝗕\n"
        f"🚀 𝗦𝗽𝗲𝗲𝗱: `{speed / (1024 * 1024):.2f}` 𝗠𝗕/𝘀\n"
        f"⏳ 𝗘𝗧𝗔: `{timedelta(seconds=eta)}`"
    )
    
    if int(percentage) % 5 == 0 and not msg.is_progress_message_updated:
        try:
            asyncio.run(safe_edit_message(msg, progress_text, reply_markup=get_progress_markup(), parse_mode=enums.ParseMode.MARKDOWN))
            msg.is_progress_message_updated = True
        except:
            pass
    elif int(percentage) % 5 != 0:
        msg.is_progress_message_updated = False

def cleanup_temp_files(files_to_delete):
    for file_path in files_to_delete:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"𝗗𝗲𝗹𝗲𝘁𝗲𝗱 𝗹𝗼𝗰𝗮𝗹 𝗳𝗶𝗹𝗲: {file_path}")
            except Exception as e:
                logger.error(f"𝗘𝗿𝗿𝗼𝗿 𝗱𝗲𝗹𝗲𝘁𝗶𝗻𝗴 𝗳𝗶𝗹𝗲 {file_path}: {e}")

def with_user_lock(func):
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        if user_id not in user_upload_locks:
            user_upload_locks[user_id] = asyncio.Lock()

        if user_upload_locks[user_id].locked():
            return await message.reply("⚠️ 𝗔𝗻𝗼𝘁𝗵𝗲𝗿 𝗼𝗽𝗲𝗿𝗮𝘁𝗶𝗼𝗻 𝗶𝘀 𝗮𝗹𝗿𝗲𝗮𝗱𝘆 𝗶𝗻 𝗽𝗿𝗼𝗴𝗿𝗲𝘀𝘀. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘄𝗮𝗶𝘁 𝘂𝗻𝘁𝗶𝗹 𝗶𝘁'𝘀 𝗳𝗶𝗻𝗶𝘀𝗵𝗲𝗱 𝗼𝗿 𝘂𝘀𝗲 𝘁𝗵𝗲 `❌ 𝗖𝗮𝗻𝗰𝗲𝗹` 𝗯𝘂𝘁𝘁𝗼𝗻.")

        async with user_upload_locks[user_id]:
            return await func(client, message, *args, **kwargs)
    return wrapper

# --- Message Handlers ---

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    if is_admin(user_id):
        welcome_msg = "🤖 **𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗨𝗣𝗟𝗢𝗔𝗗 𝗕𝗢𝗧!**\n\n"
        welcome_msg += "🛠️ 𝗬𝗢𝗨 𝗛𝗔𝗩𝗘 **𝗔𝗗𝗠𝗜𝗡 𝗣𝗥𝗜𝗩𝗜𝗟𝗘𝗚𝗘𝗦**."
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = _get_user_data(user_id)
    is_new_user = not user
    if is_new_user:
        _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 𝗡𝗲𝘄 𝘂𝘀𝗲𝗿 𝘀𝘁𝗮𝗿𝘁𝗲𝗱 𝗯𝗼𝘁: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")
        
        welcome_msg = (
            f"👋 **𝗛𝗜 {user_first_name}!**\n\n"
            "𝗧𝗛𝗜𝗦 𝗕𝗢𝗧 𝗟𝗘𝗧𝗦 𝗬𝗢𝗨 𝗨𝗣𝗟𝗢𝗔𝗗 𝗔𝗡𝗬 𝗦𝗜𝗭𝗘 𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗥𝗘𝗘𝗟𝗦 & 𝗣𝗢𝗦𝗧𝗦 𝗗𝗜𝗥𝗘𝗖𝗧𝗟𝗬 𝗙𝗥𝗢𝗠 𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠.\n\n"
            "𝗧𝗢 𝗚𝗘𝗧 𝗔 𝗧𝗔𝗦𝗧𝗘 𝗢𝗙 𝗧𝗛𝗘 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦, 𝗬𝗢𝗨 𝗖𝗔𝗡 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘 𝗔 **𝗙𝗥𝗘𝗘 𝟯-𝗛𝗢𝗨𝗥 𝗧𝗥𝗜𝗔𝗟** 𝗙𝗢𝗥 𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗥𝗜𝗚𝗛𝗧 𝗡𝗢𝗪!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲 𝗙𝗿𝗲𝗲 𝟯-𝗛𝗼𝘂𝗿", callback_data="activate_trial")],
            [InlineKeyboardButton("➡️ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺", callback_data="buypypremium")]
        ])
        await msg.reply(welcome_msg, reply_markup=trial_markup, parse_mode=enums.ParseMode.MARKDOWN)
        return
    else:
        _save_user_data(user_id, {"last_active": datetime.utcnow()})

    onam_toggle = global_settings.get("onam_toggle", False)
    if onam_toggle:
        onam_text = (
            f"🎉 **𝗛𝗔𝗣𝗣𝗬 𝗢𝗡𝗔𝗠!** 🎉\n\n"
            f"𝗪𝗜𝗦𝗛𝗜𝗡𝗚 𝗬𝗢𝗨 𝗔 𝗦𝗘𝗔𝗦𝗢𝗡 𝗢𝗙 𝗣𝗥𝗢𝗦𝗣𝗘𝗥𝗜𝗧𝗬 𝗔𝗡𝗗 𝗛𝗔𝗣𝗣𝗜𝗡𝗘𝗦𝗦. 𝗘𝗡𝗝𝗢𝗬 𝗧𝗛𝗘 𝗙𝗘𝗦𝗧𝗜𝗩𝗜𝗧𝗜𝗘𝗦 𝗪𝗜𝗧𝗛 𝗢𝗨𝗥 𝗘𝗫𝗖𝗟𝗨𝗦𝗜𝗩𝗘 **𝗢𝗡𝗔𝗠 𝗥𝗘𝗘𝗟 𝗨𝗣𝗟𝗢𝗔𝗗𝗦** 𝗙𝗘𝗔𝗧𝗨𝗥𝗘!\n\n"
            f"𝗨𝗦𝗘 𝗧𝗛𝗘 𝗕𝗨𝗧𝗧𝗢𝗡𝗦 𝗕𝗘𝗟𝗢𝗪 𝗧𝗢 𝗦𝗧𝗔𝗥𝗧 𝗨𝗣𝗟𝗢𝗔𝗗𝗜𝗡𝗚 𝗬𝗢𝗨𝗥 𝗙𝗘𝗦𝗧𝗜𝗩𝗔𝗟 𝗖𝗢𝗡𝗧𝗘𝗡𝗧!"
        )
        await msg.reply(onam_text, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user_premium = _get_user_data(user_id).get("premium", {})
    instagram_premium_data = user_premium.get("instagram", {})

    welcome_msg = f"🚀 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 𝗧𝗢 𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 ➜ 𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗗𝗜𝗥𝗘𝗖𝗧 𝗨𝗣𝗟𝗢𝗔𝗗𝗘𝗥\n\n"
    premium_details_text = ""
    is_admin_user = is_admin(user_id)
    if is_admin_user:
        premium_details_text += "🛠️ 𝗬𝗢𝗨 𝗛𝗔𝗩𝗘 **𝗔𝗗𝗠𝗜𝗡 𝗣𝗥𝗜𝗩𝗜𝗟𝗘𝗚𝗘𝗦**.\n\n"

    ig_premium_until = instagram_premium_data.get("until")

    if is_premium_for_platform(user_id, "instagram"):
        if ig_premium_until:
            remaining_time = ig_premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗲𝘅𝗽𝗶𝗿𝗲𝘀 𝗶𝗻: `{days} days, {hours} hours`.\n"
    
    if not is_admin_user and not premium_details_text.strip():
        premium_details_text = (
            "🔥 **𝗞𝗘𝗬 𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦:**\n"
            "✅ 𝗗𝗶𝗿𝗲𝗰𝘁 𝗹𝗼𝗴𝗶𝗻 (𝗻𝗼 𝘁𝗼𝗸𝗲𝗻𝘀 𝗻𝗲𝗲𝗱𝗲𝗱)\n"
            "✅ 𝗨𝗹𝘁𝗿𝗮-𝗳𝗮𝘀𝘁 𝘂𝗽𝗹𝗼𝗮𝗱𝗶𝗻𝗴\n"
            "✅ 𝗛𝗶𝗴𝗵 𝗤𝘂𝗮𝗹𝗶𝘁𝘆 / 𝗙𝗮𝘀𝘁 𝗰𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻\n"
            "✅ 𝗡𝗼 𝗳𝗶𝗹𝗲 𝘀𝗶𝘇𝗲 𝗹𝗶𝗺𝗶𝘁\n"
            "✅ 𝗨𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 𝘂𝗽𝗹𝗼𝗮𝗱𝘀\n"
            "✅ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝘀𝘂𝗽𝗽𝗼𝗿𝘁\n"
            "✅ 𝗔𝘂𝘁𝗼 𝗱𝗲𝗹𝗲𝘁𝗲 𝗮𝗳𝘁𝗲𝗿 𝘂𝗽𝗹𝗼𝗮𝗱 (𝗼𝗽𝘁𝗶𝗼𝗻𝗮𝗹)\n\n"
            "👤 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗔𝗱𝗺𝗶𝗻 𝗧𝗼𝗺 → [𝗖𝗟𝗜𝗖𝗞 𝗛𝗘𝗥𝗘](t.me/CjjTom) 𝗧𝗢 𝗚𝗘𝗧 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗡𝗢𝗪\n"
            "🔐 𝗬𝗢𝗨𝗥 𝗗𝗔𝗧𝗔 𝗜𝗦 𝗙𝗨𝗟𝗟𝗬 ✅ 𝗘𝗡𝗗 𝗧𝗢 𝗘𝗡𝗗 𝗘𝗡𝗖𝗥𝗬𝗣𝗧𝗘𝗗\n\n"
            f"🆔 𝗬𝗼𝘂𝗿 𝗜𝗗: `{user_id}`"
        )
    
    welcome_msg += premium_details_text
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱.")
    restarting_msg = await msg.reply("♻️ 𝗥𝗲𝘀𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗯𝗼𝘁...")
    await asyncio.sleep(1)
    await restart_bot(msg)

@app.on_message(filters.command("login"))
@with_user_lock
async def login_cmd(_, msg):
    logger.info(f"User {msg.from_user.id} attempting Instagram login command.")
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ 𝗡𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝗽𝗴𝗿𝗮𝗱𝗲 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝘄𝗶𝘁𝗵 /buypypremium.")
    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("𝗨𝘀𝗮𝗴𝗲: `/login <instagram_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)
    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 𝗔𝘁𝘁𝗲𝗺𝗽𝘁𝗶𝗻𝗴 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻...")
    try:
        user_insta_client = InstaClient()
        user_insta_client.delay_range = [1, 3]
        proxy_url = global_settings.get("proxy_url")
        if proxy_url:
            user_insta_client.set_proxy(proxy_url)
            logger.info(f"Applied global proxy {proxy_url} to user {user_id}'s Instagram login attempt.")
        elif INSTAGRAM_PROXY:
            user_insta_client.set_proxy(INSTAGRAM_PROXY)
            logger.info(f"Applied default proxy {INSTAGRAM_PROXY} to user {user_id}'s Instagram login attempt.")

        session = await load_instagram_session(user_id)
        if session:
            logger.info(f"Attempting to load existing Instagram session for user {user_id} (IG: {username}).")
            user_insta_client.set_settings(session)
            try:
                await asyncio.to_thread(user_insta_client.get_timeline_feed)
                await safe_edit_message(login_msg, f"✅ 𝗔𝗹𝗿𝗲𝗮𝗱𝘆 𝗹𝗼𝗴𝗴𝗲𝗱 𝗶𝗻 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗮𝘀 `{username}` (𝘀𝗲𝘀𝘀𝗶𝗼𝗻 𝗿𝗲𝗹𝗼𝗮𝗱𝗲𝗱).", parse_mode=enums.ParseMode.MARKDOWN)
                logger.info(f"Existing Instagram session for {user_id} is valid.")
                _save_user_data(user_id, {"instagram_username": username})
                return
            except LoginRequired:
                logger.info(f"Existing Instagram session for {user_id} expired. Attempting fresh login.")
                user_insta_client.set_settings({})

        logger.info(f"Attempting fresh Instagram login for user {user_id} with username: {username}")
        await asyncio.to_thread(user_insta_client.login, username, password)

        session_data = user_insta_client.get_settings()
        await save_instagram_session(user_id, session_data)
        _save_user_data(user_id, {"instagram_username": username})

        await safe_edit_message(login_msg, "✅ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻 𝘀𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹 !")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"📝 𝗡𝗲𝘄 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻\n𝗨𝘀𝗲𝗿: `{user_id}`\n"
            f"𝗨𝘀𝗲𝗿𝗻𝗮𝗺𝗲: `{msg.from_user.username or 'N/A'}`\n"
            f"𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺: `{username}`"
        )
        logger.info(f"Instagram login successful for user {user_id} ({username}).")

    except ChallengeRequired:
        await safe_edit_message(login_msg, "🔐 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝘀 𝗰𝗵𝗮𝗹𝗹𝗲𝗻𝗴𝗲 𝘃𝗲𝗿𝗶𝗳𝗶𝗰𝗮𝘁𝗶𝗼𝗻. 𝗣𝗹𝗲𝗮𝘀𝗲 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲 𝗶𝘁 𝗶𝗻 𝘁𝗵𝗲 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗮𝗽𝗽 𝗮𝗻𝗱 𝘁𝗿𝘆 𝗮𝗴𝗮𝗶𝗻.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗰𝗵𝗮𝗹𝗹𝗲𝗻𝗴𝗲 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 `{user_id}` (`{username}`).")
        logger.warning(f"Instagram Challenge Required for user {user_id} ({username}).")
    except (BadPassword, LoginRequired) as e:
        await safe_edit_message(login_msg, f"❌ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻 𝗳𝗮𝗶𝗹𝗲𝗱: {e}. 𝗣𝗹𝗲𝗮𝘀𝗲 𝗰𝗵𝗲𝗰𝗸 𝘆𝗼𝘂𝗿 𝗰𝗿𝗲𝗱𝗲𝗻𝘁𝗶𝗮𝗹𝘀.")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻 𝗳𝗮𝗶𝗹𝗲𝗱 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 `{user_id}` (`{username}`): {e}")
        logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
    except PleaseWaitFewMinutes:
        await safe_edit_message(login_msg, "⚠️ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗶𝘀 𝗮𝘀𝗸𝗶𝗻𝗴 𝘁𝗼 𝘄𝗮𝗶𝘁 𝗮 𝗳𝗲𝘄 𝗺𝗶𝗻𝘂𝘁𝗲𝘀 𝗯𝗲𝗳𝗼𝗿𝗲 𝘁𝗿𝘆𝗶𝗻𝗴 𝗮𝗴𝗮𝗶𝗻. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘁𝗿𝘆 𝗮𝗳𝘁𝗲𝗿 𝘀𝗼𝗺𝗲 𝘁𝗶𝗺𝗲.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 '𝗽𝗹𝗲𝗮𝘀𝗲 𝘄𝗮𝗶𝘁' 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 `{user_id}` (`{username}`).")
        logger.warning(f"Instagram 'Please Wait' for user {user_id} ({username}).")
    except Exception as e:
        await safe_edit_message(login_msg, f"❌ 𝗔𝗻 𝘂𝗻𝗲𝘅𝗽𝗲𝗰𝘁𝗲𝗱 𝗲𝗿𝗿𝗼𝗿 𝗼𝗰𝗰𝘂𝗿𝗿𝗲𝗱 𝗱𝘂𝗿𝗶𝗻𝗴 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻: {str(e)}")
        logger.error(f"𝗨𝗻𝗵𝗮𝗻𝗱𝗹𝗲𝗱 𝗲𝗿𝗿𝗼𝗿 𝗱𝘂𝗿𝗶𝗻𝗴 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻 𝗳𝗼𝗿 {user_id} ({username}): {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"🔥 𝗖𝗿𝗶𝘁𝗶𝗰𝗮𝗹 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻 𝗲𝗿𝗿𝗼𝗿 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.command("buypypremium"))
@app.on_message(filters.regex("⭐ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺"))
async def show_premium_options(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    premium_plans_text = (
        "⭐ **𝗨𝗣𝗚𝗥𝗔𝗗𝗘 𝗧𝗢 𝗣𝗥𝗘𝗠𝗜𝗨𝗠!** ⭐\n\n"
        "𝗨𝗻𝗹𝗼𝗰𝗸 𝗳𝘂𝗹𝗹 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀 𝗮𝗻𝗱 𝘂𝗽𝗹𝗼𝗮𝗱 𝘂𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 𝗰𝗼𝗻𝘁𝗲𝗻𝘁 𝘄𝗶𝘁𝗵𝗼𝘂𝘁 𝗿𝗲𝘀𝘁𝗿𝗶𝗰𝘁𝗶𝗼𝗻𝘀 𝗳𝗼𝗿 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺!\n\n"
        "**𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘 𝗣𝗟𝗔𝗡𝗦:**"
    )
    await msg.reply(premium_plans_text, reply_markup=get_premium_plan_markup([]), parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user = _get_user_data(user_id)
    if not user:
        return await msg.reply("𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗿𝗲𝗴𝗶𝘀𝘁𝗲𝗿𝗲𝗱 𝘄𝗶𝘁𝗵 𝘁𝗵𝗲 𝗯𝗼𝘁. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝘀𝗲 /start.")

    if is_admin(user_id):
        return await msg.reply("👑 𝗬𝗼𝘂 𝗮𝗿𝗲 𝘁𝗵𝗲 **𝗔𝗱𝗺𝗶𝗻**. 𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 𝗽𝗲𝗿𝗺𝗮𝗻𝗲𝗻𝘁 𝗳𝘂𝗹𝗹 𝗮𝗰𝗰𝗲𝘀𝘀 𝘁𝗼 𝗮𝗹𝗹 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀!", parse_mode=enums.ParseMode.MARKDOWN)

    status_text = "⭐ **𝗬𝗢𝗨𝗥 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗦𝗧𝗔𝗧𝗨𝗦:**\n\n"
    has_premium_any = False

    for platform in PREMIUM_PLATFORMS:
        platform_premium = user.get("premium", {}).get(platform, {})
        premium_type = platform_premium.get("type")
        premium_until = platform_premium.get("until")

        status_text += f"**{platform.capitalize()} 𝗣𝗿𝗲𝗺𝗶𝘂𝗺:** "
        if premium_type == "lifetime":
            status_text += "🎉 **𝗟𝗜𝗙𝗘𝗧𝗜𝗠𝗘!**\n"
            has_premium_any = True
        elif premium_until and premium_until > datetime.utcnow():
            remaining_time = premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            minutes = (remaining_time.seconds % 3600) // 60
            status_text += (
                f"`{premium_type.replace('_', ' ').title()}` 𝗲𝘅𝗽𝗶𝗿𝗲𝘀 𝗼𝗻: "
                f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')} 𝗨𝗧𝗖`\n"
                f"𝗧𝗶𝗺𝗲 𝗿𝗲𝗺𝗮𝗶𝗻𝗶𝗻𝗴: `{days} days, {hours} hours, {minutes} minutes`\n"
            )
            has_premium_any = True
        else:
            status_text += "😔 **𝗡𝗢𝗧 𝗔𝗖𝗧𝗜𝗩𝗘.**\n"
        status_text += "\n"

    if not has_premium_any:
        status_text = (
            "😔 **𝗬𝗢𝗨 𝗖𝗨𝗥𝗥𝗘𝗡𝗧𝗟𝗬 𝗛𝗔𝗩𝗘 𝗡𝗢 𝗔𝗖𝗧𝗜𝗩𝗘 𝗣𝗥𝗘𝗠𝗜𝗨𝗠.**\n\n"
            "𝗧𝗢 𝗨𝗡𝗟𝗢𝗖𝗞 𝗔𝗟𝗟 𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦, 𝗣𝗟𝗘𝗔𝗦𝗘 𝗖𝗢𝗡𝗧𝗔𝗖𝗧 **[𝗔𝗗𝗠𝗜𝗡 𝗧𝗢𝗠](https://t.me/CjjTom)** 𝗧𝗢 𝗕𝗨𝗬 𝗔 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗣𝗟𝗔𝗡."
        )

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("reset_profile"))
@with_user_lock
async def reset_profile_cmd(_, msg):
    user_id = msg.from_user.id
    await msg.reply("⚠️ **𝗪𝗔𝗥𝗡𝗜𝗡𝗚!** 𝗧𝗵𝗶𝘀 𝘄𝗶𝗹𝗹 𝗰𝗹𝗲𝗮𝗿 𝗮𝗹𝗹 𝘆𝗼𝘂𝗿 𝘀𝗮𝘃𝗲𝗱 𝘀𝗲𝘀𝘀𝗶𝗼𝗻𝘀 𝗮𝗻𝗱 𝘀𝗲𝘁𝘁𝗶𝗻𝗴𝘀. 𝗔𝗿𝗲 𝘆𝗼𝘂 𝘀𝘂𝗿𝗲 𝘆𝗼𝘂 𝘄𝗮𝗻𝘁 𝘁𝗼 𝗽𝗿𝗼𝗰𝗲𝗲𝗱?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 𝗬𝗘𝗦, 𝗥𝗘𝗦𝗘𝗧 𝗠𝗬 𝗣𝗥𝗢𝗙𝗜𝗟𝗘", callback_data="confirm_reset_profile")],
            [InlineKeyboardButton("❌ 𝗡𝗢, 𝗖𝗔𝗡𝗖𝗘𝗟", callback_data="back_to_main_menu")]
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
    
    user_states.pop(user_id, None)
    
    await query.answer("✅ 𝗬𝗢𝗨𝗥 𝗣𝗥𝗢𝗙𝗜𝗟𝗘 𝗛𝗔𝗦 𝗕𝗘𝗘𝗡 𝗥𝗘𝗦𝗘𝗧. 𝗣𝗟𝗘𝗔𝗦𝗘 𝗨𝗦𝗘 /start 𝗧𝗢 𝗕𝗘𝗚𝗜𝗡 𝗔𝗚𝗔𝗜𝗡.", show_alert=True)
    await safe_edit_message(query.message, "✅ 𝗬𝗢𝗨𝗥 𝗣𝗥𝗢𝗙𝗜𝗟𝗘 𝗛𝗔𝗦 𝗕𝗘𝗘𝗡 𝗥𝗘𝗦𝗘𝗧. 𝗣𝗟𝗘𝗔𝗦𝗘 𝗨𝗦𝗘 /start 𝗧𝗢 𝗕𝗘𝗚𝗜𝗡 𝗔𝗚𝗔𝗜𝗡.")

# Updated Settings Menu to show compression/proxy status
@app.on_message(filters.regex("⚙️ 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ 𝗡𝗢𝗧 𝗔𝗨𝗧𝗛𝗢𝗥𝗜𝗭𝗘𝗗. 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱 𝘁𝗼 𝗮𝗰𝗰𝗲𝘀𝘀 𝘀𝗲𝘁𝘁𝗶𝗻𝗴𝘀.")
    
    current_settings = await get_user_settings(user_id)
    compression_status = "𝗢𝗡 (𝗢𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗤𝘂𝗮𝗹𝗶𝘁𝘆)" if current_settings.get("no_compression") else "𝗢𝗙𝗙 (𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗲𝗻𝗮𝗯𝗹𝗲𝗱)"
    
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = "𝗡𝗼𝗻𝗲"
    if proxy_url:
        proxy_status_text = f"`{proxy_url}`"

    settings_text = "⚙️ 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀 𝗣𝗮𝗻𝗲𝗹\n\n" \
                    f"🗜️ 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗶𝘀 𝗰𝘂𝗿𝗿𝗲𝗻𝘁𝗹𝘆: **{compression_status}**\n" \
                    f"🌐 𝗕𝗼𝘁 𝗽𝗿𝗼𝘅𝘆 𝘀𝘁𝗮𝘁𝘂𝘀: {proxy_status_text}\n\n" \
                    "𝗨𝘀𝗲 𝘁𝗵𝗲 𝗯𝘂𝘁𝘁𝗼𝗻𝘀 𝗯𝗲𝗹𝗼𝘄 𝘁𝗼 𝗮𝗱𝗷𝘂𝘀𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗲𝗻𝗰𝗲𝘀."

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ 𝗨𝘀𝗲𝗿 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀", callback_data="user_settings_personal")]
        ])
    else:
        markup = user_settings_markup

    await msg.reply(settings_text, reply_markup=markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("📤 𝗜𝗻𝘀𝘁𝗮 𝗥𝗲𝗲𝗹"))
@with_user_lock
async def initiate_instagram_reel_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ 𝗬𝗼𝘂𝗿 𝗮𝗰𝗰𝗲𝘀𝘀 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗱𝗲𝗻𝗶𝗲𝗱. 𝗨𝗽𝗴𝗿𝗮𝗱𝗲 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝘁𝗼 𝘂𝗻𝗹𝗼𝗰𝗸 𝗿𝗲𝗲𝗹𝘀 𝘂𝗽𝗹𝗼𝗮𝗱. /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ 𝗣𝗹𝗲𝗮𝘀𝗲 𝗹𝗼𝗴𝗶𝗻 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗳𝗶𝗿𝘀𝘁 𝘂𝘀𝗶𝗻𝗴 `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝘃𝗶𝗱𝗲𝗼 𝗳𝗶𝗹𝗲 - 𝗥𝗲𝗲𝗹 𝗿𝗲𝗮𝗱𝘆!!")
    user_states[user_id] = {"action": "waiting_for_instagram_reel_video", "platform": "instagram", "upload_type": "reel"}

@app.on_message(filters.regex("📸 𝗜𝗻𝘀𝘁𝗮 𝗣𝗵𝗼𝘁𝗼"))
@with_user_lock
async def initiate_instagram_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("🚫 𝗡𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝘂𝗽𝗹𝗼𝗮𝗱 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗽𝗵𝗼𝘁𝗼𝘀 𝗽𝗹𝗲𝗮𝘀𝗲 𝘂𝗽𝗴𝗿𝗮𝗱𝗲 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ 𝗣𝗹𝗲𝗮𝘀𝗲 𝗹𝗼𝗴𝗶𝗻 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗳𝗶𝗿𝘀𝘁 𝘂𝘀𝗶𝗻𝗴 `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝗽𝗵𝗼𝘁𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗮𝗱𝘆 𝗳𝗼𝗿 𝗜𝗚!.")
    user_states[user_id] = {"action": "waiting_for_instagram_photo_image", "platform": "instagram", "upload_type": "post"}

@app.on_message(filters.regex("📊 𝗦𝘁𝗮𝘁𝘀"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLANS):
        return await msg.reply("❌ 𝗡𝗢𝗧 𝗔𝗨𝗧𝗛𝗢𝗥𝗜𝗭𝗘𝗗. 𝗬𝗼𝘂 𝗻𝗲𝗲𝗱 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝗰𝗲𝘀𝘀 𝗳𝗼𝗿 𝗮𝘁 𝗹𝗲𝗮𝘀𝘁 𝗼𝗻𝗲 𝗽𝗹𝗮𝘁𝗳𝗼𝗿𝗺 𝘁𝗼 𝘃𝗶𝗲𝘄 𝘀𝘁𝗮𝘁𝘀.")

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
        "📊 **𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀:**\n\n"
        f"**𝗨𝘀𝗲𝗿𝘀**\n"
        f"👥 𝗧𝗼𝘁𝗮𝗹 𝗨𝘀𝗲𝗿𝘀: `{total_users}`\n"
        f"👑 𝗔𝗱𝗺𝗶𝗻 𝗨𝘀𝗲𝗿𝘀: `{db.users.count_documents({'_id': ADMIN_ID})}`\n"
        f"⭐ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗨𝘀𝗲𝗿𝘀: `{total_premium_users}` (`{total_premium_users / total_users * 100:.2f}%`)\n"
        f"    - 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺: `{premium_counts['instagram']}` (`{premium_counts['instagram'] / total_users * 100:.2f}%`)\n"
    )

    stats_text += (
        f"\n**𝗨𝗽𝗹𝗼𝗮𝗱𝘀**\n"
        f"📈 𝗧𝗼𝘁𝗮𝗹 𝗨𝗽𝗹𝗼𝗮𝗱𝘀: `{total_uploads}`\n"
        f"🎬 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗥𝗲𝗲𝗹𝘀: `{total_instagram_reel_uploads}`\n"
        f"📸 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗼𝘀𝘁𝘀: `{total_instagram_post_uploads}`\n"
    )
    await msg.reply(stats_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_cmd(_, msg):
    if len(msg.text.split(maxsplit=1)) < 2:
        return await msg.reply("𝗨𝘀𝗮𝗴𝗲: `/broadcast <your message>`", parse_mode=enums.ParseMode.MARKDOWN)
    broadcast_message = msg.text.split(maxsplit=1)[1]
    users = db.users.find({})
    sent_count = 0
    failed_count = 0
    status_msg = await msg.reply("📢 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗯𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁...")
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
    await status_msg.edit_text(f"✅ 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗳𝗶𝗻𝗶𝘀𝗵𝗲𝗱!\n𝗦𝗲𝗻𝘁 𝘁𝗼 `{sent_count}` 𝘂𝘀𝗲𝗿𝘀, 𝗳𝗮𝗶𝗹𝗲𝗱 𝗳𝗼𝗿 `{failed_count}` 𝘂𝘀𝗲𝗿𝘀.")
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗶𝗻𝗶𝘁𝗶𝗮𝘁𝗲𝗱 𝗯𝘆 𝗔𝗱𝗺𝗶𝗻 `{msg.from_user.id}`\n"
        f"𝗦𝗲𝗻𝘁: `{sent_count}`, 𝗙𝗮𝗶𝗹𝗲𝗱: `{failed_count}`"
    )

@app.on_message(filters.text & filters.private & ~filters.command(""))
@with_user_lock
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not state_data:
        return

    action = state_data.get("action")
    
    if action == "waiting_for_caption":
        caption = msg.text
        settings = await get_user_settings(user_id)
        settings["caption"] = caption
        await save_user_settings(user_id, settings)
        await safe_edit_message(msg.reply_to_message, f"✅ 𝗖𝗮𝗽𝘁𝗶𝗼𝗻 𝘀𝗲𝘁 𝘁𝗼: `{caption}`", reply_markup=user_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)

    elif action == "waiting_for_hashtags":
        hashtags = msg.text
        settings = await get_user_settings(user_id)
        settings["hashtags"] = hashtags
        await save_user_settings(user_id, settings)
        await safe_edit_message(msg.reply_to_message, f"✅ 𝗛𝗮𝘀𝗵𝘁𝗮𝗴𝘀 𝘀𝗲𝘁 𝘁𝗼: `{hashtags}`", reply_markup=user_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    
    elif action.startswith("waiting_for_payment_details_"):
        if not is_admin(user_id):
            return await msg.reply("❌ 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝗽𝗲𝗿𝗳𝗼𝗿𝗺 𝘁𝗵𝗶𝘀 𝗮𝗰𝘁𝗶𝗼𝗻.")
        
        payment_method = action.replace("waiting_for_payment_details_", "")
        details = msg.text
        
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings[payment_method] = details
        _update_global_setting("payment_settings", new_payment_settings)
        
        await msg.reply(f"✅ 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗱𝗲𝘁𝗮𝗶𝗹𝘀 𝗳𝗼𝗿 **{payment_method.upper()}** 𝘂𝗽𝗱𝗮𝘁𝗲𝗱.", reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)

    elif action.startswith("waiting_for_google_play_qr"):
        if not is_admin(user_id):
            return await msg.reply("❌ 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝗽𝗲𝗿𝗳𝗼𝗿𝗺 𝘁𝗵𝗶𝘀 𝗮𝗰𝘁𝗶𝗼𝗻.")
        
        await msg.reply("❌ 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝗮𝗻 𝗶𝗺𝗮𝗴𝗲 𝗳𝗶𝗹𝗲 𝗰𝗼𝗻𝘁𝗮𝗶𝗻𝗶𝗻𝗴 𝘁𝗵𝗲 𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗮𝘆 𝗤𝗥 𝗰𝗼𝗱𝗲.")
        user_states.pop(user_id, None)
    
    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝗽𝗲𝗿𝗳𝗼𝗿𝗺 𝘁𝗵𝗶𝘀 𝗮𝗰𝘁𝗶𝗼𝗻.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ 𝗨𝘀𝗲𝗿 𝗜𝗗 `{target_user_id}` 𝗿𝗲𝗰𝗲𝗶𝘃𝗲𝗱. 𝗦𝗲𝗹𝗲𝗰𝘁 𝗽𝗹𝗮𝘁𝗳𝗼𝗿𝗺𝘀 𝗳𝗼𝗿 𝗽𝗿𝗲𝗺𝗶𝘂𝗺:",
                reply_markup=get_platform_selection_markup(user_id, user_states[user_id]["selected_platforms"]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝘂𝘀𝗲𝗿 𝗜𝗗. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝗮 𝘃𝗮𝗹𝗶𝗱 𝗻𝘂𝗺𝗯𝗲𝗿.")
            user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_max_uploads":
        if not is_admin(user_id):
            return await msg.reply("❌ 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝗽𝗲𝗿𝗳𝗼𝗿𝗺 𝘁𝗵𝗶𝘀 𝗮𝗰𝘁𝗶𝗼𝗻.")
        try:
            new_limit = int(msg.text)
            if new_limit <= 0:
                return await msg.reply("❌ 𝗧𝗵𝗲 𝗹𝗶𝗺𝗶𝘁 𝗺𝘂𝘀𝘁 𝗯𝗲 𝗮 𝗽𝗼𝘀𝗶𝘁𝗶𝘃𝗲 𝗶𝗻𝘁𝗲𝗴𝗲𝗿.")
            _update_global_setting("max_concurrent_uploads", new_limit)
            global upload_semaphore
            upload_semaphore = asyncio.Semaphore(new_limit)
            await msg.reply(f"✅ 𝗠𝗮𝘅𝗶𝗺𝘂𝗺 𝗰𝗼𝗻𝗰𝘂𝗿𝗿𝗲𝗻𝘁 𝘂𝗽𝗹𝗼𝗮𝗱𝘀 𝘀𝗲𝘁 𝘁𝗼 `{new_limit}`.", reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
            user_states.pop(user_id, None)
        except ValueError:
            await msg.reply("❌ 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗶𝗻𝗽𝘂𝘁. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝗮 𝘃𝗮𝗹𝗶𝗱 𝗻𝘂𝗺𝗯𝗲𝗿.")
            user_states.pop(user_id, None)
    
    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_proxy_url":
        if not is_admin(user_id):
            return await msg.reply("❌ 𝗬𝗼𝘂 𝗮𝗿𝗲 𝗻𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝗽𝗲𝗿𝗳𝗼𝗿𝗺 𝘁𝗵𝗶𝘀 𝗮𝗰𝘁𝗶𝗼𝗻.")
        proxy_url = msg.text
        if proxy_url.lower() == "none" or proxy_url.lower() == "remove":
            _update_global_setting("proxy_url", "")
            await msg.reply("✅ 𝗕𝗼𝘁 𝗽𝗿𝗼𝘅𝘆 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗿𝗲𝗺𝗼𝘃𝗲𝗱.")
            logger.info(f"Admin {user_id} removed the global proxy.")
        else:
            _update_global_setting("proxy_url", proxy_url)
            await msg.reply(f"✅ 𝗕𝗼𝘁 𝗽𝗿𝗼𝘅𝘆 𝘀𝗲𝘁 𝘁𝗼: `{proxy_url}`.")
            logger.info(f"Admin {user_id} set the global proxy to: {proxy_url}")
        user_states.pop(user_id, None)
        await safe_edit_message(msg.reply_to_message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_global_settings_markup)

    elif isinstance(state_data, dict) and state_data.get("action") == "awaiting_post_title":
        caption = msg.text
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = caption
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await start_upload_task(msg, file_info)
    
    else:
        await msg.reply("𝗜 𝗱𝗼𝗻'𝘁 𝘂𝗻𝗱𝗲𝗿𝘀𝘁𝗮𝗻𝗱 𝘁𝗵𝗮𝘁 𝗰𝗼𝗺𝗺𝗮𝗻𝗱. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝘀𝗲 𝘁𝗵𝗲 𝗺𝗲𝗻𝘂 𝗯𝘂𝘁𝘁𝗼𝗻𝘀 𝘁𝗼 𝗶𝗻𝘁𝗲𝗿𝗮𝗰𝘁 𝘄𝗶𝘁𝗵 𝗺𝗲.")

@app.on_callback_query(filters.regex("^activate_trial$"))
async def activate_trial_cb(_, query):
    user_id = query.from_user.id
    user = _get_user_data(user_id)
    user_first_name = query.from_user.first_name or "there"

    if user and is_premium_for_platform(user_id, "instagram"):
        await query.answer("𝗬𝗼𝘂𝗿 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝘁𝗿𝗶𝗮𝗹 𝗶𝘀 𝗮𝗹𝗿𝗲𝗮𝗱𝘆 𝗮𝗰𝘁𝗶𝘃𝗲! 𝗘𝗻𝗷𝗼𝘆 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝗰𝗲𝘀𝘀.", show_alert=True)
        welcome_msg = f"🤖 **𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝗯𝗮𝗰𝗸, {user_first_name}!**\n\n"
        premium_details_text = ""
        user_premium = user.get("premium", {})
        ig_expiry = user_premium.get("instagram", {}).get("until")
        if ig_expiry:
            remaining_time = ig_expiry - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗲𝘅𝗽𝗶𝗿𝗲𝘀 𝗶𝗻: `{days} days, {hours} hours`.\n"
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
    await send_log_to_channel(app, LOG_CHANNEL, f"✨ 𝗨𝘀𝗲𝗿 `{user_id}` 𝗮𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱 𝗮 3-𝗵𝗼𝘂𝗿 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝘁𝗿𝗶𝗮𝗹.")

    await query.answer("✅ 𝗙𝗥𝗘𝗘 𝟯-𝗛𝗢𝗨𝗥 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝘁𝗿𝗶𝗮𝗹 𝗮𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱! 𝗘𝗻𝗷𝗼𝘆!", show_alert=True)
    welcome_msg = (
        f"🎉 **𝗖𝗼𝗻𝗴𝗿𝗮𝘁𝘂𝗹𝗮𝘁𝗶𝗼𝗻𝘀, {user_first_name}!**\n\n"
        f"𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 𝗮𝗰𝘁𝗶𝘃𝗮𝘁𝗲𝗱 𝘆𝗼𝘂𝗿 **𝟯-𝗵𝗼𝘂𝗿 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝘁𝗿𝗶𝗮𝗹** 𝗳𝗼𝗿 **𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺**.\n\n"
        "𝗬𝗼𝘂 𝗻𝗼𝘄 𝗵𝗮𝘃𝗲 𝗮𝗰𝗰𝗲𝘀𝘀 𝘁𝗼 𝘂𝗽𝗹𝗼𝗮𝗱 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗰𝗼𝗻𝘁𝗲𝗻𝘁!\n\n"
        "𝗧𝗼 𝗴𝗲𝘁 𝘀𝘁𝗮𝗿𝘁𝗲𝗱, 𝗽𝗹𝗲𝗮𝘀𝗲 𝗹𝗼𝗴 𝗶𝗻 𝘁𝗼 𝘆𝗼𝘂𝗿 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗮𝗰𝗰𝗼𝘂𝗻𝘁 𝘄𝗶𝘁𝗵:\n"
        "`/login <your_username> <your_password>`\n\n"
        "𝗪𝗮𝗻𝘁 𝗺𝗼𝗿𝗲 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀 𝗮𝗳𝘁𝗲𝗿 𝘁𝗵𝗲 𝘁𝗿𝗶𝗮𝗹 𝗲𝗻𝗱𝘀? 𝗖𝗵𝗲𝗰𝗸 𝗼𝘂𝘁 𝗼𝘂𝗿 𝗽𝗮𝗶𝗱 𝗽𝗹𝗮𝗻𝘀 𝘄𝗶𝘁𝗵 /buypypremium."
    )
    await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buypypremium$"))
async def buypypremium_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    premium_text = (
        "⭐ **𝗨𝗣𝗚𝗥𝗔𝗗𝗘 𝗧𝗢 𝗣𝗥𝗘𝗠𝗜𝗨𝗠!** ⭐\n\n"
        "𝗨𝗻𝗹𝗼𝗰𝗸 𝗳𝘂𝗹𝗹 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀 𝗮𝗻𝗱 𝘂𝗻𝗹𝗶𝗺𝗶𝘁𝗲𝗱 𝗰𝗼𝗻𝘁𝗲𝗻𝘁 𝘄𝗶𝘁𝗵𝗼𝘂𝘁 𝗿𝗲𝘀𝘁𝗿𝗶𝗰𝘁𝗶𝗼𝗻𝘀 𝗳𝗼𝗿 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺!\n\n"
        "**𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘 𝗣𝗟𝗔𝗡𝗦:**"
    )
    await safe_edit_message(query.message, premium_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_plan_details_"))
async def show_plan_details_cb(_, query):
    user_id = query.from_user.id
    plan_key = query.data.split("show_plan_details_")[1]
    
    price_multiplier = 1 
    
    plan_details = PREMIUM_PLANS[plan_key]
    
    plan_text = (
        f"**{plan_key.replace('_', ' ').title()} 𝗣𝗹𝗮𝗻 𝗗𝗲𝘁𝗮𝗶𝗹𝘀**\n\n"
        f"**𝗗𝘂𝗿𝗮𝘁𝗶𝗼𝗻**: "
    )
    if plan_details['duration']:
        plan_text += f"{plan_details['duration'].days} 𝗱𝗮𝘆𝘀\n"
    else:
        plan_text += "𝗟𝗶𝗳𝗲𝘁𝗶𝗺𝗲\n"
    
    price_string = plan_details['price']
    if '₹' in price_string:
        try:
            base_price = float(price_string.replace('₹', '').split('/')[0].strip())
            calculated_price = base_price * price_multiplier
            price_string = f"₹{int(calculated_price)} / {round(calculated_price * 0.012, 2)}$"
        except ValueError:
            pass

    plan_text += f"**𝗣𝗿𝗶𝗰𝗲**: {price_string}\n\n"
    plan_text += "𝗧𝗼 𝗽𝘂𝗿𝗰𝗵𝗮𝘀𝗲, 𝗰𝗹𝗶𝗰𝗸 '𝗕𝘂𝘆 𝗡𝗼𝘄' 𝗼𝗿 𝗰𝗵𝗲𝗰𝗸 𝘁𝗵𝗲 𝗮𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗽𝗮𝘆𝗺𝗲𝗻𝘁 𝗺𝗲𝘁𝗵𝗼𝗱𝘀."

    await safe_edit_message(query.message, plan_text, reply_markup=get_premium_details_markup(plan_key, price_multiplier), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_methods$"))
async def show_payment_methods_cb(_, query):
    user_id = query.from_user.id
    
    payment_methods_text = "**𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘 𝗣𝗔𝗬𝗠𝗘𝗡𝗧 𝗠𝗘𝗧𝗛𝗢𝗗𝗦**\n\n"
    payment_methods_text += "𝗖𝗵𝗼𝗼𝘀𝗲 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗺𝗲𝘁𝗵𝗼𝗱 𝘁𝗼 𝗽𝗿𝗼𝗰𝗲𝗲𝗱 𝘄𝗶𝘁𝗵 𝗽𝗮𝘆𝗺𝗲𝗻𝘁."
    
    await safe_edit_message(query.message, payment_methods_text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_qr_google_play$"))
async def show_payment_qr_google_play_cb(_, query):
    user_id = query.from_user.id
    qr_file_id = global_settings.get("payment_settings", {}).get("google_play_qr_file_id")

    if not qr_file_id:
        await query.answer("𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗮𝘆 𝗤𝗥 𝗰𝗼𝗱𝗲 𝗶𝘀 𝗻𝗼𝘁 𝘀𝗲𝘁 𝗯𝘆 𝘁𝗵𝗲 𝗮𝗱𝗺𝗶𝗻 𝘆𝗲𝘁.", show_alert=True)
        return
    
    await query.message.reply_photo(
        photo=qr_file_id,
        caption="**𝗦𝗰𝗮𝗻 & 𝗣𝗮𝘆 𝘂𝘀𝗶𝗻𝗴 𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗮𝘆**\n\n"
                "𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝗮 𝘀𝗰𝗿𝗲𝗲𝗻𝘀𝗵𝗼𝘁 𝗼𝗳 𝘁𝗵𝗲 𝗽𝗮𝘆𝗺𝗲𝗻𝘁 𝘁𝗼 **[𝗔𝗱𝗺𝗶𝗻 𝗧𝗼𝗺](https://t.me/CjjTom)** 𝗳𝗼𝗿 𝗮𝗰𝘁𝗶𝘃𝗮𝘁𝗶𝗼𝗻.",
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=get_payment_methods_markup()
    )
    await safe_edit_message(query.message, "𝗖𝗵𝗼𝗼𝘀𝗲 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗺𝗲𝘁𝗵𝗼𝗱 𝘁𝗼 𝗽𝗿𝗼𝗰𝗲𝗲𝗱 𝘄𝗶𝘁𝗵 𝗽𝗮𝘆𝗺𝗲𝗻𝘁.", reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)
    
@app.on_callback_query(filters.regex("^show_payment_details_"))
async def show_payment_details_cb(_, query):
    user_id = query.from_user.id
    method = query.data.split("show_payment_details_")[1]
    
    payment_details = global_settings.get("payment_settings", {}).get(method, "𝗡𝗼 𝗱𝗲𝘁𝗮𝗶𝗹𝘀 𝗮𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲.")
    
    text = (
        f"**{method.upper()} 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗗𝗲𝘁𝗮𝗶𝗹𝘀**\n\n"
        f"{payment_details}\n\n"
        f"𝗣𝗹𝗲𝗮𝘀𝗲 𝗽𝗮𝘆 𝘁𝗵𝗲 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱 𝗮𝗺𝗼𝘂𝗻𝘁 𝗮𝗻𝗱 𝗰𝗼𝗻𝘁𝗮𝗰𝘁 **[𝗔𝗱𝗺𝗶𝗻 𝗧𝗼𝗺](https://t.me/CjjTom)** 𝘄𝗶𝘁𝗵 𝗮 𝘀𝗰𝗿𝗲𝗲𝗻𝘀𝗵𝗼𝘁 𝗼𝗳 𝘁𝗵𝗲 𝗽𝗮𝘆𝗺𝗲𝗻𝘁 𝗳𝗼𝗿 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝘁𝗶𝘃𝗮𝘁𝗶𝗼𝗻."
    )
    
    await safe_edit_message(query.message, text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buy_now"))
async def buy_now_cb(_, query):
    user_id = query.from_user.id
    text = (
        f"**𝗣𝘂𝗿𝗰𝗵𝗮𝘀𝗲 𝗖𝗼𝗻𝗳𝗶𝗿𝗺𝗮𝘁𝗶𝗼𝗻**\n\n"
        f"𝗣𝗹𝗲𝗮𝘀𝗲 𝗰𝗼𝗻𝘁𝗮𝗰𝘁 **[𝗔𝗱𝗺𝗶𝗻 𝗧𝗼𝗺](https://t.me/CjjTom)** 𝘁𝗼 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲 𝘁𝗵𝗲 𝗽𝗮𝘆𝗺𝗲𝗻𝘁 𝗽𝗿𝗼𝗰𝗲𝘀𝘀."
    )
    await safe_edit_message(query.message, text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^premiumdetails$"))
async def premium_details_cb(_, query):
    await query.message.reply("𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝘀𝗲 𝘁𝗵𝗲 `/premiumdetails` 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝗻𝘀𝘁𝗲𝗮𝗱 𝗼𝗳 𝘁𝗵𝗶𝘀 𝗯𝘂𝘁𝘁𝗼𝗻.")


@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def user_settings_personal_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if is_admin(user_id) or any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        current_settings = await get_user_settings(user_id)
        compression_status = "𝗢𝗡 (𝗢𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗤𝘂𝗮𝗹𝗶𝘁𝘆)" if current_settings.get("no_compression") else "𝗢𝗙𝗙 (𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗲𝗻𝗮𝗯𝗹𝗲𝗱)"
        settings_text = "⚙️ 𝗬𝗼𝘂𝗿 𝗽𝗲𝗿𝘀𝗼𝗻𝗮𝗹 𝘀𝗲𝘁𝘁𝗶𝗻𝗴𝘀\n\n" \
                        f"🗜️ 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗶𝘀 𝗰𝘂𝗿𝗿𝗲𝗻𝘁𝗹𝘆: **{compression_status}**\n\n" \
                        "𝗨𝘀𝗲 𝘁𝗵𝗲 𝗯𝘂𝘁𝘁𝗼𝗻𝘀 𝗯𝗲𝗹𝗼𝘄 𝘁𝗼 𝗮𝗱𝗷𝘂𝘀𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗲𝗻𝗰𝗲𝘀."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=user_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    else:
        await query.answer("❌ 𝗡𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱.", show_alert=True)
        return

@app.on_callback_query(filters.regex("^back_to_"))
async def back_to_cb(_, query):
    data = query.data
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user_states.pop(user_id, None)
    if data == "back_to_main_menu":
        await query.message.delete()
        await app.send_message(
            query.message.chat.id,
            "🏠 𝗠𝗮𝗶𝗻 𝗠𝗲𝗻𝘂",
            reply_markup=get_main_keyboard(user_id)
        )
    elif data == "back_to_settings":
        current_settings = await get_user_settings(user_id)
        compression_status = "𝗢𝗡 (𝗢𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗤𝘂𝗮𝗹𝗶𝘁𝘆)" if current_settings.get("no_compression") else "𝗢𝗙𝗙 (𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗲𝗻𝗮𝗯𝗹𝗲𝗱)"
        settings_text = "⚙️ 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀 𝗣𝗮𝗻𝗲𝗹\n\n" \
                        f"🗜️ 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗶𝘀 𝗰𝘂𝗿𝗿𝗲𝗻𝘁𝗹𝘆: **{compression_status}**\n\n" \
                        "𝗨𝘀𝗲 𝘁𝗵𝗲 𝗯𝘂𝘁𝘁𝗼𝗻𝘀 𝗯𝗲𝗹𝗼𝘄 𝘁𝗼 𝗮𝗱𝗷𝘂𝘀𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗲𝗻𝗰𝗲𝘀."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=user_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "back_to_admin_from_stats" or data == "back_to_admin_from_global":
        await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
    elif data == "back_to_main_from_admin":
        await query.message.edit_text("🏠 𝗠𝗮𝗶𝗻 𝗠𝗲𝗻𝘂", reply_markup=get_main_keyboard(user_id))

# Removed user-facing compression toggle logic. This is now an admin-only feature.
@app.on_callback_query(filters.regex("^toggle_compression_admin$"))
async def toggle_compression_admin_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    
    current_status = global_settings.get("no_compression_admin", False)
    new_status = not current_status
    _update_global_setting("no_compression_admin", new_status)
    status_text = "𝗗𝗜𝗦𝗔𝗕𝗟𝗘𝗗" if new_status else "𝗘𝗡𝗔𝗕𝗟𝗘𝗗"
    
    await query.answer(f"𝗚𝗹𝗼𝗯𝗮𝗹 𝗰𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝘁𝗼𝗴𝗴𝗹𝗲𝗱 𝘁𝗼: {status_text}.", show_alert=True)

    onam_status = "𝗢𝗡" if global_settings.get("onam_toggle") else "𝗢𝗙𝗙"
    max_uploads = global_settings.get("max_concurrent_uploads")
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = f"`{proxy_url}`" if proxy_url else "𝗡𝗼𝗻𝗲"
    
    compression_status = "𝗗𝗜𝗦𝗔𝗕𝗟𝗘𝗗" if global_settings.get("no_compression_admin") else "𝗘𝗡𝗔𝗕𝗟𝗘𝗗"
    
    settings_text = (
        "⚙️ **𝗚𝗹𝗼𝗯𝗮𝗹 𝗕𝗼𝘁 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀**\n\n"
        f"**𝗢𝗻𝗮𝗺 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗘𝘃𝗲𝗻𝘁:** `{onam_status}`\n"
        f"**𝗠𝗮𝘅 𝗖𝗼𝗻𝗰𝘂𝗿𝗿𝗲𝗻𝘁 𝗨𝗽𝗹𝗼𝗮𝗱𝘀:** `{max_uploads}`\n"
        f"**𝗚𝗹𝗼𝗯𝗮𝗹 𝗣𝗿𝗼𝘅𝘆:** {proxy_status_text}\n"
        f"**𝗚𝗹𝗼𝗯𝗮𝗹 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻:** `{compression_status}`\n"
    )
    
    await safe_edit_message(query.message, settings_text, reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_callback_query(filters.regex("^(skip_caption|cancel_upload)$"))
async def handle_upload_actions(_, query):
    user_id = query.from_user.id
    action = query.data
    state_data = user_states.get(user_id)

    if not state_data or state_data.get("action") not in ["awaiting_post_title", "processing_upload", "uploading_file"]:
        await query.answer("❌ 𝗡𝗼 𝗮𝗰𝘁𝗶𝘃𝗲 𝘂𝗽𝗹𝗼𝗮𝗱 𝘁𝗼 𝗰𝗮𝗻𝗰𝗲𝗹 𝗼𝗿 𝘀𝗸𝗶𝗽.", show_alert=True)
        return

    if action == "cancel_upload":
        if user_id in upload_tasks and not upload_tasks[user_id].done():
            upload_tasks[user_id].cancel()
            await query.answer("❌ 𝗨𝗽𝗹𝗼𝗮𝗱 𝗰𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱.", show_alert=True)
            await safe_edit_message(query.message, "❌ 𝗨𝗽𝗹𝗼𝗮𝗱 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗰𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱.")
            user_states.pop(user_id, None)
            upload_tasks.pop(user_id, None)
            cleanup_temp_files([state_data.get("file_info", {}).get("downloaded_path"), state_data.get("file_info", {}).get("transcoded_path")])
        else:
            await query.answer("❌ 𝗡𝗼 𝗮𝗰𝘁𝗶𝘃𝗲 𝘂𝗽𝗹𝗼𝗮𝗱 𝘁𝗮𝘀𝗸 𝘁𝗼 𝗰𝗮𝗻𝗰𝗲𝗹.", show_alert=True)
            user_states.pop(user_id, None)

    elif action == "skip_caption":
        await query.answer("✅ 𝗨𝘀𝗶𝗻𝗴 𝗱𝗲𝗳𝗮𝘂𝗹𝘁 𝗰𝗮𝗽𝘁𝗶𝗼𝗻.", show_alert=True)
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = None
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await safe_edit_message(query.message, f"✅ 𝗦𝗸𝗶𝗽𝗽𝗲𝗱. 𝗨𝗽𝗹𝗼𝗮𝗱𝗶𝗻𝗴 𝘄𝗶𝘁𝗵 𝗱𝗲𝗳𝗮𝘂𝗹𝘁 𝗰𝗮𝗽𝘁𝗶𝗼𝗻...")
        await start_upload_task(query.message, file_info)

async def start_upload_task(msg, file_info):
    user_id = msg.from_user.id
    task = asyncio.create_task(process_and_upload(msg, file_info))
    upload_tasks[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"𝗨𝗽𝗹𝗼𝗮𝗱 𝘁𝗮𝘀𝗸 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 {user_id} 𝘄𝗮𝘀 𝗰𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱.")
    except Exception as e:
        logger.error(f"𝗨𝗽𝗹𝗼𝗮𝗱 𝘁𝗮𝘀𝗸 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 {user_id} 𝗳𝗮𝗶𝗹𝗲𝗱 𝘄𝗶𝘁𝗵 𝗮𝗻 𝘂𝗻𝗵𝗮𝗻𝗱𝗹𝗲𝗱 𝗲𝘅𝗰𝗲𝗽𝘁𝗶𝗼𝗻: {e}")
        await msg.reply("❌ 𝗔𝗻 𝘂𝗻𝗲𝘅𝗽𝗲𝗰𝘁𝗲𝗱 𝗲𝗿𝗿𝗼𝗿 𝗼𝗰𝗰𝘂𝗿𝗿𝗲𝗱 𝗱𝘂𝗿𝗶𝗻𝗴 𝘂𝗽𝗹𝗼𝗮𝗱. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘁𝗿𝘆 𝗮𝗴𝗮𝗶𝗻.")

async def process_and_upload(msg, file_info):
    user_id = msg.from_user.id
    platform = file_info["platform"]
    upload_type = file_info["upload_type"]
    file_path = file_info["downloaded_path"]
    
    processing_msg = file_info["processing_msg"]

    try:
        video_to_upload = file_path
        transcoded_video_path = None
        
        # Get admin compression setting
        no_compression_admin = global_settings.get("no_compression_admin", False)
        
        file_extension = os.path.splitext(file_path)[1].lower()
        is_video = file_extension in ['.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv']
        
        if is_video and not no_compression_admin:
            await safe_edit_message(processing_msg, "🔄 𝗢𝗽𝘁𝗶𝗺𝗶𝘇𝗶𝗻𝗴 𝘃𝗶𝗱𝗲𝗼 (𝘁𝗿𝗮𝗻𝘀𝗰𝗼𝗱𝗶𝗻𝗴)... 𝗧𝗵𝗶𝘀 𝗺𝗮𝘆 𝘁𝗮𝗸𝗲 𝗮 𝗺𝗼𝗺𝗲𝗻𝘁.")
            transcoded_video_path = f"{file_path}_transcoded.mp4"
            ffmpeg_command = ["ffmpeg", "-i", file_path, "-map_chapters", "-1", "-y"]
            ffmpeg_command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "23",
                                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                                    "-pix_fmt", "yuv420p", "-movflags", "faststart"])
            
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
                    raise Exception(f"𝗩𝗶𝗱𝗲𝗼 𝘁𝗿𝗮𝗻𝘀𝗰𝗼𝗱𝗶𝗻𝗴 𝗳𝗮𝗶𝗹𝗲𝗱: {stderr.decode()}")
                else:
                    logger.info(f"FFmpeg transcoding successful. 𝗢𝘂𝘁𝗽𝘂𝘁: {transcoded_video_path}")
                    video_to_upload = transcoded_video_path
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"𝗗𝗲𝗹𝗲𝘁𝗲𝗱 𝗼𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗱𝗼𝘄𝗻𝗹𝗼𝗮𝗱𝗲𝗱 𝘃𝗶𝗱𝗲𝗼 𝗳𝗶𝗹𝗲: {file_path}")
            except asyncio.TimeoutError:
                process.kill()
                logger.error(f"FFmpeg process timed out for user {user_id}")
                raise Exception("𝗩𝗶𝗱𝗲𝗼 𝘁𝗿𝗮𝗻𝘀𝗰𝗼𝗱𝗶𝗻𝗴 𝘁𝗶𝗺𝗲𝗱 𝗼𝘂𝘁.")
        elif is_video and no_compression_admin:
            await safe_edit_message(processing_msg, "✅ 𝗡𝗼 𝗰𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻. 𝗨𝗽𝗹𝗼𝗮𝗱𝗶𝗻𝗴 𝗼𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗳𝗶𝗹𝗲.")
            # In this case, no transcoding is needed, file_path is already the video to upload.
            video_to_upload = file_path
        else:
             await safe_edit_message(processing_msg, "✅ 𝗡𝗼 𝗰𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻 𝗮𝗽𝗽𝗹𝗶𝗲𝗱 𝗳𝗼𝗿 𝗶𝗺𝗮𝗴𝗲𝘀.")

        settings = await get_user_settings(user_id)
        default_caption = settings.get("caption", f"𝗖𝗵𝗲𝗰𝗸 𝗼𝘂𝘁 𝗺𝘆 𝗻𝗲𝘄 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗰𝗼𝗻𝘁𝗲𝗻𝘁! 🎥")
        hashtags = settings.get("hashtags", "")
        
        final_caption = file_info.get("custom_caption") or default_caption
        if hashtags:
            final_caption = f"{final_caption}\n\n{hashtags}"

        url = "𝗡/𝗔"
        media_id = "𝗡/𝗔"
        media_type_value = ""

        await safe_edit_message(processing_msg, "🚀 **𝗨𝗽𝗹𝗼𝗮𝗱𝗶𝗻𝗴 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺...**", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup())
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
                raise LoginRequired("𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝘀𝗲𝘀𝘀𝗶𝗼𝗻 𝗲𝘅𝗽𝗶𝗿𝗲𝗱.")
            user_upload_client.set_settings(session)
            
            try:
                await asyncio.to_thread(user_upload_client.get_timeline_feed)
            except LoginRequired:
                raise LoginRequired("𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝘀𝗲𝘀𝘀𝗶𝗼𝗻 𝗲𝘅𝗽𝗶𝗿𝗲𝗱.")

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
            f"📤 𝗡𝗲𝘄 {platform.capitalize()} {upload_type.capitalize()} 𝗨𝗽𝗹𝗼𝗮𝗱\n\n"
            f"👤 𝗨𝘀𝗲𝗿: `{user_id}`\n"
            f"📛 𝗨𝘀𝗲𝗿𝗻𝗮𝗺𝗲: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 𝗨𝗥𝗟: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await safe_edit_message(processing_msg, f"✅ 𝗨𝗽𝗹𝗼𝗮𝗱𝗲𝗱 𝘀𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except asyncio.CancelledError:
        logger.info(f"𝗨𝗽𝗹𝗼𝗮𝗱 𝗽𝗿𝗼𝗰𝗲𝘀𝘀 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 {user_id} 𝘄𝗮𝘀 𝗰𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱.")
        await safe_edit_message(processing_msg, "❌ 𝗨𝗽𝗹𝗼𝗮𝗱 𝗽𝗿𝗼𝗰𝗲𝘀𝘀 𝗰𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱.")
    except LoginRequired:
        await safe_edit_message(processing_msg, f"❌ {platform.capitalize()} 𝗹𝗼𝗴𝗶𝗻 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱. 𝗬𝗼𝘂𝗿 𝘀𝗲𝘀𝘀𝗶𝗼𝗻 𝗺𝗶𝗴𝗵𝘁 𝗵𝗮𝘃𝗲 𝗲𝘅𝗽𝗶𝗿𝗲𝗱. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝘀𝗲 `/{platform}login <username> <password>` 𝗮𝗴𝗮𝗶𝗻.")
        logger.error(f"𝗟𝗼𝗴𝗶𝗻𝗥𝗲𝗾𝘂𝗶𝗿𝗲𝗱 𝗱𝘂𝗿𝗶𝗻𝗴 {platform} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗮𝗶𝗹𝗲𝗱 (𝗹𝗼𝗴𝗶𝗻 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱)\n𝗨𝘀𝗲𝗿: `{user_id}`")
    except ClientError as ce:
        await safe_edit_message(processing_msg, f"❌ {platform.capitalize()} 𝗰𝗹𝗶𝗲𝗻𝘁 𝗲𝗿𝗿𝗼𝗿 𝗱𝘂𝗿𝗶𝗻𝗴 𝘂𝗽𝗹𝗼𝗮𝗱: {ce}. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘁𝗿𝘆 𝗮𝗴𝗮𝗶𝗻 𝗹𝗮𝘁𝗲𝗿.")
        logger.error(f"𝗖𝗹𝗶𝗲𝗻𝘁𝗘𝗿𝗿𝗼𝗿 𝗱𝘂𝗿𝗶𝗻𝗴 {platform} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗮𝗶𝗹𝗲𝗱 (𝗰𝗹𝗶𝗲𝗻𝘁 𝗲𝗿𝗿𝗼𝗿)\n𝗨𝘀𝗲𝗿: `{user_id}`\n𝗘𝗿𝗿𝗼𝗿: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗮𝗶𝗹𝗲𝗱: {str(e)}"
        if processing_msg:
            await safe_edit_message(processing_msg, error_msg)
        else:
            await msg.reply(error_msg)
        logger.error(f"{platform.capitalize()} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗮𝗶𝗹𝗲𝗱 𝗳𝗼𝗿 {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} 𝘂𝗽𝗹𝗼𝗮𝗱 𝗳𝗮𝗶𝗹𝗲𝗱\n𝗨𝘀𝗲𝗿: `{user_id}`\n𝗘𝗿𝗿𝗼𝗿: `{error_msg}`")
    finally:
        cleanup_temp_files([file_path, transcoded_video_path])
        user_states.pop(user_id, None)
        upload_tasks.pop(user_id, None)

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
        user_states.pop(user_id, None)
        return await msg.reply("✅ 𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗮𝘆 𝗤𝗥 𝗰𝗼𝗱𝗲 𝗶𝗺𝗮𝗴𝗲 𝘀𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆 𝘀𝗮𝘃𝗲𝗱!")
    
    if not state_data or state_data.get("action") not in [
        "waiting_for_instagram_reel_video", "waiting_for_instagram_photo_image"
    ]:
        return await msg.reply("❌ 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝘀𝗲 𝗼𝗻𝗲 𝗼𝗳 𝘁𝗵𝗲 𝘂𝗽𝗹𝗼𝗮𝗱 𝗯𝘂𝘁𝘁𝗼𝗻𝘀 𝗳𝗶𝗿𝘀𝘁.")

    platform = state_data["platform"]
    upload_type = state_data["upload_type"]
    
    if msg.video and (upload_type in ["reel", "video"]):
        if msg.video.file_size > MAX_FILE_SIZE_BYTES:
            user_states.pop(user_id, None)
            return await msg.reply(f"❌ 𝗙𝗶𝗹𝗲 𝘀𝗶𝘇𝗲 𝗲𝘅𝗰𝗲𝗲𝗱𝘀 𝘁𝗵𝗲 𝗹𝗶𝗺𝗶𝘁 𝗼𝗳 `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` 𝗠𝗕.")
        file_info = {
            "file_id": msg.video.file_id,
            "platform": platform,
            "upload_type": upload_type,
            "file_size": msg.video.file_size,
            "processing_msg": await msg.reply("⏳ 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗱𝗼𝘄𝗻𝗹𝗼𝗮𝗱...")
        }
    elif msg.photo and (upload_type in ["post", "photo"]):
        file_info = {
            "file_id": msg.photo.file_id,
            "platform": platform,
            "upload_type": upload_type,
            "file_size": msg.photo.file_size,
            "processing_msg": await msg.reply("⏳ 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝗱𝗼𝘄𝗻𝗹𝗼𝗮𝗱...")
        }
    elif msg.document:
        return await msg.reply("⚠️ 𝗗𝗼𝗰𝘂𝗺𝗲𝗻𝘁𝘀 𝗮𝗿𝗲 𝗻𝗼𝘁 𝘀𝘂𝗽𝗽𝗼𝗿𝘁𝗲𝗱 𝗳𝗼𝗿 𝘂𝗽𝗹𝗼𝗮𝗱 𝘆𝗲𝘁. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝗮 𝘃𝗶𝗱𝗲𝗼 𝗼𝗿 𝗽𝗵𝗼𝘁𝗼.")
    else:
        user_states.pop(user_id, None)
        return await msg.reply("❌ 𝗧𝗵𝗲 𝗳𝗶𝗹𝗲 𝘁𝘆𝗽𝗲 𝗱𝗼𝗲𝘀 𝗻𝗼𝘁 𝗺𝗮𝘁𝗰𝗵 𝘁𝗵𝗲 𝗿𝗲𝗾𝘂𝗲𝘀𝘁𝗲𝗱 𝘂𝗽𝗹𝗼𝗮𝗱 𝘁𝘆𝗽𝗲.")

    file_info["downloaded_path"] = None
    
    try:
        start_time = time.time()
        file_info["processing_msg"].is_progress_message_updated = False
        file_info["downloaded_path"] = await app.download_media(
            msg,
            progress=lambda current, total: progress_callback(current, total, "𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱", file_info["processing_msg"], start_time)
        )
        await safe_edit_message(file_info["processing_msg"], "✅ 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗰𝗼𝗺𝗽𝗹𝗲𝘁𝗲. 𝗪𝗵𝗮𝘁 𝘁𝗶𝘁𝗹𝗲 𝗱𝗼 𝘆𝗼𝘂 𝘄𝗮𝗻𝘁 𝗳𝗼𝗿 𝘆𝗼𝘂𝗿 𝗽𝗼𝘀𝘁?", reply_markup=get_caption_markup())
        user_states[user_id] = {"action": "awaiting_post_title", "file_info": file_info}

    except asyncio.CancelledError:
        logger.info(f"𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗰𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱 𝗯𝘆 𝘂𝘀𝗲𝗿 {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"𝗘𝗿𝗿𝗼𝗿 𝗱𝘂𝗿𝗶𝗻𝗴 𝗳𝗶𝗹𝗲 𝗱𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 {user_id}: {e}")
        await safe_edit_message(file_info["processing_msg"], f"❌ 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗳𝗮𝗶𝗹𝗲𝗱: {str(e)}")
        cleanup_temp_files([file_info.get("downloaded_path")])
        user_states.pop(user_id, None)

# --- Admin Panel Handlers ---

@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹",
        reply_markup=admin_markup
    )

@app.on_callback_query(filters.regex("^payment_settings_panel$"))
async def payment_settings_panel_cb(_, query):
    if not is_admin(query.from_user.id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    
    current_settings = global_settings.get("payment_settings", {})
    text = (
        "💰 **𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀**\n\n"
        f"**𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗹𝗮𝘆:** {current_settings.get('google_play') or '𝗡𝗢𝗧 𝗦𝗘𝗧'}\n"
        f"**𝗨𝗣𝗜:** {current_settings.get('upi') or '𝗡𝗢𝗧 𝗦𝗘𝗧'}\n"
        f"**𝗨𝗦𝗧:** {current_settings.get('ust') or '𝗡𝗢𝗧 𝗦𝗘𝗧'}\n"
        f"**𝗕𝗧𝗖:** {current_settings.get('btc') or '𝗡𝗢𝗧 𝗦𝗘𝗧'}\n"
        f"**𝗢𝘁𝗵𝗲𝗿𝘀:** {current_settings.get('others') or '𝗡𝗢𝗧 𝗦𝗘𝗧'}\n\n"
        "𝗖𝗹𝗶𝗰𝗸 𝗮 𝗯𝘂𝘁𝘁𝗼𝗻 𝘁𝗼 𝘂𝗽𝗱𝗮𝘁𝗲 𝗶𝘁𝘀 𝗱𝗲𝘁𝗮𝗶𝗹𝘀."
    )
    
    await safe_edit_message(query.message, text, reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^set_payment_google_play_qr$"))
@with_user_lock
async def set_payment_google_play_qr_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    
    user_states[user_id] = {"action": "waiting_for_google_play_qr"}
    await safe_edit_message(
        query.message,
        "📸 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝘁𝗵𝗲 **𝗶𝗺𝗮𝗴𝗲** 𝗼𝗳 𝘁𝗵𝗲 𝗚𝗼𝗼𝗴𝗹𝗲 𝗣𝗮𝘆 𝗤𝗥 𝗰𝗼𝗱𝗲. 𝗧𝗵𝗲 𝗶𝗺𝗮𝗴𝗲 𝘄𝗶𝗹𝗹 𝗯𝗲 𝘀𝗮𝘃𝗲𝗱 𝗮𝗻𝗱 𝘀𝗵𝗼𝘄𝗻 𝘁𝗼 𝘂𝘀𝗲𝗿𝘀."
    )

@app.on_callback_query(filters.regex("^set_payment_"))
async def set_payment_cb(_, query):
    if not is_admin(query.from_user.id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    
    method = query.data.split("set_payment_")[1]
    
    user_states[query.from_user.id] = {"action": f"waiting_for_payment_details_{method}"}
    
    await safe_edit_message(query.message, f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝘁𝗵𝗲 𝗱𝗲𝘁𝗮𝗶𝗹𝘀 𝗳𝗼𝗿 **{method.upper()}**. 𝗧𝗵𝗶𝘀 𝗰𝗮𝗻 𝗯𝗲 𝘁𝗵𝗲 𝗨𝗣𝗜 𝗜𝗗, 𝘄𝗮𝗹𝗹𝗲𝘁 𝗮𝗱𝗱𝗿𝗲𝘀𝘀, 𝗼𝗿 𝗮𝗻𝘆 𝗼𝘁𝗵𝗲𝗿 𝗶𝗻𝗳𝗼𝗿𝗺𝗮𝘁𝗶𝗼𝗻.", parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^global_settings_panel$"))
async def global_settings_panel_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    onam_status = "𝗢𝗡" if global_settings.get("onam_toggle") else "𝗢𝗙𝗙"
    max_uploads = global_settings.get("max_concurrent_uploads")
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = f"`{proxy_url}`" if proxy_url else "𝗡𝗼𝗻𝗲"
    
    compression_status = "𝗗𝗜𝗦𝗔𝗕𝗟𝗘𝗗" if global_settings.get("no_compression_admin") else "𝗘𝗡𝗔𝗕𝗟𝗘𝗗"
    
    settings_text = (
        "⚙️ **𝗚𝗹𝗼𝗯𝗮𝗹 𝗕𝗼𝘁 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀**\n\n"
        f"**𝗢𝗻𝗮𝗺 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗘𝘃𝗲𝗻𝘁:** `{onam_status}`\n"
        f"**𝗠𝗮𝘅 𝗖𝗼𝗻𝗰𝘂𝗿𝗿𝗲𝗻𝘁 𝗨𝗽𝗹𝗼𝗮𝗱𝘀:** `{max_uploads}`\n"
        f"**𝗚𝗹𝗼𝗯𝗮𝗹 𝗣𝗿𝗼𝘅𝘆:** {proxy_status_text}\n"
        f"**𝗚𝗹𝗼𝗯𝗮𝗹 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻:** `{compression_status}`\n"
    )
    await safe_edit_message(
        query.message,
        settings_text,
        reply_markup=admin_global_settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^toggle_onam$"))
async def toggle_onam_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    current_status = global_settings.get("onam_toggle", False)
    new_status = not current_status
    _update_global_setting("onam_toggle", new_status)
    status_text = "𝗢𝗡" if new_status else "𝗢𝗙𝗙"
    await query.answer(f"𝗢𝗻𝗮𝗺 𝘁𝗼𝗴𝗴𝗹𝗲 𝗶𝘀 𝗻𝗼𝘄 {status_text}.", show_alert=True)
    onam_status = "𝗢𝗡" if global_settings.get("onam_toggle") else "𝗢𝗙𝗙"
    max_uploads = global_settings.get("max_concurrent_uploads")
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = f"`{proxy_url}`" if proxy_url else "𝗡𝗼𝗻𝗲"
    compression_status = "𝗗𝗜𝗦𝗔𝗕𝗟𝗘𝗗" if global_settings.get("no_compression_admin") else "𝗘𝗡𝗔𝗕𝗟𝗘𝗗"
    settings_text = (
        "⚙️ **𝗚𝗹𝗼𝗯𝗮𝗹 𝗕𝗼𝘁 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀**\n\n"
        f"**𝗢𝗻𝗮𝗺 𝗦𝗽𝗲𝗰𝗶𝗮𝗹 𝗘𝘃𝗲𝗻𝘁:** `{onam_status}`\n"
        f"**𝗠𝗮𝘅 𝗖𝗼𝗻𝗰𝘂𝗿𝗿𝗲𝗻𝘁 𝗨𝗽𝗹𝗼𝗮𝗱𝘀:** `{max_uploads}`\n"
        f"**𝗚𝗹𝗼𝗯𝗮𝗹 𝗣𝗿𝗼𝘅𝘆:** {proxy_status_text}\n"
        f"**𝗚𝗹𝗼𝗯𝗮𝗹 𝗖𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻:** `{compression_status}`\n"
    )
    await safe_edit_message(
        query.message,
        settings_text,
        reply_markup=admin_global_settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_max_uploads$"))
@with_user_lock
async def set_max_uploads_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    user_states[user_id] = {"action": "waiting_for_max_uploads"}
    current_limit = global_settings.get("max_concurrent_uploads")
    await safe_edit_message(
        query.message,
        f"🔄 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝘁𝗵𝗲 𝗻𝗲𝘄 𝗺𝗮𝘅𝗶𝗺𝘂𝗺 𝗻𝘂𝗺𝗯𝗲𝗿 𝗼𝗳 𝗰𝗼𝗻𝗰𝘂𝗿𝗿𝗲𝗻𝘁 𝘂𝗽𝗹𝗼𝗮𝗱𝘀.\n\n"
        f"𝗖𝘂𝗿𝗿𝗲𝗻𝘁 𝗹𝗶𝗺𝗶𝘁 𝗶𝘀: `{current_limit}`"
    )

@app.on_callback_query(filters.regex("^set_proxy_url$"))
@with_user_lock
async def set_proxy_url_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    user_states[user_id] = {"action": "waiting_for_proxy_url"}
    current_proxy = global_settings.get("proxy_url", "No proxy set.")
    await safe_edit_message(
        query.message,
        f"🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝘁𝗵𝗲 𝗻𝗲𝘄 𝗽𝗿𝗼𝘅𝘆 𝗨𝗥𝗟 (e.g., `http://user:pass@ip:port`).\n"
        f"𝗧𝘆𝗽𝗲 '𝗻𝗼𝗻𝗲' 𝗼𝗿 '𝗿𝗲𝗺𝗼𝘃𝗲' 𝘁𝗼 𝗱𝗶𝘀𝗮𝗯𝗹𝗲 𝘁𝗵𝗲 𝗽𝗿𝗼𝘅𝘆.\n\n"
        f"𝗖𝘂𝗿𝗿𝗲𝗻𝘁 𝗽𝗿𝗼𝘅𝘆: `{current_proxy}`"
    )

@app.on_callback_query(filters.regex("^reset_stats$"))
@with_user_lock
async def reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    await safe_edit_message(query.message, "⚠️ **𝗪𝗔𝗥𝗡𝗜𝗡𝗚!** 𝗔𝗿𝗲 𝘆𝗼𝘂 𝘀𝘂𝗿𝗲 𝘆𝗼𝘂 𝘄𝗮𝗻𝘁 𝘁𝗼 𝗿𝗲𝘀𝗲𝘁 𝗮𝗹𝗹 𝘂𝗽𝗹𝗼𝗮𝗱 𝘀𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀? 𝗧𝗵𝗶𝘀 𝗮𝗰𝘁𝗶𝗼𝗻 𝗶𝘀 𝗶𝗿𝗿𝗲𝘃𝗲𝗿𝘀𝗶𝗯𝗹𝗲.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 𝗬𝗘𝗦, 𝗥𝗘𝗦𝗘𝗧 𝗦𝗧𝗔𝗧𝗦", callback_data="confirm_reset_stats")],
            [InlineKeyboardButton("❌ 𝗡𝗢, 𝗖𝗔𝗡𝗖𝗘𝗟", callback_data="admin_panel")]
        ]), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^confirm_reset_stats$"))
@with_user_lock
async def confirm_reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    result = db.uploads.delete_many({})
    await query.answer(f"✅ 𝗔𝗟𝗟 𝗨𝗣𝗟𝗢𝗔𝗗 𝗦𝗧𝗔𝗧𝗦 𝗛𝗔𝗩𝗘 𝗕𝗘𝗘𝗡 𝗥𝗘𝗦𝗘𝗧! 𝗗𝗲𝗹𝗲𝘁𝗲𝗱 {result.deleted_count} 𝗲𝗻𝘁𝗿𝗶𝗲𝘀.", show_alert=True)
    await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
    await send_log_to_channel(app, LOG_CHANNEL, f"📊 𝗔𝗱𝗺𝗶𝗻 `{user_id}` 𝗵𝗮𝘀 𝗿𝗲𝘀𝗲𝘁 𝗮𝗹𝗹 𝗯𝗼𝘁 𝘂𝗽𝗹𝗼𝗮𝗱 𝘀𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀.")

@app.on_callback_query(filters.regex("^show_system_stats$"))
async def show_system_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        system_stats_text = (
            "💻 **𝗦𝘆𝘀𝘁𝗲𝗺 𝗦𝘁𝗮𝘁𝘀**\n\n"
            f"**𝗖𝗣𝗨:** `{cpu_usage}%`\n"
            f"**𝗥𝗔𝗠:** `{ram.percent}%` (𝗨𝘀𝗲𝗱: `{ram.used / (1024**3):.2f}` 𝗚𝗕 / 𝗧𝗼𝘁𝗮𝗹: `{ram.total / (1024**3):.2f}` 𝗚𝗕)\n"
            f"**𝗗𝗶𝘀𝗸:** `{disk.percent}%` (𝗨𝘀𝗲𝗱: `{disk.used / (1024**3):.2f}` 𝗚𝗕 / 𝗧𝗼𝘁𝗮𝗹: `{disk.total / (1024**3):.2f}` 𝗚𝗕)\n\n"
        )
        gpu_info = "𝗡𝗢 𝗚𝗣𝗨 𝗙𝗢𝗨𝗡𝗗 𝗢𝗥 𝗚𝗣𝗨𝗧𝗜𝗟 𝗜𝗦 𝗡𝗢𝗧 𝗜𝗡𝗦𝗧𝗔𝗟𝗟𝗘𝗗."
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu_info = "**𝗚𝗣𝗨 𝗜𝗻𝗳𝗼:**\n"
                for i, gpu in enumerate(gpus):
                    gpu_info += (
                        f"    - **𝗚𝗣𝗨 {i}:** `{gpu.name}`\n"
                        f"      - 𝗟𝗼𝗮𝗱: `{gpu.load*100:.1f}%`\n"
                        f"      - 𝗠𝗲𝗺𝗼𝗿𝘆: `{gpu.memoryUsed}/{gpu.memoryTotal}` 𝗠𝗕\n"
                        f"      - 𝗧𝗲𝗺𝗽: `{gpu.temperature}°𝗖`\n"
                    )
            else:
                gpu_info = "𝗡𝗼 𝗚𝗣𝗨 𝗳𝗼𝘂𝗻𝗱."
        except Exception:
            gpu_info = "𝗖𝗼𝘂𝗹𝗱 𝗻𝗼𝘁 𝗿𝗲𝘁𝗿𝗶𝗲𝘃𝗲 𝗚𝗣𝗨 𝗶𝗻𝗳𝗼."
        system_stats_text += gpu_info
        await safe_edit_message(
            query.message,
            system_stats_text,
            reply_markup=admin_global_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await query.answer("❌ 𝗙𝗮𝗶𝗹𝗲𝗱 𝘁𝗼 𝗿𝗲𝘁𝗿𝗶𝗲𝘃𝗲 𝘀𝘆𝘀𝘁𝗲𝗺 𝘀𝘁𝗮𝘁𝘀.", show_alert=True)
        logger.error(f"𝗘𝗿𝗿𝗼𝗿 𝗿𝗲𝘁𝗿𝗶𝗲𝘃𝗶𝗻𝗴 𝘀𝘆𝘀𝘁𝗲𝗺 𝘀𝘁𝗮𝘁𝘀 𝗳𝗼𝗿 𝗮𝗱𝗺𝗶𝗻 {user_id}: {e}")
        await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    users = list(db.users.find({}))
    if not users:
        await safe_edit_message(
            query.message,
            "👥 𝗡𝗼 𝘂𝘀𝗲𝗿𝘀 𝗳𝗼𝘂𝗻𝗱 𝗶𝗻 𝘁𝗵𝗲 𝗱𝗮𝘁𝗮𝗯𝗮𝘀𝗲.",
            reply_markup=admin_markup
        )
        return
    user_list_text = "👥 **𝗔𝗟𝗟 𝗨𝗦𝗘𝗥𝗦:**\n\n"
    for user in users:
        user_id = user["_id"]
        instagram_username = user.get("instagram_username", "𝗡/𝗔")
        added_at = user.get("added_at", "𝗡/𝗔").strftime("%Y-%m-%d") if isinstance(user.get("added_at"), datetime) else "𝗡/𝗔"
        last_active = user.get("last_active", "𝗡/𝗔").strftime("%Y-%m-%d %H:%M") if isinstance(user.get("last_active"), datetime) else "𝗡/𝗔"
        platform_statuses = []
        if user_id == ADMIN_ID:
            platform_statuses.append("👑 𝗔𝗗𝗠𝗜𝗡")
        else:
            for platform in PREMIUM_PLATFORMS:
                if is_premium_for_platform(user_id, platform):
                    platform_data = user.get("premium", {}).get(platform, {})
                    premium_type = platform_data.get("type")
                    premium_until = platform_data.get("until")
                    if premium_type == "lifetime":
                        platform_statuses.append(f"⭐ {platform.capitalize()}: 𝗟𝗜𝗙𝗘𝗧𝗜𝗠𝗘")
                    elif premium_until:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: 𝗘𝗫𝗣𝗜𝗥𝗘𝗦 `{premium_until.strftime('%Y-%m-%d')}`")
                    else:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: 𝗔𝗖𝗧𝗜𝗩𝗘")
                else:
                    platform_statuses.append(f"❌ {platform.capitalize()}: 𝗙𝗥𝗘𝗘")
        status_line = " | ".join(platform_statuses)
        user_list_text += (
            f"𝗜𝗗: `{user_id}` | {status_line}\n"
            f"𝗜𝗚: `{instagram_username}`\n"
            f"𝗔𝗱𝗱𝗲𝗱: `{added_at}` | 𝗟𝗮𝘀𝘁 𝗔𝗰𝘁𝗶𝘃𝗲: `{last_active}`\n"
            "-----------------------------------\n"
        )
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "𝗨𝘀𝗲𝗿 𝗹𝗶𝘀𝘁 𝗶𝘀 𝘁𝗼𝗼 𝗹𝗼𝗻𝗴. 𝗦𝗲𝗻𝗱𝗶𝗻𝗴 𝗮𝘀 𝗮 𝗳𝗶𝗹𝗲...")
        with open("users.txt", "w") as f:
            f.write(user_list_text.replace("`", ""))
        await app.send_document(query.message.chat.id, "users.txt", caption="👥 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥𝗦 𝗟𝗜𝗦𝗧")
        os.remove("users.txt")
        await safe_edit_message(
            query.message,
            "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹",
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
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    user_states[query.from_user.id] = {"action": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(
        query.message,
        "➕ 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝘁𝗵𝗲 **𝘂𝘀𝗲𝗿 𝗜𝗗** 𝘁𝗼 𝗺𝗮𝗻𝗮𝗴𝗲 𝘁𝗵𝗲𝗶𝗿 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝗰𝗲𝘀𝘀."
    )

@app.on_callback_query(filters.regex("^select_platform_"))
@with_user_lock
async def select_platform_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("𝗘𝗿𝗿𝗼𝗿: 𝗨𝘀𝗲𝗿 𝘀𝗲𝗹𝗲𝗰𝘁𝗶𝗼𝗻 𝗹𝗼𝘀𝘁. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘁𝗿𝘆 '𝗺𝗮𝗻𝗮𝗴𝗲 𝗽𝗿𝗲𝗺𝗶𝘂𝗺' 𝗮𝗴𝗮𝗶𝗻.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
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
        f"✅ 𝗨𝘀𝗲𝗿 𝗜𝗗 `{state_data['target_user_id']}` 𝗿𝗲𝗰𝗲𝗶𝘃𝗲𝗱. 𝗦𝗲𝗹𝗲𝗰𝘁 𝗽𝗹𝗮𝘁𝗳𝗼𝗿𝗺𝘀 𝗳𝗼𝗿 𝗽𝗿𝗲𝗺𝗶𝘂𝗺:",
        reply_markup=get_platform_selection_markup(user_id, selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_platform_selection$"))
@with_user_lock
async def confirm_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("𝗘𝗿𝗿𝗼𝗿: 𝗣𝗹𝗲𝗮𝘀𝗲 𝗿𝗲𝘀𝘁𝗮𝗿𝘁 𝘁𝗵𝗲 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗺𝗮𝗻𝗮𝗴𝗲𝗺𝗲𝗻𝘁 𝗽𝗿𝗼𝗰𝗲𝘀𝘀.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = [p for p, selected in state_data.get("selected_platforms", {}).items() if selected]
    if not selected_platforms:
        return await query.answer("𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝗮𝘁 𝗹𝗲𝗮𝘀𝘁 𝗼𝗻𝗲 𝗽𝗹𝗮𝘁𝗳𝗼𝗿𝗺!", show_alert=True)
    state_data["action"] = "select_premium_plan_for_platforms"
    state_data["final_selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    await safe_edit_message(
        query.message,
        f"✅ 𝗣𝗹𝗮𝘁𝗳𝗼𝗿𝗺𝘀 𝘀𝗲𝗹𝗲𝗰𝘁𝗲𝗱: `{', '.join(platform.capitalize() for platform in selected_platforms)}`. 𝗡𝗼𝘄, 𝘀𝗲𝗹𝗲𝗰𝘁 𝗮 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗽𝗹𝗮𝗻 𝗳𝗼𝗿 𝘂𝘀𝗲𝗿 `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_plan_"))
@with_user_lock
async def select_plan_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_premium_plan_for_platforms":
        await query.answer("𝗘𝗿𝗿𝗼𝗿: 𝗣𝗹𝗮𝗻 𝘀𝗲𝗹𝗲𝗰𝘁𝗶𝗼𝗻 𝗹𝗼𝘀𝘁. 𝗣𝗹𝗲𝗮𝘀𝗲 𝗿𝗲𝘀𝘁𝗮𝗿𝘁 𝘁𝗵𝗲 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗺𝗮𝗻𝗮𝗴𝗲𝗺𝗲𝗻𝘁 𝗽𝗿𝗼𝗰𝗲𝘀𝘀.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = state_data["final_selected_platforms"]
    premium_plan_key = query.data.split("select_plan_")[1]
    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗽𝗹𝗮𝗻 𝘀𝗲𝗹𝗲𝗰𝘁𝗲𝗱.", show_alert=True)
        return await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
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
    db.users.update_one({"_id": target_user_id}, {"$set": update_query}, upsert=True)
    admin_confirm_text = f"✅ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝘁𝗼 𝘂𝘀𝗲𝗿 `{target_user_id}` 𝗳𝗼𝗿:\n"
    for platform in selected_platforms:
        updated_user = _get_user_data(target_user_id)
        platform_data = updated_user.get("premium", {}).get(platform, {})
        confirm_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if platform_data.get("until"):
            confirm_line += f" (𝗲𝘅𝗽𝗶𝗿𝗲𝘀: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} 𝗨𝗧𝗖`)"
        admin_confirm_text += f"- {confirm_line}\n"
    await safe_edit_message(
        query.message,
        admin_confirm_text,
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer("𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗴𝗿𝗮𝗻𝘁𝗲𝗱!", show_alert=False)
    user_states.pop(user_id, None)
    try:
        user_msg = (
            f"🎉 **𝗖𝗼𝗻𝗴𝗿𝗮𝘁𝘂𝗹𝗮𝘁𝗶𝗼𝗻𝘀!** 🎉\n\n"
            f"𝗬𝗼𝘂 𝗵𝗮𝘃𝗲 𝗯𝗲𝗲𝗻 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝗰𝗲𝘀𝘀 𝗳𝗼𝗿 𝘁𝗵𝗲 𝗳𝗼𝗹𝗹𝗼𝘄𝗶𝗻𝗴 𝗽𝗹𝗮𝘁𝗳𝗼𝗿𝗺𝘀:\n"
        )
        for platform in selected_platforms:
            updated_user = _get_user_data(target_user_id)
            platform_data = updated_user.get("premium", {}).get(platform, {})
            msg_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
            if platform_data.get("until"):
                msg_line += f" (𝗲𝘅𝗽𝗶𝗿𝗲𝘀: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} 𝗨𝗧𝗖`)"
            user_msg += f"- {msg_line}\n"
        user_msg += "\n𝗘𝗻𝗷𝗼𝘆 𝘆𝗼𝘂𝗿 𝗻𝗲𝘄 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀! ✨"
        await app.send_message(target_user_id, user_msg, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗴𝗿𝗮𝗻𝘁𝗲𝗱 𝗻𝗼𝘁𝗶𝗳𝗶𝗰𝗮𝘁𝗶𝗼𝗻 𝘀𝗲𝗻𝘁 𝘁𝗼 `{target_user_id}` 𝗯𝘆 𝗔𝗱𝗺𝗶𝗻 `{user_id}`. 𝗣𝗹𝗮𝘁𝗳𝗼𝗿𝗺𝘀: `{', '.join(selected_platforms)}`, 𝗣𝗹𝗮𝗻: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"𝗙𝗮𝗶𝗹𝗲𝗱 𝘁𝗼 𝗻𝗼𝘁𝗶𝗳𝘆 𝘂𝘀𝗲𝗿 {target_user_id} 𝗮𝗯𝗼𝘂𝘁 𝗽𝗿𝗲𝗺𝗶𝘂𝗺: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ 𝗙𝗮𝗶𝗹𝗲𝗱 𝘁𝗼 𝗻𝗼𝘁𝗶𝗳𝘆 𝘂𝘀𝗲𝗿 `{target_user_id}` 𝗮𝗯𝗼𝘂𝘁 𝗽𝗿𝗲𝗺𝗶𝘂𝗺. 𝗘𝗿𝗿𝗼𝗿: `{str(e)}`"
        )

@app.on_callback_query(filters.regex("^back_to_platform_selection$"))
@with_user_lock
async def back_to_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") not in ["select_platforms_for_premium", "select_premium_plan_for_platforms"]:
        await query.answer("𝗘𝗿𝗿𝗼𝗿: 𝗜𝗻𝘃𝗮𝗹𝗶𝗱 𝘀𝘁𝗮𝘁𝗲 𝗳𝗼𝗿 𝗯𝗮𝗰𝗸 𝗮𝗰𝘁𝗶𝗼𝗻. 𝗣𝗹𝗲𝗮𝘀𝗲 𝗿𝗲𝘀𝘁𝗮𝗿𝘁 𝘁𝗵𝗲 𝗽𝗿𝗼𝗰𝗲𝘀𝘀.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    current_selected_platforms = state_data.get("selected_platforms", {})
    user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": current_selected_platforms}
    await safe_edit_message(
        query.message,
        f"✅ 𝗨𝘀𝗲𝗿 𝗜𝗗 `{target_user_id}` 𝗿𝗲𝗰𝗲𝗶𝘃𝗲𝗱. 𝗦𝗲𝗹𝗲𝗰𝘁 𝗽𝗹𝗮𝘁𝗳𝗼𝗿𝗺𝘀 𝗳𝗼𝗿 𝗽𝗿𝗲𝗺𝗶𝘂𝗺:",
        reply_markup=get_platform_selection_markup(user_id, current_selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "📢 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗻𝗱 𝘁𝗵𝗲 𝗺𝗲𝘀𝘀𝗮𝗴𝗲 𝘆𝗼𝘂 𝘄𝗮𝗻𝘁 𝘁𝗼 𝗯𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝘁𝗼 𝗮𝗹𝗹 𝘂𝘀𝗲𝗿𝘀.\n\n"
        "𝗨𝘀𝗲 `/broadcast <message>` 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗶𝗻𝘀𝘁𝗲𝗮𝗱."
    )

@app.on_callback_query(filters.regex("^admin_stats_panel$"))
async def admin_stats_panel_cb(_, query):
    if not is_admin(query.from_user.id):
        return await query.answer("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱", show_alert=True)
    
    total_users = db.users.count_documents({})
    total_uploads = db.uploads.count_documents({})
    
    stats_text = (
        "📊 **𝗔𝗱𝗺𝗶𝗻 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀 𝗣𝗮𝗻𝗲𝗹**\n\n"
        f"**𝗧𝗼𝘁𝗮𝗹 𝗨𝘀𝗲𝗿𝘀**: `{total_users}`\n"
        f"**𝗧𝗼𝘁𝗮𝗹 𝗨𝗽𝗹𝗼𝗮𝗱𝘀**: `{total_uploads}`\n\n"
        "𝗨𝘀𝗲 `/stats` 𝗰𝗼𝗺𝗺𝗮𝗻𝗱 𝗳𝗼𝗿 𝗺𝗼𝗿𝗲 𝗱𝗲𝘁𝗮𝗶𝗹𝗲𝗱 𝘀𝘁𝗮𝘁𝘀."
    )
    
    await safe_edit_message(query.message, stats_text, reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)

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

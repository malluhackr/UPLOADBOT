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

from TikTokApi import TikTokApi

async def tiktok_login():
    try:
        api = TikTokApi()
        await api.create_sessions(headless=True, username="your_username", password="your_password")
    except Exception as e:
        print(f"Login failed: {e}")

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

# Session file path for the bot's primary Instagram client
SESSION_FILE = "instagrapi_session.json"
TIKTOK_SESSION_FILE = "tiktok_session.json"

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
    }
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

PREMIUM_PLATFORMS = ["instagram", "tiktok"]

# Keyboards
def get_main_keyboard(user_id):
    buttons = [
        [KeyboardButton("⚙️ Settings"), KeyboardButton("📊 Stats")]
    ]
    is_instagram_premium = is_premium_for_platform(user_id, "instagram")
    is_tiktok_premium = is_premium_for_platform(user_id, "tiktok")

    upload_buttons_row = []
    if is_instagram_premium:
        upload_buttons_row.extend([KeyboardButton("📸 Insta Photo"), KeyboardButton("📤 Insta Reel")])
    if is_tiktok_premium:
        upload_buttons_row.extend([KeyboardButton("🎵 TikTok Video"), KeyboardButton("🖼️ TikTok Photo")])

    if upload_buttons_row:
        buttons.insert(0, upload_buttons_row)

    buttons.append([KeyboardButton("⭐ Premium"), KeyboardButton("/premiumdetails")])
    if is_admin(user_id):
        buttons.append([KeyboardButton("🛠 Admin Panel"), KeyboardButton("🔄 Restart Bot")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)


settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
    [InlineKeyboardButton("📐 Aspect Ratio (Video)", callback_data="set_aspect_ratio")],
    [InlineKeyboardButton("🗜️ Toggle Compression", callback_data="toggle_compression")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Manage Premium", callback_data="manage_premium")],
    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_message")],
    [InlineKeyboardButton("⚙️ Global Settings", callback_data="global_settings_panel")],
    [InlineKeyboardButton("📊 Stats Panel", callback_data="admin_stats_panel")],
    [InlineKeyboardButton("💰 Payment Settings", callback_data="payment_settings_panel")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝗠𝗲𝗻𝘂", callback_data="back_to_main_menu")]
])

admin_global_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Onam Toggle", callback_data="toggle_onam")],
    [InlineKeyboardButton("Max Upload Users", callback_data="set_max_uploads")],
    [InlineKeyboardButton("Reset Stats", callback_data="reset_stats")],
    [InlineKeyboardButton("Show System Stats", callback_data="show_system_stats")],
    [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
])

payment_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Google Play QR Code", callback_data="set_payment_google_play")],
    [InlineKeyboardButton("UPI", callback_data="set_payment_upi")],
    [InlineKeyboardButton("UST", callback_data="set_payment_ust")],
    [InlineKeyboardButton("BTC", callback_data="set_payment_btc")],
    [InlineKeyboardButton("Others", callback_data="set_payment_others")],
    [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")]
])

upload_type_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Reel", callback_data="set_type_reel")],
    [InlineKeyboardButton("📷 Post", callback_data="set_type_post")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_settings")]
])

aspect_ratio_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Original Aspect Ratio", callback_data="set_ar_original")],
    [InlineKeyboardButton("9:16 (Crop/Fit)", callback_data="set_ar_9_16")],
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸", callback_data="back_to_settings")]
])

def get_platform_selection_markup(user_id, current_selection=None):
    if current_selection is None:
        current_selection = {}
    buttons = []
    for platform in PREMIUM_PLATFORMS:
        emoji = "✅" if current_selection.get(platform) else "⬜"
        buttons.append([InlineKeyboardButton(f"{emoji} {platform.capitalize()}", callback_data=f"select_platform_{platform}")])
    buttons.append([InlineKeyboardButton("➡️ Continue to Plans", callback_data="confirm_platform_selection")])
    buttons.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_premium_plan_markup(selected_platforms):
    buttons = []
    for key, value in PREMIUM_PLANS.items():
        buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()}", callback_data=f"show_plan_details_{key}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")])
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
            
    buttons.append([InlineKeyboardButton(f"💰 BUY NOW ({price_string})", callback_data=f"buy_now_{plan_key}_{price_multiplier}")])
    buttons.append([InlineKeyboardButton("➡️ Check Payment Methods", callback_data="show_payment_methods")])
    buttons.append([InlineKeyboardButton("🔙 Back to Plans", callback_data="buypypremium")])
    return InlineKeyboardMarkup(buttons)


def get_payment_methods_markup():
    payment_buttons = []
    settings = global_settings.get("payment_settings", {})
    if settings.get("google_play"):
        payment_buttons.append([InlineKeyboardButton("Google Play QR Code", callback_data="show_payment_qr_google_play")])
    if settings.get("upi"):
        payment_buttons.append([InlineKeyboardButton("UPI", callback_data="show_payment_details_upi")])
    if settings.get("ust"):
        payment_buttons.append([InlineKeyboardButton("UST", callback_data="show_payment_details_ust")])
    if settings.get("btc"):
        payment_buttons.append([InlineKeyboardButton("BTC", callback_data="show_payment_details_btc")])
    if settings.get("others"):
        payment_buttons.append([InlineKeyboardButton("Other Methods", callback_data="show_payment_details_others")])

    payment_buttons.append([InlineKeyboardButton("🔙 Back to Premium Plans", callback_data="buypypremium")])
    return InlineKeyboardMarkup(payment_buttons)


def get_upload_buttons(user_id):
    buttons = [
        [InlineKeyboardButton("➡️ Use default caption", callback_data="skip_caption")],
        [InlineKeyboardButton("❌ Cancel Upload", callback_data="cancel_upload")],
    ]
    return InlineKeyboardMarkup(buttons)

def get_progress_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")]
    ])

def get_caption_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Skip (use default)", callback_data="skip_caption")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")]
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

async def save_tiktok_session(user_id, session_data):
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"tiktok_session": session_data}},
        upsert=True
    )
    logger.info(f"TikTok session saved for user {user_id}")

async def load_tiktok_session(user_id):
    session = db.sessions.find_one({"user_id": user_id})
    return session.get("tiktok_session") if session else None

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
        "🔄 Bot Restart Initiated!\n\n"
        f"📅 Date: {dt['date']}\n"
        f"⏰ Time: {dt['time']}\n"
        f"🌐 Timezone: {dt['timezone']}\n"
        f"👤 By: {msg.from_user.mention} (ID: {msg.from_user.id})"
    )
    logger.info(f"User {msg.from_user.id} attempting restart command.")
    await send_log_to_channel(app, LOG_CHANNEL, restart_msg_log)
    await msg.reply("✅ Bot is restarting...")
    await asyncio.sleep(2)
    try:
        logger.info("Executing os.execv to restart process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execute restart via os.execv: {e}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ Restart failed for {msg.from_user.id}: {str(e)}")
        await msg.reply(f"❌ Failed to restart bot: {str(e)}")

def load_instagram_client_session():
    if INSTAGRAM_PROXY:
        insta_client.set_proxy(INSTAGRAM_PROXY)
        logger.info(f"Instagram proxy set to: {INSTAGRAM_PROXY}")
    else:
        logger.info("No Instagram proxy configured for bot's client.")

    if os.path.exists(SESSION_FILE):
        try:
            insta_client.load_settings(SESSION_FILE)
            logger.info("Loaded instagrapi session from file.")
            insta_client.get_timeline_feed()
            logger.info("Instagrapi session is valid for bot's client.")
            return True
        except LoginRequired:
            logger.warning("Instagrapi session expired for bot's client. Attempting fresh login.")
            insta_client.set_settings({})
        except Exception as e:
            logger.error(f"Error loading instagrapi session for bot's client: {e}. Attempting fresh login.")
            insta_client.set_settings({})

    if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD:
        logger.info(f"Attempting initial login for bot's primary Instagram account: {INSTAGRAM_USERNAME}")
        try:
            insta_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            insta_client.dump_settings(SESSION_FILE)
            logger.info(f"Successfully logged in and saved session for {INSTAGRAM_USERNAME}")
            return True
        except ChallengeRequired:
            logger.critical(f"Instagram Challenge Required for bot's primary account {INSTAGRAM_USERNAME}. Please complete it manually.")
            return False
        except (BadPassword, LoginRequired) as e:
            logger.critical(f"Login failed for bot's primary account {INSTAGRAM_USERNAME}: {e}. Check credentials.")
            return False
        except PleaseWaitFewMinutes:
            logger.critical(f"Instagram is asking to wait for bot's primary account {INSTAGRAM_USERNAME}. Please try again later.")
            return False
        except Exception as e:
            logger.critical(f"Unhandled error during initial login for bot's primary account {INSTAGRAM_USERNAME}: {e}")
            return False
    else:
        logger.warning("INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD not set in .env. Bot's primary Instagram client will not be logged in.")
        return False

# Progress bar function
def progress_callback(current, total, ud_type, msg, start_time):
    percentage = current * 100 / total
    speed = current / (time.time() - start_time)
    elapsed_time = time.time() - start_time
    eta = (total - current) / speed
    
    progress_bar = f"[{'█' * int(percentage / 5)}{' ' * (20 - int(percentage / 5))}]"
    
    progress_text = (
        f"{ud_type} progress: `{progress_bar}`\n"
        f"📊 Percentage: `{percentage:.2f}%`\n"
        f"✅ Downloaded: `{current / (1024 * 1024):.2f}` MB\n"
        f"📦 Total size: `{total / (1024 * 1024):.2f}` MB\n"
        f"🚀 Speed: `{speed / (1024 * 1024):.2f}` MB/s\n"
        f"⏳ ETA: `{timedelta(seconds=eta)}`"
    )
    
    if int(percentage) % 5 == 0 and not msg.is_progress_message_updated:
        try:
            asyncio.run(msg.edit_text(progress_text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup()))
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
                logger.info(f"Deleted local file: {file_path}")
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {e}")

# Decorator to handle user locks and state management
def with_user_lock(func):
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        if user_id not in user_upload_locks:
            user_upload_locks[user_id] = asyncio.Lock()

        if user_upload_locks[user_id].locked():
            return await message.reply("⚠️ Another operation is already in progress. Please wait until it's finished or use the `❌ Cancel` button.")

        async with user_upload_locks[user_id]:
            return await func(client, message, *args, **kwargs)
    return wrapper

# --- Message Handlers ---

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    if is_admin(user_id):
        welcome_msg = "🤖 **ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ & ᴛɪᴋᴛᴏᴋ ᴜᴘʟᴏᴀᴅ ʙᴏᴛ!**\n\n"
        welcome_msg += "🛠️ ʏᴏᴜ ʜᴀᴠᴇ **ᴀᴅᴍɪɴ ᴘʀɪᴠɪʟᴇɢᴇꜱ**."
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = _get_user_data(user_id)
    is_new_user = not user
    if is_new_user:
        _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")
        
        welcome_msg = (
            f"👋 **ʜɪ {user_first_name}!**\n\n"
            "ᴛʜɪꜱ ʙᴏᴛ ʟᴇᴛꜱ ʏᴏᴜ ᴜᴘʟᴏᴀᴅ ᴀɴʏ ꜱɪᴢᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ & ᴘᴏꜱᴛꜱ ᴅɪʀᴇᴄᴛʟʏ ꜰʀᴏᴍ ᴛᴇʟᴇɢʀᴀᴍ.\n\n"
            "ᴛᴏ ɢᴇᴛ ᴀ ᴛᴀꜱᴛᴇ ᴏꜰ ᴛʜᴇ ᴘʀᴇᴍɪᴜᴍ ꜰᴇᴀᴛᴜʀᴇꜱ, ʏᴏᴜ ᴄᴀɴ ᴀᴄᴛɪᴠᴀᴛᴇ ᴀ **ꜰʀᴇᴇ 3-ʜᴏᴜʀ ᴛʀɪᴀʟ** ꜰᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ ʀɪɢʜᴛ ɴᴏᴡ!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲 𝗙𝗿𝗲𝗲 3-𝗛𝗼𝘂𝗿", callback_data="activate_trial")],
            [InlineKeyboardButton("➡️ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺", callback_data="buypypremium")]
        ])
        await msg.reply(welcome_msg, reply_markup=trial_markup, parse_mode=enums.ParseMode.MARKDOWN)
        return
    else:
        _save_user_data(user_id, {"last_active": datetime.utcnow()})

    onam_toggle = global_settings.get("onam_toggle", False)
    if onam_toggle:
        onam_text = (
            f"🎉 **ʜᴀᴘᴘʏ ᴏɴᴀᴍ!** 🎉\n\n"
            f"ᴡɪꜱʜɪɴɢ ʏᴏᴜ ᴀ ꜱᴇᴀꜱᴏɴ ᴏꜰ ᴘʀᴏꜱᴘᴇʀɪᴛʏ ᴀɴᴅ ʜᴀᴘᴘɪɴᴇꜱꜱ. ᴇɴᴊᴏʏ ᴛʜᴇ ꜰᴇꜱᴛɪᴠɪᴛɪᴇꜱ ᴡɪᴛʜ ᴏᴜʀ ᴇxᴄʟᴜꜱɪᴠᴇ **ᴏɴᴀᴍ ʀᴇᴇʟ ᴜᴘʟᴏᴀᴅꜱ** ꜰᴇᴀᴛᴜʀᴇ!\n\n"
            f"ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ꜱᴛᴀʀᴛ ᴜᴘʟᴏᴀᴅɪɴɢ ʏᴏᴜʀ ꜰᴇꜱᴛɪᴠᴀʟ ᴄᴏɴᴛᴇɴᴛ!"
        )
        await msg.reply(onam_text, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user_premium = _get_user_data(user_id).get("premium", {})
    instagram_premium_data = user_premium.get("instagram", {})
    tiktok_premium_data = user_premium.get("tiktok", {})

    welcome_msg = f"🚀 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛᴇʟᴇɢʀᴀᴍ ➜ ɪɴꜱᴛᴀɢʀᴀᴍ & ᴛɪᴋᴛᴏᴋ ᴅɪʀᴇᴄᴛ ᴜᴘʟᴏᴀᴅᴇʀ\n\n"
    premium_details_text = ""
    is_admin_user = is_admin(user_id)
    if is_admin_user:
        premium_details_text += "🛠️ ʏᴏᴜ ʜᴀᴠᴇ **ᴀᴅᴍɪɴ ᴘʀɪᴠɪʟᴇɢᴇꜱ**.\n\n"

    ig_premium_until = instagram_premium_data.get("until")
    tt_premium_until = tiktok_premium_data.get("until")

    if is_premium_for_platform(user_id, "instagram"):
        if ig_premium_until:
            remaining_time = ig_premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ ɪɴꜱᴛᴀɢʀᴀᴍ ᴘʀᴇᴍɪᴜᴍ ᴇxᴘɪʀᴇꜱ ɪɴ: `{days} days, {hours} hours`.\n"
    
    if is_premium_for_platform(user_id, "tiktok"):
        if tt_premium_until:
            remaining_time = tt_premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ ᴛɪᴋᴛᴏᴋ ᴘʀᴇᴍɪᴜᴍ ᴇxᴘɪʀᴇꜱ ɪɴ: `{days} days, {hours} hours`.\n"

    if not is_admin_user and not premium_details_text.strip():
        premium_details_text = (
            "🔥 **ᴋᴇʏ ꜰᴇᴀᴛᴜʀᴇꜱ:**\n"
            "✅ ᴅɪʀᴇᴄᴛ ʟᴏɢɪɴ (ɴᴏ ᴛᴏᴋᴇɴꜱ ɴᴇᴇᴅᴇᴅ)\n"
            "✅ ᴜʟᴛʀᴀ-ꜰᴀꜱᴛ ᴜᴘʟᴏᴀᴅɪɴɢ\n"
            "✅ ʜɪɢʜ Qᴜᴀʟɪᴛʏ / ꜰᴀꜱᴛ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ\n"
            "✅ ɴᴏ ꜰɪʟᴇ ꜱɪᴢᴇ ʟɪᴍɪᴛ\n"
            "✅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴜᴘʟᴏᴀᴅꜱ\n"
            "✅ ɪɴꜱᴛᴀɢʀᴀᴍ & ᴛɪᴋᴛᴏᴋ ꜱᴜᴘᴘᴏʀᴛ\n"
            "✅ ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ ᴀꜰᴛᴇʀ ᴜᴘʟᴏᴀᴅ (ᴏᴘᴛɪᴏɴᴀʟ)\n\n"
            "👤 ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ ᴛᴏᴍ → [ᴄʟɪᴄᴋ ʜᴇʀᴇ](t.me/CjjTom) ᴛᴏ ɢᴇᴛ ᴘʀᴇᴍɪᴜᴍ ɴᴏᴡ\n"
            "🔐 ʏᴏᴜʀ ᴅᴀᴛᴀ ɪꜱ ꜰᴜʟʟʏ ✅ ᴇɴᴅ ᴛᴏ ᴇɴᴅ ᴇɴᴄʀʏᴘᴛᴇᴅ\n\n"
            f"🆔 ʏᴏᴜʀ ɪᴅ: `{user_id}`"
        )
    
    welcome_msg += premium_details_text
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱.")
    restarting_msg = await msg.reply("♻️ ʀᴇꜱᴛᴀʀᴛɪɴɢ ʙᴏᴛ...")
    await asyncio.sleep(1)
    await restart_bot(msg)

@app.on_message(filters.command("login"))
@with_user_lock
async def login_cmd(_, msg):
    logger.info(f"User {msg.from_user.id} attempting Instagram login command.")
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply(" ❌ 𝗡𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱. ᴘʟᴇᴀꜱᴇ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴘʀᴇᴍɪᴜᴍ ᴡɪᴛʜ /buypypremium.")
    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("ᴜꜱᴀɢᴇ: `/login <instagram_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)
    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 ᴀᴛᴛᴇᴍᴘᴛɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ...")
    try:
        user_insta_client = InstaClient()
        user_insta_client.delay_range = [1, 3]
        if INSTAGRAM_PROXY:
            user_insta_client.set_proxy(INSTAGRAM_PROXY)
            logger.info(f"Applied proxy {INSTAGRAM_PROXY} to user {user_id}'s Instagram login attempt.")

        session = await load_instagram_session(user_id)
        if session:
            logger.info(f"Attempting to load existing Instagram session for user {user_id} (IG: {username}).")
            user_insta_client.set_settings(session)
            try:
                await asyncio.to_thread(user_insta_client.get_timeline_feed)
                await login_msg.edit_text(f"✅ ᴀʟʀᴇᴀᴅʏ ʟᴏɢɢᴇᴅ ɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀꜱ `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
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

        await login_msg.edit_text("✅ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗹𝗼𝗴𝗶𝗻 𝘀𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹 !")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"📝 ɴᴇᴡ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ\nᴜꜱᴇʀ: `{user_id}`\n"
            f"ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
            f"ɪɴꜱᴛᴀɢʀᴀᴍ: `{username}`"
        )
        logger.info(f"Instagram login successful for user {user_id} ({username}).")

    except ChallengeRequired:
        await login_msg.edit_text("🔐 ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇQᴜɪʀᴇꜱ ᴄʜᴀʟʟᴇɴɢᴇ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ. ᴘʟᴇᴀꜱᴇ ᴄᴏᴍᴘʟᴇᴛᴇ ɪᴛ ɪɴ ᴛʜᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀᴘᴘ ᴀɴᴅ ᴛʀʏ ᴀɢᴀɪɴ.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ ɪɴꜱᴛᴀɢʀᴀᴍ ᴄʜᴀʟʟᴇɴɢᴇ ʀᴇQᴜɪʀᴇᴅ ꜰᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`).")
        logger.warning(f"Instagram Challenge Required for user {user_id} ({username}).")
    except (BadPassword, LoginRequired) as e:
        await login_msg.edit_text(f"❌ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ꜰᴀɪʟᴇᴅ: {e}. ᴘʟᴇᴀꜱᴇ ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴄʀᴇᴅᴇɴᴛɪᴀʟꜱ.")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ꜰᴀɪʟᴇᴅ ꜰᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`): {e}")
        logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
    except PleaseWaitFewMinutes:
        await login_msg.edit_text("⚠️ ɪɴꜱᴛᴀɢʀᴀᴍ ɪꜱ ᴀꜱᴋɪɴɢ ᴛᴏ ᴡᴀɪᴛ ᴀ ꜰᴇᴡ ᴍɪɴᴜᴛᴇꜱ ʙᴇꜰᴏʀᴇ ᴛʀʏɪɴɢ ᴀɢᴀɪɴ. ᴘʟᴇᴀꜱᴇ ᴛʀʏ ᴀꜰᴛᴇʀ ꜱᴏᴍᴇ ᴛɪᴍᴇ.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ ɪɴꜱᴛᴀɢʀᴀᴍ 'ᴘʟᴇᴀꜱᴇ ᴡᴀɪᴛ' ꜰᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`).")
        logger.warning(f"Instagram 'Please Wait' for user {user_id} ({username}).")
    except Exception as e:
        await login_msg.edit_text(f"❌ ᴀɴ ᴜɴᴇxᴘᴇᴄᴛᴇᴅ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ ᴅᴜʀɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ: {str(e)}")
        logger.error(f"ᴜɴʜᴀɴᴅʟᴇᴅ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ꜰᴏʀ {user_id} ({username}): {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"🔥 ᴄʀɪᴛɪᴄᴀʟ ɪɴꜱᴛᴀɢʀᴀᴍ ʟᴏɢɪɴ ᴇʀʀᴏʀ ꜰᴏʀ ᴜꜱᴇʀ `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.command("tiktoklogin"))
@with_user_lock
async def tiktok_login_cmd(_, msg):
    logger.info(f"User {msg.from_user.id} attempting TikTok login command.")
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴜꜱᴇ ᴛɪᴋᴛᴏᴋ ꜰᴇᴀᴛᴜʀᴇꜱ. ᴘʟᴇᴀꜱᴇ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴛɪᴋᴛᴏᴋ ᴘʀᴇᴍɪᴜᴍ ᴡɪᴛʜ /buypypremium.")

    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("ᴜꜱᴀɢᴇ: `/tiktoklogin <tiktok_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 ᴀᴛᴛᴇᴍᴘᴛɪɴɢ ᴛɪᴋᴛᴏᴋ ʟᴏɢɪɴ...")
    api = None
    try:
        api = TikTokApi()
        session = await load_tiktok_session(user_id)
        
        if session:
            try:
                await api.create_sessions(
                    session_path=TIKTOK_SESSION_FILE,
                    headless=True,
                    browser_session_id=session.get('browser_session_id')
                )
                await api.get_for_you_feed()
                await login_msg.edit_text(f"✅ ᴀʟʀᴇᴀᴅʏ ʟᴏɢɢᴇᴅ ɪɴ ᴛᴏ ᴛɪᴋᴛᴏᴋ ᴀꜱ `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                _save_user_data(user_id, {"tiktok_username": username})
                return
            except Exception as e:
                logger.warning(f"Failed to validate TikTok session for user {user_id}: {e}. Retrying with fresh login.")
            finally:
                if api and getattr(api, 'browser', None):
                    await api.browser.close()
        
        # Re-initialize api for fresh login
        api = TikTokApi()
        # Fresh login
        await api.create_sessions(
            session_path=TIKTOK_SESSION_FILE,
            headless=True,
            username=username,
            password=password
        )
        session_data = {'browser_session_id': api.browser_session_id}
        await save_tiktok_session(user_id, session_data)
        _save_user_data(user_id, {"tiktok_username": username})
        await login_msg.edit_text("✅ ᴛɪᴋᴛᴏᴋ ʟᴏɢɪɴ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ!")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"📝 ɴᴇᴡ ᴛɪᴋᴛᴏᴋ ʟᴏɢɪɴ\nᴜꜱᴇʀ: `{user_id}`\n"
            f"ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
            f"ᴛɪᴋᴛᴏᴋ: `{username}`"
        )
    except Exception as e:
        # Catch all exceptions related to the login process
        if "login" in str(e).lower() or "captcha" in str(e).lower():
            await login_msg.edit_text(f"❌ ᴛɪᴋᴛᴏᴋ ʟᴏɢɪɴ ꜰᴀɪʟᴇᴅ: {e}. ᴘʟᴇᴀꜱᴇ ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴄʀᴇᴅᴇɴᴛɪᴀʟꜱ ᴏʀ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ.")
        else:
            await login_msg.edit_text(f"❌ ᴀɴ ᴜɴᴇxᴘᴇᴄᴛᴇᴅ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ ᴅᴜʀɪɴɢ ᴛɪᴋᴛᴏᴋ ʟᴏɢɪɴ: {str(e)}")
        logger.error(f"ᴜɴʜᴀɴᴅʟᴇᴅ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ᴛɪᴋᴛᴏᴋ ʟᴏɢɪɴ ꜰᴏʀ {user_id} ({username}): {str(e)}")
    finally:
        if api and getattr(api, 'browser', None):
            await api.browser.close()

@app.on_message(filters.regex("⭐ Premium"))
async def show_premium_options(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    premium_plans_text = (
        "⭐ **ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ!** ⭐\n\n"
        "ᴜɴʟᴏᴄᴋ ꜰᴜʟʟ ꜰᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜᴘʟᴏᴀᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ ꜰᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀɴᴅ ᴛɪᴋᴛᴏᴋ!\n\n"
        "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴘʟᴀɴꜱ:**"
    )
    await msg.reply(premium_plans_text, reply_markup=get_premium_plan_markup([]), parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user = _get_user_data(user_id)
    if not user:
        return await msg.reply("ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ʀᴇɢɪꜱᴛᴇʀᴇᴅ ᴡɪᴛʜ ᴛʜᴇ ʙᴏᴛ. ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ /start.")

    if is_admin(user_id):
        return await msg.reply("👑 ʏᴏᴜ ᴀʀᴇ ᴛʜᴇ **ᴀᴅᴍɪɴ**. ʏᴏᴜ ʜᴀᴠᴇ ᴘᴇʀᴍᴀɴᴇɴᴛ ꜰᴜʟʟ ᴀᴄᴄᴇꜱꜱ ᴛᴏ ᴀʟʟ ꜰᴇᴀᴛᴜʀᴇꜱ!", parse_mode=enums.ParseMode.MARKDOWN)

    status_text = "⭐ **ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ꜱᴛᴀᴛᴜꜱ:**\n\n"
    has_premium_any = False

    for platform in PREMIUM_PLATFORMS:
        platform_premium = user.get("premium", {}).get(platform, {})
        premium_type = platform_premium.get("type")
        premium_until = platform_premium.get("until")

        status_text += f"**{platform.capitalize()} ᴘʀᴇᴍɪᴜᴍ:** "
        if premium_type == "lifetime":
            status_text += "🎉 **ʟɪꜰᴇᴛɪᴍᴇ!**\n"
            has_premium_any = True
        elif premium_until and premium_until > datetime.utcnow():
            remaining_time = premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            minutes = (remaining_time.seconds % 3600) // 60
            status_text += (
                f"`{premium_type.replace('_', ' ').title()}` ᴇxᴘɪʀᴇꜱ ᴏɴ: "
                f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`\n"
                f"ᴛɪᴍᴇ ʀᴇᴍᴀɪɴɪɴɢ: `{days} days, {hours} hours, {minutes} minutes`\n"
            )
            has_premium_any = True
        else:
            status_text += "😔 **ɴᴏᴛ ᴀᴄᴛɪᴠᴇ.**\n"
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
    await msg.reply("⚠️ **ᴡᴀʀɴɪɴɢ!** ᴛʜɪꜱ ᴡɪʟʟ ᴄʟᴇᴀʀ ᴀʟʟ ʏᴏᴜʀ ꜱᴀᴠᴇᴅ ꜱᴇꜱꜱɪᴏɴꜱ ᴀɴᴅ ꜱᴇᴛᴛɪɴɢꜱ. ᴀʀᴇ ʏᴏᴜ ꜱᴜʀᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ʏᴇꜱ, ʀᴇꜱᴇᴛ ᴍʏ ᴘʀᴏꜰɪʟᴇ", callback_data="confirm_reset_profile")],
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
    
    user_states.pop(user_id, None)
    
    await query.answer("✅ ʏᴏᴜʀ ᴘʀᴏꜰɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ʀᴇꜱᴇᴛ. ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ /start ᴛᴏ ʙᴇɢɪɴ ᴀɢᴀɪɴ.", show_alert=True)
    await safe_edit_message(query.message, "✅ ʏᴏᴜʀ ᴘʀᴏꜰɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ʀᴇꜱᴇᴛ. ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ /start ᴛᴏ ʙᴇɢɪɴ ᴀɢᴀɪɴ.")

@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ ᴛᴏ ᴀᴄᴄᴇꜱꜱ ꜱᴇᴛᴛɪɴɢꜱ.")
    
    current_settings = await get_user_settings(user_id)
    compression_status = "ᴏꜰꜰ (ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)" if not current_settings.get("no_compression") else "ᴏɴ (ᴏʀɪɢɪɴᴀʟ Qᴜᴀʟɪᴛʏ)"
    
    settings_text = "⚙️ ꜱᴇᴛᴛɪɴɢꜱ ᴘᴀɴᴇʟ\n\n" \
                    f"🗜️ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟʏ: **{compression_status}**\n\n" \
                    "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ ʏᴏᴜʀ ᴘʀᴇꜰᴇʀᴇɴᴄᴇꜱ."

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ ᴜꜱᴇʀ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="user_settings_personal")]
        ])
    else:
        markup = settings_markup

    await msg.reply(settings_text, reply_markup=markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("📤 Insta Reel"))
@with_user_lock
async def initiate_instagram_reel_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ ʏᴏᴜʀ ᴀᴄᴄᴇꜱꜱ ʜᴀꜱ ʙᴇᴇɴ ᴅᴇɴɪᴇᴅ. ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴘʀᴇᴍɪᴜᴍ ᴛᴏ ᴜɴʟᴏᴄᴋ ʀᴇᴇʟꜱ ᴜᴘʟᴏᴀᴅ. /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ ᴘʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ꜰɪʀꜱᴛ ᴜꜱɪɴɢ `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("✅ ꜱᴇɴᴅ ᴠɪᴅᴇᴏ ꜰɪʟᴇ - ʀᴇᴇʟ ʀᴇᴀᴅʏ!!")
    user_states[user_id] = {"action": "waiting_for_instagram_reel_video", "platform": "instagram", "upload_type": "reel"}

@app.on_message(filters.regex("📸 Insta Photo"))
@with_user_lock
async def initiate_instagram_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("🚫 ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴜᴘʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴘʜᴏᴛᴏꜱ ᴘʟᴇᴀꜱᴇ ᴜᴘɢʀᴀᴅᴇ ᴘʀᴇᴍɪᴜᴍ /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ ᴘʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ꜰɪʀꜱᴛ ᴜꜱɪɴɢ `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ ꜱᴇɴᴅ ᴘʜᴏᴛᴏ ꜰɪʟᴇ - ʀᴇᴀᴅʏ ꜰᴏʀ ɪɢ!.")
    user_states[user_id] = {"action": "waiting_for_instagram_photo_image", "platform": "instagram", "upload_type": "post"}

@app.on_message(filters.regex("🎵 TikTok Video"))
@with_user_lock
async def initiate_tiktok_video_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴜᴘʟᴏᴀᴅ ᴛɪᴋᴛᴏᴋ ᴠɪᴅᴇᴏꜱ. ᴘʟᴇᴀꜱᴇ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴛɪᴋᴛᴏᴋ ᴘʀᴇᴍɪᴜᴍ ᴡɪᴛʜ /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ ᴘʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ᴛɪᴋᴛᴏᴋ ꜰɪʀꜱᴛ ᴜꜱɪɴɢ `/tiktoklogin <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ ʀᴇᴀᴅʏ ꜰᴏʀ ᴛɪᴋᴛᴏᴋ ᴠɪᴅᴇᴏ ᴜᴘʟᴏᴀᴅ!")
    user_states[user_id] = {"action": "waiting_for_tiktok_video", "platform": "tiktok", "upload_type": "video"}

@app.on_message(filters.regex("🖼️ TikTok Photo"))
@with_user_lock
async def initiate_tiktok_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴜᴘʟᴏᴀᴅ ᴛɪᴋᴛᴏᴋ ᴘʜᴏᴛᴏꜱ. ᴘʟᴇᴀꜱᴇ ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴛɪᴋᴛᴏᴋ ᴘʀᴇᴍɪᴜᴍ ᴡɪᴛʜ /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ ᴛɪᴋᴛᴏᴋ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ᴛɪᴋᴛᴏᴋ ꜰɪʀꜱᴛ ᴜꜱɪɴɢ `/tiktoklogin <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ ʀᴇᴀᴅʏ ꜰᴏʀ ᴛɪᴋᴛᴏᴋ ᴘʜᴏᴛᴏ ᴜᴘʟᴏᴀᴅ!")
    user_states[user_id] = {"action": "waiting_for_tiktok_photo", "platform": "tiktok", "upload_type": "photo"}

@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLANS):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ʏᴏᴜ ɴᴇᴇᴅ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ꜰᴏʀ ᴀᴛ ʟᴇᴀꜱᴛ ᴏɴᴇ ᴘʟᴀᴛꜰᴏʀᴍ ᴛᴏ ᴠɪᴇᴡ ꜱᴛᴀᴛꜱ.")

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
    total_tiktok_video_uploads = db.uploads.count_documents({"platform": "tiktok", "upload_type": "video"})
    total_tiktok_photo_uploads = db.uploads.count_documents({"platform": "tiktok", "upload_type": "photo"})
    premium_percentage = (total_premium_users / total_users * 100) if total_users > 0 else 0

    stats_text = (
        "📊 **ʙᴏᴛ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ:**\n\n"
        f"**ᴜꜱᴇʀꜱ**\n"
        f"👥 ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ: `{total_users}`\n"
        f"👑 ᴀᴅᴍɪɴ ᴜꜱᴇʀꜱ: `{db.users.count_documents({'_id': ADMIN_ID})}`\n"
        f"⭐ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ: `{total_premium_users}` (`{premium_percentage:.2f}%`)\n"
    )
    for platform in PREMIUM_PLATFORMS:
        platform_premium_percentage = (premium_counts[platform] / total_users * 100) if total_users > 0 else 0
        stats_text += f"   - {platform.capitalize()} ᴘʀᴇᴍɪᴜᴍ: `{premium_counts[platform]}` (`{platform_premium_percentage:.2f}%`)\n"

    stats_text += (
        f"\n**ᴜᴘʟᴏᴀᴅꜱ**\n"
        f"📈 ᴛᴏᴛᴀʟ ᴜᴘʟᴏᴀᴅꜱ: `{total_uploads}`\n"
        f"🎬 ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ: `{total_instagram_reel_uploads}`\n"
        f"📸 ɪɴꜱᴛᴀɢʀᴀᴍ ᴘᴏꜱᴛꜱ: `{total_instagram_post_uploads}`\n"
        f"🎵 ᴛɪᴋᴛᴏᴋ ᴠɪᴅᴇᴏꜱ: `{total_tiktok_video_uploads}`\n"
        f"🖼️ ᴛɪᴋᴛᴏᴋ ᴘʜᴏᴛᴏꜱ: `{total_tiktok_photo_uploads}`"
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
    await status_msg.edit_text(f"✅ ʙʀᴏᴀᴅᴄᴀꜱᴛ ꜰɪɴɪꜱʜᴇᴅ!\nꜱᴇɴᴛ ᴛᴏ `{sent_count}` ᴜꜱᴇʀꜱ, ꜰᴀɪʟᴇᴅ ꜰᴏʀ `{failed_count}` ᴜꜱᴇʀꜱ.")
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ ɪɴɪᴛɪᴀᴛᴇᴅ ʙʏ ᴀᴅᴍɪɴ `{msg.from_user.id}`\n"
        f"ꜱᴇɴᴛ: `{sent_count}`, ꜰᴀɪʟᴇᴅ: `{failed_count}`"
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
        await msg.reply(f"✅ ᴄᴀᴘᴛɪᴏɴ ꜱᴇᴛ ᴛᴏ: `{caption}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)

    elif action == "waiting_for_hashtags":
        hashtags = msg.text
        settings = await get_user_settings(user_id)
        settings["hashtags"] = hashtags
        await save_user_settings(user_id, settings)
        await msg.reply(f"✅ ʜᴀꜱʜᴛᴀɢꜱ ꜱᴇᴛ ᴛᴏ: `{hashtags}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    
    elif action.startswith("waiting_for_payment_details_"):
        if not is_admin(user_id):
            return await msg.reply("❌ ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴘᴇʀꜰᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        
        payment_method = action.replace("waiting_for_payment_details_", "")
        details = msg.text
        
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings[payment_method] = details
        _update_global_setting("payment_settings", new_payment_settings)
        
        await msg.reply(f"✅ ᴘᴀʏᴍᴇɴᴛ ᴅᴇᴛᴀɪʟꜱ ꜰᴏʀ **{payment_method.upper()}** ᴜᴘᴅᴀᴛᴇᴅ.", reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴘᴇʀꜰᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ ᴜꜱᴇʀ ɪᴅ `{target_user_id}` ʀᴇᴄᴇɪᴠᴇᴅ. ꜱᴇʟᴇᴄᴛ ᴘʟᴀᴛꜰᴏʀᴍꜱ ꜰᴏʀ ᴘʀᴇᴍɪᴜᴍ:",
                reply_markup=get_platform_selection_markup(user_id, user_states[user_id]["selected_platforms"]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ ɪɴᴠᴀʟɪᴅ ᴜꜱᴇʀ ɪᴅ. ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ.")
            user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_max_uploads":
        if not is_admin(user_id):
            return await msg.reply("❌ ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴘᴇʀꜰᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        try:
            new_limit = int(msg.text)
            if new_limit <= 0:
                return await msg.reply("❌ ᴛʜᴇ ʟɪᴍɪᴛ ᴍᴜꜱᴛ ʙᴇ ᴀ ᴘᴏꜱɪᴛɪᴠᴇ ɪɴᴛᴇɢᴇʀ.")
            _update_global_setting("max_concurrent_uploads", new_limit)
            global upload_semaphore
            upload_semaphore = asyncio.Semaphore(new_limit)
            await msg.reply(f"✅ ᴍᴀxɪᴍᴜᴍ ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴘʟᴏᴀᴅꜱ ꜱᴇᴛ ᴛᴏ `{new_limit}`.", reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
            user_states.pop(user_id, None)
        except ValueError:
            await msg.reply("❌ ɪɴᴠᴀʟɪᴅ ɪɴᴘᴜᴛ. ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ.")
            user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "awaiting_post_title":
        caption = msg.text
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = caption
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await start_upload_task(msg, file_info)
    
    else:
        await msg.reply("ɪ ᴅᴏɴ'ᴛ ᴜɴᴅᴇʀꜱᴛᴀɴᴅ ᴛʜᴀᴛ ᴄᴏᴍᴍᴀɴᴅ. ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ ᴛʜᴇ ᴍᴇɴᴜ ʙᴜᴛᴛᴏɴꜱ ᴛᴏ ɪɴᴛᴇʀᴀᴄᴛ ᴡɪᴛʜ ᴍᴇ.")

@app.on_callback_query(filters.regex("^activate_trial$"))
async def activate_trial_cb(_, query):
    user_id = query.from_user.id
    user = _get_user_data(user_id)
    user_first_name = query.from_user.first_name or "there"

    if user and is_premium_for_platform(user_id, "instagram"):
        await query.answer("Your Instagram trial is already active! Enjoy your premium access.", show_alert=True)
        welcome_msg = f"🤖 **ᴡᴇʟᴄᴏᴍᴇ ʙᴀᴄᴋ, {user_first_name}!**\n\n"
        premium_details_text = ""
        user_premium = user.get("premium", {})
        ig_expiry = user_premium.get("instagram", {}).get("until")
        if ig_expiry:
            remaining_time = ig_expiry - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ ɪɴꜱᴛᴀɢʀᴀᴍ ᴘʀᴇᴍɪᴜᴍ ᴇxᴘɪʀᴇꜱ ɪɴ: `{days} days, {hours} hours`.\n"
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

    await query.answer("✅ ꜰʀᴇᴇ 3-ʜᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ ᴀᴄᴛɪᴠᴀᴛᴇᴅ! ᴇɴᴊᴏʏ!", show_alert=True)
    welcome_msg = (
        f"🎉 **ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ, {user_first_name}!**\n\n"
        f"ʏᴏᴜ ʜᴀᴠᴇ ᴀᴄᴛɪᴠᴀᴛᴇᴅ ʏᴏᴜʀ **3-ʜᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴛʀɪᴀʟ** ꜰᴏʀ **ɪɴꜱᴛᴀɢʀᴀᴍ**.\n\n"
        "ʏᴏᴜ ɴᴏᴡ ʜᴀᴠᴇ ᴀᴄᴄᴇꜱꜱ ᴛᴏ ᴜᴘʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴄᴏɴᴛᴇɴᴛ!\n\n"
        "ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ, ᴘʟᴇᴀꜱᴇ ʟᴏɢ ɪɴ ᴛᴏ ʏᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀᴄᴄᴏᴜɴᴛ ᴡɪᴛʜ:\n"
        "`/login <your_username> <your_password>`\n\n"
        "ᴡᴀɴᴛ ᴍᴏʀᴇ ꜰᴇᴀᴛᴜʀᴇꜱ ᴀꜰᴛᴇʀ ᴛʜᴇ ᴛʀɪᴀʟ ᴇɴᴅꜱ? ᴄʜᴇᴄᴋ ᴏᴜᴛ ᴏᴜʀ ᴘᴀɪᴅ ᴘʟᴀɴꜱ ᴡɪᴛʜ /buypypremium."
    )
    await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buypypremium$"))
async def buypypremium_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    premium_text = (
        "⭐ **ᴜᴘɢʀᴀᴅᴇ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ!** ⭐\n\n"
        "ᴜɴʟᴏᴄᴋ ꜰᴜʟʟ ꜰᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ ꜰᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀɴᴅ ᴛɪᴋᴛᴏᴋ!\n\n"
        "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴘʟᴀɴꜱ:**\n"
    )
    await safe_edit_message(query.message, premium_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_plan_details_"))
async def show_plan_details_cb(_, query):
    user_id = query.from_user.id
    plan_key = query.data.split("show_plan_details_")[1]
    
    # Placeholder for pricing logic based on number of platforms, for now just 1
    price_multiplier = 1 
    
    plan_details = PREMIUM_PLANS[plan_key]
    
    plan_text = (
        f"**{plan_key.replace('_', ' ').title()} Plan Details**\n\n"
        f"**ᴅᴜʀᴀᴛɪᴏɴ**: "
    )
    if plan_details['duration']:
        plan_text += f"{plan_details['duration'].days} ᴅᴀʏꜱ\n"
    else:
        plan_text += "ʟɪꜰᴇᴛɪᴍᴇ\n"
    
    price_string = plan_details['price']
    if '₹' in price_string:
        try:
            base_price = float(price_string.replace('₹', '').split('/')[0].strip())
            calculated_price = base_price * price_multiplier
            price_string = f"₹{int(calculated_price)} / {round(calculated_price * 0.012, 2)}$" # Placeholder conversion
        except ValueError:
            pass

    plan_text += f"**ᴘʀɪᴄᴇ**: {price_string}\n\n"
    plan_text += "ᴛᴏ ᴘᴜʀᴄʜᴀꜱᴇ, ᴄʟɪᴄᴋ 'ʙᴜʏ ɴᴏᴡ' ᴏʀ ᴄʜᴇᴄᴋ ᴛʜᴇ ᴀᴠᴀɪʟᴀʙʟᴇ ᴘᴀʏᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ."

    await safe_edit_message(query.message, plan_text, reply_markup=get_premium_details_markup(plan_key, price_multiplier), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_methods$"))
async def show_payment_methods_cb(_, query):
    user_id = query.from_user.id
    
    payment_methods_text = "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴘᴀʏᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ**\n\n"
    payment_methods_text += "ᴄʜᴏᴏꜱᴇ ʏᴏᴜʀ ᴘʀᴇꜰᴇʀʀᴇᴅ ᴍᴇᴛʜᴏᴅ ᴛᴏ ᴘʀᴏᴄᴇᴇᴅ ᴡɪᴛʜ ᴘᴀʏᴍᴇɴᴛ."
    
    await safe_edit_message(query.message, payment_methods_text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_details_"))
async def show_payment_details_cb(_, query):
    user_id = query.from_user.id
    method = query.data.split("show_payment_details_")[1]
    
    payment_details = global_settings.get("payment_settings", {}).get(method, "No details available.")
    
    text = (
        f"**{method.upper()} ᴘᴀʏᴍᴇɴᴛ ᴅᴇᴛᴀɪʟꜱ**\n\n"
        f"{payment_details}\n\n"
        f"ᴘʟᴇᴀꜱᴇ ᴘᴀʏ ᴛʜᴇ ʀᴇQᴜɪʀᴇᴅ ᴀᴍᴏᴜɴᴛ ᴀɴᴅ ᴄᴏɴᴛᴀᴄᴛ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ᴡɪᴛʜ ᴀ ꜱᴄʀᴇᴇɴꜱʜᴏᴛ ᴏꜰ ᴛʜᴇ ᴘᴀʏᴍᴇɴᴛ ꜰᴏʀ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ."
    )
    
    await safe_edit_message(query.message, text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buy_now_"))
async def buy_now_cb(_, query):
    user_id = query.from_user.id
    data_parts = query.data.split("_")
    plan_key = data_parts[2]
    price_multiplier = int(data_parts[3])
    
    plan_details = PREMIUM_PLANS[plan_key]
    price_string = plan_details['price']
    if '₹' in price_string:
        try:
            base_price = float(price_string.replace('₹', '').split('/')[0].strip())
            calculated_price = base_price * price_multiplier
            price_string = f"₹{int(calculated_price)}"
        except ValueError:
            pass
    
    text = (
        f"**ᴘᴜʀᴄʜᴀꜱᴇ ᴄᴏɴꜰɪʀᴍᴀᴛɪᴏɴ**\n\n"
        f"ʏᴏᴜ ᴀʀᴇ ᴀʙᴏᴜᴛ ᴛᴏ ᴘᴜʀᴄʜᴀꜱᴇ **{plan_key.replace('_', ' ').title()}** ᴘʟᴀɴ ꜰᴏʀ {price_string}.\n\n"
        f"ᴘʟᴇᴀꜱᴇ ᴄᴏɴᴛᴀᴄᴛ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ᴛᴏ ᴄᴏᴍᴘʟᴇᴛᴇ ᴛʜᴇ ᴘᴀʏᴍᴇɴᴛ ᴘʀᴏᴄᴇꜱꜱ."
    )
    await safe_edit_message(query.message, text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^premiumdetails$"))
async def premium_details_cb(_, query):
    await query.message.reply("ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ ᴛʜᴇ `/premiumdetails` ᴄᴏᴍᴍᴀɴᴅ ɪɴꜱᴛᴇᴀᴅ ᴏꜰ ᴛʜɪꜱ ʙᴜᴛᴛᴏɴ.")


@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def user_settings_personal_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if is_admin(user_id) or any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        current_settings = await get_user_settings(user_id)
        compression_status = "ᴏꜰꜰ (ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)" if not current_settings.get("no_compression") else "ᴏɴ (ᴏʀɪɢɪɴᴀʟ Qᴜᴀʟɪᴛʏ)"
        settings_text = "⚙️ ʏᴏᴜʀ ᴘᴇʀꜱᴏɴᴀʟ ꜱᴇᴛᴛɪɴɢꜱ\n\n" \
                        f"🗜️ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟʏ: **{compression_status}**\n\n" \
                        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ ʏᴏᴜʀ ᴘʀᴇꜰᴇʀᴇɴᴄᴇꜱ."
        await safe_edit_message(
        query.message,
        settings_text,
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
        )
    else:
        await query.answer("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ.", show_alert=True)
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
            "🏠 ᴍᴀɪɴ ᴍᴇɴᴜ",
            reply_markup=get_main_keyboard(user_id)
        )
    elif data == "back_to_settings":
        current_settings = await get_user_settings(user_id)
        compression_status = "ᴏꜰꜰ (ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)" if not current_settings.get("no_compression") else "ᴏɴ (ᴏʀɪɢɪɴᴀʟ Qᴜᴀʟɪᴛʏ)"
        settings_text = "⚙️ ꜱᴇᴛᴛɪɴɢꜱ ᴘᴀɴᴇʟ\n\n" \
                        f"🗜️ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟʏ: **{compression_status}**\n\n" \
                        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ ʏᴏᴜʀ ᴘʀᴇꜰᴇʀᴇɴᴄᴇꜱ."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "back_to_admin_from_stats" or data == "back_to_admin_from_global":
        await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
    elif data == "back_to_main_from_admin":
        await query.message.edit_text("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ", reply_markup=get_main_keyboard(user_id))

@app.on_callback_query(filters.regex("^(skip_caption|cancel_upload)$"))
async def handle_upload_actions(_, query):
    user_id = query.from_user.id
    action = query.data
    state_data = user_states.get(user_id)

    if not state_data or state_data.get("action") not in ["awaiting_post_title", "processing_upload", "uploading_file"]:
        await query.answer("❌ ɴᴏ ᴀᴄᴛɪᴠᴇ ᴜᴘʟᴏᴀᴅ ᴛᴏ ᴄᴀɴᴄᴇʟ ᴏʀ ꜱᴋɪᴘ.", show_alert=True)
        return

    if action == "cancel_upload":
        if user_id in upload_tasks and not upload_tasks[user_id].done():
            upload_tasks[user_id].cancel()
            await query.answer("❌ ᴜᴘʟᴏᴀᴅ ᴄᴀɴᴄᴇʟʟᴇᴅ.", show_alert=True)
            await safe_edit_message(query.message, "❌ ᴜᴘʟᴏᴀᴅ ʜᴀꜱ ʙᴇᴇɴ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
            user_states.pop(user_id, None)
            upload_tasks.pop(user_id, None)
            cleanup_temp_files([state_data.get("file_info", {}).get("downloaded_path"), state_data.get("file_info", {}).get("transcoded_path")])
        else:
            await query.answer("❌ ɴᴏ ᴀᴄᴛɪᴠᴇ ᴜᴘʟᴏᴀᴅ ᴛᴀꜱᴋ ᴛᴏ ᴄᴀɴᴄᴇʟ.", show_alert=True)
            user_states.pop(user_id, None)

    elif action == "skip_caption":
        await query.answer("✅ ᴜꜱɪɴɢ ᴅᴇꜰᴀᴜʟᴛ ᴄᴀᴘᴛɪᴏɴ.", show_alert=True)
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = None
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await safe_edit_message(query.message, f"✅ ꜱᴋɪᴘᴘᴇᴅ. ᴜᴘʟᴏᴀᴅɪɴɢ ᴡɪᴛʜ ᴅᴇꜰᴀᴜʟᴛ ᴄᴀᴘᴛɪᴏɴ...")
        await start_upload_task(query.message, file_info)

async def start_upload_task(msg, file_info):
    user_id = msg.from_user.id
    task = asyncio.create_task(process_and_upload(msg, file_info))
    upload_tasks[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"ᴜᴘʟᴏᴀᴅ ᴛᴀꜱᴋ ꜰᴏʀ ᴜꜱᴇʀ {user_id} ᴡᴀꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
    except Exception as e:
        logger.error(f"ᴜᴘʟᴏᴀᴅ ᴛᴀꜱᴋ ꜰᴏʀ ᴜꜱᴇʀ {user_id} ꜰᴀɪʟᴇᴅ ᴡɪᴛʜ ᴀɴ ᴜɴʜᴀɴᴅʟᴇᴅ ᴇxᴄᴇᴘᴛɪᴏɴ: {e}")
        await msg.reply("❌ ᴀɴ ᴜɴᴇxᴘᴇᴄᴛᴇᴅ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ ᴅᴜʀɪɴɢ ᴜᴘʟᴏᴀᴅ. ᴘʟᴇᴀꜱᴇ ᴛʀʏ ᴀɢᴀɪɴ.")

async def process_and_upload(msg, file_info):
    user_id = msg.from_user.id
    platform = file_info["platform"]
    upload_type = file_info["upload_type"]
    file_path = file_info["downloaded_path"]
    
    processing_msg = file_info["processing_msg"]

    try:
        video_to_upload = file_path
        transcoded_video_path = None
        
        settings = await get_user_settings(user_id)
        no_compression = settings.get("no_compression", true)
        aspect_ratio_setting = settings.get("aspect_ratio", "original")

        if upload_type in ["reel", "video"] and (not no_compression or aspect_ratio_setting != "original"):
            await processing_msg.edit_text("🔄 ᴏᴘᴛɪᴍɪᴢɪɴɢ ᴠɪᴅᴇᴏ (ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ᴀᴜᴅɪᴏ/ᴠɪᴅᴇᴏ)... ᴛʜɪꜱ ᴍᴀʏ ᴛᴀᴋᴇ ᴀ ᴍᴏᴍᴇɴᴛ.")
            transcoded_video_path = f"{file_path}_transcoded.mp4"
            ffmpeg_command = ["ffmpeg", "-i", file_path, "-map_chapters", "-1", "-y"]

            if not no_compression:
                ffmpeg_command.extend([
                    "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                    "-pix_fmt", "yuv420p", "-movflags", "faststart",
                ])
            else:
                ffmpeg_command.extend(["-c:v", "copy", "-c:a", "copy"])

            if aspect_ratio_setting == "9_16":
                ffmpeg_command.extend([
                    "-vf", "scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920),crop=1080:1920,setsar=1:1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                    "-s", "1080x1920"
                ])
            ffmpeg_command.append(transcoded_video_path)
            
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
                    raise Exception(f"ᴠɪᴅᴇᴏ ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ꜰᴀɪʟᴇᴅ: {stderr.decode()}")
                else:
                    logger.info(f"FFmpeg transcoding successful. ᴏᴜᴛᴘᴜᴛ: {transcoded_video_path}")
                    video_to_upload = transcoded_video_path
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"ᴅᴇʟᴇᴛᴇᴅ ᴏʀɪɢɪɴᴀʟ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ ᴠɪᴅᴇᴏ ꜰɪʟᴇ: {file_path}")
            except asyncio.TimeoutError:
                process.kill()
                logger.error(f"FFmpeg process timed out for user {user_id}")
                raise Exception("ᴠɪᴅᴇᴏ ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ᴛɪᴍᴇᴅ ᴏᴜᴛ.")
        else:
            await processing_msg.edit_text("✅ ᴏʀɪɢɪɴᴀʟ ꜰɪʟᴇ. ɴᴏ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ.")

        settings = await get_user_settings(user_id)
        default_caption = settings.get("caption", f"ᴄʜᴇᴄᴋ ᴏᴜᴛ ᴍʏ ɴᴇᴡ {platform.capitalize()} ᴄᴏɴᴛᴇɴᴛ! 🎥")
        hashtags = settings.get("hashtags", "")
        
        final_caption = file_info.get("custom_caption") or default_caption
        if hashtags:
            final_caption = f"{final_caption}\n\n{hashtags}"

        url = "ɴ/ᴀ"
        media_id = "ɴ/ᴀ"
        media_type_value = ""

        await processing_msg.edit_text("🚀 **ᴜᴘʟᴏᴀᴅɪɴɢ ᴛᴏ ᴘʟᴀᴛꜰᴏʀᴍ...**", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup())
        start_time = time.time()

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            if INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
            session = await load_instagram_session(user_id)
            if not session:
                raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ.")
            user_upload_client.set_settings(session)
            
            try:
                await asyncio.to_thread(user_upload_client.get_timeline_feed)
            except LoginRequired:
                raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ.")

            if upload_type == "reel":
                result = await asyncio.to_thread(user_upload_client.clip_upload, video_to_upload, caption=final_caption)
                url = f"https://instagram.com/reel/{result.code}"
                media_id = result.pk
                # Fix for the 'int' object has no attribute 'value' error
                media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type
            elif upload_type == "post":
                result = await asyncio.to_thread(user_upload_client.photo_upload, video_to_upload, caption=final_caption)
                url = f"https://instagram.com/p/{result.code}"
                media_id = result.pk
                # Fix for the 'int' object has no attribute 'value' error
                media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type

        elif platform == "tiktok":
            tiktok_client = TikTokApi()
            session = await load_tiktok_session(user_id)
            if not session:
                raise Exception("ᴛɪᴋᴛᴏᴋ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ.")

            try:
                await tiktok_client.create_sessions(
                    session_path=TIKTOK_SESSION_FILE,
                    headless=True,
                    browser_session_id=session.get('browser_session_id')
                )
                if upload_type == "video":
                    await tiktok_client.upload.video(video_to_upload, title=final_caption)
                elif upload_type == "photo":
                    await tiktok_client.upload.photo_album([file_path], title=final_caption)
                url = "ɴ/ᴀ"
                media_id = "ɴ/ᴀ"
                media_type_value = upload_type
            finally:
                if tiktok_client and getattr(tiktok_client, 'browser', None):
                    await tiktok_client.browser.close()

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
            f"📤 ɴᴇᴡ {platform.capitalize()} {upload_type.capitalize()} ᴜᴘʟᴏᴀᴅ\n\n"
            f"👤 ᴜꜱᴇʀ: `{user_id}`\n"
            f"📛 ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 ᴜʀʟ: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await processing_msg.edit_text(f"✅ ᴜᴘʟᴏᴀᴅᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except asyncio.CancelledError:
        logger.info(f"ᴜᴘʟᴏᴀᴅ ᴘʀᴏᴄᴇꜱꜱ ꜰᴏʀ ᴜꜱᴇʀ {user_id} ᴡᴀꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
        await processing_msg.edit_text("❌ ᴜᴘʟᴏᴀᴅ ᴘʀᴏᴄᴇꜱꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
    except LoginRequired:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} ʟᴏɢɪɴ ʀᴇQᴜɪʀᴇᴅ. ʏᴏᴜʀ ꜱᴇꜱꜱɪᴏɴ ᴍɪɢʜᴛ ʜᴀᴠᴇ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ `/{platform}login <username> <password>` ᴀɢᴀɪɴ.")
        logger.error(f"ʟᴏɢɪɴʀᴇQᴜɪʀᴇᴅ ᴅᴜʀɪɴɢ {platform} ᴜᴘʟᴏᴀᴅ ꜰᴏʀ ᴜꜱᴇʀ {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ (ʟᴏɢɪɴ ʀᴇQᴜɪʀᴇᴅ)\nᴜꜱᴇʀ: `{user_id}`")
    except ClientError as ce:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ᴜᴘʟᴏᴀᴅ: {ce}. ᴘʟᴇᴀꜱᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ.")
        logger.error(f"ᴄʟɪᴇɴᴛᴇʀʀᴏʀ ᴅᴜʀɪɴɢ {platform} ᴜᴘʟᴏᴀᴅ ꜰᴏʀ ᴜꜱᴇʀ {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ (ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ)\nᴜꜱᴇʀ: `{user_id}`\nᴇʀʀᴏʀ: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ: {str(e)}"
        if processing_msg:
            await processing_msg.edit_text(error_msg)
        else:
            await msg.reply(error_msg)
        logger.error(f"{platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ ꜰᴏʀ {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ\nᴜꜱᴇʀ: `{user_id}`\nᴇʀʀᴏʀ: `{error_msg}`")
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

    if not state_data or state_data.get("action") not in [
        "waiting_for_instagram_reel_video", "waiting_for_instagram_photo_image",
        "waiting_for_tiktok_video", "waiting_for_tiktok_photo"
    ]:
        return await msg.reply("❌ ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ ᴏɴᴇ ᴏꜰ ᴛʜᴇ ᴜᴘʟᴏᴀᴅ ʙᴜᴛᴛᴏɴꜱ ꜰɪʀꜱᴛ.")

    platform = state_data["platform"]
    upload_type = state_data["upload_type"]

    if msg.video and (upload_type in ["reel", "video"]):
        if msg.video.file_size > MAX_FILE_SIZE_BYTES:
            user_states.pop(user_id, None)
            return await msg.reply(f"❌ ꜰɪʟᴇ ꜱɪᴢᴇ ᴇxᴄᴇᴇᴅꜱ ᴛʜᴇ ʟɪᴍɪᴛ ᴏꜰ `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` ᴍʙ.")
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
    else:
        user_states.pop(user_id, None)
        return await msg.reply("❌ ᴛʜᴇ ꜰɪʟᴇ ᴛʏᴘᴇ ᴅᴏᴇꜱ ɴᴏᴛ ᴍᴀᴛᴄʜ ᴛʜᴇ ʀᴇQᴜᴇꜱᴛᴇᴅ ᴜᴘʟᴏᴀᴅ ᴛʏᴘᴇ.")

    file_info["downloaded_path"] = None
    
    try:
        start_time = time.time()
        file_info["processing_msg"].is_progress_message_updated = False
        file_info["downloaded_path"] = await app.download_media(
            msg,
            progress=lambda current, total: progress_callback(current, total, "ᴅᴏᴡɴʟᴏᴀᴅ", file_info["processing_msg"], start_time)
        )
        await file_info["processing_msg"].edit_text("✅ ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴏᴍᴘʟᴇᴛᴇ. ᴡʜᴀᴛ ᴛɪᴛʟᴇ ᴅᴏ ʏᴏᴜ ᴡᴀɴᴛ ꜰᴏʀ ʏᴏᴜʀ ᴘᴏꜱᴛ?", reply_markup=get_caption_markup())
        user_states[user_id] = {"action": "awaiting_post_title", "file_info": file_info}

    except asyncio.CancelledError:
        logger.info(f"ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴀɴᴄᴇʟʟᴇᴅ ʙʏ ᴜꜱᴇʀ {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ꜰɪʟᴇ ᴅᴏᴡɴʟᴏᴀᴅ ꜰᴏʀ ᴜꜱᴇʀ {user_id}: {e}")
        await file_info["processing_msg"].edit_text(f"❌ ᴅᴏᴡɴʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ: {str(e)}")
        cleanup_temp_files([file_info.get("downloaded_path")])
        user_states.pop(user_id, None)

# --- Admin Panel Handlers ---

@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ",
        reply_markup=admin_markup
    )

@app.on_callback_query(filters.regex("^payment_settings_panel$"))
async def payment_settings_panel_cb(_, query):
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    
    current_settings = global_settings.get("payment_settings", {})
    text = (
        "💰 **ᴘᴀʏᴍᴇɴᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        f"**ɢᴏᴏɢʟᴇ ᴘʟᴀʏ:** {current_settings.get('google_play') or 'ɴᴏᴛ ꜱᴇᴛ'}\n"
        f"**ᴜᴘɪ:** {current_settings.get('upi') or 'ɴᴏᴛ ꜱᴇᴛ'}\n"
        f"**ᴜꜱᴛ:** {current_settings.get('ust') or 'ɴᴏᴛ ꜱᴇᴛ'}\n"
        f"**ʙᴛᴄ:** {current_settings.get('btc') or 'ɴᴏᴛ ꜱᴇᴛ'}\n"
        f"**ᴏᴛʜᴇʀꜱ:** {current_settings.get('others') or 'ɴᴏᴛ ꜱᴇᴛ'}\n\n"
        "ᴄʟɪᴄᴋ ᴀ ʙᴜᴛᴛᴏɴ ᴛᴏ ᴜᴘᴅᴀᴛᴇ ɪᴛꜱ ᴅᴇᴛᴀɪʟꜱ."
    )
    
    await safe_edit_message(query.message, text, reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^set_payment_"))
async def set_payment_cb(_, query):
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    
    method = query.data.split("set_payment_")[1]
    
    user_states[query.from_user.id] = {"action": f"waiting_for_payment_details_{method}"}
    
    await safe_edit_message(query.message, f"ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ ᴅᴇᴛᴀɪʟꜱ ꜰᴏʀ **{method.upper()}**. ᴛʜɪꜱ ᴄᴀɴ ʙᴇ ᴛʜᴇ ᴜᴘɪ ɪᴅ, ᴡᴀʟʟᴇᴛ ᴀᴅᴅʀᴇꜱꜱ, ᴏʀ ᴀɴʏ ᴏᴛʜᴇʀ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ.", parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^global_settings_panel$"))
async def global_settings_panel_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    onam_status = "ᴏɴ" if global_settings.get("onam_toggle") else "ᴏꜰꜰ"
    max_uploads = global_settings.get("max_concurrent_uploads")
    settings_text = (
        "⚙️ **ɢʟᴏʙᴀʟ ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        f"**ᴏɴᴀᴍ ꜱᴘᴇᴄɪᴀʟ ᴇᴠᴇɴᴛ:** `{onam_status}`\n"
        f"**ᴍᴀx ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴘʟᴏᴀᴅꜱ:** `{max_uploads}`\n"
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
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
    current_status = global_settings.get("onam_toggle", False)
    new_status = not current_status
    _update_global_setting("onam_toggle", new_status)
    status_text = "ᴏɴ" if new_status else "ᴏꜰꜰ"
    await query.answer(f"ᴏɴᴀᴍ ᴛᴏɢɢʟᴇ ɪꜱ ɴᴏᴡ {status_text}.", show_alert=True)
    onam_status = "ᴏɴ" if global_settings.get("onam_toggle") else "ᴏꜰꜰ"
    max_uploads = global_settings.get("max_concurrent_uploads")
    settings_text = (
        "⚙️ **ɢʟᴏʙᴀʟ ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        f"**ᴏɴᴀᴍ ꜱᴘᴇᴄɪᴀʟ ᴇᴠᴇɴᴛ:** `{onam_status}`\n"
        f"**ᴍᴀx ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴘʟᴏᴀᴅꜱ:** `{max_uploads}`\n"
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
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
    user_states[user_id] = {"action": "waiting_for_max_uploads"}
    current_limit = global_settings.get("max_concurrent_uploads")
    await safe_edit_message(
        query.message,
        f"🔄 ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ ɴᴇᴡ ᴍᴀxɪᴍᴜᴍ ɴᴜᴍʙᴇʀ ᴏꜰ ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴘʟᴏᴀᴅꜱ.\n\n"
        f"ᴄᴜʀʀᴇɴᴛ ʟɪᴍɪᴛ ɪꜱ: `{current_limit}`"
    )

@app.on_callback_query(filters.regex("^reset_stats$"))
@with_user_lock
async def reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
    await query.message.edit_text("⚠️ **ᴡᴀʀɴɪɴɢ!** ᴀʀᴇ ʏᴏᴜ ꜱᴜʀᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʀᴇꜱᴇᴛ ᴀʟʟ ᴜᴘʟᴏᴀᴅ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ? ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ ɪꜱ ɪʀʀᴇᴠᴇʀꜱɪʙʟᴇ.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ʏᴇꜱ, ʀᴇꜱᴇᴛ ꜱᴛᴀᴛꜱ", callback_data="confirm_reset_stats")],
            [InlineKeyboardButton("❌ ɴᴏ, ᴄᴀɴᴄᴇʟ", callback_data="admin_panel")]
        ]), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^confirm_reset_stats$"))
@with_user_lock
async def confirm_reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
    result = db.uploads.delete_many({})
    await query.answer(f"✅ ᴀʟʟ ᴜᴘʟᴏᴀᴅ ꜱᴛᴀᴛꜱ ʜᴀᴠᴇ ʙᴇᴇɴ ʀᴇꜱᴇᴛ! ᴅᴇʟᴇᴛᴇᴅ {result.deleted_count} ᴇɴᴛʀɪᴇꜱ.", show_alert=True)
    await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
    await send_log_to_channel(app, LOG_CHANNEL, f"📊 ᴀᴅᴍɪɴ `{user_id}` ʜᴀꜱ ʀᴇꜱᴇᴛ ᴀʟʟ ʙᴏᴛ ᴜᴘʟᴏᴀᴅ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ.")

@app.on_callback_query(filters.regex("^show_system_stats$"))
async def show_system_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        system_stats_text = (
            "💻 **ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ**\n\n"
            f"**ᴄᴘᴜ:** `{cpu_usage}%`\n"
            f"**ʀᴀᴍ:** `{ram.percent}%` (ᴜꜱᴇᴅ: `{ram.used / (1024**3):.2f}` ɢʙ / ᴛᴏᴛᴀʟ: `{ram.total / (1024**3):.2f}` ɢʙ)\n"
            f"**ᴅɪꜱᴋ:** `{disk.percent}%` (ᴜꜱᴇᴅ: `{disk.used / (1024**3):.2f}` ɢʙ / ᴛᴏᴛᴀʟ: `{disk.total / (1024**3):.2f}` ɢʙ)\n\n"
        )
        gpu_info = "ɴᴏ ɢᴘᴜ ꜰᴏᴜɴᴅ ᴏʀ ɢᴘᴜᴛɪʟ ɪꜱ ɴᴏᴛ ɪɴꜱᴛᴀʟʟᴇᴅ."
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu_info = "**ɢᴘᴜ ɪɴꜰᴏ:**\n"
                for i, gpu in enumerate(gpus):
                    gpu_info += (
                        f"   - **ɢᴘᴜ {i}:** `{gpu.name}`\n"
                        f"     - ʟᴏᴀᴅ: `{gpu.load*100:.1f}%`\n"
                        f"     - ᴍᴇᴍᴏʀʏ: `{gpu.memoryUsed}/{gpu.memoryTotal}` ᴍʙ\n"
                        f"     - ᴛᴇᴍᴘ: `{gpu.temperature}°ᴄ`\n"
                    )
            else:
                gpu_info = "ɴᴏ ɢᴘᴜ ꜰᴏᴜɴᴅ."
        except Exception:
            gpu_info = "ᴄᴏᴜʟᴅ ɴᴏᴛ ʀᴇᴛʀɪᴇᴠᴇ ɢᴘᴜ ɪɴꜰᴏ."
        system_stats_text += gpu_info
        await safe_edit_message(
            query.message,
            system_stats_text,
            reply_markup=admin_global_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await query.answer("❌ ꜰᴀɪʟᴇᴅ ᴛᴏ ʀᴇᴛʀɪᴇᴠᴇ ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ.", show_alert=True)
        logger.error(f"ᴇʀʀᴏʀ ʀᴇᴛʀɪᴇᴠɪɴɢ ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ ꜰᴏʀ ᴀᴅᴍɪɴ {user_id}: {e}")
        await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    users = list(db.users.find({}))
    if not users:
        await safe_edit_message(
            query.message,
            "👥 ɴᴏ ᴜꜱᴇʀꜱ ꜰᴏᴜɴᴅ ɪɴ ᴛʜᴇ ᴅᴀᴛᴀʙᴀꜱᴇ.",
            reply_markup=admin_markup
        )
        return
    user_list_text = "👥 **ᴀʟʟ ᴜꜱᴇʀꜱ:**\n\n"
    for user in users:
        user_id = user["_id"]
        instagram_username = user.get("instagram_username", "ɴ/ᴀ")
        tiktok_username = user.get("tiktok_username", "ɴ/ᴀ")
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
                        platform_statuses.append(f"⭐ {platform.capitalize()}: ʟɪꜰᴇᴛɪᴍᴇ")
                    elif premium_until:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: ᴇxᴘɪʀᴇꜱ `{premium_until.strftime('%Y-%m-%d')}`")
                    else:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: ᴀᴄᴛɪᴠᴇ")
                else:
                    platform_statuses.append(f"❌ {platform.capitalize()}: ꜰʀᴇᴇ")
        status_line = " | ".join(platform_statuses)
        user_list_text += (
            f"ɪᴅ: `{user_id}` | {status_line}\n"
            f"ɪɢ: `{instagram_username}` | ᴛɪᴋᴛᴏᴋ: `{tiktok_username}`\n"
            f"ᴀᴅᴅᴇᴅ: `{added_at}` | ʟᴀꜱᴛ ᴀᴄᴛɪᴠᴇ: `{last_active}`\n"
            "-----------------------------------\n"
        )
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "ᴜꜱᴇʀ ʟɪꜱᴛ ɪꜱ ᴛᴏᴏ ʟᴏɴɢ. ꜱᴇɴᴅɪɴɢ ᴀꜱ ᴀ ꜰɪʟᴇ...")
        with open("users.txt", "w") as f:
            f.write(user_list_text.replace("`", ""))
        await app.send_document(query.message.chat.id, "users.txt", caption="👥 ᴀʟʟ ᴜꜱᴇʀꜱ ʟɪꜱᴛ")
        os.remove("users.txt")
        await safe_edit_message(
            query.message,
            "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ",
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
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    user_states[query.from_user.id] = {"action": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(
        query.message,
        "➕ ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ **ᴜꜱᴇʀ ɪᴅ** ᴛᴏ ᴍᴀɴᴀɢᴇ ᴛʜᴇɪʀ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ."
    )

@app.on_callback_query(filters.regex("^select_platform_"))
@with_user_lock
async def select_platform_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("ᴇʀʀᴏʀ: ᴜꜱᴇʀ ꜱᴇʟᴇᴄᴛɪᴏɴ ʟᴏꜱᴛ. ᴘʟᴇᴀꜱᴇ ᴛʀʏ 'ᴍᴀɴᴀɢᴇ ᴘʀᴇᴍɪᴜᴍ' ᴀɢᴀɪɴ.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
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
        f"✅ ᴜꜱᴇʀ ɪᴅ `{state_data['target_user_id']}` ʀᴇᴄᴇɪᴠᴇᴅ. ꜱᴇʟᴇᴄᴛ ᴘʟᴀᴛꜰᴏʀᴍꜱ ꜰᴏʀ ᴘʀᴇᴍɪᴜᴍ:",
        reply_markup=get_platform_selection_markup(user_id, selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_platform_selection$"))
@with_user_lock
async def confirm_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("ᴇʀʀᴏʀ: ᴘʟᴇᴀꜱᴇ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ᴘʀᴇᴍɪᴜᴍ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ᴘʀᴏᴄᴇꜱꜱ.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = [p for p, selected in state_data.get("selected_platforms", {}).items() if selected]
    if not selected_platforms:
        return await query.answer("ᴘʟᴇᴀꜱᴇ ꜱᴇʟᴇᴄᴛ ᴀᴛ ʟᴇᴀꜱᴛ ᴏɴᴇ ᴘʟᴀᴛꜰᴏʀᴍ!", show_alert=True)
    state_data["action"] = "select_premium_plan_for_platforms"
    state_data["final_selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    await safe_edit_message(
        query.message,
        f"✅ ᴘʟᴀᴛꜰᴏʀᴍꜱ ꜱᴇʟᴇᴄᴛᴇᴅ: `{', '.join(platform.capitalize() for platform in selected_platforms)}`. ɴᴏᴡ, ꜱᴇʟᴇᴄᴛ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ ꜰᴏʀ ᴜꜱᴇʀ `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_plan_"))
@with_user_lock
async def select_plan_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_premium_plan_for_platforms":
        await query.answer("ᴇʀʀᴏʀ: ᴘʟᴀɴ ꜱᴇʟᴇᴄᴛɪᴏɴ ʟᴏꜱᴛ. ᴘʟᴇᴀꜱᴇ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ᴘʀᴇᴍɪᴜᴍ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ᴘʀᴏᴄᴇꜱꜱ.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = state_data["final_selected_platforms"]
    premium_plan_key = query.data.split("select_plan_")[1]
    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("ɪɴᴠᴀʟɪᴅ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ ꜱᴇʟᴇᴄᴛᴇᴅ.", show_alert=True)
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
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
    admin_confirm_text = f"✅ ᴘʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ᴛᴏ ᴜꜱᴇʀ `{target_user_id}` ꜰᴏʀ:\n"
    for platform in selected_platforms:
        updated_user = _get_user_data(target_user_id)
        platform_data = updated_user.get("premium", {}).get(platform, {})
        confirm_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if platform_data.get("until"):
            confirm_line += f" (ᴇxᴘɪʀᴇꜱ: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`)"
        admin_confirm_text += f"- {confirm_line}\n"
    await safe_edit_message(
        query.message,
        admin_confirm_text,
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer("ᴘʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ!", show_alert=False)
    user_states.pop(user_id, None)
    try:
        user_msg = (
            f"🎉 **ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ!** 🎉\n\n"
            f"ʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ɢʀᴀɴᴛᴇᴅ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ꜰᴏʀ ᴛʜᴇ ꜰᴏʟʟᴏᴡɪɴɢ ᴘʟᴀᴛꜰᴏʀᴍꜱ:\n"
        )
        for platform in selected_platforms:
            updated_user = _get_user_data(target_user_id)
            platform_data = updated_user.get("premium", {}).get(platform, {})
            msg_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
            if platform_data.get("until"):
                msg_line += f" (ᴇxᴘɪʀᴇꜱ: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`)"
            user_msg += f"- {msg_line}\n"
        user_msg += "\nᴇɴᴊᴏʏ ʏᴏᴜʀ ɴᴇᴡ ꜰᴇᴀᴛᴜʀᴇꜱ! ✨"
        await app.send_message(target_user_id, user_msg, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 ᴘʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ ꜱᴇɴᴛ ᴛᴏ `{target_user_id}` ʙʏ ᴀᴅᴍɪɴ `{user_id}`. ᴘʟᴀᴛꜰᴏʀᴍꜱ: `{', '.join(selected_platforms)}`, ᴘʟᴀɴ: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"ꜰᴀɪʟᴇᴅ ᴛᴏ ɴᴏᴛɪꜰʏ ᴜꜱᴇʀ {target_user_id} ᴀʙᴏᴜᴛ ᴘʀᴇᴍɪᴜᴍ: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ ꜰᴀɪʟᴇᴅ ᴛᴏ ɴᴏᴛɪꜰʏ ᴜꜱᴇʀ `{target_user_id}` ᴀʙᴏᴜᴛ ᴘʀᴇᴍɪᴜᴍ. ᴇʀʀᴏʀ: `{str(e)}`"
        )

@app.on_callback_query(filters.regex("^back_to_platform_selection$"))
@with_user_lock
async def back_to_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") not in ["select_platforms_for_premium", "select_premium_plan_for_platforms"]:
        await query.answer("ᴇʀʀᴏʀ: ɪɴᴠᴀʟɪᴅ ꜱᴛᴀᴛᴇ ꜰᴏʀ ʙᴀᴄᴋ ᴀᴄᴛɪᴏɴ. ᴘʟᴇᴀꜱᴇ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ᴘʀᴏᴄᴇꜱꜱ.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    current_selected_platforms = state_data.get("selected_platforms", {})
    user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": current_selected_platforms}
    await safe_edit_message(
        query.message,
        f"✅ ᴜꜱᴇʀ ɪᴅ `{target_user_id}` ʀᴇᴄᴇɪᴠᴇᴅ. ꜱᴇʟᴇᴄᴛ ᴘʟᴀᴛꜰᴏʀᴍꜱ ꜰᴏʀ ᴘʀᴇᴍɪᴜᴍ:",
        reply_markup=get_platform_selection_markup(user_id, current_selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇQᴜɪʀᴇᴅ", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "📢 ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴛʜᴇ ᴍᴇꜱꜱᴀɢᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴛᴏ ᴀʟʟ ᴜꜱᴇʀꜱ.\n\n"
        "ᴜꜱᴇ `/broadcast <message>` ᴄᴏᴍᴍᴀɴᴅ ɪɴꜱᴛᴇᴀᴅ."
    )

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
            "🏠 ᴍᴀɪɴ ᴍᴇɴᴜ",
            reply_markup=get_main_keyboard(user_id)
        )
    elif data == "back_to_settings":
        current_settings = await get_user_settings(user_id)
        compression_status = "ᴏꜰꜰ (ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ᴇɴᴀʙʟᴇᴅ)" if not current_settings.get("no_compression") else "ᴏɴ (ᴏʀɪɢɪɴᴀʟ Qᴜᴀʟɪᴛʏ)"
        settings_text = "⚙️ ꜱᴇᴛᴛɪɴɢꜱ ᴘᴀɴᴇʟ\n\n" \
                        f"🗜️ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ ɪꜱ ᴄᴜʀʀᴇɴᴛʟʏ: **{compression_status}**\n\n" \
                        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴀᴅᴊᴜꜱᴛ ʏᴏᴜʀ ᴘʀᴇꜰᴇʀᴇɴᴄᴇꜱ."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "back_to_admin_from_stats" or data == "back_to_admin_from_global":
        await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴘᴀɴᴇʟ", reply_markup=admin_markup)
    elif data == "back_to_main_from_admin":
        await query.message.edit_text("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ", reply_markup=get_main_keyboard(user_id))

@app.on_callback_query(filters.regex("^(skip_caption|cancel_upload)$"))
async def handle_upload_actions(_, query):
    user_id = query.from_user.id
    action = query.data
    state_data = user_states.get(user_id)

    if not state_data or state_data.get("action") not in ["awaiting_post_title", "processing_upload", "uploading_file"]:
        await query.answer("❌ ɴᴏ ᴀᴄᴛɪᴠᴇ ᴜᴘʟᴏᴀᴅ ᴛᴏ ᴄᴀɴᴄᴇʟ ᴏʀ ꜱᴋɪᴘ.", show_alert=True)
        return

    if action == "cancel_upload":
        if user_id in upload_tasks and not upload_tasks[user_id].done():
            upload_tasks[user_id].cancel()
            await query.answer("❌ ᴜᴘʟᴏᴀᴅ ᴄᴀɴᴄᴇʟʟᴇᴅ.", show_alert=True)
            await safe_edit_message(query.message, "❌ ᴜᴘʟᴏᴀᴅ ʜᴀꜱ ʙᴇᴇɴ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
            user_states.pop(user_id, None)
            upload_tasks.pop(user_id, None)
            cleanup_temp_files([state_data.get("file_info", {}).get("downloaded_path"), state_data.get("file_info", {}).get("transcoded_path")])
        else:
            await query.answer("❌ ɴᴏ ᴀᴄᴛɪᴠᴇ ᴜᴘʟᴏᴀᴅ ᴛᴀꜱᴋ ᴛᴏ ᴄᴀɴᴄᴇʟ.", show_alert=True)
            user_states.pop(user_id, None)

    elif action == "skip_caption":
        await query.answer("✅ ᴜꜱɪɴɢ ᴅᴇꜰᴀᴜʟᴛ ᴄᴀᴘᴛɪᴏɴ.", show_alert=True)
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = None
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await safe_edit_message(query.message, f"✅ ꜱᴋɪᴘᴘᴇᴅ. ᴜᴘʟᴏᴀᴅɪɴɢ ᴡɪᴛʜ ᴅᴇꜰᴀᴜʟᴛ ᴄᴀᴘᴛɪᴏɴ...")
        await start_upload_task(query.message, file_info)

async def start_upload_task(msg, file_info):
    user_id = msg.from_user.id
    task = asyncio.create_task(process_and_upload(msg, file_info))
    upload_tasks[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"ᴜᴘʟᴏᴀᴅ ᴛᴀꜱᴋ ꜰᴏʀ ᴜꜱᴇʀ {user_id} ᴡᴀꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
    except Exception as e:
        logger.error(f"ᴜᴘʟᴏᴀᴅ ᴛᴀꜱᴋ ꜰᴏʀ ᴜꜱᴇʀ {user_id} ꜰᴀɪʟᴇᴅ ᴡɪᴛʜ ᴀɴ ᴜɴʜᴀɴᴅʟᴇᴅ ᴇxᴄᴇᴘᴛɪᴏɴ: {e}")
        await msg.reply("❌ ᴀɴ ᴜɴᴇxᴘᴇᴄᴛᴇᴅ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ ᴅᴜʀɪɴɢ ᴜᴘʟᴏᴀᴅ. ᴘʟᴇᴀꜱᴇ ᴛʀʏ ᴀɢᴀɪɴ.")

async def process_and_upload(msg, file_info):
    user_id = msg.from_user.id
    platform = file_info["platform"]
    upload_type = file_info["upload_type"]
    file_path = file_info["downloaded_path"]
    
    processing_msg = file_info["processing_msg"]

    try:
        video_to_upload = file_path
        transcoded_video_path = None
        
        settings = await get_user_settings(user_id)
        no_compression = settings.get("no_compression", False)
        aspect_ratio_setting = settings.get("aspect_ratio", "original")

        if upload_type in ["reel", "video"] and (not no_compression or aspect_ratio_setting != "original"):
            await processing_msg.edit_text("🔄 ᴏᴘᴛɪᴍɪᴢɪɴɢ ᴠɪᴅᴇᴏ (ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ᴀᴜᴅɪᴏ/ᴠɪᴅᴇᴏ)... ᴛʜɪꜱ ᴍᴀʏ ᴛᴀᴋᴇ ᴀ ᴍᴏᴍᴇɴᴛ.")
            transcoded_video_path = f"{file_path}_transcoded.mp4"
            ffmpeg_command = ["ffmpeg", "-i", file_path, "-map_chapters", "-1", "-y"]

            if not no_compression:
                ffmpeg_command.extend([
                    "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                    "-pix_fmt", "yuv420p", "-movflags", "faststart",
                ])
            else:
                ffmpeg_command.extend(["-c:v", "copy", "-c:a", "copy"])

            if aspect_ratio_setting == "9_16":
                ffmpeg_command.extend([
                    "-vf", "scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920),crop=1080:1920,setsar=1:1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                    "-s", "1080x1920"
                ])
            ffmpeg_command.append(transcoded_video_path)
            
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
                    raise Exception(f"ᴠɪᴅᴇᴏ ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ꜰᴀɪʟᴇᴅ: {stderr.decode()}")
                else:
                    logger.info(f"FFmpeg transcoding successful. ᴏᴜᴛᴘᴜᴛ: {transcoded_video_path}")
                    video_to_upload = transcoded_video_path
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"ᴅᴇʟᴇᴛᴇᴅ ᴏʀɪɢɪɴᴀʟ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ ᴠɪᴅᴇᴏ ꜰɪʟᴇ: {file_path}")
            except asyncio.TimeoutError:
                process.kill()
                logger.error(f"FFmpeg process timed out for user {user_id}")
                raise Exception("ᴠɪᴅᴇᴏ ᴛʀᴀɴꜱᴄᴏᴅɪɴɢ ᴛɪᴍᴇᴅ ᴏᴜᴛ.")
        else:
            await processing_msg.edit_text("✅ ᴏʀɪɢɪɴᴀʟ ꜰɪʟᴇ. ɴᴏ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ.")

        settings = await get_user_settings(user_id)
        default_caption = settings.get("caption", f"ᴄʜᴇᴄᴋ ᴏᴜᴛ ᴍʏ ɴᴇᴡ {platform.capitalize()} ᴄᴏɴᴛᴇɴᴛ! 🎥")
        hashtags = settings.get("hashtags", "")
        
        final_caption = file_info.get("custom_caption") or default_caption
        if hashtags:
            final_caption = f"{final_caption}\n\n{hashtags}"

        url = "ɴ/ᴀ"
        media_id = "ɴ/ᴀ"
        media_type_value = ""

        await processing_msg.edit_text("🚀 **ᴜᴘʟᴏᴀᴅɪɴɢ ᴛᴏ ᴘʟᴀᴛꜰᴏʀᴍ...**", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup())
        start_time = time.time()

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            if INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
            session = await load_instagram_session(user_id)
            if not session:
                raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ.")
            user_upload_client.set_settings(session)
            
            try:
                await asyncio.to_thread(user_upload_client.get_timeline_feed)
            except LoginRequired:
                raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ.")

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

        elif platform == "tiktok":
            tiktok_client = TikTokApi()
            session = await load_tiktok_session(user_id)
            if not session:
                raise Exception("ᴛɪᴋᴛᴏᴋ ꜱᴇꜱꜱɪᴏɴ ᴇxᴘɪʀᴇᴅ.")

            try:
                await tiktok_client.create_sessions(
                    session_path=TIKTOK_SESSION_FILE,
                    headless=True,
                    browser_session_id=session.get('browser_session_id')
                )
                if upload_type == "video":
                    await tiktok_client.upload.video(video_to_upload, title=final_caption)
                elif upload_type == "photo":
                    await tiktok_client.upload.photo_album([file_path], title=final_caption)
                url = "ɴ/ᴀ"
                media_id = "ɴ/ᴀ"
                media_type_value = upload_type
            finally:
                if tiktok_client and getattr(tiktok_client, 'browser', None):
                    await tiktok_client.browser.close()

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
            f"📤 ɴᴇᴡ {platform.capitalize()} {upload_type.capitalize()} ᴜᴘʟᴏᴀᴅ\n\n"
            f"👤 ᴜꜱᴇʀ: `{user_id}`\n"
            f"📛 ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 ᴜʀʟ: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await processing_msg.edit_text(f"✅ ᴜᴘʟᴏᴀᴅᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except asyncio.CancelledError:
        logger.info(f"ᴜᴘʟᴏᴀᴅ ᴘʀᴏᴄᴇꜱꜱ ꜰᴏʀ ᴜꜱᴇʀ {user_id} ᴡᴀꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
        await processing_msg.edit_text("❌ ᴜᴘʟᴏᴀᴅ ᴘʀᴏᴄᴇꜱꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
    except LoginRequired:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} ʟᴏɢɪɴ ʀᴇQᴜɪʀᴇᴅ. ʏᴏᴜʀ ꜱᴇꜱꜱɪᴏɴ ᴍɪɢʜᴛ ʜᴀᴠᴇ ᴇxᴘɪʀᴇᴅ. ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ `/{platform}login <username> <password>` ᴀɢᴀɪɴ.")
        logger.error(f"ʟᴏɢɪɴʀᴇQᴜɪʀᴇᴅ ᴅᴜʀɪɴɢ {platform} ᴜᴘʟᴏᴀᴅ ꜰᴏʀ ᴜꜱᴇʀ {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ (ʟᴏɢɪɴ ʀᴇQᴜɪʀᴇᴅ)\nᴜꜱᴇʀ: `{user_id}`")
    except ClientError as ce:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ᴜᴘʟᴏᴀᴅ: {ce}. ᴘʟᴇᴀꜱᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ.")
        logger.error(f"ᴄʟɪᴇɴᴛᴇʀʀᴏʀ ᴅᴜʀɪɴɢ {platform} ᴜᴘʟᴏᴀᴅ ꜰᴏʀ ᴜꜱᴇʀ {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ (ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ)\nᴜꜱᴇʀ: `{user_id}`\nᴇʀʀᴏʀ: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ: {str(e)}"
        if processing_msg:
            await processing_msg.edit_text(error_msg)
        else:
            await msg.reply(error_msg)
        logger.error(f"{platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ ꜰᴏʀ {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} ᴜᴘʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ\nᴜꜱᴇʀ: `{user_id}`\nᴇʀʀᴏʀ: `{error_msg}`")
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

    if not state_data or state_data.get("action") not in [
        "waiting_for_instagram_reel_video", "waiting_for_instagram_photo_image",
        "waiting_for_tiktok_video", "waiting_for_tiktok_photo"
    ]:
        return await msg.reply("❌ ᴘʟᴇᴀꜱᴇ ᴜꜱᴇ ᴏɴᴇ ᴏꜰ ᴛʜᴇ ᴜᴘʟᴏᴀᴅ ʙᴜᴛᴛᴏɴꜱ ꜰɪʀꜱᴛ.")

    platform = state_data["platform"]
    upload_type = state_data["upload_type"]

    if msg.video and (upload_type in ["reel", "video"]):
        if msg.video.file_size > MAX_FILE_SIZE_BYTES:
            user_states.pop(user_id, None)
            return await msg.reply(f"❌ ꜰɪʟᴇ ꜱɪᴢᴇ ᴇxᴄᴇᴇᴅꜱ ᴛʜᴇ ʟɪᴍɪᴛ ᴏꜰ `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` ᴍʙ.")
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
    else:
        user_states.pop(user_id, None)
        return await msg.reply("❌ ᴛʜᴇ ꜰɪʟᴇ ᴛʏᴘᴇ ᴅᴏᴇꜱ ɴᴏᴛ ᴍᴀᴛᴄʜ ᴛʜᴇ ʀᴇQᴜᴇꜱᴛᴇᴅ ᴜᴘʟᴏᴀᴅ ᴛʏᴘᴇ.")

    file_info["downloaded_path"] = None
    
    try:
        start_time = time.time()
        file_info["processing_msg"].is_progress_message_updated = False
        file_info["downloaded_path"] = await app.download_media(
            msg,
            progress=lambda current, total: progress_callback(current, total, "ᴅᴏᴡɴʟᴏᴀᴅ", file_info["processing_msg"], start_time)
        )
        await file_info["processing_msg"].edit_text("✅ ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴏᴍᴘʟᴇᴛᴇ. ᴡʜᴀᴛ ᴛɪᴛʟᴇ ᴅᴏ ʏᴏᴜ ᴡᴀɴᴛ ꜰᴏʀ ʏᴏᴜʀ ᴘᴏꜱᴛ?", reply_markup=get_caption_markup())
        user_states[user_id] = {"action": "awaiting_post_title", "file_info": file_info}

    except asyncio.CancelledError:
        logger.info(f"ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴀɴᴄᴇʟʟᴇᴅ ʙʏ ᴜꜱᴇʀ {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ꜰɪʟᴇ ᴅᴏᴡɴʟᴏᴀᴅ ꜰᴏʀ ᴜꜱᴇʀ {user_id}: {e}")
        await file_info["processing_msg"].edit_text(f"❌ ᴅᴏᴡɴʟᴏᴀᴅ ꜰᴀɪʟᴇᴅ: {str(e)}")
        cleanup_temp_files([file_info.get("downloaded_path")])
        user_states.pop(user_id, None)

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

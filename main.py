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

# TikTok Client
from TikTokApi import TikTokApi
# The problematic import is removed to prevent the crash.
# Errors are now handled more generally, which is the correct approach for the latest library versions.

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
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
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
    "max_file_size_mb": 250, # Max file size in MB
}

# Initialize MongoDB Client
try:
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo.NowTok
    mongo.admin.command('ismaster')  # The ismaster command is cheap and does not require auth.
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
user_upload_locks = {} # {user_id: asyncio.Lock()}

# FFMpeg timeout constant
FFMPEG_TIMEOUT_SECONDS = 600 # 10 minutes

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
user_states = {} # {user_id: {"action": "waiting_for_caption", "file_info": {...}}}
upload_tasks = {} # {user_id: task_object}

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

    buttons.append([KeyboardButton("/buypypremium"), KeyboardButton("/premiumdetails")])
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
    [InlineKeyboardButton("🔙 𝗕𝗮𝗰𝗸 𝗠𝗲𝗻𝘂", callback_data="back_to_main_menu")]
])

admin_global_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Onam Toggle", callback_data="toggle_onam")],
    [InlineKeyboardButton("Max Upload Users", callback_data="set_max_uploads")],
    [InlineKeyboardButton("Reset Stats", callback_data="reset_stats")],
    [InlineKeyboardButton("Show System Stats", callback_data="show_system_stats")],
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
        if value["duration"] is None:
            buttons.append([InlineKeyboardButton(f"Lifetime ({value['price']})", callback_data=f"select_plan_{key}")])
        else:
            price_multiplier = len(selected_platforms) if selected_platforms else 1
            display_price = value['price']
            if '₹' in display_price:
                try:
                    base_price = float(display_price.replace('₹', '').split('/')[0].strip())
                    calculated_price = base_price * price_multiplier
                    display_price = f"₹{int(calculated_price)}"
                except ValueError:
                    pass
            buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()} ({display_price})", callback_data=f"select_plan_{key}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Platform Selection", callback_data="back_to_platform_selection")])
    return InlineKeyboardMarkup(buttons)

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
    
    # We update the message every 5% to avoid rate limits
    if int(percentage) % 5 == 0 and not msg.is_progress_message_updated:
        try:
            asyncio.run(msg.edit_text(progress_text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup()))
            msg.is_progress_message_updated = True
        except:
            pass # Ignore edit message errors
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
        welcome_msg = "🤖 **Welcome to Instagram & TikTok Upload Bot!**\n\n"
        welcome_msg += "🛠 You have **admin privileges**."
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = _get_user_data(user_id)
    is_new_user = not user
    if is_new_user:
        _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")
        
        welcome_msg = (
            f"👋 **Hi {user_first_name}!**\n\n"
            "This Bot lets you upload any size Instagram Reels & Posts directly from Telegram.\n\n"
            "To get a taste of the premium features, you can activate a **free 3-hour trial** for Instagram right now!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 𝗔𝗰𝘁𝗶𝘃𝗮𝘁𝗲 𝗙𝗿𝗲𝗲 3-𝗛𝗼𝘂𝗿", callback_data="activate_trial")],
            [InlineKeyboardButton("➡️ 𝗣𝗿𝗲𝗺𝗶𝘂𝗺", callback_data="buy_premium_redirect")]
        ])
        await msg.reply(welcome_msg, reply_markup=trial_markup, parse_mode=enums.ParseMode.MARKDOWN)
        return
    else:
        _save_user_data(user_id, {"last_active": datetime.utcnow()})

    onam_toggle = global_settings.get("onam_toggle", False)
    if onam_toggle:
        onam_text = (
            f"🎉 **Happy Onam!** 🎉\n\n"
            f"Wishing you a season of prosperity and happiness. Enjoy the festivities with our exclusive **Onam Reel Uploads** feature!\n\n"
            f"Use the buttons below to start uploading your festival content!"
        )
        await msg.reply(onam_text, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user_premium = _get_user_data(user_id).get("premium", {})
    instagram_premium_data = user_premium.get("instagram", {})
    tiktok_premium_data = user_premium.get("tiktok", {})

    welcome_msg = f"🚀 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 𝗧𝗲𝗹𝗲𝗴𝗿𝗮𝗺 ➜ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 & 𝗧𝗶𝗸𝗧𝗼𝗸 𝗗𝗶𝗿𝗲𝗰𝘁 𝗨𝗽𝗹𝗼𝗮𝗱𝗲𝗿\n\n"
    premium_details_text = ""
    is_admin_user = is_admin(user_id)
    if is_admin_user:
        premium_details_text += "🛠 You have **admin privileges**.\n\n"

    ig_premium_until = instagram_premium_data.get("until")
    tt_premium_until = tiktok_premium_data.get("until")

    if is_premium_for_platform(user_id, "instagram"):
        if ig_premium_until:
            remaining_time = ig_premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗲𝘅𝗽𝗶𝗿𝗲𝘀 𝗶𝗻: `{days} days, {hours} hours`.\n"
    
    if is_premium_for_platform(user_id, "tiktok"):
        if tt_premium_until:
            remaining_time = tt_premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ 𝗧𝗶𝗸𝗧𝗼𝗸 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗲𝘅𝗽𝗶𝗿𝗲𝘀 𝗶𝗻: `{days} days, {hours} hours`.\n"

    if not is_admin_user and not premium_details_text.strip():
        premium_details_text = (
            "🔥 𝗞𝗲𝘆 𝗙𝗲𝗮𝘁𝘂𝗿𝗲𝘀:\n"
            "✅ ᴅɪʀᴇᴄᴛ ʟᴏɢɪɴ (ɴᴏ ᴛᴏᴋᴇɴꜱ ɴᴇᴇᴅᴇᴅ)\n"
            "✅ ᴜʟᴛʀᴀ-ꜰᴀꜱᴛ ᴜᴘʟᴏᴀᴅɪɴɢ\n"
            "✅ ʜɪɢʜ Qᴜᴀʟɪᴛʏ / ꜰᴀꜱᴛ ᴄᴏᴍᴘʀᴇꜱꜱɪᴏɴ\n"
            "✅ ɴᴏ ꜰɪʟᴇ ꜱɪᴢᴇ ʟɪᴍɪᴛ\n"
            "✅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴜᴘʟᴏᴀᴅꜱ\n"
            "✅ ɪɴꜱᴛᴀɢʀᴀᴍ & ᴛɪᴋᴛᴏᴋ ꜱᴜᴘᴘᴏʀᴛ\n"
            "✅ ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ ᴀꜰᴛᴇʀ ᴜᴘʟᴏᴀᴅ (ᴏᴘᴛɪᴏɴᴀʟ)\n\n"
            "👤 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗔𝗗𝗠𝗜𝗡 𝗧𝗢𝗠 → [𝗖𝗹𝗶𝗰𝗸 𝗛𝗲𝗿𝗲](t.me/CjjTom) 𝘁𝗼 𝗚𝗲𝘁 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗡𝗼𝘄\n"
            "🔐 𝗬𝗼𝘂𝗿 𝗗𝗮𝘁𝗮 𝗜𝘀 𝗙𝘂𝗹𝗹𝘆 ✅ 𝗘𝗻𝗱 𝗧𝗼 𝗘𝗻𝗱 𝗘𝗻𝗰𝗿𝘆𝗽𝘁𝗲𝗱\n\n"
            f"🆔 𝗬𝗼𝘂𝗿 𝗜𝗗: `{user_id}`"
        )
    
    welcome_msg += premium_details_text
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ 𝗔𝗱𝗺𝗶𝗻 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱.")
    restarting_msg = await msg.reply("♻️ Restarting bot...")
    await asyncio.sleep(1)
    await restart_bot(msg)

@app.on_message(filters.command("login"))
@with_user_lock
async def login_cmd(_, msg):
    logger.info(f"User {msg.from_user.id} attempting Instagram login command.")
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply(" ❌ 𝗡𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝗽𝗴𝗿𝗮𝗱𝗲 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝘄𝗶𝘁𝗵 /buypypremium.")
    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Usage: `/login <instagram_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)
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
            f"📝 New Instagram login\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`\n"
            f"Instagram: `{username}`"
        )
        logger.info(f"Instagram login successful for user {user_id} ({username}).")

    except ChallengeRequired:
        await login_msg.edit_text("🔐 Instagram requires challenge verification. Please complete it in the Instagram app and try again.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Instagram Challenge Required for user `{user_id}` (`{username}`).")
        logger.warning(f"Instagram Challenge Required for user {user_id} ({username}).")
    except (BadPassword, LoginRequired) as e:
        await login_msg.edit_text(f"❌ Instagram login failed: {e}. Please check your credentials.")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ Instagram Login Failed for user `{user_id}` (`{username}`): {e}")
        logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
    except PleaseWaitFewMinutes:
        await login_msg.edit_text("⚠️ Instagram is asking to wait a few minutes before trying again. Please try after some time.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Instagram 'Please Wait' for user `{user_id}` (`{username}`).")
        logger.warning(f"Instagram 'Please Wait' for user {user_id} ({username}).")
    except Exception as e:
        await login_msg.edit_text(f"❌ An unexpected error occurred during Instagram login: {str(e)}")
        logger.error(f"Unhandled error during Instagram login for {user_id} ({username}): {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"🔥 Critical Instagram Login Error for user `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.command("tiktoklogin"))
@with_user_lock
async def tiktok_login_cmd(_, msg):
    logger.info(f"User {msg.from_user.id} attempting TikTok login command.")
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ 𝗡𝗼𝘁 𝗮𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝘂𝘀𝗲 𝗧𝗶𝗸𝗧𝗼𝗸 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘂𝗽𝗴𝗿𝗮𝗱𝗲 𝘁𝗼 𝗧𝗶𝗸𝗧𝗼𝗸 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝘄𝗶𝘁𝗵 /buypypremium.")

    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Usage: `/tiktoklogin <tiktok_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 𝗔𝘁𝘁𝗲𝗺𝗽𝘁𝗶𝗻𝗴 𝗧𝗶𝗸𝗧𝗼𝗸 𝗹𝗼𝗴𝗶𝗻...")
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
                await login_msg.edit_text(f"✅ Already logged in to TikTok as `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                _save_user_data(user_id, {"tiktok_username": username})
                return
            except Exception as e:
                logger.warning(f"Failed to validate TikTok session for user {user_id}: {e}. Retrying with fresh login.")
            finally:
                if api and api.browser:
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
        await login_msg.edit_text("✅ 𝗧𝗶𝗸𝗧𝗼𝗸 𝗹𝗼𝗴𝗶𝗻 𝘀𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹!")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"📝 New TikTok login\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`\n"
            f"TikTok: `{username}`"
        )
    except Exception as e:
        # Catch all exceptions related to the login process
        if "login" in str(e).lower() or "captcha" in str(e).lower():
            await login_msg.edit_text(f"❌ TikTok login failed: {e}. Please check your credentials or try again later.")
        else:
            await login_msg.edit_text(f"❌ An unexpected error occurred during TikTok login: {str(e)}")
        logger.error(f"Unhandled error during TikTok login for {user_id} ({username}): {str(e)}")
    finally:
        if api and api.browser:
            await api.browser.close()

@app.on_message(filters.command("buypypremium"))
async def buypypremium_cmd(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    premium_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions for Instagram and TikTok!\n\n"
        "**Available Plans:**\n"
        "• **3 Hour Trial**: Free / Free (Perfect for new users!)\n"
        "• **3 Days Premium**: `₹10 / $0.50`\n"
        "• **7 Days Premium**: `₹25 / $0.70`\n"
        "• **15 Days Premium**: `₹35 / $1.00`\n"
        "• **1 Month Premium**: `₹60 / $2.00`\n"
        "• **3 Months Premium**: `₹150 / $4.50`\n"
        "• **1 Year Premium**: `Negotiable / Negotiable`\n"
        "• **Lifetime Premium**: `Negotiable / Negotiable`\n\n"
        "**Note:** Price might vary based on the number of platforms you choose (Instagram, TikTok, or both).\n\n"
        "To purchase, please contact **[ADMIN TOM](https://t.me/CjjTom)**."
    )
    await msg.reply(premium_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user = _get_user_data(user_id)
    if not user:
        return await msg.reply("You are not registered with the bot. Please use /start.")

    if is_admin(user_id):
        return await msg.reply("👑 You are the **Admin**. You have permanent full access to all features!", parse_mode=enums.ParseMode.MARKDOWN)

    status_text = "⭐ **Your Premium Status:**\n\n"
    has_premium_any = False

    for platform in PREMIUM_PLATFORMS:
        platform_premium = user.get("premium", {}).get(platform, {})
        premium_type = platform_premium.get("type")
        premium_until = platform_premium.get("until")

        status_text += f"**{platform.capitalize()} Premium:** "
        if premium_type == "lifetime":
            status_text += "🎉 **Lifetime!**\n"
            has_premium_any = True
        elif premium_until and premium_until > datetime.utcnow():
            remaining_time = premium_until - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            minutes = (remaining_time.seconds % 3600) // 60
            status_text += (
                f"`{premium_type.replace('_', ' ').title()}` expires on: "
                f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
                f"Time remaining: `{days} days, {hours} hours, {minutes} minutes`\n"
            )
            has_premium_any = True
        else:
            status_text += "😔 **Not Active.**\n"
        status_text += "\n"

    if not has_premium_any:
        status_text = (
            "😔 **𝗬𝗼𝘂 𝗰𝘂𝗿𝗿𝗲𝗻𝘁𝗹𝘆 𝗵𝗮𝘃𝗲 𝗻𝗼 𝗮𝗰𝘁𝗶𝘃𝗲 𝗽𝗿𝗲𝗺𝗶𝘂𝗺.**\n\n"
            "𝗧𝗼 𝘂𝗻𝗹𝗼𝗰𝗸 𝗮𝗹𝗹 𝗳𝗲𝗮𝘁𝘂𝗿𝗲𝘀, 𝗽𝗹𝗲𝗮𝘀𝗲 𝗰𝗼𝗻𝘁𝗮𝗰𝘁 **[𝗔𝗗𝗠𝗜𝗡 𝗧𝗢𝗠](https://t.me/CjjTom)** 𝘁𝗼 𝗯𝘂𝘆 𝗮 𝗽𝗿𝗲𝗺𝗶𝘂𝗺 𝗽𝗹𝗮𝗻."
        )

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("reset_profile"))
@with_user_lock
async def reset_profile_cmd(_, msg):
    user_id = msg.from_user.id
    await msg.reply("⚠️ **Warning!** This will clear all your saved sessions and settings. Are you sure you want to proceed?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Reset My Profile", callback_data="confirm_reset_profile")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data="back_to_main_menu")]
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
    
    # Also clean up state from memory
    user_states.pop(user_id, None)
    
    await query.answer("✅ Your profile has been reset. Please use /start to begin again.", show_alert=True)
    await safe_edit_message(query.message, "✅ Your profile has been reset. Please use /start to begin again.")

@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ 𝗡𝗼𝘁 𝗔𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱. 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗮𝗰𝗰𝗲𝘀𝘀 𝗿𝗲𝗾𝘂𝗶𝗿𝗲𝗱 𝘁𝗼 𝗮𝗰𝗰𝗲𝘀𝘀 𝘀𝗲𝘁𝘁𝗶𝗻𝗴𝘀.")
    
    current_settings = await get_user_settings(user_id)
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
    
    settings_text = "⚙️ Settings Panel\n\n" \
                    f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                    "Use the buttons below to adjust your preferences."

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ 𝗨𝘀𝗲𝗿 𝗦𝗲𝘁𝘁𝗶𝗻𝗴𝘀", callback_data="user_settings_personal")]
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
        return await msg.reply("❌ 𝗬𝗼𝘂𝗿 𝗮𝗰𝗰𝗲𝘀𝘀 𝗵𝗮𝘀 𝗯𝗲𝗲𝗻 𝗱𝗲𝗻𝗶𝗲𝗱. 𝗨𝗽𝗴𝗿𝗮𝗱𝗲 𝘁𝗼 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝘁𝗼 𝘂𝗻𝗹𝗼𝗰𝗸 𝗥𝗲𝗲𝗹𝘀 𝘂𝗽𝗹𝗼𝗮𝗱. /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝘃𝗶𝗱𝗲𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗲𝗹 𝗿𝗲𝗮𝗱𝘆!!")
    user_states[user_id] = {"action": "waiting_for_instagram_reel_video", "platform": "instagram", "upload_type": "reel"}

@app.on_message(filters.regex("📸 Insta Photo"))
@with_user_lock
async def initiate_instagram_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("🚫 𝗡𝗼𝘁 𝗔𝘂𝘁𝗵𝗼𝗿𝗶𝘇𝗲𝗱 𝘁𝗼 𝗨𝗽𝗹𝗼𝗮𝗱 𝗜𝗻𝘀𝘁𝗮𝗴𝗿𝗮𝗺 𝗣𝗵𝗼𝘁𝗼𝘀 𝗣𝗹𝗲𝗮𝘀𝗲 𝗨𝗽𝗴𝗿𝗮𝗱𝗲 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 /buypypremium.")
    
    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝗽𝗵𝗼𝘁𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗮𝗱𝘆 𝗳𝗼𝗿 𝗜𝗚!.")
    user_states[user_id] = {"action": "waiting_for_instagram_photo_image", "platform": "instagram", "upload_type": "post"}

@app.on_message(filters.regex("🎵 TikTok Video"))
@with_user_lock
async def initiate_tiktok_video_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to upload TikTok videos. Please upgrade to TikTok Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ Please login to TikTok first using `/tiktoklogin <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗥𝗲𝗮𝗱𝘆 𝗳𝗼𝗿 𝗧𝗶𝗸𝗧𝗼𝗸 𝗩𝗶𝗱𝗲𝗼 𝗨𝗽𝗹𝗼𝗮𝗱!")
    user_states[user_id] = {"action": "waiting_for_tiktok_video", "platform": "tiktok", "upload_type": "video"}

@app.on_message(filters.regex("🖼️ TikTok Photo"))
@with_user_lock
async def initiate_tiktok_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to upload TikTok photos. Please upgrade to TikTok Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ TikTok session expired. Please login to TikTok first using `/tiktoklogin <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗥𝗲𝗮𝗱𝘆 𝗳𝗼𝗿 𝗧𝗶𝗸𝗧𝗼𝗸 𝗣𝗵𝗼𝘁𝗼 𝗨𝗽𝗹𝗼𝗮𝗱!")
    user_states[user_id] = {"action": "waiting_for_tiktok_photo", "platform": "tiktok", "upload_type": "photo"}

@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLANS):
        return await msg.reply("❌ Not authorized. You need premium access for at least one platform to view stats.")

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
        "📊 **Bot Statistics:**\n\n"
        f"**Users**\n"
        f"👥 Total users: `{total_users}`\n"
        f"👑 Admin users: `{db.users.count_documents({'_id': ADMIN_ID})}`\n"
        f"⭐ Premium users: `{total_premium_users}` (`{premium_percentage:.2f}%`)\n"
    )
    for platform in PREMIUM_PLATFORMS:
        platform_premium_percentage = (premium_counts[platform] / total_users * 100) if total_users > 0 else 0
        stats_text += f"   - {platform.capitalize()} Premium: `{premium_counts[platform]}` (`{platform_premium_percentage:.2f}%`)\n"

    stats_text += (
        f"\n**Uploads**\n"
        f"📈 Total uploads: `{total_uploads}`\n"
        f"🎬 Instagram Reels: `{total_instagram_reel_uploads}`\n"
        f"📸 Instagram Posts: `{total_instagram_post_uploads}`\n"
        f"🎵 TikTok Videos: `{total_tiktok_video_uploads}`\n"
        f"🖼️ TikTok Photos: `{total_tiktok_photo_uploads}`"
    )
    await msg.reply(stats_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_cmd(_, msg):
    if len(msg.text.split(maxsplit=1)) < 2:
        return await msg.reply("Usage: `/broadcast <your message>`", parse_mode=enums.ParseMode.MARKDOWN)
    broadcast_message = msg.text.split(maxsplit=1)[1]
    users = db.users.find({})
    sent_count = 0
    failed_count = 0
    status_msg = await msg.reply("📢 Starting broadcast...")
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
    await status_msg.edit_text(f"✅ Broadcast finished!\nSent to `{sent_count}` users, failed for `{failed_count}` users.")
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 Broadcast initiated by Admin `{msg.from_user.id}`\n"
        f"Sent: `{sent_count}`, Failed: `{failed_count}`"
    )

@app.on_message(filters.text & filters.private & ~filters.command(""))
@with_user_lock
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if not state_data:
        return # Not in a state that requires text input

    action = state_data.get("action")
    
    if action == "waiting_for_caption":
        caption = msg.text
        settings = await get_user_settings(user_id)
        settings["caption"] = caption
        await save_user_settings(user_id, settings)
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)

    elif action == "waiting_for_hashtags":
        hashtags = msg.text
        settings = await get_user_settings(user_id)
        settings["hashtags"] = hashtags
        await save_user_settings(user_id, settings)
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ User ID `{target_user_id}` received. Select platforms for premium:",
                reply_markup=get_platform_selection_markup(user_id, user_states[user_id]["selected_platforms"]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
            user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_max_uploads":
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            new_limit = int(msg.text)
            if new_limit <= 0:
                return await msg.reply("❌ The limit must be a positive integer.")
            _update_global_setting("max_concurrent_uploads", new_limit)
            global upload_semaphore
            upload_semaphore = asyncio.Semaphore(new_limit)
            await msg.reply(f"✅ Maximum concurrent uploads set to `{new_limit}`.", reply_markup=admin_global_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
            user_states.pop(user_id, None)
        except ValueError:
            await msg.reply("❌ Invalid input. Please send a valid number.")
            user_states.pop(user_id, None)

    elif isinstance(state_data, dict) and state_data.get("action") == "awaiting_post_title":
        caption = msg.text
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = caption
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await start_upload_task(msg, file_info)
    
    else:
        # Default fallback for unhandled text
        await msg.reply("I don't understand that command. Please use the menu buttons to interact with me.")

@app.on_callback_query(filters.regex("^activate_trial$"))
async def activate_trial_cb(_, query):
    user_id = query.from_user.id
    user = _get_user_data(user_id)
    user_first_name = query.from_user.first_name or "there"

    if user and is_premium_for_platform(user_id, "instagram"):
        await query.answer("Your Instagram trial is already active! Enjoy your premium access.", show_alert=True)
        welcome_msg = f"🤖 **Welcome back, {user_first_name}!**\n\n"
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
    await send_log_to_channel(app, LOG_CHANNEL, f"✨ User `{user_id}` activated a 3-hour Instagram trial.")

    await query.answer("✅ Free 3-hour Instagram trial activated! Enjoy!", show_alert=True)
    welcome_msg = (
        f"🎉 **Congratulations, {user_first_name}!**\n\n"
        f"You have activated your **3-hour premium trial** for **Instagram**.\n\n"
        "You now have access to upload Instagram content!\n\n"
        "To get started, please log in to your Instagram account with:\n"
        "`/login <your_username> <your_password>`\n\n"
        "Want more features after the trial ends? Check out our paid plans with /buypypremium."
    )
    await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buy_premium_redirect$"))
async def buy_premium_redirect_cb(_, query):
    user_id = query.from_user.id
    premium_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions for Instagram and TikTok!\n\n"
        "**Available Plans:**\n"
        "• **3 Hour Trial**: Free / Free (Perfect for new users!)\n"
        "• **3 Days Premium**: `₹10 / $0.50`\n"
        "• **7 Days Premium**: `₹25 / $0.70`\n"
        "• **15 Days Premium**: `₹35 / $1.00`\n"
        "• **1 Month Premium**: `₹60 / $2.00`\n"
        "• **3 Months Premium**: `₹150 / $4.50`\n"
        "• **1 Year Premium**: `Negotiable / Negotiable`\n"
        "• **Lifetime Premium**: `Negotiable / Negotiable`\n\n"
        "**Note:** Price might vary based on the number of platforms you choose (Instagram, TikTok, or both).\n\n"
        "To purchase, please contact **[ADMIN TOM](https://t.me/CjjTom)**."
    )
    await safe_edit_message(query.message, premium_text, reply_markup=None, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^upload_type$"))
async def upload_type_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    await safe_edit_message(
        query.message,
        "📌 Select upload type:",
        reply_markup=upload_type_markup
    )

@app.on_callback_query(filters.regex("^set_type_"))
async def set_type_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    upload_type = query.data.split("_")[-1]
    current_settings = await get_user_settings(user_id)
    current_settings["upload_type"] = upload_type
    await save_user_settings(user_id, current_settings)
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
    await query.answer(f"✅ Upload type set to {upload_type.capitalize()}!", show_alert=False)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel\n\n🗜️ Compression is currently: **" + compression_status + "**\n\nUse the buttons below to adjust your preferences.",
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio$"))
async def set_aspect_ratio_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    await safe_edit_message(
        query.message,
        "📐 Select desired aspect ratio for videos:",
        reply_markup=aspect_ratio_markup
    )

@app.on_callback_query(filters.regex("^set_ar_"))
async def set_ar_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    aspect_ratio_value = query.data.split("set_ar_")[1]
    current_settings = await get_user_settings(user_id)
    current_settings["aspect_ratio"] = aspect_ratio_value
    await save_user_settings(user_id, current_settings)
    display_text = "Original" if aspect_ratio_value == "original" else "9:16 (Crop/Fit)"
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
    await query.answer(f"✅ Aspect ratio set to {display_text}!", show_alert=False)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel\n\n🗜️ Compression is currently: **" + compression_status + "**\n\nUse the buttons below to adjust your preferences.",
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_caption$"))
@with_user_lock
async def set_caption_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user_states[user_id] = {"action": "waiting_for_caption"}
    current_settings = await get_user_settings(user_id)
    current_caption = current_settings.get("caption", "Not set")
    await safe_edit_message(
        query.message,
        f"📝 Please send the new caption for your uploads.\n\n"
        f"Current caption: {current_caption}",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_hashtags$"))
@with_user_lock
async def set_hashtags_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user_states[user_id] = {"action": "waiting_for_hashtags"}
    current_settings = await get_user_settings(user_id)
    current_hashtags = current_settings.get("hashtags", "Not set")
    await safe_edit_message(
        query.message,
        f"🏷️ Please send the new hashtags for your uploads (e.g., #coding #bot).\n\n"
        f"Current hashtags: {current_hashtags}",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^toggle_compression$"))
async def toggle_compression_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    settings = await get_user_settings(user_id)
    current = settings.get("no_compression", False)
    new_setting = not current
    settings["no_compression"] = new_setting
    await save_user_settings(user_id, settings)
    status = "OFF (Compression Enabled)" if not new_setting else "ON (Original Quality)"
    await query.answer(f"🗜️ Compression is now {status}", show_alert=True)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel\n\n🗜️ Compression is currently: **" + status + "**\n\nUse the buttons below to adjust your preferences.",
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "🛠 Admin Panel",
        reply_markup=admin_markup
    )

@app.on_callback_query(filters.regex("^global_settings_panel$"))
async def global_settings_panel_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    onam_status = "ON" if global_settings.get("onam_toggle") else "OFF"
    max_uploads = global_settings.get("max_concurrent_uploads")
    settings_text = (
        "⚙️ **Global Bot Settings**\n\n"
        f"**Onam Special Event:** `{onam_status}`\n"
        f"**Max Concurrent Uploads:** `{max_uploads}`\n"
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
        return await query.answer("❌ Admin access required", show_alert=True)
    current_status = global_settings.get("onam_toggle", False)
    new_status = not current_status
    _update_global_setting("onam_toggle", new_status)
    status_text = "ON" if new_status else "OFF"
    await query.answer(f"Onam Toggle is now {status_text}.", show_alert=True)
    onam_status = "ON" if global_settings.get("onam_toggle") else "OFF"
    max_uploads = global_settings.get("max_concurrent_uploads")
    settings_text = (
        "⚙️ **Global Bot Settings**\n\n"
        f"**Onam Special Event:** `{onam_status}`\n"
        f"**Max Concurrent Uploads:** `{max_uploads}`\n"
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
        return await query.answer("❌ Admin access required", show_alert=True)
    user_states[user_id] = {"action": "waiting_for_max_uploads"}
    current_limit = global_settings.get("max_concurrent_uploads")
    await safe_edit_message(
        query.message,
        f"🔄 Please send the new maximum number of concurrent uploads.\n\n"
        f"Current limit is: `{current_limit}`"
    )

@app.on_callback_query(filters.regex("^reset_stats$"))
@with_user_lock
async def reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)
    await query.message.edit_text("⚠️ **Warning!** Are you sure you want to reset all upload statistics? This action is irreversible.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Reset Stats", callback_data="confirm_reset_stats")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data="admin_panel")]
        ]), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^confirm_reset_stats$"))
@with_user_lock
async def confirm_reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)
    result = db.uploads.delete_many({})
    await query.answer(f"✅ All upload stats have been reset! Deleted {result.deleted_count} entries.", show_alert=True)
    await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    await send_log_to_channel(app, LOG_CHANNEL, f"📊 Admin `{user_id}` has reset all bot upload statistics.")

@app.on_callback_query(filters.regex("^show_system_stats$"))
async def show_system_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        system_stats_text = (
            "💻 **System Stats**\n\n"
            f"**CPU:** `{cpu_usage}%`\n"
            f"**RAM:** `{ram.percent}%` (Used: `{ram.used / (1024**3):.2f}` GB / Total: `{ram.total / (1024**3):.2f}` GB)\n"
            f"**Disk:** `{disk.percent}%` (Used: `{disk.used / (1024**3):.2f}` GB / Total: `{disk.total / (1024**3):.2f}` GB)\n\n"
        )
        gpu_info = "No GPU found or GPUtil is not installed."
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu_info = "**GPU Info:**\n"
                for i, gpu in enumerate(gpus):
                    gpu_info += (
                        f"   - **GPU {i}:** `{gpu.name}`\n"
                        f"     - Load: `{gpu.load*100:.1f}%`\n"
                        f"     - Memory: `{gpu.memoryUsed}/{gpu.memoryTotal}` MB\n"
                        f"     - Temp: `{gpu.temperature}°C`\n"
                    )
            else:
                gpu_info = "No GPU found."
        except Exception:
            gpu_info = "Could not retrieve GPU info."
        system_stats_text += gpu_info
        await safe_edit_message(
            query.message,
            system_stats_text,
            reply_markup=admin_global_settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await query.answer("❌ Failed to retrieve system stats.", show_alert=True)
        logger.error(f"Error retrieving system stats for admin {user_id}: {e}")
        await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    users = list(db.users.find({}))
    if not users:
        await safe_edit_message(
            query.message,
            "👥 No users found in the database.",
            reply_markup=admin_markup
        )
        return
    user_list_text = "👥 **All Users:**\n\n"
    for user in users:
        user_id = user["_id"]
        instagram_username = user.get("instagram_username", "N/A")
        tiktok_username = user.get("tiktok_username", "N/A")
        added_at = user.get("added_at", "N/A").strftime("%Y-%m-%d") if isinstance(user.get("added_at"), datetime) else "N/A"
        last_active = user.get("last_active", "N/A").strftime("%Y-%m-%d %H:%M") if isinstance(user.get("last_active"), datetime) else "N/A"
        platform_statuses = []
        if user_id == ADMIN_ID:
            platform_statuses.append("👑 Admin")
        else:
            for platform in PREMIUM_PLATFORMS:
                if is_premium_for_platform(user_id, platform):
                    platform_data = user.get("premium", {}).get(platform, {})
                    premium_type = platform_data.get("type")
                    premium_until = platform_data.get("until")
                    if premium_type == "lifetime":
                        platform_statuses.append(f"⭐ {platform.capitalize()}: Lifetime")
                    elif premium_until:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: Expires `{premium_until.strftime('%Y-%m-%d')}`")
                    else:
                        platform_statuses.append(f"⭐ {platform.capitalize()}: Active")
                else:
                    platform_statuses.append(f"❌ {platform.capitalize()}: Free")
        status_line = " | ".join(platform_statuses)
        user_list_text += (
            f"ID: `{user_id}` | {status_line}\n"
            f"IG: `{instagram_username}` | TikTok: `{tiktok_username}`\n"
            f"Added: `{added_at}` | Last Active: `{last_active}`\n"
            "-----------------------------------\n"
        )
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "User list is too long. Sending as a file...")
        with open("users.txt", "w") as f:
            f.write(user_list_text.replace("`", ""))
        await app.send_document(query.message.chat.id, "users.txt", caption="👥 All Users List")
        os.remove("users.txt")
        await safe_edit_message(
            query.message,
            "🛠 Admin Panel",
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
        await query.answer("❌ Admin access required", show_alert=True)
        return
    user_states[query.from_user.id] = {"action": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(
        query.message,
        "➕ Please send the **User ID** to manage their premium access."
    )

@app.on_callback_query(filters.regex("^select_platform_"))
@with_user_lock
async def select_platform_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("Error: User selection lost. Please try 'Manage Premium' again.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
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
        f"✅ User ID `{state_data['target_user_id']}` received. Select platforms for premium:",
        reply_markup=get_platform_selection_markup(user_id, selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_platform_selection$"))
@with_user_lock
async def confirm_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        await query.answer("Error: Please restart the premium management process.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = [p for p, selected in state_data.get("selected_platforms", {}).items() if selected]
    if not selected_platforms:
        return await query.answer("Please select at least one platform!", show_alert=True)
    state_data["action"] = "select_premium_plan_for_platforms"
    state_data["final_selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    await safe_edit_message(
        query.message,
        f"✅ Platforms selected: `{', '.join(platform.capitalize() for platform in selected_platforms)}`. Now, select a premium plan for user `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_plan_"))
@with_user_lock
async def select_plan_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_premium_plan_for_platforms":
        await query.answer("Error: Plan selection lost. Please restart the premium management process.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    selected_platforms = state_data["final_selected_platforms"]
    premium_plan_key = query.data.split("select_plan_")[1]
    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("Invalid premium plan selected.", show_alert=True)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
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
    admin_confirm_text = f"✅ Premium granted to user `{target_user_id}` for:\n"
    for platform in selected_platforms:
        updated_user = _get_user_data(target_user_id)
        platform_data = updated_user.get("premium", {}).get(platform, {})
        confirm_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if platform_data.get("until"):
            confirm_line += f" (Expires: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} UTC`)"
        admin_confirm_text += f"- {confirm_line}\n"
    await safe_edit_message(
        query.message,
        admin_confirm_text,
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer("Premium granted!", show_alert=False)
    user_states.pop(user_id, None)
    try:
        user_msg = (
            f"🎉 **Congratulations!** 🎉\n\n"
            f"You have been granted premium access for the following platforms:\n"
        )
        for platform in selected_platforms:
            updated_user = _get_user_data(target_user_id)
            platform_data = updated_user.get("premium", {}).get(platform, {})
            msg_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
            if platform_data.get("until"):
                msg_line += f" (Expires: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} UTC`)"
            user_msg += f"- {msg_line}\n"
        user_msg += "\nEnjoy your new features! ✨"
        await app.send_message(target_user_id, user_msg, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 Premium granted notification sent to `{target_user_id}` by Admin `{user_id}`. Platforms: `{', '.join(selected_platforms)}`, Plan: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {target_user_id} about premium: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ Failed to notify user `{target_user_id}` about premium. Error: `{str(e)}`"
        )

@app.on_callback_query(filters.regex("^back_to_platform_selection$"))
@with_user_lock
async def back_to_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") not in ["select_platforms_for_premium", "select_premium_plan_for_platforms"]:
        await query.answer("Error: Invalid state for back action. Please restart the process.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    target_user_id = state_data["target_user_id"]
    current_selected_platforms = state_data.get("selected_platforms", {})
    user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": current_selected_platforms}
    await safe_edit_message(
        query.message,
        f"✅ User ID `{target_user_id}` received. Select platforms for premium:",
        reply_markup=get_platform_selection_markup(user_id, current_selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    await safe_edit_message(
        query.message,
        "📢 Please send the message you want to broadcast to all users.\n\n"
        "Use `/broadcast <message>` command instead."
    )

@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def user_settings_personal_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if is_admin(user_id) or any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
        settings_text = "⚙️ Your Personal Settings\n\n" \
                        f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                        "Use the buttons below to adjust your preferences."
        await safe_edit_message(
        query.message,
        settings_text,
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
        )
    else:
        await query.answer("❌ Not authorized.", show_alert=True)
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
            "🏠 Main Menu",
            reply_markup=get_main_keyboard(user_id)
        )
    elif data == "back_to_settings":
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
        settings_text = "⚙️ Settings Panel\n\n" \
                        f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                        "Use the buttons below to adjust your preferences."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "back_to_admin_from_stats" or data == "back_to_admin_from_global":
        await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    elif data == "back_to_main_from_admin":
        await query.message.edit_text("🏠 Main Menu", reply_markup=get_main_keyboard(user_id))

@app.on_callback_query(filters.regex("^(skip_caption|cancel_upload)$"))
async def handle_upload_actions(_, query):
    user_id = query.from_user.id
    action = query.data
    state_data = user_states.get(user_id)

    if not state_data or state_data.get("action") not in ["awaiting_post_title", "processing_upload", "uploading_file"]:
        await query.answer("❌ No active upload to cancel or skip.", show_alert=True)
        return

    if action == "cancel_upload":
        if user_id in upload_tasks and not upload_tasks[user_id].done():
            upload_tasks[user_id].cancel()
            await query.answer("❌ Upload cancelled.", show_alert=True)
            await safe_edit_message(query.message, "❌ Upload has been cancelled.")
            user_states.pop(user_id, None)
            upload_tasks.pop(user_id, None)
            cleanup_temp_files([state_data.get("file_info", {}).get("downloaded_path"), state_data.get("file_info", {}).get("transcoded_path")])
        else:
            await query.answer("❌ No active upload task to cancel.", show_alert=True)
            user_states.pop(user_id, None)

    elif action == "skip_caption":
        await query.answer("✅ Using default caption.", show_alert=True)
        file_info = state_data.get("file_info")
        file_info["custom_caption"] = None
        user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
        await safe_edit_message(query.message, f"✅ Skipped. Uploading with default caption...")
        await start_upload_task(query.message, file_info)

async def start_upload_task(msg, file_info):
    user_id = msg.from_user.id
    task = asyncio.create_task(process_and_upload(msg, file_info))
    upload_tasks[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        logger.info(f"Upload task for user {user_id} was cancelled.")
    except Exception as e:
        logger.error(f"Upload task for user {user_id} failed with an unhandled exception: {e}")
        await msg.reply("❌ An unexpected error occurred during upload. Please try again.")

async def process_and_upload(msg, file_info):
    user_id = msg.from_user.id
    platform = file_info["platform"]
    upload_type = file_info["upload_type"]
    file_path = file_info["downloaded_path"]
    
    processing_msg = file_info["processing_msg"]

    try:
        # Check if FFmpeg is needed
        video_to_upload = file_path
        transcoded_video_path = None
        
        settings = await get_user_settings(user_id)
        no_compression = settings.get("no_compression", False)
        aspect_ratio_setting = settings.get("aspect_ratio", "original")

        if upload_type in ["reel", "video"] and (not no_compression or aspect_ratio_setting != "original"):
            await processing_msg.edit_text("🔄 Optimizing video (transcoding audio/video)... This may take a moment.")
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
                # Video processing for 9:16 aspect ratio
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
                    raise Exception(f"Video transcoding failed: {stderr.decode()}")
                else:
                    logger.info(f"FFmpeg transcoding successful. Output: {transcoded_video_path}")
                    video_to_upload = transcoded_video_path
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Deleted original downloaded video file: {file_path}")
            except asyncio.TimeoutError:
                process.kill()
                logger.error(f"FFmpeg process timed out for user {user_id}")
                raise Exception("Video transcoding timed out.")
        else:
            await processing_msg.edit_text("✅ 𝗢𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝗳𝗶𝗹𝗲. 𝗡𝗼 𝗰𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻.")

        # Final caption preparation
        settings = await get_user_settings(user_id)
        default_caption = settings.get("caption", f"Check out my new {platform.capitalize()} content! 🎥")
        hashtags = settings.get("hashtags", "")
        
        final_caption = file_info.get("custom_caption") or default_caption
        if hashtags:
            final_caption = f"{final_caption}\n\n{hashtags}"

        url = "N/A"
        media_id = "N/A"
        media_type_value = ""

        await processing_msg.edit_text("🚀 **Uploading to platform...**", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup())
        start_time = time.time()

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            if INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
            session = await load_instagram_session(user_id)
            if not session:
                raise LoginRequired("Instagram session expired.")
            user_upload_client.set_settings(session)
            
            try:
                await asyncio.to_thread(user_upload_client.get_timeline_feed)
            except LoginRequired:
                raise LoginRequired("Instagram session expired.")

            if upload_type == "reel":
                result = await asyncio.to_thread(user_upload_client.clip_upload, video_to_upload, caption=final_caption)
                url = f"https://instagram.com/reel/{result.code}"
                media_id = result.pk
                media_type_value = result.media_type.value
            elif upload_type == "post":
                result = await asyncio.to_thread(user_upload_client.photo_upload, video_to_upload, caption=final_caption)
                url = f"https://instagram.com/p/{result.code}"
                media_id = result.pk
                media_type_value = result.media_type.value

        elif platform == "tiktok":
            tiktok_client = TikTokApi()
            session = await load_tiktok_session(user_id)
            if not session:
                raise Exception("TikTok session expired.")

            await tiktok_client.create_sessions(
                session_path=TIKTOK_SESSION_FILE,
                headless=True,
                browser_session_id=session.get('browser_session_id')
            )
            try:
                if upload_type == "video":
                    await tiktok_client.upload.video(video_to_upload, title=final_caption)
                elif upload_type == "photo":
                    await tiktok_client.upload.photo_album([file_path], title=final_caption)
                url = "N/A" # TikTok API doesn't return a direct link
                media_id = "N/A"
                media_type_value = upload_type
            finally:
                if tiktok_client.browser:
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
            f"📤 New {platform.capitalize()} {upload_type.capitalize()} Upload\n\n"
            f"👤 User: `{user_id}`\n"
            f"📛 Username: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 URL: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await processing_msg.edit_text(f"✅ Uploaded successfully!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except asyncio.CancelledError:
        logger.info(f"Upload process for user {user_id} was cancelled.")
        await processing_msg.edit_text("❌ Upload process cancelled.")
    except LoginRequired:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} login required. Your session might have expired. Please use `/{platform}login <username> <password>` again.")
        logger.error(f"LoginRequired during {platform} upload for user {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} upload failed (Login Required)\nUser: `{user_id}`")
    except ClientError as ce:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} client error during upload: {ce}. Please try again later.")
        logger.error(f"ClientError during {platform} upload for user {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} upload failed: {str(e)}"
        if processing_msg:
            await processing_msg.edit_text(error_msg)
        else:
            await msg.reply(error_msg)
        logger.error(f"{platform.capitalize()} upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
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
        return await msg.reply("❌ Please use one of the upload buttons first.")

    platform = state_data["platform"]
    upload_type = state_data["upload_type"]

    if msg.video and (upload_type in ["reel", "video"]):
        if msg.video.file_size > MAX_FILE_SIZE_BYTES:
            user_states.pop(user_id, None)
            return await msg.reply(f"❌ File size exceeds the limit of `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` MB.")
        file_info = {
            "file_id": msg.video.file_id,
            "platform": platform,
            "upload_type": upload_type,
            "file_size": msg.video.file_size,
            "processing_msg": await msg.reply("⏳ Starting download...")
        }
    elif msg.photo and (upload_type in ["post", "photo"]):
        file_info = {
            "file_id": msg.photo.file_id,
            "platform": platform,
            "upload_type": upload_type,
            "file_size": msg.photo.file_size,
            "processing_msg": await msg.reply("⏳ Starting download...")
        }
    else:
        user_states.pop(user_id, None)
        return await msg.reply("❌ The file type does not match the requested upload type.")

    file_info["downloaded_path"] = None
    
    try:
        start_time = time.time()
        file_info["processing_msg"].is_progress_message_updated = False
        file_info["downloaded_path"] = await app.download_media(
            msg,
            progress=lambda current, total: progress_callback(current, total, "Download", file_info["processing_msg"], start_time)
        )
        await file_info["processing_msg"].edit_text("✅ Download complete. What title do you want for your post?", reply_markup=get_caption_markup())
        user_states[user_id] = {"action": "awaiting_post_title", "file_info": file_info}

    except asyncio.CancelledError:
        logger.info(f"Download cancelled by user {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"Error during file download for user {user_id}: {e}")
        await file_info["processing_msg"].edit_text(f"❌ Download failed: {str(e)}")
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

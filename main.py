import os
import sys
import asyncio
import threading
import logging
import subprocess
import shutil
import math
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

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

# Instagram Integration
from instagrapi import Client as InstaClient
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    BadPassword,
    PleaseWaitFewMinutes,
    ClientError
)

# TikTok Integration
# NOTE: Requires Python 3.8+ and Playwright. 
# Make sure to install with: pip install TikTokApi playwright
# And then run: playwright install
from TikTokApi import TikTokApi
#from TikTokApi.exceptions import TikTokAPIError

# Utilities
import psutil
import GPUtil

# Import log handler from local file
from log_handler import send_log_to_channel

# === CONFIGURATION AND INITIALIZATION ===

# Environment variables
API_ID = int(os.getenv("TELEGRAM_API_ID", "27356561"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "efa4696acce7444105b02d82d0b2e381")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002544142397"))
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6644681404"))

# Instagram account credentials for the bot's own primary account, if any.
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

# Local file paths for session data
SESSION_FILE = "instagrapi_session.json"

# === DATABASE & LOGGING SETUP ===

try:
    mongo = MongoClient(MONGO_URI)
    db = mongo["Telegram Instagram"]
    logging.info("Connected to MongoDB successfully.")
except Exception as e:
    logging.critical(f"Failed to connect to MongoDB: {e}")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger("Telegram Instagram Bot")

# === GLOBAL STATE MANAGEMENT ===

DEFAULT_GLOBAL_SETTINGS = {
    "onam_toggle": False,
    "max_concurrent_uploads": 15,
    "upload_file_size_limit": 100 * 1024 * 1024
}

global_settings = db.settings.find_one({"_id": "global_settings"}) or DEFAULT_GLOBAL_SETTINGS
db.settings.update_one({"_id": "global_settings"}, {"$set": global_settings}, upsert=True)
logger.info(f"Global settings loaded: {global_settings}")

MAX_CONCURRENT_UPLOADS = global_settings.get("max_concurrent_uploads", DEFAULT_GLOBAL_SETTINGS["max_concurrent_uploads"])
upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)

FFMPEG_TIMEOUT_SECONDS = 300
FILE_SIZE_LIMIT = global_settings.get("upload_file_size_limit", DEFAULT_GLOBAL_SETTINGS["upload_file_size_limit"])

app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
insta_client = InstaClient()
insta_client.delay_range = [1, 3]

user_states = {}
active_uploads = {}

# === PREMIUM DEFINITIONS ===
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

# === KEYBOARDS & UI ELEMENTS ===

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
    [InlineKeyboardButton("🌐 Set Proxy", callback_data="set_proxy")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Manage Premium", callback_data="manage_premium")],
    [InlineKeyboardButton("⚙️ Global Settings", callback_data="global_settings_panel")],
    [InlineKeyboardButton("📊 Stats Panel", callback_data="admin_stats_panel")],
    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_message")],
    [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_to_main_menu")]
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
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_settings")]
])

aspect_ratio_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Original Aspect Ratio", callback_data="set_ar_original")],
    [InlineKeyboardButton("9:16 (Crop/Fit)", callback_data="set_ar_9_16")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_settings")]
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
                    base_price = float(display_price.replace('₹', '').strip())
                    calculated_price = base_price * price_multiplier
                    display_price = f"₹{int(calculated_price)}"
                except ValueError:
                    pass
            
            buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()} ({display_price})", callback_data=f"select_plan_{key}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Platform Selection", callback_data="back_to_platform_selection")])
    return InlineKeyboardMarkup(buttons)

# === HELPER FUNCTIONS ===

def is_admin(user_id):
    return user_id == ADMIN_ID

def _get_user_data(user_id):
    """Retrieves user data from MongoDB."""
    return db.users.find_one({"_id": user_id})

def _save_user_data(user_id, data_to_update):
    """Updates user data in MongoDB. Uses $set for partial updates."""
    db.users.update_one(
        {"_id": user_id},
        {"$set": data_to_update},
        upsert=True
    )

def _update_global_setting(key, value):
    """Updates a single global setting in MongoDB and the in-memory dictionary."""
    db.settings.update_one({"_id": "global_settings"}, {"$set": {key: value}}, upsert=True)
    global_settings[key] = value

def is_premium_for_platform(user_id, platform):
    """Checks if a user has active premium for a specific platform."""
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
    """Saves Instagram session data to MongoDB."""
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"instagram_session": session_data}},
        upsert=True
    )
    logger.info(f"Instagram session saved for user {user_id}")

async def load_instagram_session(user_id):
    """Loads Instagram session data from MongoDB."""
    session = db.sessions.find_one({"user_id": user_id})
    return session.get("instagram_session") if session else None

async def save_tiktok_session(user_id, session_data):
    """Saves TikTok session data to MongoDB."""
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"tiktok_session": session_data}},
        upsert=True
    )
    logger.info(f"TikTok session saved for user {user_id}")

async def load_tiktok_session(user_id):
    """Loads TikTok session data from MongoDB."""
    session = db.sessions.find_one({"user_id": user_id})
    return session.get("tiktok_session") if session else None

async def save_user_settings(user_id, settings):
    """Saves user-specific settings to MongoDB."""
    db.settings.update_one(
        {"_id": user_id},
        {"$set": settings},
        upsert=True
    )
    logger.info(f"User settings saved for user {user_id}")

async def get_user_settings(user_id):
    """Retrieves user-specific settings from MongoDB, with defaults."""
    settings = db.settings.find_one({"_id": user_id}) or {}
    if "aspect_ratio" not in settings:
        settings["aspect_ratio"] = "original"
    if "no_compression" not in settings:
        settings["no_compression"] = False
    if "proxy" not in settings:
        settings["proxy"] = None
    return settings

async def safe_edit_message(message, text, reply_markup=None, parse_mode=enums.ParseMode.MARKDOWN):
    """Safely edits a Telegram message, handling potential errors."""
    try:
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        logger.warning(f"Couldn't edit message: {e}")

async def restart_bot(msg):
    """Restarts the bot process."""
    dt = get_current_datetime()
    restart_msg_log = (
        "🔄 Bot Restart Initiated!\n\n"
        f"📅 Date: {dt['date']}\n"
        f"⏰ Time: {dt['time']}\n"
        f"🌐 Timezone: {dt['timezone']}\n"
        f"👤 By: {msg.from_user.mention} (ID: `{msg.from_user.id}`)"
    )
    logger.info(f"User {msg.from_user.id} attempting restart command.")
    await send_log_to_channel(await app.get_me(), LOG_CHANNEL, restart_msg_log)
    await msg.reply("✅ Bot is restarting...")
    await asyncio.sleep(2)

    try:
        logger.info("Executing os.execv to restart process...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execute restart via os.execv: {e}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"❌ Restart failed for {msg.from_user.id}: {str(e)}")
        await msg.reply(f"❌ Failed to restart bot: {str(e)}")

def load_instagram_client_session():
    """Attempts to load or login the bot's own primary Instagram client."""
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

async def _get_tiktok_api_instance(user_id):
    """Centralized function to create and configure a TikTokApi instance for a user."""
    user_settings = await get_user_settings(user_id)
    proxy = user_settings.get("proxy")

    try:
        if proxy:
            api = await TikTokApi(proxies=[proxy]).__aenter__()
        else:
            api = await TikTokApi().__aenter__()
    except Exception as e:
        logger.error(f"Failed to create TikTokApi instance for user {user_id}: {e}")
        raise

    return api

def cleanup_temp_files(files_to_delete):
    """Centralized function to delete temporary files."""
    for file_path in files_to_delete:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Deleted local file: {file_path}")
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {e}")

def humanbytes(size):
    """Convert bytes to a human-readable format."""
    if size is None:
        return ""
    if size == 0:
        return "0B"
    power = 2**10
    n = 0
    dic_powerN = {0: ' ', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size > power:
        size /= power
        n += 1
    return f"{round(size, 2)}{dic_powerN.get(n, ' ')}B"

async def progress_callback(current, total, client, message, start_time, action_text):
    """Pyrogram download/upload progress callback with real-time updates."""
    if not message.is_edited:
        pass
    
    elapsed_time = time.time() - start_time
    if elapsed_time == 0:
        elapsed_time = 1

    speed = current / elapsed_time
    eta = (total - current) / speed if speed > 0 else 0
    
    percentage = int(current * 100 / total)
    progress_bar = f"[{'■' * (percentage // 10)}{'□' * (10 - percentage // 10)}]"

    progress_text = (
        f"**{action_text}**\n\n"
        f"**Progress:** `{progress_bar}`\n"
        f"**Percentage:** `{percentage:.2f}%`\n"
        f"**Speed:** `{humanbytes(speed)}/s`\n"
        f"**Size:** `{humanbytes(current)} / {humanbytes(total)}`\n"
        f"**ETA:** `{str(timedelta(seconds=int(eta))) if eta > 0 else 'Calculating...'}`"
    )
    
    try:
        last_edit_time = getattr(message, 'last_edit_time', 0)
        if percentage % 5 == 0 and time.time() - last_edit_time > 2:
            if len(progress_text) > 4096:
                progress_text = progress_text[:4090] + "..."
            await message.edit_text(progress_text, parse_mode=enums.ParseMode.MARKDOWN)
            setattr(message, 'last_edit_time', time.time())
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"Error updating progress message: {e}")

# === MESSAGE HANDLERS ===

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    if is_admin(user_id):
        welcome_msg = "🤖 **Welcome to Telegram Instagram Bot!**\n\n"
        welcome_msg += "🛠 You have **admin privileges**."
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = _get_user_data(user_id)
    is_new_user = not user
    
    if is_new_user:
        _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")
        
        welcome_msg = (
            f"👋 **Hi {user_first_name}!**\n\n"
            "This Bot lets you upload any size Instagram Reels & Posts directly from Telegram.\n\n"
            "To get a taste of the premium features, you can activate a **free 3-hour trial** for Instagram right now!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Activate Free 3-Hour Trial", callback_data="activate_trial")],
            [InlineKeyboardButton("➡️ Buy Premium", callback_data="buy_premium_redirect")]
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

    welcome_msg = f"🤖 **Welcome to Telegram Instagram Bot!**\n\n"

    premium_details_text = ""
    is_admin_user = is_admin(user_id)
    if is_admin_user:
        premium_details_text += "🛠 You have **admin privileges**.\n\n"
    
    if is_premium_for_platform(user_id, "instagram"):
        ig_expiry = instagram_premium_data.get("until")
        if ig_expiry:
            remaining_time = ig_expiry - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ **Instagram Premium** expires in: `{days} days, {hours} hours`.\n"
    if is_premium_for_platform(user_id, "tiktok"):
        tt_expiry = tiktok_premium_data.get("until")
        if tt_expiry:
            remaining_time = tt_expiry - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ **Tiktok Premium** expires in: `{days} days, {hours} hours`.\n"

    if not is_admin_user and not premium_details_text:
        premium_details_text = (
            "You currently have no active premium. 😔\n\n"
            "To unlock all features, please contact **[ADMIN TOM](https://t.me/CjjTom)** to buy a premium plan."
        )

    welcome_msg += premium_details_text

    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Admin access required.")

    restarting_msg = await msg.reply("♻️ Restarting bot...")
    await asyncio.sleep(1)
    await restart_bot(msg)

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    """Handles user Instagram login."""
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Not authorized to use Instagram features. Please upgrade to Instagram Premium with /buypypremium.")

    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Usage: `/login <instagram_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 Attempting Instagram login...")

    try:
        user_insta_client = InstaClient()
        user_insta_client.delay_range = [1, 3]
        if INSTAGRAM_PROXY:
            user_insta_client.set_proxy(INSTAGRAM_PROXY)
        
        session = await load_instagram_session(user_id)
        if session:
            try:
                await asyncio.to_thread(user_insta_client.set_settings, session)
                await asyncio.to_thread(user_insta_client.get_timeline_feed)
                await login_msg.edit_text(f"✅ Already logged in to Instagram as `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                return
            except LoginRequired:
                await asyncio.to_thread(user_insta_client.set_settings, {})

        await asyncio.to_thread(user_insta_client.login, username, password)
        session_data = await asyncio.to_thread(user_insta_client.get_settings)
        await save_instagram_session(user_id, session_data)
        _save_user_data(user_id, {"instagram_username": username})

        await login_msg.edit_text("✅ Instagram login successful!")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL,
            f"📝 New Instagram login\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`\n"
            f"Instagram: `{username}`"
        )
    except ChallengeRequired:
        await login_msg.edit_text("🔐 Instagram requires challenge verification. Please complete it in the Instagram app and try again.")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"⚠️ Instagram Challenge Required for user `{user_id}` (`{username}`).")
    except (BadPassword, LoginRequired) as e:
        await login_msg.edit_text(f"❌ Instagram login failed: {e}. Please check your credentials.")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"❌ Instagram Login Failed for user `{user_id}` (`{username}`): {e}")
    except PleaseWaitFewMinutes:
        await login_msg.edit_text("⚠️ Instagram is asking to wait a few minutes before trying again. Please try after some time.")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"⚠️ Instagram 'Please Wait' for user `{user_id}` (`{username}`).")
    except Exception as e:
        await login_msg.edit_text(f"❌ An unexpected error occurred during Instagram login: {str(e)}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"🔥 Critical Instagram Login Error for user `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.command("cookie"))
async def login_cookie_cmd(_, msg):
    """Handles Instagram login via session cookie."""
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Not authorized to use this feature.")

    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return await msg.reply("Usage: `/cookie <sessionid>`\n\nYou can get your sessionid from your browser developer tools.", parse_mode=enums.ParseMode.MARKDOWN)

    sessionid = args[1].strip()
    login_msg = await msg.reply("🔐 Verifying session cookie...")
    
    try:
        user_insta_client = InstaClient()
        await asyncio.to_thread(user_insta_client.login_by_sessionid, sessionid)
        
        session_data = await asyncio.to_thread(user_insta_client.get_settings)
        await save_instagram_session(user_id, session_data)
        
        username = user_insta_client.username
        _save_user_data(user_id, {"instagram_username": username})
        
        await login_msg.edit_text(f"✅ Instagram login via cookie successful as `{username}`!")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL,
            f"📝 New Instagram login via cookie\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`\n"
            f"Instagram: `{username}`"
        )
    except Exception as e:
        await login_msg.edit_text(f"❌ Failed to login with cookie: {str(e)}. Please check the sessionid.")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"❌ Instagram Cookie Login Failed for user `{user_id}`: {str(e)}")

@app.on_message(filters.command("tiktoklogin"))
async def tiktok_login_cmd(_, msg):
    """Handles real user TikTok login."""
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to use TikTok features. Please upgrade to TikTok Premium with /buypypremium.")

    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return await msg.reply("Usage: `/tiktoklogin <session_cookie>`\n\nTo log in, please provide a valid session cookie. You can find this in your browser's developer tools.", parse_mode=enums.ParseMode.MARKDOWN)

    session_cookie = args[1].strip()
    login_msg = await msg.reply("🔐 Attempting TikTok login with provided session cookie...")

    try:
        api = await _get_tiktok_api_instance(user_id)

        session_data = {}
        for part in session_cookie.split(';'):
            if '=' in part:
                key, value = part.strip().split('=', 1)
                session_data[key] = value

        await asyncio.to_thread(api.set_session_cookies, session_data)
        
        try:
            me = await asyncio.to_thread(api.me)
            if not me:
                raise Exception("Session invalid, profile data could not be retrieved.")
            username = me.get('unique_id', 'unknown')
        except Exception:
            raise Exception("Session invalid, profile data could not be retrieved.")

        await save_tiktok_session(user_id, session_data)
        _save_user_data(user_id, {"tiktok_username": username})

        await login_msg.edit_text(f"✅ TikTok login successful as `{username}`!")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL,
            f"📝 New TikTok login\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`"
        )
    except Exception as e:
        if "Captcha" in str(e):
            await login_msg.edit_text("❌ TikTok login failed: A CAPTCHA is required. Please try again later or use a different session.")
            await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"❌ TikTok Login Failed for user `{user_id}`: CAPTCHA required.")
        else:
            await login_msg.edit_text(f"❌ TikTok login failed: {str(e)}. Please check your session cookie.")
            await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"🔥 Critical TikTok Login Error for user `{user_id}`: {str(e)}")


@app.on_message(filters.command("buypypremium"))
async def buypypremium_cmd(_, msg):
    """Displays premium plans."""
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
    """Shows user's current premium status for all platforms."""
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
            "You currently have no active premium. 😔\n\n"
            "To unlock all features, please contact **[ADMIN TOM](https://t.me/CjjTom)** to buy a premium plan."
        )

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("reset_profile") & filters.private)
async def reset_profile_cmd(_, msg):
    """Resets all user data (excluding premium status for security)."""
    user_id = msg.from_user.id
    user_data = _get_user_data(user_id)
    if not user_data:
        return await msg.reply("❌ You do not have a profile to reset.")
    
    confirm_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Reset My Profile", callback_data="confirm_reset_profile")],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main_menu")]
    ])
    await msg.reply("⚠️ **Warning!** This will reset all your stored data (sessions, settings, etc.) but will NOT remove your premium status. Are you sure you want to proceed?",
                    reply_markup=confirm_markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("repeat") & filters.private)
async def repeat_upload_cmd(_, msg):
    """Re-uploads the last file sent by the user."""
    user_id = msg.from_user.id
    last_upload = db.uploads.find_one({"user_id": user_id}, sort=[("timestamp", -1)])
    if not last_upload:
        return await msg.reply("❌ No previous uploads found to repeat.")

    await msg.reply("🔄 Re-uploading your last file...")
    await msg.reply("❌ Feature not yet fully implemented. Please send the file again.")


@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    """Displays the settings menu."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ Not authorized. You need premium access for at least one platform to access settings.")

    current_settings = await get_user_settings(user_id)
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
    proxy_status = f"✅ `{current_settings.get('proxy')}`" if current_settings.get('proxy') else "❌ None"

    settings_text = "⚙️ Settings Panel\n\n" \
                    f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                    f"🌐 Proxy for TikTok: {proxy_status}\n\n" \
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
async def initiate_instagram_reel_upload(_, msg):
    """Initiates the process for uploading an Instagram Reel."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Not authorized to upload Instagram Reels. Please upgrade to Instagram Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>` or `/cookie <sessionid>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝘃𝗶𝗱𝗲𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗲𝗹 𝗿𝗲𝗮𝗱𝘆!!")
    user_states[user_id] = {"state": "waiting_for_instagram_reel_video", "data": {}}

@app.on_message(filters.regex("📸 Insta Photo"))
async def initiate_instagram_photo_upload(_, msg):
    """Initiates the process for uploading an Instagram Photo."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Not authorized to upload Instagram Photos. Please upgrade to Instagram Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>` or `/cookie <sessionid>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝗽𝗵𝗼𝘁𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗮𝗱𝘆 𝗳𝗼𝗿 𝗜𝗚!.")
    user_states[user_id] = {"state": "waiting_for_instagram_photo_image", "data": {}}

@app.on_message(filters.regex("🎵 TikTok Video"))
async def initiate_tiktok_video_upload(_, msg):
    """Initiates the process for uploading a TikTok video."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to upload TikTok videos. Please upgrade to TikTok Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ TikTok session expired. Please login to TikTok first using `/tiktoklogin <session_cookie>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for TikTok video upload! Please send me the video file.")
    user_states[user_id] = {"state": "waiting_for_tiktok_video", "data": {}}

@app.on_message(filters.regex("🖼️ TikTok Photo"))
async def initiate_tiktok_photo_upload(_, msg):
    """Initiates the process for uploading a TikTok photo."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to upload TikTok photos. Please upgrade to TikTok Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ TikTok session expired. Please login to TikTok first using `/tiktoklogin <session_cookie>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for TikTok photo upload! Please send me the image file.")
    user_states[user_id] = {"state": "waiting_for_tiktok_photo", "data": {}}

@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    """Displays bot usage statistics."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
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
    upload_percentage = (total_uploads / (total_users * 100)) if total_users > 0 else 0

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
    """Allows admin to broadcast a message to all users."""
    if len(msg.text.split(maxsplit=1)) < 2:
        return await msg.reply("Usage: `/broadcast <your message>`")

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
    await send_log_to_channel(await app.get_me(), LOG_CHANNEL,
        f"📢 Broadcast initiated by Admin `{msg.from_user.id}`\n"
        f"Sent: `{sent_count}`, Failed: `{failed_count}`"
    )

@app.on_message(filters.command("skip") & filters.private)
async def skip_caption_cmd(_, msg):
    user_id = msg.from_user.id
    if user_states.get(user_id, {}).get("state") == "waiting_for_caption_or_skip":
        state_data = user_states[user_id]["data"]
        state_data["caption_title"] = ""
        user_states[user_id]["state"] = "final_upload"
        
        await msg.reply("✅ Skipped. Using default caption.")
        
        asyncio.create_task(process_and_upload(user_id, state_data["platform"], state_data["upload_type"], state_data["file_path"], state_data["message_to_edit"]))
    else:
        await msg.reply("❌ The `/skip` command is only available when a caption is requested.")

@app.on_message(filters.command("cancel_upload") & filters.private)
async def cancel_upload_command(_, msg):
    user_id = msg.from_user.id
    if user_id in active_uploads:
        active_uploads[user_id].cancel()
        user_states.pop(user_id, None)
        await msg.reply("❌ Upload cancelled.")
    else:
        await msg.reply("❌ No active upload to cancel.")

@app.on_message(filters.command("reset_login") & filters.private)
async def reset_login_cmd(_, msg):
    user_id = msg.from_user.id
    # Reset sessions
    db.sessions.delete_one({"user_id": user_id})
    await msg.reply("✅ Your stored login sessions have been cleared. Please log in again.")
    # Also update the user data to clear the username
    _save_user_data(user_id, {"instagram_username": None, "tiktok_username": None})

@app.on_message(filters.text & filters.private & ~filters.command(""))
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id, {})
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if state_data.get("state") == "waiting_for_caption":
        caption = msg.text
        await save_user_settings(user_id, {"caption": caption})
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif state_data.get("state") == "waiting_for_hashtags":
        hashtags = msg.text
        await save_user_settings(user_id, {"hashtags": hashtags})
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Quality)"
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif state_data.get("state") == "waiting_for_proxy":
        proxy = msg.text.strip()
        await save_user_settings(user_id, {"proxy": proxy})
        await msg.reply(f"✅ Proxy set to `{proxy}`.", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif isinstance(state_data, dict) and state_data.get("state") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"state": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ User ID `{target_user_id}` received. Select platforms for premium:",
                reply_markup=get_platform_selection_markup(user_id, user_states[user_id]["selected_platforms"]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
            user_states.pop(user_id, None)
    elif isinstance(state_data, dict) and state_data.get("state") == "waiting_for_max_uploads":
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
    elif state_data.get("state") == "waiting_for_caption_or_skip":
        state_data["data"]["caption_title"] = msg.text
        user_states[user_id]["state"] = "final_upload"
        
        await msg.reply("✅ Title saved. Uploading now...", reply_markup=ReplyKeyboardRemove())
        
        asyncio.create_task(process_and_upload(user_id, state_data["data"]["platform"], state_data["data"]["upload_type"], state_data["data"]["file_path"], state_data["data"]["message_to_edit"]))
    else:
        await msg.reply("I'm not sure how to handle that message. Please use a button or command.")

# === CALLBACK HANDLERS ===

@app.on_callback_query(filters.regex("^activate_trial$"))
async def activate_trial_cb(_, query):
    user_id = query.from_user.id
    user = _get_user_data(user_id)
    user_first_name = query.from_user.first_name or "there"

    if user and is_premium_for_platform(user_id, "instagram"):
        await query.answer("Your Instagram trial is already active! Enjoy your premium access.", show_alert=True)
        welcome_msg = (
            f"🤖 **Welcome back, {user_first_name}!**\n\n"
        )
        premium_details_text = ""
        user_premium = user.get("premium", {})
        ig_expiry = premium_details_text = user_premium.get("instagram", {}).get("until")
        if ig_expiry:
            remaining_time = ig_expiry - datetime.utcnow()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            premium_details_text += f"⭐ **Instagram Premium** expires in: `{days} days, {hours} hours`.\n"
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
    await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"✨ User `{user_id}` activated a 3-hour Instagram trial.")
    
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
    aspect_ratio_key_parts = query.data.split("_")[2:]
    aspect_ratio_value = "_".join(aspect_ratio_key_parts)

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
async def set_caption_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user_states[user_id] = {"state": "waiting_for_caption"}
    current_settings = await get_user_settings(user_id)
    current_caption = current_settings.get("caption", "Not set")
    await safe_edit_message(
        query.message,
        f"📝 Please send the new caption for your uploads.\n\n"
        f"Current caption: `{current_caption}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_hashtags$"))
async def set_hashtags_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user_states[user_id] = {"state": "waiting_for_hashtags"}
    current_settings = await get_user_settings(user_id)
    current_hashtags = current_settings.get("hashtags", "Not set")
    await safe_edit_message(
        query.message,
        f"🏷️ Please send the new hashtags for your uploads (e.g., #coding #bot).\n\n"
        f"Current hashtags: `{current_hashtags}`",
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

@app.on_callback_query(filters.regex("^set_proxy$"))
async def set_proxy_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    user_states[user_id] = {"state": "waiting_for_proxy"}
    current_settings = await get_user_settings(user_id)
    current_proxy = current_settings.get("proxy", "Not set")

    await safe_edit_message(
        query.message,
        f"🌐 Please send the new proxy for your TikTok uploads.\n"
        f"Example: `socks5://user:password@host:port` or `http://user:password@host:port`\n"
        f"Send `clear` to remove your current proxy.\n\n"
        f"Current proxy: `{current_proxy}`",
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
async def set_max_uploads_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)

    user_states[user_id] = {"state": "waiting_for_max_uploads"}
    current_limit = global_settings.get("max_concurrent_uploads")
    await safe_edit_message(
        query.message,
        f"🔄 Please send the new maximum number of concurrent uploads.\n\n"
        f"Current limit is: `{current_limit}`"
    )

@app.on_callback_query(filters.regex("^reset_stats$"))
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
async def confirm_reset_stats_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)
    
    result = db.uploads.delete_many({})
    await query.answer(f"✅ All upload stats have been reset! Deleted {result.deleted_count} entries.", show_alert=True)
    await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"📊 Admin `{user_id}` has reset all bot upload statistics.")

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
                        f"  - **GPU {i}:** `{gpu.name}`\n"
                        f"    - Load: `{gpu.load*100:.1f}%`\n"
                        f"    - Memory: `{gpu.memoryUsed}/{gpu.memoryTotal}` MB\n"
                        f"    - Temp: `{gpu.temperature}°C`\n"
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
async def manage_premium_cb(_, query):
    _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    user_states[query.from_user.id] = {"state": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(
        query.message,
        "➕ Please send the **User ID** to manage their premium access."
    )

@app.on_callback_query(filters.regex("^select_platform_"))
async def select_platform_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("state") != "select_platforms_for_premium":
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
async def confirm_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("state") != "select_platforms_for_premium":
        await query.answer("Error: Please restart the premium management process.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    target_user_id = state_data["target_user_id"]
    selected_platforms = [p for p, selected in state_data.get("selected_platforms", {}).items() if selected]

    if not selected_platforms:
        return await query.answer("Please select at least one platform!", show_alert=True)

    state_data["state"] = "select_premium_plan_for_platforms"
    state_data["final_selected_platforms"] = selected_platforms
    user_states[user_id] = state_data

    await safe_edit_message(
        query.message,
        f"✅ Platforms selected: `{', '.join(platform.capitalize() for platform in selected_platforms)}`. Now, select a premium plan for user `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_plan_"))
async def select_plan_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("state") != "select_premium_plan_for_platforms":
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

    admin_confirm_text = (
        f"✅ Premium granted to user `{target_user_id}` for:\n"
    )
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
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL,
            f"💰 Premium granted notification sent to `{target_user_id}` by Admin `{user_id}`. Platforms: `{', '.join(selected_platforms)}`, Plan: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {target_user_id} about premium: {e}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL,
            f"⚠️ Failed to notify user `{target_user_id}` about premium. Error: `{str(e)}`"
        )

@app.on_callback_query(filters.regex("^back_to_platform_selection$"))
async def back_to_platform_selection_cb(_, query):
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("state") not in ["select_platforms_for_premium", "select_premium_plan_for_platforms"]:
        await query.answer("Error: Invalid state for back action. Please restart the process.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    target_user_id = state_data["target_user_id"]
    current_selected_platforms = state_data.get("selected_platforms", {})
    
    user_states[user_id] = {"state": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": current_selected_platforms}
    
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
        proxy_status = f"✅ `{current_settings.get('proxy')}`" if current_settings.get('proxy') else "❌ None"

        settings_text = "⚙️ Your Personal Settings\n\n" \
                        f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                        f"🌐 Proxy for TikTok: {proxy_status}\n\n" \
                        "Use the buttons below to adjust your preferences."
        
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
            [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
            [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
            [InlineKeyboardButton("📐 Aspect Ratio (Video)", callback_data="set_aspect_ratio")],
            [InlineKeyboardButton("🗜️ Toggle Compression", callback_data="toggle_compression")],
            [InlineKeyboardButton("🌐 Set Proxy", callback_data="set_proxy")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
        ])

        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=markup,
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
        proxy_status = f"✅ `{current_settings.get('proxy')}`" if current_settings.get('proxy') else "❌ None"

        settings_text = "⚙️ Settings Panel\n\n" \
                        f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                        f"🌐 Proxy for TikTok: {proxy_status}\n\n" \
                        "Use the buttons below to adjust your preferences."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "back_to_admin_from_stats":
        await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    elif data == "back_to_admin_from_global":
        await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^confirm_reset_profile$"))
async def confirm_reset_profile_cb(_, query):
    user_id = query.from_user.id
    try:
        user_data = _get_user_data(user_id)
        if not user_data:
            return await query.answer("❌ No profile found to reset.", show_alert=True)
        
        premium_status = user_data.get("premium", {})
        
        db.users.delete_one({"_id": user_id})
        db.settings.delete_one({"_id": user_id})
        db.sessions.delete_one({"user_id": user_id})
        
        new_user_data = {
            "_id": user_id,
            "premium": premium_status,
            "added_by": "reset_profile",
            "added_at": datetime.utcnow()
        }
        db.users.insert_one(new_user_data)
        
        try:
            shutil.rmtree(f"downloads/{user_id}", ignore_errors=True)
            logger.info(f"Deleted local files for user {user_id}")
        except Exception as e:
            logger.warning(f"Failed to delete local user folder for {user_id}: {e}")

        await query.answer("✅ Your profile has been reset successfully!", show_alert=True)
        await safe_edit_message(query.message, "🏠 Profile Reset. Please use /start again.", reply_markup=None)
        await app.send_message(user_id, "🏠 Main Menu", reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Failed to reset profile for user {user_id}: {e}")
        await query.answer("❌ Failed to reset your profile. Please try again later.", show_alert=True)


async def process_and_upload(user_id, platform, upload_type, file_path, message_to_edit):
    """Handles the core video/photo processing and upload logic."""
    transcoded_file_path = None
    try:
        settings = await get_user_settings(user_id)
        no_compression = settings.get("no_compression", False)
        aspect_ratio_setting = settings.get("aspect_ratio", "original")
        caption_title = user_states[user_id]["data"].get("caption_title")
        
        file_to_upload = file_path

        if file_to_upload and file_to_upload.lower().endswith(".webm"):
            await message_to_edit.edit_text("🔄 Converting WEBM to MP4...")
            mp4_file = file_to_upload.replace(".webm", ".mp4")
            
            ffmpeg_command = ["ffmpeg", "-i", file_to_upload, "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-y", mp4_file]
            
            process = await asyncio.create_subprocess_exec(*ffmpeg_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
            if process.returncode != 0:
                raise Exception(f"Failed to convert WEBM to MP4: {stderr.decode()}")
            
            cleanup_temp_files([file_to_upload])
            file_to_upload = mp4_file
            transcoded_file_path = mp4_file

        if "video" in upload_type and (not no_compression or aspect_ratio_setting != "original"):
            await message_to_edit.edit_text("🔄 Optimizing video (transcoding audio/video)... This may take a moment.")
            transcoded_file_path = f"{file_path}_transcoded.mp4"

            ffmpeg_command = [
                "ffmpeg", "-i", file_to_upload, "-map_chapters", "-1", "-y",
                "-movflags", "faststart",
                "-preset", "slow",
                "-crf", "20",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-pix_fmt", "yuv420p",
                "-vf", "eq=saturation=1.2,format=yuv420p,scale=-1:'min(ih,1920)'"
            ]

            if aspect_ratio_setting == "9_16":
                 ffmpeg_command.extend([
                     "-vf", f"{ffmpeg_command[-1]},scale=1080:'min(ih,1920)',crop=1080:1920:0:(ih-1920)/2"
                 ])
            
            ffmpeg_command.append(transcoded_file_path)

            logger.info(f"Running FFmpeg command: {' '.join(ffmpeg_command)}")
            
            try:
                process = await asyncio.create_subprocess_exec(*ffmpeg_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
                
                if process.returncode != 0:
                    logger.error(f"FFmpeg transcoding failed: {stderr.decode()}")
                    raise Exception(f"Video transcoding failed: {stderr.decode()}")
                
                file_to_upload = transcoded_file_path
                cleanup_temp_files([file_path])
            except asyncio.TimeoutError:
                process.kill()
                raise Exception("Video transcoding timed out.")
        
        await message_to_edit.edit_text("✅ 𝗣𝗿𝗼𝗰𝗲𝘀𝘀𝗶𝗻𝗴 𝗱𝗼𝗻𝗲. 𝗦𝘁𝗮𝗿𝘁𝗶𝗻𝗴 𝘂𝗽𝗹𝗼𝗮𝗱...")

        settings = await get_user_settings(user_id)
        default_caption = settings.get("caption", f"Check out my new {platform.capitalize()} content! 🎥")
        hashtags = settings.get("hashtags", "")

        final_caption = f"**{caption_title}**\n\n{default_caption}" if caption_title else default_caption
        if hashtags:
            final_caption = f"{final_caption}\n\n{hashtags}"

        if len(final_caption) > 4096:
            final_caption = final_caption[:4090] + "..."

        url = "N/A"
        media_id = "N/A"
        media_type_value = ""

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            if INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
            
            session = await load_instagram_session(user_id)
            if not session:
                raise LoginRequired("Instagram session expired.")
            
            await asyncio.to_thread(user_upload_client.set_settings, session)
            await asyncio.to_thread(user_upload_client.get_timeline_feed)
            
            await message_to_edit.edit_text("🚀 𝗨𝗽𝗹𝗼𝗮𝗱𝗶𝗻𝗴 𝗮𝘀 𝗥𝗲𝗲𝗹...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")]]))
            
            if upload_type == "reel":
                result = await asyncio.to_thread(user_upload_client.clip_upload, file_to_upload, caption=final_caption)
            else:
                result = await asyncio.to_thread(user_upload_client.photo_upload, file_to_upload, caption=final_caption)

            url = f"https://instagram.com/reel/{result.code}" if upload_type == "reel" else f"https://instagram.com/p/{result.code}"
            media_id = result.pk
            media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type

        elif platform == "tiktok":
            api = await _get_tiktok_api_instance(user_id)
            session_data = await load_tiktok_session(user_id)
            if not session_data:
                 raise Exception("TikTok session expired.")
            await asyncio.to_thread(api.set_session_cookies, session_data)

            try:
                me = await asyncio.to_thread(api.me)
                if not me:
                    raise Exception("Session invalid, profile data could not be retrieved.")
            except Exception:
                raise Exception("Session invalid, profile data could not be retrieved.")

            await message_to_edit.edit_text("🚀 Uploading video to TikTok...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")]]))
            
            if upload_type == "video":
                result = await asyncio.to_thread(api.upload_video, file_to_upload, final_caption)
            else:
                result = await asyncio.to_thread(api.upload_image, file_to_upload, final_caption)
            
            url = f"https://tiktok.com/@{me.get('unique_id', 'unknown')}/video/{result['id']}" if 'id' in result else 'N/A'
            media_id = result.get('id')
            media_type_value = "video" if upload_type == "video" else "photo"

        db.uploads.insert_one({
            "user_id": user_id,
            "media_id": media_id,
            "media_type": media_type_value,
            "platform": platform,
            "upload_type": upload_type,
            "timestamp": datetime.utcnow(),
            "url": url
        })

        user_data = _get_user_data(user_id)
        log_msg = (
            f"📤 New {platform.capitalize()} {upload_type.capitalize()} Upload\n\n"
            f"👤 User: `{user_id}`\n"
            f"📛 Username: `{user_data.get('username', 'N/A')}`\n"
            f"🔗 URL: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await message_to_edit.edit_text(f"✅ Uploaded successfully!\n\n{url}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, log_msg)

    except (LoginRequired, TikTokAPIError) as e:
        await message_to_edit.edit_text(f"❌ {platform.capitalize()} login required. Your session might have expired. Please use `/{platform}login` again. Reason: {str(e)}")
        logger.error(f"Login issue for user {user_id}: {e}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"⚠️ {platform.capitalize()} upload failed (Login Required)\nUser: `{user_id}`")
    except ClientError as ce:
        await message_to_edit.edit_text(f"❌ {platform.capitalize()} client error during upload: {ce}. Please try again later.")
        logger.error(f"Instagrapi ClientError for user {user_id}: {ce}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"⚠️ {platform.capitalize()} upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} upload failed: {str(e)}"
        if message_to_edit:
            await message_to_edit.edit_text(error_msg)
        else:
            await app.send_message(user_id, error_msg)
        logger.error(f"{platform.capitalize()} upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(await app.get_me(), LOG_CHANNEL, f"❌ {platform.capitalize()} Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
    finally:
        cleanup_temp_files([file_path, transcoded_file_path])
        user_states.pop(user_id, None)
        active_uploads.pop(user_id, None)
        await app.send_message(user_id, "🏠 Main Menu", reply_markup=get_main_keyboard(user_id))


@app.on_message(filters.video & filters.private)
async def handle_video_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    state_info = user_states.get(user_id, {})
    if state_info.get("state") not in ["waiting_for_instagram_reel_video", "waiting_for_tiktok_video"]:
        return await msg.reply("❌ Please use the '📤 Insta Reel' or '🎵 TikTok Video' button first to initiate a video upload.")

    platform = state_info["state"].split("_")[-2]
    upload_type = "reel" if platform == "instagram" else "video"

    if not is_admin(user_id) and not is_premium_for_platform(user_id, platform):
        user_states.pop(user_id, None)
        return await msg.reply(f"❌ Not authorized to upload {platform.capitalize()} videos. Please upgrade to {platform.capitalize()} Premium with /buypypremium.")

    if msg.video and msg.video.file_size is not None and msg.video.file_size > FILE_SIZE_LIMIT:
        return await msg.reply(f"❌ The file size ({humanbytes(msg.video.file_size)}) exceeds the maximum limit of {humanbytes(FILE_SIZE_LIMIT)}. Please send a smaller file.")
    
    if user_id in active_uploads:
        return await msg.reply("❌ You already have an active upload. Please wait for it to finish or use /cancel_upload.")
    
    user_states[user_id] = {
        "state": "waiting_for_caption_or_skip",
        "data": {
            "platform": platform,
            "upload_type": upload_type,
            "file_id": msg.video.file_id,
            "message_to_edit": None,
            "file_path": None
        }
    }
    
    caption_markup = ReplyKeyboardMarkup([[KeyboardButton("/skip")]], resize_keyboard=True, one_time_keyboard=True)
    await msg.reply("🔖 **Send Your Title...**", reply_markup=caption_markup, parse_mode=enums.ParseMode.MARKDOWN)
    
    active_uploads[user_id] = asyncio.create_task(handle_upload_flow(user_id, msg))


@app.on_message(filters.photo & filters.private)
async def handle_photo_upload(_, msg):
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.utcnow()})

    state_info = user_states.get(user_id, {})
    if state_info.get("state") not in ["waiting_for_instagram_photo_image", "waiting_for_tiktok_photo"]:
        return await msg.reply("❌ Please use the '📸 Insta Photo' or '🖼️ TikTok Photo' button first to initiate an image upload.")
    
    platform = state_info["state"].split("_")[-2]
    upload_type = "post" if platform == "instagram" else "photo"

    if not is_admin(user_id) and not is_premium_for_platform(user_id, platform):
        user_states.pop(user_id, None)
        return await msg.reply(f"❌ Not authorized to upload {platform.capitalize()} photos. Please upgrade to {platform.capitalize()} Premium with /buypypremium.")
    
    if msg.photo and msg.photo.file_size is not None and msg.photo.file_size > FILE_SIZE_LIMIT:
        return await msg.reply(f"❌ The file size ({humanbytes(msg.photo.file_size)}) exceeds the maximum limit of {humanbytes(FILE_SIZE_LIMIT)}. Please send a smaller file.")

    if user_id in active_uploads:
        return await msg.reply("❌ You already have an active upload. Please wait for it to finish or use /cancel_upload.")

    user_states[user_id] = {
        "state": "waiting_for_caption_or_skip",
        "data": {
            "platform": platform,
            "upload_type": upload_type,
            "file_id": msg.photo.file_id,
            "message_to_edit": None,
            "file_path": None
        }
    }
    
    caption_markup = ReplyKeyboardMarkup([[KeyboardButton("/skip")]], resize_keyboard=True, one_time_keyboard=True)
    await msg.reply("🔖 **Send Your Title...**", reply_markup=caption_markup, parse_mode=enums.ParseMode.MARKDOWN)
    
    active_uploads[user_id] = asyncio.create_task(handle_upload_flow(user_id, msg))


async def handle_upload_flow(user_id, msg):
    try:
        progress_msg = await msg.reply("⬇️ Downloading file...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")]]))
        user_states[user_id]["data"]["message_to_edit"] = progress_msg
        
        file_to_download = msg.video or msg.photo
        if not file_to_download:
            raise ValueError("No media found in the message.")

        start_time = time.time()
        file_path = await app.download_media(file_to_download, progress=progress_callback, progress_args=(app, progress_msg, start_time, "Downloading..."))
        user_states[user_id]["data"]["file_path"] = file_path
        
        while user_states.get(user_id, {}).get("state") == "waiting_for_caption_or_skip":
            await asyncio.sleep(1)
        
        if user_states.get(user_id, {}).get("state") == "final_upload":
            await process_and_upload(user_id, user_states[user_id]["data"]["platform"], user_states[user_id]["data"]["upload_type"], file_path, progress_msg)
        else:
            await progress_msg.edit_text("❌ Upload cancelled.")
            cleanup_temp_files([file_path])
            user_states.pop(user_id, None)
    except asyncio.CancelledError:
        logger.info(f"Upload flow cancelled for user {user_id}")
        file_path = user_states.get(user_id, {}).get("data", {}).get("file_path")
        if file_path:
            cleanup_temp_files([file_path])
        user_states.pop(user_id, None)
    except Exception as e:
        logger.error(f"Error in upload flow for user {user_id}: {e}")
        file_path = user_states.get(user_id, {}).get("data", {}).get("file_path")
        if file_path:
            cleanup_temp_files([file_path])
        user_states.pop(user_id, None)
        await msg.reply(f"❌ An error occurred during the download process: {str(e)}")

# === HTTP SERVER FOR HEALTH CHECK ===

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

if __name__ == "__main__":
    os.makedirs("sessions", exist_ok=True)
    os.makedirs("downloads", exist_ok=True)
    logger.info("Session and download directories ensured.")

    load_instagram_client_session()

    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Health check server started on port 8080.")

    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}")
        sys.exit(1)

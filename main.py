import os
import sys
import asyncio
import threading
import logging
import subprocess
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import signal
from functools import wraps, partial
import re
import time
# Load environment variables
from dotenv import load_dotenv

load_dotenv()
# MongoDB
from pymongo import MongoClient
from pymongo.errors import OperationFailure
# Pyrogram (Telegram Bot)
from pyrogram import Client, filters, enums, idle
from pyrogram.errors import UserNotParticipant, FloodWait
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
from instagrapi.types import Usertag, Location, StoryMention, StoryLocation, StoryHashtag, StoryLink
# System Utilities
import psutil
import GPUtil
# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger("BotUser")

# === Load and Validate Environment Variables ===
API_ID_STR = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LOG_CHANNEL_STR = os.getenv("LOG_CHANNEL_ID") # Make sure this is a valid channel/supergroup ID, e.g., -1001234567890
MONGO_URI = os.getenv("MONGO_DB")
ADMIN_ID_STR = os.getenv("ADMIN_ID")

# Validate required environment variables
if not all([API_ID_STR, API_HASH, BOT_TOKEN, ADMIN_ID_STR, MONGO_URI]):
    logger.critical("FATAL ERROR: One or more required environment variables are missing. Please check TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN, ADMIN_ID, and MONGO_DB.")
    sys.exit(1)

# Convert to correct types after validation
API_ID = int(API_ID_STR)
ADMIN_ID = int(ADMIN_ID_STR)
LOG_CHANNEL = int(LOG_CHANNEL_STR) if LOG_CHANNEL_STR else None

# Instagram Client Credentials (for the bot's own primary account, if any)
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
PROXY_SETTINGS = os.getenv("PROXY_SETTINGS", "")

# === Global Bot Settings ===
DEFAULT_GLOBAL_SETTINGS = {
    "special_event_toggle": False,
    "special_event_title": "🎉 Special Event!",
    "special_event_message": "Enjoy our special event features!",
    "max_concurrent_uploads": 15,
    "max_file_size_mb": 250,
    "payment_settings": {
        "google_play_qr_file_id": "",
        "upi": "",
        "ust": "",
        "btc": "",
        "others": ""
    },
    "no_compression_admin": True
}

# --- Global State & DB Management ---
mongo = None
db = None
global_settings = {}
upload_semaphore = None
user_upload_locks = {}
MAX_FILE_SIZE_BYTES = 0
MAX_CONCURRENT_UPLOADS = 0
shutdown_event = asyncio.Event()
valid_log_channel = False

# Pyrogram Client
app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
# Instagram Client
insta_client = InstaClient()
insta_client.delay_range = [1, 3]

# --- Task Management ---
class TaskTracker:
    def __init__(self):
        self._tasks = set()
        self._user_specific_tasks = {}
        self.loop = None
        self._progress_futures = {}

    def create_task(self, coro, user_id=None, task_name=None):
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("Could not create task: No running event loop.")
                return
        if user_id and task_name:
            self.cancel_user_task(user_id, task_name)
        task = self.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if user_id and task_name:
            if user_id not in self._user_specific_tasks:
                self._user_specific_tasks[user_id] = {}
            self._user_specific_tasks[user_id][task_name] = task
            logger.info(f"User-specific task '{task_name}' for user {user_id} created.")
        logger.info(f"Task {task.get_name()} created. Total tracked tasks: {len(self._tasks)}")
        return task

    def add_progress_future(self, future, user_id, message_id):
        if user_id not in self._progress_futures:
            self._progress_futures[user_id] = {}
        self._progress_futures[user_id][message_id] = future
        future.add_done_callback(lambda f: self._progress_futures.get(user_id, {}).pop(message_id, None))
        logger.info(f"Progress future added for user {user_id}, msg {message_id}.")

    def cancel_user_task(self, user_id, task_name):
        if user_id in self._user_specific_tasks and task_name in self._user_specific_tasks[user_id]:
            task_to_cancel = self._user_specific_tasks[user_id].pop(task_name)
            if not task_to_cancel.done():
                task_to_cancel.cancel()
                logger.info(f"Cancelled previous task '{task_name}' for user {user_id}.")
            if not self._user_specific_tasks[user_id]:
                del self._user_specific_tasks[user_id]

    async def cancel_all_user_tasks(self, user_id):
        if user_id in self._user_specific_tasks:
            user_tasks = self._user_specific_tasks.pop(user_id)
            for task_name, task in user_tasks.items():
                if not task.done():
                    task.cancel()
                    logger.info(f"Cancelled task '{task_name}' for user {user_id} during cleanup.")
            await asyncio.gather(*[t for t in user_tasks.values() if not t.done()], return_exceptions=True)

    async def cancel_and_wait_all(self):
        tasks_to_cancel = [t for t in self._tasks if not t.done()]
        if not tasks_to_cancel:
            return
        
        logger.info(f"Cancelling {len(tasks_to_cancel)} outstanding background tasks...")
        for t in tasks_to_cancel:
            t.cancel()
        
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        logger.info("All background tasks have been awaited.")

task_tracker = None

async def safe_task_wrapper(coro):
    try:
        await coro
    except asyncio.CancelledError:
        logger.warning(f"Task {asyncio.current_task().get_name()} was cancelled.")
    except Exception:
        logger.exception(f"Unhandled exception in background task: {asyncio.current_task().get_name()}")

async def send_log_to_channel(client, channel_id, text):
    global valid_log_channel
    if not valid_log_channel:
        return
    try:
        await client.send_message(channel_id, text, disable_web_page_preview=True, parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to log to channel {channel_id} (General Error): {e}")
        valid_log_channel = False

user_states = {}

PREMIUM_PLANS = {
    "6_hour_trial": {"duration": timedelta(hours=6), "price": "Free / Free"},
    "3_days": {"duration": timedelta(days=3), "price": "₹10 / $0.40"},
    "7_days": {"duration": timedelta(days=7), "price": "₹25 / $0.70"},
    "15_days": {"duration": timedelta(days=15), "price": "₹35 / $0.90"},
    "1_month": {"duration": timedelta(days=30), "price": "₹60 / $2.50"},
    "3_months": {"duration": timedelta(days=90), "price": "₹150 / $4.50"},
    "1_year": {"duration": timedelta(days=365), "price": "Negotiable / Negotiable"},
    "lifetime": {"duration": None, "price": "Negotiable / Negotiable"}
}
PREMIUM_PLATFORMS = ["instagram"]


# ===================================================================
# ==================== MARKUP GENERATORS ============================
# ===================================================================

def get_main_keyboard(user_id, premium_platforms):
    buttons = [
        [KeyboardButton("⚙️ ꜱᴇᴛᴛɪɴɢꜱ"), KeyboardButton("📊 ꜱᴛᴀᴛꜱ")]
    ]
    upload_buttons_row = []
    if "instagram" in premium_platforms:
        upload_buttons_row.extend([
            KeyboardButton("⚡ ɪɴꜱᴛᴀ ꜱᴛᴏʀy"),
            KeyboardButton("📸 ɪɴꜱᴛᴀ ᴩʜᴏᴛᴏ"),
            KeyboardButton("📤 ɪɴꜱᴛᴀ ʀᴇᴇʟ"),
            KeyboardButton("🗂️ ɪɴꜱᴛᴀ ᴀʟʙᴜᴍ")
        ])
    
    if upload_buttons_row:
        buttons.insert(0, upload_buttons_row)
    
    buttons.append([KeyboardButton("⭐ ᴩʀᴇᴍɪᴜᴍ"), KeyboardButton("/premiumdetails")])
    if is_admin(user_id):
        buttons.append([KeyboardButton("🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ"), KeyboardButton("🔄 ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

# NEW: Main settings hub markup
def get_main_settings_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Instagram Settings", callback_data="hub_settings_instagram")],
        [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_main_menu")]
    ])

# NEW: Instagram-specific settings markup
def get_insta_settings_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 ᴄᴀᴩᴛɪᴏɴ", callback_data="set_caption_instagram")],
        [InlineKeyboardButton("🏷️ ʜᴀꜱʜᴛᴀɢꜱ", callback_data="set_hashtags_instagram")],
        [InlineKeyboardButton("📐 ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ (ᴠɪᴅᴇᴏ)", callback_data="set_aspect_ratio_instagram")],
        [InlineKeyboardButton("👤 ᴍᴀɴᴀɢᴇ ɪɢ ᴀᴄᴄᴏᴜɴᴛꜱ", callback_data="manage_ig_accounts")],
        [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ꜱᴇᴛᴛɪɴɢꜱ ʜᴜʙ", callback_data="back_to_settings_hub")]
    ])

# ### FIX: Converted to async function to correctly await get_user_settings ###
async def get_insta_account_markup(user_id, logged_in_accounts):
    buttons = []
    # Fetch active account directly from settings for consistency
    # ### FIX: Awaiting the async function instead of using asyncio.run ###
    user_settings = await get_user_settings(user_id)
    active_account = user_settings.get("active_ig_username")

    for account in logged_in_accounts:
        emoji = "✅" if active_account == account else "⬜"
        buttons.append([InlineKeyboardButton(f"{emoji} @{account}", callback_data=f"select_ig_account_{account}")])
    buttons.append([InlineKeyboardButton("❌ ʟᴏɢᴏᴜᴛ ᴀᴄᴛɪᴠᴇ ᴀᴄᴄᴏᴜɴᴛ", callback_data="logout_ig_account")])
    buttons.append([InlineKeyboardButton("➕ ᴀᴅᴅ ɴᴇᴡ ᴀᴄᴄᴏᴜɴᴛ", callback_data="add_account_instagram")])
    buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ɪɢ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="hub_settings_instagram")])
    return InlineKeyboardMarkup(buttons)

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 ᴜꜱᴇʀꜱ ʟɪꜱᴛ", callback_data="users_list")],
    [InlineKeyboardButton("➕ ᴍᴀɴᴀɢᴇ ᴩʀᴇᴍɪᴜᴍ", callback_data="manage_premium")],
    [InlineKeyboardButton("📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ", callback_data="broadcast_message")],
    [InlineKeyboardButton("⚙️ ɢʟᴏʙᴀʟ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="global_settings_panel")],
    [InlineKeyboardButton("📊 ꜱᴛᴀᴛꜱ ᴩᴀɴᴇʟ", callback_data="admin_stats_panel")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴍᴇɴᴜ", callback_data="back_to_main_menu")]
])

def get_admin_global_settings_markup():
    event_status = "ON" if global_settings.get("special_event_toggle") else "OFF"
    compression_status = "ᴅɪꜱᴀʙʟᴇᴅ" if global_settings.get("no_compression_admin") else "ᴇɴᴀʙʟᴇᴅ"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 Special Event ({event_status})", callback_data="toggle_special_event")],
        [InlineKeyboardButton("✏️ Set Event Title", callback_data="set_event_title")],
        [InlineKeyboardButton("💬 Set Event Message", callback_data="set_event_message")],
        [InlineKeyboardButton("ᴍᴀx ᴜᴩʟᴏᴀᴅ ᴜꜱᴇʀꜱ", callback_data="set_max_uploads")],
        [InlineKeyboardButton("ʀᴇꜱᴇᴛ ꜱᴛᴀᴛꜱ", callback_data="reset_stats")],
        [InlineKeyboardButton("ꜱʜᴏᴡ ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ", callback_data="show_system_stats")],
        [InlineKeyboardButton("🌐 ᴩʀᴏxʏ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="set_proxy_url")],
        [InlineKeyboardButton(f"🗜️ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ({compression_status})", callback_data="toggle_compression_admin")],
        [InlineKeyboardButton("💰 ᴩᴀyᴍᴇɴᴛ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="payment_settings_panel")],
        [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴀᴅᴍɪɴ", callback_data="admin_panel")]
    ])

payment_settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("ɢᴏᴏɢʟᴇ ᴩʟᴀy ǫʀ ᴄᴏᴅᴇ", callback_data="set_payment_google_play_qr")],
    [InlineKeyboardButton("ᴜᴩɪ", callback_data="set_payment_upi")],
    [InlineKeyboardButton("ᴜꜱᴛ", callback_data="set_payment_ust")],
    [InlineKeyboardButton("ʙᴛᴄ", callback_data="set_payment_btc")],
    [InlineKeyboardButton("ᴏᴛʜᴇʀꜱ", callback_data="set_payment_others")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ɢʟᴏʙᴀʟ", callback_data="global_settings_panel")]
])

upload_type_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 ʀᴇᴇʟ", callback_data="set_type_reel")],
    [InlineKeyboardButton("📷 ᴩᴏꜱᴛ", callback_data="set_type_post")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="hub_settings_instagram")]
])

aspect_ratio_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("ᴏʀɪɢɪɴᴀʟ ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ", callback_data="set_ar_original")],
    [InlineKeyboardButton("9:16 (ᴄʀᴏᴩ/ғɪᴛ)", callback_data="set_ar_9_16")],
    [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="hub_settings_instagram")]
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

def get_premium_details_markup(plan_key, is_admin_flow=False):
    plan_details = PREMIUM_PLANS[plan_key]
    buttons = []
    if is_admin_flow:
        buttons.append([InlineKeyboardButton(f"✅ Grant this Plan", callback_data=f"grant_plan_{plan_key}")])
    else:
        price_string = plan_details['price']
        buttons.append([InlineKeyboardButton(f"💰 ʙᴜy ɴᴏᴡ ({price_string})", callback_data="buy_now")])
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

def get_progress_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_upload")]
    ])

def get_caption_markup(is_album=False, is_premium=True):
    buttons = []
    
    if is_premium:
        buttons.extend([
            [InlineKeyboardButton("👥 ᴛᴀɢ ᴜꜱᴇʀꜱ", callback_data="tag_users_insta")],
            [InlineKeyboardButton("📍 ᴀᴅᴅ ʟᴏᴄᴀᴛɪᴏɴ", callback_data="add_location_insta")]
        ])
    
    if is_album:
        buttons.insert(0, [InlineKeyboardButton("✅ ᴅᴏɴᴇ", callback_data="upload_album_done")])
    else:
        buttons.append([InlineKeyboardButton("🚀 ᴜᴩʟᴏᴀᴅ ɴᴏᴡ", callback_data="upload_now")])

    buttons.append([InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_upload")])
    return InlineKeyboardMarkup(buttons)


# ===================================================================
# ====================== HELPER FUNCTIONS ===========================
# ===================================================================

def is_admin(user_id):
    return user_id == ADMIN_ID

async def _get_user_data(user_id):
    if db is None:
        return {"_id": user_id, "premium": {}}
    return await asyncio.to_thread(db.users.find_one, {"_id": user_id})

async def _save_user_data(user_id, data_to_update):
    if db is None:
        logger.warning(f"DB not connected. Skipping save for user {user_id}.")
        return
    serializable_data = {}
    for key, value in data_to_update.items():
        if isinstance(value, dict):
            serializable_data[key] = {k: v for k, v in value.items() if not k.startswith('$')}
        else:
            serializable_data[key] = value
    await asyncio.to_thread(
        db.users.update_one,
        {"_id": user_id},
        {"$set": serializable_data},
        upsert=True
    )

async def _update_global_setting(key, value):
    global_settings[key] = value
    if db is None:
        logger.warning(f"DB not connected. Skipping save for global setting '{key}'.")
        return
    await asyncio.to_thread(db.settings.update_one, {"_id": "global_settings"}, {"$set": {key: value}}, upsert=True)

async def is_premium_for_platform(user_id, platform):
    if user_id == ADMIN_ID:
        return True
    
    if db is None:
        return False

    user = await _get_user_data(user_id)
    if not user:
        return False

    platform_premium = user.get("premium", {}).get(platform, {})
    premium_type = platform_premium.get("type")
    premium_until = platform_premium.get("until")

    if premium_type == "lifetime":
        return True

    if premium_until and isinstance(premium_until, datetime) and premium_until > datetime.utcnow():
        return True

    # Expire premium if date has passed
    if premium_type and premium_until and premium_until <= datetime.utcnow():
        await asyncio.to_thread(
            db.users.update_one,
            {"_id": user_id},
            {"$unset": {f"premium.{platform}": ""}}
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

async def save_platform_session(user_id, platform, session_data, username):
    if db is None: return
    await asyncio.to_thread(
        db.sessions.update_one,
        {"user_id": user_id, "platform": platform, "username": username},
        {"$set": {"session_data": session_data}},
        upsert=True
    )

async def load_platform_sessions(user_id, platform):
    if db is None: return []
    sessions = await asyncio.to_thread(list, db.sessions.find({"user_id": user_id, "platform": platform}))
    return sessions

async def load_platform_session_data(user_id, platform, username):
    if db is None: return None
    session = await asyncio.to_thread(db.sessions.find_one, {"user_id": user_id, "platform": platform, "username": username})
    return session.get("session_data") if session else None

async def delete_platform_session(user_id, platform, username):
    if db is None: return
    await asyncio.to_thread(db.sessions.delete_one, {"user_id": user_id, "platform": platform, "username": username})

async def save_user_settings(user_id, settings):
    if db is None:
        logger.warning(f"DB not connected. Skipping user settings save for user {user_id}.")
        return
    await asyncio.to_thread(
        db.settings.update_one,
        {"_id": user_id},
        {"$set": settings},
        upsert=True
    )

async def get_user_settings(user_id):
    settings = {}
    if db is not None:
        settings = await asyncio.to_thread(db.settings.find_one, {"_id": user_id}) or {}
    
    # Set default values if not present
    settings.setdefault("aspect_ratio_instagram", "original")
    settings.setdefault("caption_instagram", "")
    settings.setdefault("hashtags_instagram", "")
    settings.setdefault("active_ig_username", None)
    
    return settings

async def safe_edit_message(message, text, reply_markup=None, parse_mode=enums.ParseMode.MARKDOWN):
    try:
        if not message:
            logger.warning("safe_edit_message called with a None message object.")
            return
        current_text = getattr(message, 'text', '') or getattr(message, 'caption', '')
        # Avoid MESSAGE_NOT_MODIFIED error if text and markup are the same
        if current_text and hasattr(current_text, 'strip') and current_text.strip() == text.strip() and message.reply_markup == reply_markup:
            return
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.warning(f"Couldn't edit message: {e}")

async def restart_bot(msg):
    restart_msg_log = (
        "🔄 **Bot Restart Initiated (Graceful)**\n\n"
        f"👤 **By**: {msg.from_user.mention} (ID: `{msg.from_user.id}`)"
    )
    logger.info(f"User {msg.from_user.id} initiated graceful restart.")
    await send_log_to_channel(app, LOG_CHANNEL, restart_msg_log)
    await msg.reply(
        "✅ **Graceful restart initiated...**\n\n"
        "The bot will shut down cleanly. If running under a process manager "
        "(like Docker, Koyeb, or systemd), it will restart automatically."
    )
    shutdown_event.set()

_progress_updates = {}

def progress_callback_threaded(current, total, ud_type, msg_id, chat_id, start_time, last_update_time):
    now = time.time()
    if now - last_update_time[0] < 2 and current != total:
        return
    last_update_time[0] = now
    
    with threading.Lock():
        _progress_updates[(chat_id, msg_id)] = {
            "current": current, "total": total, "ud_type": ud_type, "start_time": start_time, "now": now
        }

async def monitor_progress_task(chat_id, msg_id, progress_msg):
    try:
        while True:
            await asyncio.sleep(2)
            with threading.Lock():
                update_data = _progress_updates.get((chat_id, msg_id))
            if update_data:
                current, total, ud_type, start_time, now = (
                    update_data['current'], update_data['total'], update_data['ud_type'],
                    update_data['start_time'], update_data['now']
                )
                percentage = current * 100 / total
                speed = current / (now - start_time) if (now - start_time) > 0 else 0
                eta_seconds = (total - current) / speed if speed > 0 else 0
                eta = timedelta(seconds=int(eta_seconds))
                progress_bar = f"[{'█' * int(percentage / 5)}{' ' * (20 - int(percentage / 5))}]"
                progress_text = (
                    f"{ud_type} ᴩʀᴏɢʀᴇꜱꜱ: `{progress_bar}`\n"
                    f"📊 ᴩᴇʀᴄᴇɴᴛᴀɢᴇ: `{percentage:.2f}%`\n"
                    f"✅ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ: `{current / (1024 * 1024):.2f}` ᴍʙ / `{total / (1024 * 1024):.2f}` ᴍʙ\n"
                    f"🚀 ꜱᴩᴇᴇᴅ: `{speed / (1024 * 1024):.2f}` ᴍʙ/ꜱ\n"
                    f"⏳ ᴇᴛᴀ: `{eta}`"
                )
                try:
                    await safe_edit_message(
                        progress_msg, progress_text,
                        reply_markup=get_progress_markup(),
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    pass
            
            if update_data and update_data['current'] == update_data['total']:
                with threading.Lock():
                    _progress_updates.pop((chat_id, msg_id), None)
                break
    except asyncio.CancelledError:
        logger.info(f"Progress monitor task for msg {msg_id} was cancelled.")

def cleanup_temp_files(files_to_delete):
    for file_path in files_to_delete:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {e}")

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

# ===================================================================
# ======================== COMMAND HANDLERS =========================
# ===================================================================

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"
    
    is_ig_premium = await is_premium_for_platform(user_id, "instagram")
    premium_platforms = []
    if is_ig_premium: premium_platforms.append("instagram")

    if is_admin(user_id):
        welcome_msg = "🤖 **Welcome to the Direct Upload Bot!**\n\n"
        welcome_msg += "🛠️ You have **ADMIN privileges**."
        # Admin gets all platform buttons regardless of premium status
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id, ["instagram"]), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = await _get_user_data(user_id)
    is_new_user = not user or "added_by" not in user
    if is_new_user:
        await _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")
        welcome_msg = (
            f"👋 **Hi {user_first_name}!**\n\n"
            "This bot lets you upload content to Instagram directly from Telegram.\n\n"
            "To get a taste of the premium features, you can activate a **FREE 6-hour trial** for Instagram right now!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Activate FREE 6-hour trial", callback_data="activate_trial_instagram")],
            [InlineKeyboardButton("➡️ View premium plans", callback_data="buypypremium")]
        ])
        await msg.reply(welcome_msg, reply_markup=trial_markup, parse_mode=enums.ParseMode.MARKDOWN)
        return
    else:
        await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    event_toggle = global_settings.get("special_event_toggle", False)
    if event_toggle:
        event_title = global_settings.get("special_event_title", "🎉 Special Event!")
        event_message = global_settings.get("special_event_message", "Enjoy our special event features!")
        event_text = f"**{event_title}**\n\n{event_message}"
        await msg.reply(event_text, reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user_premium = user.get("premium", {})
    ig_premium_data = user_premium.get("instagram", {})
    welcome_msg = f"🚀 Welcome back to Telegram ➜ Direct Uploader\n\n"
    premium_details_text = ""
    if is_ig_premium:
        ig_expiry = ig_premium_data.get("until")
        if ig_expiry:
            remaining_time = ig_expiry - datetime.utcnow()
            days, hours = remaining_time.days, remaining_time.seconds // 3600
            premium_details_text += f"⭐ Instagram premium expires in: `{days} days, {hours} hours`.\n"
    else:
        premium_details_text = (
            "🔥 **Key Features:**\n"
            "✅ Direct Login (No tokens needed)\n"
            "✅ Ultra-fast uploading & High Quality\n"
            "✅ No file size limit & unlimited uploads\n"
            "✅ Instagram Support\n\n"
            "👤 Contact Admin → [Click Here](t.me/CjjTom) to get premium\n"
            "🔐 Your data is fully encrypted\n\n"
            f"🆔 Your ID: `{user_id}`"
        )
    welcome_msg += premium_details_text
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart") & filters.user(ADMIN_ID))
async def restart_cmd(_, msg):
    await restart_bot(msg)

@app.on_message(filters.command(["instagramlogin", "iglogin"]))
@with_user_lock
async def instagram_login_cmd(_, msg):
    user_id = msg.from_user.id
    if not await is_premium_for_platform(user_id, "instagram") and not is_admin(user_id):
        return await msg.reply("❌ Instagram premium access is required. Use /buypypremium to upgrade.")

    user_states[user_id] = {"action": "waiting_for_instagram_username", "platform": "instagram"}
    await msg.reply("👤 Please send your Instagram **username**.")

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    user_id = msg.from_user.id
    if not await is_premium_for_platform(user_id, "instagram") and not is_admin(user_id):
        return await msg.reply("❌ This is a premium feature. Please upgrade with /buypypremium.")
    
    # This command is now a hub for the new specific commands
    await msg.reply(
        "Please use the specific login command:\n"
        "- `/instagramlogin` or `/iglogin` to log into Instagram."
    )

@app.on_message(filters.command("buypypremium"))
@app.on_message(filters.regex("⭐ ᴩʀᴇᴍɪᴜᴍ"))
async def show_premium_options(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    premium_plans_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions.\n\n"
        "**Available Plans:**"
    )
    await msg.reply(premium_plans_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user = await _get_user_data(user_id)
    if not user:
        return await msg.reply("You are not registered with the bot. Please use /start.")
    if is_admin(user_id):
        return await msg.reply("👑 You are the **ADMIN**. You have permanent full access to all features!", parse_mode=enums.ParseMode.MARKDOWN)

    status_text = "⭐ **Your Premium Status:**\n\n"
    has_premium_any = False
    for platform in PREMIUM_PLATFORMS:
        if await is_premium_for_platform(user_id, platform):
            has_premium_any = True
            platform_premium = user.get("premium", {}).get(platform, {})
            premium_type = platform_premium.get("type")
            premium_until = platform_premium.get("until")
            status_text += f"**{platform.capitalize()} Premium:** "
            if premium_type == "lifetime":
                status_text += "🎉 **Lifetime!**\n"
            elif premium_until:
                remaining_time = premium_until - datetime.utcnow()
                days, hours, minutes = remaining_time.days, remaining_time.seconds // 3600, (remaining_time.seconds % 3600) // 60
                status_text += (
                    f"`{premium_type.replace('_', ' ').title()}` expires on: "
                    f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n"
                    f"Time Remaining: `{days} days, {hours} hours, {minutes} minutes`\n"
                )
            status_text += "\n"
    
    if not has_premium_any:
        status_text = (
            "😔 **You currently have no active premium.**\n\n"
            "To unlock all features, please contact **[Admin Tom](https://t.me/CjjTom)** to buy a premium plan."
        )
    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("reset_profile"))
@with_user_lock
async def reset_profile_cmd(_, msg):
    user_id = msg.from_user.id
    await msg.reply("⚠️ **Warning!** This will clear all your saved sessions and settings. Are you sure you want to proceed?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, reset my profile", callback_data="confirm_reset_profile")],
            [InlineKeyboardButton("❌ No, cancel", callback_data="back_to_main_menu")]
        ]),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_cmd(_, msg):
    if db is None:
        return await msg.reply("⚠️ Database is unavailable. Cannot fetch user list for broadcast.")
    if len(msg.text.split(maxsplit=1)) < 2:
        return await msg.reply("Usage: `/broadcast <your message>`", parse_mode=enums.ParseMode.MARKDOWN)
    
    broadcast_message = msg.text.split(maxsplit=1)[1]
    users_cursor = await asyncio.to_thread(db.users.find, {})
    users = await asyncio.to_thread(list, users_cursor)
    sent_count, failed_count = 0, 0
    status_msg = await msg.reply("📢 Starting broadcast...")
    
    for user in users:
        try:
            if user["_id"] == ADMIN_ID: continue
            await app.send_message(user["_id"], broadcast_message, parse_mode=enums.ParseMode.MARKDOWN)
            sent_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to send broadcast to user {user['_id']}: {e}")
            
    await status_msg.edit_text(f"✅ Broadcast finished!\nSent to `{sent_count}` users, failed for `{failed_count}` users.")
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 Broadcast initiated by admin `{msg.from_user.id}`\n"
        f"Sent: `{sent_count}`, Failed: `{failed_count}`"
    )

@app.on_message(filters.command("done") & filters.private)
@with_user_lock
async def handle_done_command(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    if not state_data or state_data.get('action') not in ['waiting_for_album_media']:
        return await msg.reply("❌ There is no active multi-media upload process. Please use the appropriate button to start.")

    media_paths = state_data.get('media_paths', [])
    if not media_paths:
        return await msg.reply("❌ You must send at least one media file.")

    is_premium = await is_premium_for_platform(user_id, state_data['platform'])
    file_info = {
        "platform": state_data['platform'],
        "upload_type": state_data.get('upload_type', 'post'),
        "media_paths": media_paths,
        "processing_msg": msg
    }
    user_states[user_id] = {"action": "waiting_for_caption", "file_info": file_info}
    
    if state_data['platform'] == 'instagram':
        await msg.reply(
            "✅ Album files received. What caption do you want for your album?",
            reply_markup=get_caption_markup(is_album=True, is_premium=is_premium)
        )

# ===================================================================
# ======================== REGEX HANDLERS ===========================
# ===================================================================

@app.on_message(filters.regex("🔄 ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ") & filters.user(ADMIN_ID))
async def restart_button_handler(_, msg):
    await restart_bot(msg)

@app.on_message(filters.regex("⚙️ ꜱᴇᴛᴛɪɴɢꜱ"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    is_any_premium = any([await is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS])
    if not is_admin(user_id) and not is_any_premium:
        return await msg.reply("❌ Premium access is required to access settings. Use /buypypremium to upgrade.")
    
    await msg.reply(
        "⚙️ Welcome to the settings panel. Choose a platform to configure:",
        reply_markup=get_main_settings_markup()
    )

@app.on_message(filters.regex("🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ") & filters.user(ADMIN_ID))
async def admin_panel_button_handler(_, msg):
    await msg.reply(
        "🛠 Welcome to the Admin Panel!\n\n"
        "Use the buttons below to manage the bot.",
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.regex("📤 ɪɴꜱᴛᴀ ʀᴇᴇʟ"))
@with_user_lock
async def initiate_instagram_reel_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Your access has been denied. Upgrade to Instagram premium to unlock reels upload. /buypypremium.")
    
    sessions = await load_platform_sessions(user_id, "instagram")
    if not sessions:
        return await msg.reply("❌ Please login to Instagram first using `/instagramlogin` or `/iglogin`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("✅ Send video file - reel ready!!")
    user_states[user_id] = {"action": "waiting_for_instagram_reel_video", "platform": "instagram", "upload_type": "reel"}

@app.on_message(filters.regex("📸 ɪɴꜱᴛᴀ ᴩʜᴏᴛᴏ"))
@with_user_lock
async def initiate_instagram_photo_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("🚫 Not authorized to upload Instagram photos please upgrade premium /buypypremium.")
    
    sessions = await load_platform_sessions(user_id, "instagram")
    if not sessions:
        return await msg.reply("❌ Please login to Instagram first using `/instagramlogin` or `/iglogin`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("✅ Send photo file - ready for IG!.")
    user_states[user_id] = {"action": "waiting_for_instagram_photo_image", "platform": "instagram", "upload_type": "post"}

@app.on_message(filters.regex("🗂️ ɪɴꜱᴛᴀ ᴀʟʙᴜᴍ"))
@with_user_lock
async def initiate_instagram_album_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Album uploads are a Premium feature. Please upgrade with /buypypremium.")
    
    sessions = await load_platform_sessions(user_id, "instagram")
    if not sessions:
        return await msg.reply("❌ Please login to Instagram first using `/instagramlogin` or `/iglogin`", parse_mode=enums.ParseMode.MARKDOWN)
    
    user_states[user_id] = {
        "action": "waiting_for_album_media", "platform": "instagram",
        "upload_type": "album", "media_paths": []
    }
    await msg.reply(
        "🗂️ **Album Mode**\n\n"
        "Please send your photos and videos (up to 10). "
        "Once you are done, send the `/done` command to continue."
    )

@app.on_message(filters.regex("⚡ ɪɴꜱᴛᴀ ꜱᴛᴏʀy"))
@with_user_lock
async def initiate_instagram_story_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Story uploads are a Premium feature. Please upgrade with /buypypremium.")
    
    sessions = await load_platform_sessions(user_id, "instagram")
    if not sessions:
        return await msg.reply("❌ Please login to Instagram first using `/instagramlogin` or `/iglogin`", parse_mode=enums.ParseMode.MARKDOWN)
    
    await msg.reply("⚡ Send a photo or video file for your Instagram story.")
    user_states[user_id] = {"action": "waiting_for_instagram_story", "platform": "instagram", "upload_type": "story"}

@app.on_message(filters.regex("📊 ꜱᴛᴀᴛꜱ"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if db is None: return await msg.reply("⚠️ Database is currently unavailable.")
    
    is_any_premium = any([await is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS])
    if not is_admin(user_id) and not is_any_premium:
        return await msg.reply("❌ Not authorized. Premium access required.")

    total_users = await asyncio.to_thread(db.users.count_documents, {})
    
    # ### FIX: Replaced buggy $anyElementTrue with robust $or operator ###
    # This pipeline correctly checks if a user has any active premium plan.
    pipeline = [
        {"$project": {
            "is_premium": {"$or": [
                {"$or": [
                    {"$eq": [f"$premium.{p}.type", "lifetime"]},
                    {"$gt": [f"$premium.{p}.until", datetime.utcnow()]}
                ]} for p in PREMIUM_PLATFORMS
            ]},
            "platforms": {p: {"$or": [
                {"$eq": [f"$premium.{p}.type", "lifetime"]},
                {"$gt": [f"$premium.{p}.until", datetime.utcnow()]}
            ]} for p in PREMIUM_PLATFORMS}
        }},
        {"$group": {
            "_id": None,
            "total_premium": {"$sum": {"$cond": ["$is_premium", 1, 0]}},
            **{f"{p}_premium": {"$sum": {"$cond": [f"$platforms.{p}", 1, 0]}} for p in PREMIUM_PLATFORMS}
        }}
    ]
    
    try:
        result = await asyncio.to_thread(list, db.users.aggregate(pipeline))
    except OperationFailure as e:
        logger.error(f"Stats aggregation failed: {e}")
        return await msg.reply("⚠️ Could not fetch bot statistics due to a database error.")

    total_premium_users = 0
    premium_counts = {p: 0 for p in PREMIUM_PLATFORMS}
    if result:
        total_premium_users = result[0].get('total_premium', 0)
        for p in PREMIUM_PLATFORMS:
            premium_counts[p] = result[0].get(f'{p}_premium', 0)
            
    total_uploads = await asyncio.to_thread(db.uploads.count_documents, {})
    
    stats_text = (
        f"📊 **Bot Statistics:**\n\n"
        f"**Users**\n"
        f"👥 Total Users: `{total_users}`\n"
        f"👑 Admin Users: `{await asyncio.to_thread(db.users.count_documents, {'_id': ADMIN_ID})}`\n"
        f"⭐ Premium Users: `{total_premium_users}` ({total_premium_users / total_users * 100 if total_users > 0 else 0:.2f}%)\n"
    )
    for p in PREMIUM_PLATFORMS:
        stats_text += f"       - {p.capitalize()} Premium: `{premium_counts[p]}` ({premium_counts[p] / total_users * 100 if total_users > 0 else 0:.2f}%)\n"
        
    stats_text += (
        f"\n**Uploads**\n"
        f"📈 Total Uploads: `{total_uploads}`\n"
        f"🎬 Instagram Reels: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'reel'})}`\n"
        f"📸 Instagram Posts: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'post'})}`\n"
        f"⚡ Instagram Story: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'story'})}`\n"
        f"🗂️ Instagram Albums: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'album'})}`\n"
    )
                      
    await msg.reply(stats_text, parse_mode=enums.ParseMode.MARKDOWN)

# ===================================================================
# ======================== TEXT HANDLERS ============================
# ===================================================================

@app.on_message(filters.text & filters.private & ~filters.command(""))
@with_user_lock
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not state_data:
        return await msg.reply("I don't understand that command. Please use the menu buttons to interact with me.")

    action = state_data.get("action")

    # --- Login Flow ---
    if action == "waiting_for_instagram_username":
        user_states[user_id]["username"] = msg.text
        user_states[user_id]["action"] = "waiting_for_instagram_password"
        return await msg.reply("🔑 Please send your Instagram **password**.")
    
    elif action == "waiting_for_instagram_password":
        username = user_states[user_id]["username"]
        password = msg.text
        login_msg = await msg.reply("🔐 Attempting Instagram login...")
        
        async def login_task():
            try:
                user_insta_client = InstaClient()
                user_insta_client.delay_range = [1, 3]
                proxy_url = global_settings.get("proxy_url")
                if proxy_url: user_insta_client.set_proxy(proxy_url)
                elif INSTAGRAM_PROXY: user_insta_client.set_proxy(INSTAGRAM_PROXY)
                
                await asyncio.to_thread(user_insta_client.login, username, password)
                session_data = user_insta_client.get_settings()
                await save_platform_session(user_id, "instagram", session_data, username)
                
                user_settings = await get_user_settings(user_id)
                user_settings["active_ig_username"] = username
                await save_user_settings(user_id, user_settings)
                
                await safe_edit_message(login_msg, f"✅ Instagram login successful for @{username}!")
                log_text = (
                    f"📝 New Instagram Login\nUser: `{user_id}`\n"
                    f"Username: `{msg.from_user.username or 'N/A'}`\n"
                    f"Instagram: `{username}`"
                )
                await send_log_to_channel(app, LOG_CHANNEL, log_text)
                logger.info(f"Instagram login successful for user {user_id} ({username}).")
            except ChallengeRequired:
                await safe_edit_message(login_msg, "🔐 Challenge required. Please complete it in the Instagram app and try again.")
                logger.warning(f"Instagram Challenge Required for user {user_id} ({username}).")
            except (BadPassword, LoginRequired) as e:
                await safe_edit_message(login_msg, f"❌ Login failed: {e}. Please check your credentials.")
                logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
            except PleaseWaitFewMinutes:
                await safe_edit_message(login_msg, "⚠️ Instagram is asking to wait a few minutes. Please try again later.")
                logger.warning(f"Instagram 'Please Wait' for user {user_id} ({username}).")
            except Exception as e:
                await safe_edit_message(login_msg, f"❌ An unexpected error occurred: {str(e)}")
                logger.error(f"Unhandled error during Instagram login for {user_id} ({username}): {str(e)}", exc_info=True)
            finally:
                if user_id in user_states: del user_states[user_id]
        
        task_tracker.create_task(safe_task_wrapper(login_task()), user_id=user_id, task_name="login_instagram")
        return

    # --- Settings Flow ---
    elif action == "waiting_for_caption_instagram":
        platform = "instagram"
        settings = await get_user_settings(user_id)
        settings[f"caption_{platform}"] = msg.text
        await save_user_settings(user_id, settings)
        
        markup = get_insta_settings_markup()
        await safe_edit_message(msg.reply_to_message, f"✅ Default caption for {platform.capitalize()} has been set.", reply_markup=markup)
        if user_id in user_states: del user_states[user_id]

    elif action == "waiting_for_hashtags_instagram":
        settings = await get_user_settings(user_id)
        settings["hashtags_instagram"] = msg.text
        await save_user_settings(user_id, settings)
        await safe_edit_message(msg.reply_to_message, "✅ Default hashtags for Instagram have been set.", reply_markup=get_insta_settings_markup())
        if user_id in user_states: del user_states[user_id]

    # --- Upload Flow ---
    elif action == "waiting_for_caption":
        is_premium = await is_premium_for_platform(user_id, state_data["platform"])
        caption = msg.text
        if not is_premium and len(caption) > 280:
                 return await msg.reply("❌ For free accounts, the caption limit is 280 characters.")
        
        file_info = state_data.get("file_info", {})
        file_info["custom_caption"] = caption
        state_data["file_info"] = file_info
        
        await safe_edit_message(msg.reply_to_message, f"📝 **Caption Set**\n\n`{caption}`\n\nWhat's next?", 
            reply_markup=get_caption_markup(is_album=state_data.get('upload_type') == 'album', is_premium=is_premium), 
            parse_mode=enums.ParseMode.MARKDOWN)
        state_data['action'] = "caption_set_waiting_for_options"
        user_states[user_id] = state_data

    elif action == "waiting_for_usertags_insta":
        file_info = state_data.get("file_info", {})
        usernames = [u.strip().replace("@", "") for u in msg.text.split(",") if u.strip()]
        file_info["usertags"] = usernames
        state_data["file_info"] = file_info
        
        await safe_edit_message(msg.reply_to_message, f"👥 **Users to tag:** `{', '.join(usernames)}`\n\nContinue with other options or upload now.",
            reply_markup=get_caption_markup(is_album=state_data.get('upload_type') == 'album'))
        state_data['action'] = "caption_set_waiting_for_options"
        user_states[user_id] = state_data

    elif action == "waiting_for_location_search_insta":
        location_search_term = msg.text
        await safe_edit_message(msg.reply_to_message, f"Searching for location: `{location_search_term}`...")
        
        async def search_location_task():
            user_upload_client = InstaClient()
            user_settings = await get_user_settings(user_id)
            active_username = user_settings.get("active_ig_username")
            session = await load_platform_session_data(user_id, "instagram", active_username)
            if not session:
                return await safe_edit_message(msg.reply_to_message, "❌ Instagram session expired. Please `/login` again.")
            user_upload_client.set_settings(session)
            
            try:
                locations = await asyncio.to_thread(user_upload_client.location_search, location_search_term)
                if not locations:
                    await safe_edit_message(msg.reply_to_message, f"📍 No locations found for `{location_search_term}`. Try again or cancel.", reply_markup=get_caption_markup())
                    user_states[user_id]["action"] = "waiting_for_location_search_insta"
                    return
                
                location_buttons = [[InlineKeyboardButton(f"{loc.name} ({loc.address})", callback_data=f"select_location_{loc.pk}")] for loc in locations[:5]]
                location_buttons.append([InlineKeyboardButton("❌ Cancel Location", callback_data="cancel_location_insta")])
                
                await safe_edit_message(msg.reply_to_message, "📍 **Select a location:**", reply_markup=InlineKeyboardMarkup(location_buttons))
                user_states[user_id]['action'] = "selecting_location_insta"
                user_states[user_id]['location_choices'] = {loc.pk: loc for loc in locations}
            except Exception as e:
                await safe_edit_message(msg.reply_to_message, f"❌ Error searching for locations: {e}")
                user_states[user_id]['action'] = "caption_set_waiting_for_options"
        
        task_tracker.create_task(safe_task_wrapper(search_location_task()), user_id=user_id, task_name="location_search")

    # --- Admin Flow ---
    elif action == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id): return
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ User ID `{target_user_id}` received. Select platforms for premium:",
                reply_markup=get_platform_selection_markup(user_id, {}),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ Invalid user ID. Please send a valid number.")
            if user_id in user_states: del user_states[user_id]

    elif action == "waiting_for_max_uploads":
        if not is_admin(user_id): return
        try:
            new_limit = int(msg.text)
            if new_limit <= 0: return await msg.reply("❌ Limit must be a positive integer.")
            await _update_global_setting("max_concurrent_uploads", new_limit)
            global upload_semaphore
            upload_semaphore = asyncio.Semaphore(new_limit)
            await msg.reply(f"✅ Max concurrent uploads set to `{new_limit}`.", reply_markup=get_admin_global_settings_markup())
            if user_id in user_states: del user_states[user_id]
        except ValueError:
            await msg.reply("❌ Invalid input. Please send a valid number.")

    elif action == "waiting_for_proxy_url":
        if not is_admin(user_id): return
        proxy_url = msg.text
        if proxy_url.lower() in ["none", "remove"]:
            await _update_global_setting("proxy_url", "")
            await msg.reply("✅ Bot proxy has been removed.")
        else:
            await _update_global_setting("proxy_url", proxy_url)
            await msg.reply(f"✅ Bot proxy set to: `{proxy_url}`.")
        if user_id in user_states: del user_states[user_id]
        if msg.reply_to_message:
            await safe_edit_message(msg.reply_to_message, "Global Settings", reply_markup=get_admin_global_settings_markup())

    elif action in ["waiting_for_event_title", "waiting_for_event_message"]:
        if not is_admin(user_id): return
        setting_key = "special_event_title" if action == "waiting_for_event_title" else "special_event_message"
        await _update_global_setting(setting_key, msg.text)
        await msg.reply(f"✅ Special event `{setting_key.split('_')[-1]}` updated!", reply_markup=get_admin_global_settings_markup())
        if user_id in user_states: del user_states[user_id]

    elif action.startswith("waiting_for_payment_details_"):
        if not is_admin(user_id): return
        payment_method = action.replace("waiting_for_payment_details_", "")
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings[payment_method] = msg.text
        await _update_global_setting("payment_settings", new_payment_settings)
        await msg.reply(f"✅ Payment details for **{payment_method.upper()}** updated.", reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states: del user_states[user_id]

# ===================================================================
# =================== CALLBACK QUERY HANDLERS =======================
# ===================================================================

@app.on_callback_query(filters.regex("^confirm_reset_profile$"))
@with_user_lock
async def confirm_reset_profile_cb(_, query):
    user_id = query.from_user.id
    if db is not None:
        await asyncio.to_thread(db.users.delete_one, {"_id": user_id})
        await asyncio.to_thread(db.settings.delete_one, {"_id": user_id})
        await asyncio.to_thread(db.sessions.delete_many, {"user_id": user_id})
    
    if user_id in user_states:
        del user_states[user_id]
    
    await query.answer("✅ Your profile has been reset. Please use /start to begin again.", show_alert=True)
    await safe_edit_message(query.message, "✅ Your profile has been reset. Please use /start to begin again.")

# --- Settings Hub Callbacks (NEW) ---
@app.on_callback_query(filters.regex("^back_to_settings_hub$"))
async def back_to_settings_hub_cb(_, query):
    await safe_edit_message(
        query.message,
        "⚙️ Welcome to the settings panel. Choose a platform to configure:",
        reply_markup=get_main_settings_markup()
    )

@app.on_callback_query(filters.regex("^hub_settings_instagram$"))
async def hub_settings_instagram_cb(_, query):
    await safe_edit_message(
        query.message, "⚙️ Configure your Instagram settings:", reply_markup=get_insta_settings_markup()
    )

# --- Account Management Callbacks ---
@app.on_callback_query(filters.regex("^manage_ig_accounts$"))
async def manage_ig_accounts_cb(_, query):
    user_id = query.from_user.id
    sessions = await load_platform_sessions(user_id, "instagram")
    logged_in_accounts = [s['username'] for s in sessions]
    
    if not logged_in_accounts:
        await query.answer("You have no Instagram accounts logged in. Let's add one.", show_alert=True)
        user_states[user_id] = {"action": "waiting_for_instagram_username"}
        return await safe_edit_message(query.message, "👤 Please send your Instagram **username**.")

    user_settings = await get_user_settings(user_id)
    active_account = user_settings.get("active_ig_username")
    
    await safe_edit_message(query.message, f"👤 **Your Instagram Accounts**\n\nActive: `@{active_account or 'None'}`\n\nSelect an account to make it active, or manage accounts.",
        # ### FIX: Awaiting the now-async get_insta_account_markup function ###
        reply_markup=await get_insta_account_markup(user_id, logged_in_accounts),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_ig_account_"))
async def select_ig_account_cb(_, query):
    user_id = query.from_user.id
    username = query.data.split("select_ig_account_")[-1]
    
    user_settings = await get_user_settings(user_id)
    user_settings["active_ig_username"] = username
    await save_user_settings(user_id, user_settings)
    
    await query.answer(f"✅ @{username} is now your active Instagram account.", show_alert=True)
    await manage_ig_accounts_cb(app, query)

@app.on_callback_query(filters.regex("^logout_ig_account$"))
async def logout_ig_account_cb(_, query):
    user_id = query.from_user.id
    user_settings = await get_user_settings(user_id)
    active_username = user_settings.get("active_ig_username")
    
    if not active_username:
        return await query.answer("No active Instagram account to log out from.", show_alert=True)
        
    await delete_platform_session(user_id, "instagram", active_username)
    
    sessions = await load_platform_sessions(user_id, "instagram")
    user_settings["active_ig_username"] = sessions[0]['username'] if sessions else None
    await save_user_settings(user_id, user_settings)
    
    await query.answer(f"✅ Logged out from @{active_username}.", show_alert=True)
    await manage_ig_accounts_cb(app, query)

@app.on_callback_query(filters.regex("^add_account_"))
async def add_account_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("add_account_")[-1]
    
    if not await is_premium_for_platform(user_id, platform) and not is_admin(user_id):
        return await query.answer("❌ This is a premium feature.", show_alert=True)
    
    user_states[user_id] = {"action": f"waiting_for_{platform}_username"}
    await safe_edit_message(query.message, f"👤 Please send your {platform.capitalize()} **username**.")

# --- General Callbacks ---
@app.on_callback_query(filters.regex("^cancel_upload$"))
async def cancel_upload_cb(_, query):
    user_id = query.from_user.id
    await query.answer("Upload cancelled.", show_alert=True)
    await safe_edit_message(query.message, "❌ **Upload Cancelled**\n\nYour operation has been successfully cancelled.")

    state_data = user_states.get(user_id, {})
    files_to_clean = []
    if "media_paths" in state_data.get("file_info", {}):
        files_to_clean.extend(state_data["file_info"]["media_paths"])
    if "downloaded_path" in state_data.get("file_info", {}):
        files_to_clean.append(state_data["file_info"].get("downloaded_path"))
    
    cleanup_temp_files(files_to_clean)
    if user_id in user_states: del user_states[user_id]
    await task_tracker.cancel_all_user_tasks(user_id)
    logger.info(f"User {user_id} cancelled their upload.")

@app.on_callback_query(filters.regex("^skip_caption$"))
async def skip_caption_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    if not state_data or "file_info" not in state_data:
        return await query.answer("❌ Error: No upload process found.", show_alert=True)
    
    await query.answer("✅ Using default caption...")
    file_info = state_data["file_info"]
    file_info["custom_caption"] = None # Signal to use default
    
    await safe_edit_message(query.message, "🚀 Preparing to upload with default caption...")
    await start_upload_task(query.message, file_info)

@app.on_callback_query(filters.regex("^upload_now$"))
async def upload_now_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    if not state_data or "file_info" not in state_data:
        return await query.answer("❌ Error: No upload process found to continue.", show_alert=True)
    
    file_info = state_data["file_info"]
    await safe_edit_message(query.message, "🚀 Starting upload now...")
    await start_upload_task(query.message, file_info)

@app.on_callback_query(filters.regex("^upload_album_done$"))
async def upload_album_done_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    if not state_data or state_data.get('action') != 'caption_set_waiting_for_options':
        return await query.answer("❌ Error: Not in the right state to finalize album upload.", show_alert=True)

    await upload_now_cb(app, query) # Same as clicking upload now

@app.on_callback_query(filters.regex("^tag_users_insta$"))
async def tag_users_cb(_, query):
    user_id = query.from_user.id
    if not await is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ This is a premium feature.", show_alert=True)

    state_data = user_states.get(user_id)
    if not state_data or 'file_info' not in state_data:
        return await query.answer("❌ Error: State lost, please start over.", show_alert=True)

    user_states[user_id]['action'] = 'waiting_for_usertags_insta'
    await safe_edit_message(
        query.message,
        "👥 Please send a comma-separated list of Instagram usernames to tag (e.g., `user1, user2`)."
    )

@app.on_callback_query(filters.regex("^add_location_insta$"))
async def add_location_cb(_, query):
    user_id = query.from_user.id
    if not await is_premium_for_platform(user_id, "instagram"):
        return await query.answer("❌ This is a premium feature.", show_alert=True)

    state_data = user_states.get(user_id)
    if not state_data or 'file_info' not in state_data:
        return await query.answer("❌ Error: State lost, please start over.", show_alert=True)

    user_states[user_id]['action'] = 'waiting_for_location_search_insta'
    await safe_edit_message(
        query.message,
        "📍 Please send the name of the location you want to tag (e.g., `New York`)."
    )

@app.on_callback_query(filters.regex("^select_location_"))
async def select_location_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    if not state_data or state_data.get('action') != 'selecting_location_insta':
        return await query.answer("❌ Error: State lost. Please try adding a location again.", show_alert=True)

    location_pk = int(query.data.split("select_location_")[1])
    location_obj = state_data['location_choices'].get(location_pk)
    if not location_obj:
        return await query.answer("❌ Invalid location selected.", show_alert=True)

    file_info = state_data.get("file_info", {})
    file_info["location"] = location_obj
    state_data["file_info"] = file_info

    await safe_edit_message(query.message, f"📍 **Location Set:** `{location_obj.name}`\n\nContinue with other options or upload now.",
        reply_markup=get_caption_markup(is_album=state_data['upload_type'] == 'album'))
    state_data['action'] = 'caption_set_waiting_for_options'
    user_states[user_id] = state_data

@app.on_callback_query(filters.regex("^cancel_location_insta$"))
async def cancel_location_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    if not state_data:
        return await query.answer("❌ Error: No upload process to cancel.", show_alert=True)
    
    await query.answer("Location tagging cancelled.", show_alert=False)
    file_info = state_data.get("file_info", {})
    if "location" in file_info: del file_info["location"]

    await safe_edit_message(
        query.message,
        "📍 Location tagging cancelled. What's next?",
        reply_markup=get_caption_markup(is_album=state_data['upload_type'] == 'album')
    )
    state_data['action'] = 'caption_set_waiting_for_options'
    user_states[user_id] = state_data

# --- Premium & Payment Callbacks ---
@app.on_callback_query(filters.regex("^buypypremium$"))
async def buypypremium_cb(_, query):
    user_id = query.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    premium_plans_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions.\n\n"
        "**Available Plans:**"
    )
    await safe_edit_message(query.message, premium_plans_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_plan_details_"))
async def show_plan_details_cb(_, query):
    user_id = query.from_user.id
    plan_key = query.data.split("show_plan_details_")[1]
    
    state_data = user_states.get(user_id, {})
    is_admin_adding_premium = (is_admin(user_id) and state_data.get("action") == "select_premium_plan_for_platforms")
    
    plan_details = PREMIUM_PLANS[plan_key]
    plan_text = f"**{plan_key.replace('_', ' ').title()} Plan Details**\n\n**Duration**: "
    plan_text += f"{plan_details['duration'].days} days\n" if plan_details['duration'] else "Lifetime\n"
    plan_text += f"**Price**: {plan_details['price']}\n\n"
    
    if is_admin_adding_premium:
        target_user_id = state_data.get('target_user_id', 'Unknown User')
        plan_text += f"Click below to grant this plan to user `{target_user_id}`."
    else:
        plan_text += "To purchase, click 'Buy Now' or check the available payment methods."
        
    await safe_edit_message(
        query.message, plan_text,
        reply_markup=get_premium_details_markup(plan_key, is_admin_flow=is_admin_adding_premium),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^show_payment_methods$"))
async def show_payment_methods_cb(_, query):
    payment_methods_text = "**Available Payment Methods**\n\n"
    payment_methods_text += "Choose your preferred method to proceed with payment."
    await safe_edit_message(query.message, payment_methods_text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_qr_google_play$"))
async def show_payment_qr_google_play_cb(_, query):
    qr_file_id = global_settings.get("payment_settings", {}).get("google_play_qr_file_id")
    if not qr_file_id:
        return await query.answer("Google Pay QR code is not set by the admin yet.", show_alert=True)
    
    await query.message.reply_photo(
        photo=qr_file_id,
        caption="**Scan & Pay using Google Pay**\n\n"
                "Please send a screenshot of the payment to **[Admin Tom](https://t.me/CjjTom)** for activation.",
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=get_payment_methods_markup()
    )
    await safe_edit_message(query.message, "Choose your preferred method to proceed with payment.", reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_details_"))
async def show_payment_details_cb(_, query):
    method = query.data.split("show_payment_details_")[1]
    payment_details = global_settings.get("payment_settings", {}).get(method, "No details available.")
    text = (
        f"**{method.upper()} Payment Details**\n\n"
        f"`{payment_details}`\n\n"
        f"Please pay the required amount and contact **[Admin Tom](https://t.me/CjjTom)** with a screenshot of the payment for premium activation."
    )
    await safe_edit_message(query.message, text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^buy_now$"))
async def buy_now_cb(_, query):
    text = (
        f"**Purchase Confirmation**\n\n"
        f"Please contact **[Admin Tom](https://t.me/CjjTom)** to complete the payment process."
    )
    await safe_edit_message(query.message, text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^premiumdetails$"))
async def premium_details_cb(_, query):
    # This button might be from old keyboards, redirect to the command.
    await query.message.reply("Please use the `/premiumdetails` command instead.")

# --- Admin Panel Callbacks ---
@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)
    await safe_edit_message(
        query.message,
        "🛠 Welcome to the Admin Panel!",
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^global_settings_panel$"))
async def global_settings_panel_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)
    
    settings_text = (
        "⚙️ **Global Bot Settings**\n\n"
        f"**📢 Special Event:** `{global_settings.get('special_event_toggle', False)}`\n"
        f"**Max concurrent uploads:** `{global_settings.get('max_concurrent_uploads')}`\n"
        f"**Global Proxy:** `{global_settings.get('proxy_url') or 'None'}`\n"
        f"**Global Compression:** `{'Disabled' if global_settings.get('no_compression_admin') else 'Enabled'}`"
    )
    await safe_edit_message(query.message, settings_text, reply_markup=get_admin_global_settings_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^payment_settings_panel$"))
async def payment_settings_panel_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ Admin access required", show_alert=True)

    await safe_edit_message(
        query.message,
        "💰 **Payment Settings**\n\nManage payment details for premium purchases.",
        reply_markup=payment_settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^back_to_"))
async def back_to_cb(_, query):
    data = query.data
    user_id = query.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    await task_tracker.cancel_all_user_tasks(user_id)
    if user_id in user_states: del user_states[user_id]
        
    if data == "back_to_main_menu":
        try:
            await query.message.delete()
        except Exception:
            pass
        is_ig_premium = await is_premium_for_platform(user_id, "instagram")
        premium_platforms = []
        if is_ig_premium: premium_platforms.append("instagram")
        await app.send_message(
            query.message.chat.id, "🏠 Main Menu",
            reply_markup=get_main_keyboard(user_id, premium_platforms)
        )
    elif data == "back_to_settings":
        await safe_edit_message(query.message, "⚙️ Settings Panel", reply_markup=get_main_settings_markup())
    elif data == "back_to_admin":
        await admin_panel_cb(app, query)
    elif data == "back_to_premium_plans":
        await buypypremium_cb(app, query)
    elif data == "back_to_global":
        await global_settings_panel_cb(app, query)
    else:
        await query.answer("❌ Unknown back action", show_alert=True)

@app.on_callback_query(filters.regex("^activate_trial_instagram$"))
async def activate_trial_instagram_cb(_, query):
    user_id = query.from_user.id
    user_first_name = query.from_user.first_name or "there"
    
    if await is_premium_for_platform(user_id, "instagram"):
        return await query.answer("Your Instagram trial is already active!", show_alert=True)

    premium_until = datetime.utcnow() + timedelta(hours=6)
    user_premium_data = (await _get_user_data(user_id)).get("premium", {})
    user_premium_data["instagram"] = {
        "type": "6_hour_trial", "added_by": "callback_trial",
        "added_at": datetime.utcnow(), "until": premium_until
    }
    await _save_user_data(user_id, {"premium": user_premium_data})

    logger.info(f"User {user_id} activated a 6-hour Instagram trial.")
    await send_log_to_channel(app, LOG_CHANNEL, f"✨ User `{user_id}` activated a 6-hour Instagram trial.")
    
    await query.answer("✅ Free 6-hour Instagram trial activated!", show_alert=True)
    welcome_msg = (
        f"🎉 **Congratulations, {user_first_name}!**\n\n"
        f"You have activated your **6-hour premium trial** for **Instagram**.\n\n"
        "To get started, please log in with: `/instagramlogin` or `/iglogin`"
    )
    premium_platforms = ["instagram"]
    await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^toggle_special_event$"))
async def toggle_special_event_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    
    new_status = not global_settings.get("special_event_toggle", False)
    await _update_global_setting("special_event_toggle", new_status)
    await query.answer(f"Special Event toggled {'ON' if new_status else 'OFF'}.", show_alert=True)
    await global_settings_panel_cb(app, query)

@app.on_callback_query(filters.regex("^set_event_title$"))
async def set_event_title_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required.", show_alert=True)
    user_states[query.from_user.id] = {"action": "waiting_for_event_title"}
    await safe_edit_message(query.message, "✏️ Please send the new title for the special event.")

@app.on_callback_query(filters.regex("^set_event_message$"))
async def set_event_message_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required.", show_alert=True)
    user_states[query.from_user.id] = {"action": "waiting_for_event_message"}
    await safe_edit_message(query.message, "💬 Please send the new message for the special event.")

@app.on_callback_query(filters.regex("^toggle_compression_admin$"))
async def toggle_compression_admin_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    
    new_status = not global_settings.get("no_compression_admin", False)
    await _update_global_setting("no_compression_admin", new_status)
    await query.answer(f"Global compression toggled to: {'DISABLED' if new_status else 'ENABLED'}.", show_alert=True)
    await global_settings_panel_cb(app, query)

@app.on_callback_query(filters.regex("^set_max_uploads$"))
@with_user_lock
async def set_max_uploads_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    user_states[query.from_user.id] = {"action": "waiting_for_max_uploads"}
    current_limit = global_settings.get("max_concurrent_uploads")
    await safe_edit_message(
        query.message,
        f"🔄 Please send the new max number of concurrent uploads.\nCurrent limit: `{current_limit}`"
    )

@app.on_callback_query(filters.regex("^set_proxy_url$"))
@with_user_lock
async def set_proxy_url_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    user_states[query.from_user.id] = {"action": "waiting_for_proxy_url"}
    current_proxy = global_settings.get("proxy_url", "None set.")
    await safe_edit_message(
        query.message,
        f"🌐 Please send the new proxy URL (e.g., `http://user:pass@ip:port`).\n"
        f"Type 'none' or 'remove' to disable.\nCurrent proxy: `{current_proxy}`"
    )

@app.on_callback_query(filters.regex("^reset_stats$"))
@with_user_lock
async def reset_stats_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    await safe_edit_message(query.message, "⚠️ **WARNING!** Are you sure you want to reset all upload stats? This is irreversible.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, reset stats", callback_data="confirm_reset_stats")],
            [InlineKeyboardButton("❌ No, cancel", callback_data="admin_panel")]
        ]), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^confirm_reset_stats$"))
@with_user_lock
async def confirm_reset_stats_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    if db is None: return await query.answer("⚠️ Database unavailable.", show_alert=True)
    
    result = await asyncio.to_thread(db.uploads.delete_many, {})
    await query.answer(f"✅ All stats reset! Deleted {result.deleted_count} uploads.", show_alert=True)
    await admin_panel_cb(app, query)
    await send_log_to_channel(app, LOG_CHANNEL, f"📊 Admin `{query.from_user.id}` has reset all bot upload stats.")

@app.on_callback_query(filters.regex("^show_system_stats$"))
async def show_system_stats_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        system_stats_text = (
            f"💻 **System Stats**\n\n"
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
                        f"  - Load: `{gpu.load*100:.1f}%`\n"
                        f"  - Memory: `{gpu.memoryUsed}/{gpu.memoryTotal}` MB\n"
                        f"  - Temp: `{gpu.temperature}°C`\n"
                    )
            else:
                gpu_info = "No GPU found."
        except Exception:
            gpu_info = "Could not retrieve GPU info."
            
        system_stats_text += gpu_info
        await safe_edit_message(
            query.message, system_stats_text,
            reply_markup=get_admin_global_settings_markup(),
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await query.answer("❌ Failed to retrieve system stats.", show_alert=True)
        logger.error(f"Error retrieving system stats for admin {query.from_user.id}: {e}")
        await admin_panel_cb(app, query)

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    await _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    if db is None: return await query.answer("⚠️ Database unavailable.", show_alert=True)
    
    users = await asyncio.to_thread(list, db.users.find({}))
    if not users:
        return await safe_edit_message(query.message, "👥 No users found.", reply_markup=admin_markup)
        
    user_list_text = "👥 **All Users:**\n\n"
    for user in users:
        user_id = user["_id"]
        ig_sessions = await load_platform_sessions(user_id, "instagram")
        
        insta_usernames = [s["username"] for s in ig_sessions]
        added_at = user.get("added_at", "N/A").strftime("%Y-%m-%d") if isinstance(user.get("added_at"), datetime) else "N/A"
        last_active = user.get("last_active", "N/A").strftime("%Y-%m-%d %H:%M") if isinstance(user.get("last_active"), datetime) else "N/A"
        
        platform_statuses = []
        if user_id == ADMIN_ID:
            platform_statuses.append("👑 Admin")
        else:
            for platform in PREMIUM_PLATFORMS:
                if await is_premium_for_platform(user_id, platform):
                    platform_statuses.append(f"⭐ {platform.capitalize()}")
        status_line = " | ".join(platform_statuses) if platform_statuses else "❌ Free"
        
        user_list_text += (
            f"ID: `{user_id}` | {status_line}\n"
            f"IG Accounts: `{', '.join(insta_usernames) or 'N/A'}`\n"
            f"Added: `{added_at}` | Last Active: `{last_active}`\n"
            "-----------------------------------\n"
        )
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "User list is too long, sending as a file...")
        with open("users.txt", "w", encoding="utf-8") as f:
            f.write(user_list_text.replace("`", ""))
        await app.send_document(query.message.chat.id, "users.txt", caption="👥 All Users List")
        os.remove("users.txt")
        await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)
    else:
        await safe_edit_message(query.message, user_list_text, reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^manage_premium$"))
@with_user_lock
async def manage_premium_cb(_, query):
    await _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    
    user_states[query.from_user.id] = {"action": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(query.message, "➕ Please send the **USER ID** to manage their premium access.")

@app.on_callback_query(filters.regex("^select_platform_"))
async def select_platform_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id): return await query.answer("❌ Admin access required", show_alert=True)
    
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        return await query.answer("Error: State lost. Please try again.", show_alert=True)

    platform_to_toggle = query.data.split("select_platform_")[-1]
    selected_platforms = state_data.get("selected_platforms", {})
    selected_platforms[platform_to_toggle] = not selected_platforms.get(platform_to_toggle, False)
    
    state_data["selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    
    await safe_edit_message(
        query.message,
        f"✅ User ID `{state_data['target_user_id']}`. Select platforms for premium:",
        reply_markup=get_platform_selection_markup(user_id, selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_platform_selection$"))
async def confirm_platform_selection_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id): return await query.answer("❌ Admin access required", show_alert=True)
    
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_platforms_for_premium":
        return await query.answer("Error: State lost. Please restart.", show_alert=True)
        
    selected_platforms = [p for p, selected in state_data.get("selected_platforms", {}).items() if selected]
    if not selected_platforms:
        return await query.answer("Please select at least one platform!", show_alert=True)
        
    state_data["action"] = "select_premium_plan_for_platforms"
    state_data["final_selected_platforms"] = selected_platforms
    user_states[user_id] = state_data
    
    await safe_edit_message(
        query.message,
        f"✅ Platforms selected: `{', '.join(p.capitalize() for p in selected_platforms)}`.\nNow, select a premium plan for user `{state_data['target_user_id']}`:",
        reply_markup=get_premium_plan_markup(user_id),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^grant_plan_"))
async def grant_plan_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id): return await query.answer("❌ Admin access required", show_alert=True)
    if db is None: return await query.answer("⚠️ Database unavailable.", show_alert=True)
    
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") != "select_premium_plan_for_platforms":
        return await query.answer("❌ Error: State lost. Please start over.", show_alert=True)
        
    target_user_id = state_data["target_user_id"]
    selected_platforms = state_data["final_selected_platforms"]
    premium_plan_key = query.data.split("grant_plan_")[1]
    
    plan_details = PREMIUM_PLANS.get(premium_plan_key)
    if not plan_details:
        return await query.answer("Invalid premium plan selected.", show_alert=True)
    
    target_user_data = await _get_user_data(target_user_id) or {"_id": target_user_id, "premium": {}}
    premium_data = target_user_data.get("premium", {})
    
    for platform in selected_platforms:
        new_premium_until = None
        if plan_details["duration"] is not None:
            new_premium_until = datetime.utcnow() + plan_details["duration"]
        
        platform_premium_data = {
            "type": premium_plan_key, "added_by": user_id, "added_at": datetime.utcnow()
        }
        if new_premium_until:
            platform_premium_data["until"] = new_premium_until
        
        premium_data[platform] = platform_premium_data
    
    await _save_user_data(target_user_id, {"premium": premium_data})
    
    admin_confirm_text = f"✅ Premium granted to user `{target_user_id}` for:\n"
    user_msg_text = "🎉 **Congratulations!** 🎉\n\nYou have been granted premium access for:\n"
    
    for platform in selected_platforms:
        updated_user = await _get_user_data(target_user_id)
        p_data = updated_user.get("premium", {}).get(platform, {})
        line = f"**{platform.capitalize()}**: `{p_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if p_data.get("until"):
            line += f" (Expires: `{p_data['until'].strftime('%Y-%m-%d %H:%M')}` UTC)"
        admin_confirm_text += f"- {line}\n"
        user_msg_text += f"- {line}\n"
    
    user_msg_text += "\nEnjoy your new features! ✨"
    
    await safe_edit_message(query.message, admin_confirm_text, reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)
    await query.answer("Premium granted!", show_alert=False)
    if user_id in user_states: del user_states[user_id]
        
    try:
        await app.send_message(target_user_id, user_msg_text, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 Premium granted to `{target_user_id}` by admin `{user_id}`. Platforms: `{', '.join(selected_platforms)}`, Plan: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {target_user_id} about premium: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ Failed to notify user `{target_user_id}` about premium. Error: `{e}`"
        )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    await _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    await safe_edit_message(
        query.message,
        "📢 Please use the `/broadcast <message>` command to send a message to all users."
    )

@app.on_callback_query(filters.regex("^admin_stats_panel$"))
async def admin_stats_panel_cb(_, query):
    if not is_admin(query.from_user.id): return await query.answer("❌ Admin access required", show_alert=True)
    await safe_edit_message(query.message, "Please use the /stats command to view detailed statistics.", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^set_caption_"))
async def set_caption_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("set_caption_")[-1]
    user_states[user_id] = {"action": f"waiting_for_caption_{platform}"}
    await safe_edit_message(
        query.message,
        f"📝 Please send your new default caption for {platform.capitalize()}."
    )

@app.on_callback_query(filters.regex("^set_hashtags_"))
async def set_hashtags_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("set_hashtags_")[-1]
    if platform != "instagram":
        return await query.answer("Hashtags can only be set for Instagram.", show_alert=True)
    user_states[user_id] = {"action": f"waiting_for_hashtags_{platform}"}
    await safe_edit_message(
        query.message,
        f"🏷️ Please send your new default hashtags for {platform.capitalize()}."
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio_instagram$"))
async def set_aspect_ratio_cb(_, query):
    await safe_edit_message(
        query.message,
        "📐 Select the aspect ratio for your videos:",
        reply_markup=aspect_ratio_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_ar_"))
async def set_aspect_ratio_value_cb(_, query):
    user_id = query.from_user.id
    aspect_ratio = query.data.split("set_ar_")[1]
    settings = await get_user_settings(user_id)
    settings["aspect_ratio_instagram"] = aspect_ratio
    await save_user_settings(user_id, settings)
    
    await query.answer(f"✅ Aspect ratio set to {aspect_ratio}.", show_alert=True)
    await safe_edit_message(query.message, "⚙️ Configure your Instagram settings:", reply_markup=get_insta_settings_markup())

@app.on_callback_query(filters.regex("^login_platform_"))
async def login_platform_cb(_, query):
    # This handler is now deprecated in favor of specific commands, but kept for old inline buttons.
    await query.message.reply("This button is outdated. Please use `/instagramlogin` or `/iglogin`.")

# === Timeout Task ===
async def timeout_task(user_id, message_id):
    await asyncio.sleep(600) # 10 minutes
    if user_id in user_states:
        del user_states[user_id]
        logger.info(f"Task for user {user_id} timed out and was canceled.")
        try:
            await app.edit_message_text(
                chat_id=user_id, message_id=message_id,
                text="⚠️ Timeout! The operation was canceled due to inactivity."
            )
        except Exception as e:
            logger.warning(f"Could not send timeout message to user {user_id}: {e}")

# ===================================================================
# ======================== MEDIA HANDLERS ===========================
# ===================================================================

@app.on_message(filters.media & filters.private)
@with_user_lock
async def handle_media_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    state_data = user_states.get(user_id)

    # Admin: Set Payment QR
    if is_admin(user_id) and state_data and state_data.get("action") == "waiting_for_google_play_qr" and msg.photo:
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings["google_play_qr_file_id"] = msg.photo.file_id
        await _update_global_setting("payment_settings", new_payment_settings)
        if user_id in user_states: del user_states[user_id]
        return await msg.reply("✅ Google Pay QR code image saved!")

    # User: Media Upload
    valid_actions = [
        "waiting_for_instagram_reel_video", "waiting_for_instagram_photo_image",
        "waiting_for_instagram_story", "waiting_for_album_media"
    ]
    if not state_data or state_data.get("action") not in valid_actions:
        return await msg.reply("❌ Please use one of the upload buttons first.")

    media = msg.video or msg.photo or msg.document
    if not media: return await msg.reply("❌ Unsupported media type.")

    if media.file_size > MAX_FILE_SIZE_BYTES:
        if user_id in user_states: del user_states[user_id]
        return await msg.reply(f"❌ File size exceeds the limit of `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` MB.")

    # Handle multi-media uploads (Album)
    if state_data.get("action") == "waiting_for_album_media":
        if len(state_data['media_paths']) >= 10:
            return await msg.reply("⚠️ Max 10 items in an album. Send `/done` to finish.")
        
        processing_msg = await msg.reply("⏳ Downloading media...")
        file_path = await app.download_media(msg)
        state_data['media_paths'].append(file_path)
        
        num_files = len(state_data['media_paths'])
        platform_name = "album"
        await safe_edit_message(processing_msg, f"✅ Downloaded file {num_files} for your {platform_name}. Send more or use `/done`.")
        return

    # Handle single media uploads
    processing_msg = await msg.reply("⏳ Starting download...")
    file_info = {
        "file_id": media.file_id, "platform": state_data["platform"], 
        "upload_type": state_data["upload_type"], "file_size": media.file_size,
        "processing_msg": processing_msg, "original_msg_id": msg.id,
        "downloaded_path": None, "usertags": [], "location": None
    }

    try:
        start_time = time.time()
        last_update_time = [0]
        task_tracker.create_task(monitor_progress_task(msg.chat.id, processing_msg.id, processing_msg), user_id=user_id, task_name="progress_monitor")
        
        file_info["downloaded_path"] = await app.download_media(
            msg,
            progress=progress_callback_threaded,
            progress_args=("Download", processing_msg.id, msg.chat.id, start_time, last_update_time)
        )
        task_tracker.cancel_user_task(user_id, "progress_monitor")

        # Story uploads don't need a caption flow
        if file_info["upload_type"] == "story":
            user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
            await start_upload_task(msg, file_info)
            return

        is_premium = await is_premium_for_platform(user_id, file_info['platform'])
        caption_text = "✅ Download complete. Please enter a caption for your post."
        if not is_premium:
            caption_text += "\n\n⚠️ As a free user, your caption is limited to 280 characters and you cannot add tags or locations."

        caption_msg = await processing_msg.reply_text(
            caption_text,
            reply_markup=get_caption_markup(is_album=False, is_premium=is_premium),
            reply_to_message_id=msg.id
        )
        file_info['processing_msg'] = caption_msg
        user_states[user_id] = {"action": "waiting_for_caption", "file_info": file_info}
        task_tracker.create_task(safe_task_wrapper(timeout_task(user_id, caption_msg.id)), user_id=user_id, task_name="timeout")

    except asyncio.CancelledError:
        logger.info(f"Download cancelled by user {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"Error during file download for user {user_id}: {e}", exc_info=True)
        await safe_edit_message(file_info.get("processing_msg"), f"❌ Download failed: {e}")
        cleanup_temp_files([file_info.get("downloaded_path")])
        if user_id in user_states: del user_states[user_id]


# ===================================================================
# ==================== UPLOAD PROCESSING ==========================
# ===================================================================

async def start_upload_task(msg, file_info):
    user_id = msg.from_user.id
    task_tracker.create_task(
        safe_task_wrapper(process_and_upload(msg, file_info)),
        user_id=user_id,
        task_name="upload"
    )

async def process_and_upload(msg, file_info, is_scheduled=False):
    user_id = msg.from_user.id
    platform = file_info["platform"]
    upload_type = file_info["upload_type"]
    processing_msg = file_info.get("processing_msg")
    
    task_tracker.cancel_user_task(user_id, "timeout")

    async with upload_semaphore:
        logger.info(f"Semaphore acquired for user {user_id}. Starting upload to {platform}.")
        files_to_clean = []
        try:
            user_settings = await get_user_settings(user_id)
            is_premium = await is_premium_for_platform(user_id, platform)
            
            # --- Build Caption ---
            default_caption = user_settings.get(f"caption_{platform}", "")
            hashtags = user_settings.get(f"hashtags_instagram", "") if platform == "instagram" else ""
            final_caption = file_info.get("custom_caption", default_caption)
            if hashtags:
                final_caption = f"{final_caption}\n\n{hashtags}"

            await safe_edit_message(processing_msg, f"🚀 **Uploading to {platform.capitalize()}...**", reply_markup=get_progress_markup())
            
            url, media_id, media_type_value = "N/A", "N/A", "N/A"

            # --- Instagram Upload ---
            if platform == "instagram":
                user_upload_client = InstaClient()
                proxy_url = global_settings.get("proxy_url")
                if proxy_url: user_upload_client.set_proxy(proxy_url)
                
                active_username = user_settings.get("active_ig_username")
                if not active_username: raise LoginRequired("No active IG account. Please login.")
                
                session = await load_platform_session_data(user_id, "instagram", active_username)
                if not session: raise LoginRequired("IG session expired. Please re-login.")
                
                user_upload_client.set_settings(session)
                try:
                    # Relogin with session to validate it
                    user_upload_client.login_by_sessionid(session['authorization_data']['sessionid'])
                except LoginRequired:
                    raise LoginRequired("IG session is invalid or expired. Please re-login.")

                usertags_to_add = []
                if is_premium and file_info.get("usertags"):
                    for u_name in file_info["usertags"]:
                        try:
                            user_info = await asyncio.to_thread(user_upload_client.user_info_by_username, u_name)
                            usertags_to_add.append(Usertag(user=user_info, x=0.5, y=0.5))
                        except Exception as e:
                            logger.warning(f"Could not find user to tag: {u_name}, Error: {e}")

                location_to_add = file_info.get("location") if is_premium else None

                path = file_info.get("downloaded_path")
                paths = file_info.get("media_paths")
                
                if upload_type == "reel":
                    files_to_clean.append(path)
                    result = await asyncio.to_thread(user_upload_client.clip_upload, path, final_caption, usertags=usertags_to_add, location=location_to_add)
                    url = f"https://instagram.com/reel/{result.code}"
                elif upload_type == "post":
                    files_to_clean.append(path)
                    result = await asyncio.to_thread(user_upload_client.photo_upload, path, final_caption, usertags=usertags_to_add, location=location_to_add)
                    url = f"https://instagram.com/p/{result.code}"
                elif upload_type == "album":
                    files_to_clean.extend(paths)
                    result = await asyncio.to_thread(user_upload_client.album_upload, paths, final_caption, usertags=usertags_to_add, location=location_to_add)
                    url = f"https://instagram.com/p/{result.code}"
                elif upload_type == "story":
                    files_to_clean.append(path)
                    uploader_func = user_upload_client.photo_upload_to_story if msg.photo else user_upload_client.video_upload_to_story
                    result = await asyncio.to_thread(uploader_func, path)
                    url = f"https://instagram.com/stories/{active_username}"
                
                media_id, media_type_value = result.pk, result.media_type

            # --- Log and Finish ---
            if db is not None:
                await asyncio.to_thread(db.uploads.insert_one, {
                    "user_id": user_id, "media_id": str(media_id), "media_type": str(media_type_value),
                    "platform": platform, "upload_type": upload_type, "timestamp": datetime.utcnow(),
                    "url": url, "caption": final_caption
                })

            log_msg = f"📤 New {platform.capitalize()} {upload_type.capitalize()} Upload\n" \
                      f"👤 User: `{user_id}`\n🔗 URL: {url}\n📅 {get_current_datetime()['date']}"
            await safe_edit_message(processing_msg, f"✅ Uploaded successfully!\n\n{url}")
            await send_log_to_channel(app, LOG_CHANNEL, log_msg)

        except asyncio.CancelledError:
            logger.warning(f"Upload process for user {user_id} was cancelled.")
            await safe_edit_message(processing_msg, "❌ Upload process cancelled.")
        except LoginRequired as e:
            error_msg = f"❌ Login Required for {platform.capitalize()}. Session may have expired. Please use `/instagramlogin` or `/iglogin`.\nError: {e}"
            await safe_edit_message(processing_msg, error_msg)
            logger.error(f"LoginRequired during upload for user {user_id}: {e}")
        except ClientError as e:
            error_msg = f"❌ Instagram client error: {e}. Please try again later."
            await safe_edit_message(processing_msg, error_msg)
            logger.error(f"ClientError during upload for user {user_id}: {e}")
        except Exception as e:
            error_msg = f"❌ Upload to {platform.capitalize()} failed: {str(e)}"
            await safe_edit_message(processing_msg, error_msg)
            logger.error(f"General upload failed for {user_id} on {platform}: {e}", exc_info=True)
        finally:
            cleanup_temp_files(files_to_clean)
            if user_id in user_states: del user_states[user_id]
            logger.info(f"Semaphore released for user {user_id}.")

# === HTTP Server for Health Checks ===
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
    try:
        server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
        logger.info("HTTP health check server started on port 8080.")
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server failed: {e}")

# ===================================================================
# ======================== BOT STARTUP ============================
# ===================================================================
async def start_bot():
    global mongo, db, global_settings, upload_semaphore, MAX_CONCURRENT_UPLOADS, MAX_FILE_SIZE_BYTES, task_tracker, valid_log_channel

    os.makedirs("sessions", exist_ok=True)
    logger.info("Session directories ensured.")

    # --- Database and Settings Setup ---
    try:
        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo.admin.command('ping')
        db = mongo.NowTok # Use your database name
        logger.info("✅ Connected to MongoDB successfully.")
        
        settings_from_db = await asyncio.to_thread(db.settings.find_one, {"_id": "global_settings"}) or {}
        global_settings.update(settings_from_db)
        
        # Ensure default settings are present and update DB if needed
        updated = False
        for key, value in DEFAULT_GLOBAL_SETTINGS.items():
            if key not in global_settings:
                global_settings[key] = value
                updated = True
        if updated:
            await asyncio.to_thread(db.settings.update_one, {"_id": "global_settings"}, {"$set": global_settings}, upsert=True)

        logger.info("Global settings loaded and synchronized.")
    except Exception as e:
        logger.critical(f"❌ DATABASE SETUP FAILED: {e}. Running in degraded mode.")
        db = None
        global_settings = DEFAULT_GLOBAL_SETTINGS

    MAX_CONCURRENT_UPLOADS = global_settings.get("max_concurrent_uploads")
    upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
    MAX_FILE_SIZE_BYTES = global_settings.get("max_file_size_mb") * 1024 * 1024

    # --- Start Health Check Thread ---
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # --- Start Pyrogram Client ---
    await app.start()
    
    # Initialize TaskTracker with the running loop from Pyrogram
    task_tracker.loop = asyncio.get_running_loop()

    # --- Post-start operations ---
    if LOG_CHANNEL:
        try:
            await app.send_message(LOG_CHANNEL, "✅ **Bot is now online and running!**")
            valid_log_channel = True
        except Exception as e:
            logger.error(f"Could not log to channel {LOG_CHANNEL}. Invalid or bot isn't admin. Error: {e}")
            valid_log_channel = False

    logger.info("Bot is now online! Waiting for tasks...")
    await idle() # Keep the bot running until shutdown signal

    # --- Graceful Shutdown ---
    logger.info("Shutting down...")
    await task_tracker.cancel_and_wait_all()
    await app.stop()
    if mongo:
        mongo.close()
    logger.info("Bot has been shut down gracefully.")

if __name__ == "__main__":
    task_tracker = TaskTracker()
    try:
        app.run(start_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received.")
    except Exception as e:
        logger.critical(f"Bot crashed during startup: {e}", exc_info=True)

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

# Twitter Client
from twscrape import API, AccountsPool

# System Utilities
import psutil
import GPUtil
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
LOG_CHANNEL_STR = os.getenv("LOG_CHANNEL_ID")
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
        "google_play": "",
        "upi": "",
        "ust": "",
        "btc": "",
        "others": ""
    },
    "no_compression_admin": True # Now defaults to True as per your new request.
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

# X (Twitter) Client
x_api = API()

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

scheduler = AsyncIOScheduler(timezone='UTC')

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
PREMIUM_PLATFORMS = ["instagram", "x"]

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
    
    if "x" in premium_platforms:
        upload_buttons_row.append(KeyboardButton("🐦 X ᴩᴏꜱᴛ"))

    if upload_buttons_row:
        buttons.insert(0, upload_buttons_row)

    buttons.append([KeyboardButton("⭐ ᴩʀᴇᴍɪᴜᴍ"), KeyboardButton("/premiumdetails")])
    if is_admin(user_id):
        buttons.append([KeyboardButton("🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ"), KeyboardButton("🔄 ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

def get_user_settings_markup(user_id):
    insta_buttons = [
        [InlineKeyboardButton("📝 ᴄᴀᴩᴛɪᴏɴ (ɪɢ)", callback_data="set_caption_instagram")],
        [InlineKeyboardButton("🏷️ ʜᴀꜱʜᴛᴀɢꜱ (ɪɢ)", callback_data="set_hashtags_instagram")],
        [InlineKeyboardButton("📐 ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ (ᴠɪᴅᴇᴏ)", callback_data="set_aspect_ratio_instagram")]
    ]
    x_buttons = [
        [InlineKeyboardButton("📝 ᴄᴀᴩᴛɪᴏɴ (X)", callback_data="set_caption_x")]
    ]
    
    account_buttons = [
        [InlineKeyboardButton("👤 ᴍᴀɴᴀɢᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀᴄᴄᴏᴜɴᴛꜱ", callback_data="manage_ig_accounts")],
        [InlineKeyboardButton("👤 ᴍᴀɴᴀɢᴇ X ᴀᴄᴄᴏᴜɴᴛꜱ", callback_data="manage_x_accounts")]
    ]
    
    markup = InlineKeyboardMarkup(insta_buttons + x_buttons + account_buttons + [[InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_main_menu")]])
    return markup

def get_insta_account_markup(user_id, logged_in_accounts):
    buttons = []
    for account in logged_in_accounts:
        is_active = user_states.get(user_id, {}).get("active_ig_username") == account
        emoji = "✅" if is_active else "⬜"
        buttons.append([InlineKeyboardButton(f"{emoji} @{account}", callback_data=f"select_ig_account_{account}")])
    buttons.append([InlineKeyboardButton("❌ ʟᴏɢᴏᴜᴛ ᴀᴄᴛɪᴠᴇ ᴀᴄᴄᴏᴜɴᴛ", callback_data="logout_ig_account")])
    buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(buttons)

def get_x_account_markup(user_id, logged_in_accounts):
    buttons = []
    for account in logged_in_accounts:
        is_active = user_states.get(user_id, {}).get("active_x_username") == account
        emoji = "✅" if is_active else "⬜"
        buttons.append([InlineKeyboardButton(f"{emoji} @{account}", callback_data=f"select_x_account_{account}")])
    buttons.append([InlineKeyboardButton("❌ ʟᴏɢᴏᴜᴛ ᴀᴄᴛɪᴠᴇ ᴀᴄᴄᴏᴜɴᴛ", callback_data="logout_x_account")])
    buttons.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="back_to_settings")])
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

def get_caption_markup(is_album=False, is_x=False, is_premium=True):
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

# === Helper Functions ===

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
    if db is None:
        return
    await asyncio.to_thread(
        db.sessions.update_one,
        {"user_id": user_id, "platform": platform, "username": username},
        {"$set": {"session_data": session_data}},
        upsert=True
    )

async def load_platform_sessions(user_id, platform):
    if db is None:
        return []
    sessions = await asyncio.to_thread(list, db.sessions.find({"user_id": user_id, "platform": platform}))
    return sessions

async def load_platform_session_data(user_id, platform, username):
    if db is None:
        return None
    session = await asyncio.to_thread(db.sessions.find_one, {"user_id": user_id, "platform": platform, "username": username})
    return session.get("session_data") if session else None

async def delete_platform_session(user_id, platform, username):
    if db is None:
        return
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
    
    if "aspect_ratio_instagram" not in settings:
        settings["aspect_ratio_instagram"] = "original"
    if "caption_instagram" not in settings:
        settings["caption_instagram"] = ""
    if "hashtags_instagram" not in settings:
        settings["hashtags_instagram"] = ""
    if "caption_x" not in settings:
        settings["caption_x"] = ""
    if "active_ig_username" not in settings:
        settings["active_ig_username"] = None
    if "active_x_username" not in settings:
        settings["active_x_username"] = None

    return settings

async def safe_edit_message(message, text, reply_markup=None, parse_mode=enums.ParseMode.MARKDOWN):
    try:
        if not message:
            logger.warning("safe_edit_message called with a None message object.")
            return

        current_text = getattr(message, 'text', '') or getattr(message, 'caption', '')
        if current_text and hasattr(current_text, 'strip') and current_text.strip() == text.strip():
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
            "current": current,
            "total": total,
            "ud_type": ud_type,
            "start_time": start_time,
            "now": now
        }

async def monitor_progress_task(chat_id, msg_id, progress_msg):
    try:
        while True:
            await asyncio.sleep(2)
            with threading.Lock():
                update_data = _progress_updates.get((chat_id, msg_id))

            if update_data:
                current, total, ud_type, start_time, now = (
                    update_data['current'],
                    update_data['total'],
                    update_data['ud_type'],
                    update_data['start_time'],
                    update_data['now']
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
                        progress_msg,
                        progress_text,
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

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"
    
    is_ig_premium = await is_premium_for_platform(user_id, "instagram")
    is_x_premium = await is_premium_for_platform(user_id, "x")

    premium_platforms = []
    if is_ig_premium: premium_platforms.append("instagram")
    if is_x_premium: premium_platforms.append("x")

    if is_admin(user_id):
        welcome_msg = "🤖 **ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴜᴩʟᴏᴀᴅ ʙᴏᴛ!**\n\n"
        welcome_msg += "🛠️ yᴏᴜ ʜᴀᴠᴇ **ᴀᴅᴍɪɴ ᴩʀɪᴠɪʟᴇɢᴇꜱ**."
        await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)
        return

    user = await _get_user_data(user_id)
    is_new_user = not user or "added_by" not in user
    if is_new_user:
        await _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.utcnow()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 ɴᴇᴡ ᴜꜱᴇʀ ꜱᴛᴀʀᴛᴇᴅ ʙᴏᴛ: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")

        welcome_msg = (
            f"👋 **ʜɪ {user_first_name}!**\n\n"
            "ᴛʜɪꜱ ʙᴏᴛ ʟᴇᴛꜱ yᴏᴜ ᴜᴩʟᴏᴀᴅ ᴀɴy ꜱɪᴢᴇ ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ & ᴩᴏꜱᴛꜱ ᴅɪʀᴇᴄᴛʟy ғʀᴏᴍ ᴛᴇʟᴇɢʀᴀᴍ.\n\n"
            "ᴛᴏ ɢᴇᴛ ᴀ ᴛᴀꜱᴛᴇ ᴏғ ᴛʜᴇ ᴩʀᴇᴍɪᴜᴍ ғᴇᴀᴛᴜʀᴇꜱ, yᴏᴜ ᴄᴀɴ ᴀᴄᴛɪᴠᴀᴛᴇ ᴀ **ғʀᴇᴇ 6-ʜᴏᴜʀ ᴛʀɪᴀʟ** ғᴏʀ ɪɴꜱᴛᴀɢʀᴀᴍ ʀɪɢʜᴛ ɴᴏᴡ!"
        )
        trial_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ᴀᴄᴛɪᴠᴀᴛᴇ ғʀᴇᴇ 6-ʜᴏᴜʀ", callback_data="activate_trial_instagram")],
            [InlineKeyboardButton("➡️ ᴩʀᴇᴍɪᴜᴍ", callback_data="buypypremium")]
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
    x_premium_data = user_premium.get("x", {})

    welcome_msg = f"🚀 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛᴇʟᴇɢʀᴀᴍ ➜ ᴅɪʀᴇᴄᴛ ᴜᴩʟᴏᴀᴅᴇʀ\n\n"
    premium_details_text = ""

    if is_ig_premium or is_x_premium:
        if is_ig_premium:
            ig_expiry = ig_premium_data.get("until")
            if ig_expiry:
                remaining_time = ig_expiry - datetime.utcnow()
                days = remaining_time.days
                hours = remaining_time.seconds // 3600
                premium_details_text += f"⭐ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ ᴇxᴩɪʀᴇꜱ ɪɴ: `{days} ᴅᴀyꜱ, {hours} ʜᴏᴜʀꜱ`.\n"
        if is_x_premium:
            x_expiry = x_premium_data.get("until")
            if x_expiry:
                remaining_time = x_expiry - datetime.utcnow()
                days = remaining_time.days
                hours = remaining_time.seconds // 3600
                premium_details_text += f"⭐ X ᴩʀᴇᴍɪᴜᴍ ᴇxᴩɪʀᴇꜱ ɪɴ: `{days} ᴅᴀyꜱ, {hours} ʜᴏᴜʀꜱ`.\n"
    else:
        premium_details_text = (
            "🔥 **ᴋᴇy ғᴇᴀᴛᴜʀᴇꜱ:**\n"
            "✅ ᴅɪʀᴇᴄᴛ ʟᴏɢɪɴ (ɴᴏ ᴛᴏᴋᴇᴇɴꜱ ɴᴇᴇᴅᴇᴅ)\n"
            "✅ ᴜʟᴛʀᴀ-ғᴀꜱᴛ ᴜᴩʟᴏᴀᴅɪɴɢ\n"
            "✅ ʜɪɢʜ ǫᴜᴀʟɪᴛy / ғᴀꜱᴛ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ\n"
            "✅ ɴᴏ ғɪʟᴇ ꜱɪᴢᴇ ʟɪᴍɪᴛ\n"
            "✅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴜᴩʟᴏᴀᴅꜱ\n"
            "✅ ɪɴꜱᴛᴀɢʀᴀᴍ & X ꜱᴜᴩᴩᴏʀᴛ\n\n"
            "👤 ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ ᴛᴏᴍ → [ᴄʟɪᴄᴋ ʜᴇʀᴇ](t.me/CjjTom) ᴛᴏ ɢᴇᴛ ᴩʀᴇᴍɪᴜᴍ ɴᴏᴡ\n"
            "🔐 yᴏᴜʀ ᴅᴀᴛᴀ ɪꜱ ғᴜʟʟy ✅ ᴇɴᴅ ᴛᴏ ᴇɴᴅ ᴇɴᴄʀyᴩᴛᴇᴅ\n\n"
            f"🆔 yᴏᴜʀ ɪᴅ: `{user_id}`"
        )

    welcome_msg += premium_details_text
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("restart") & filters.user(ADMIN_ID))
async def restart_cmd(_, msg):
    await restart_bot(msg)

@app.on_message(filters.regex("🔄 ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ") & filters.user(ADMIN_ID))
async def restart_button_handler(_, msg):
    await restart_bot(msg)

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    user_id = msg.from_user.id
    if not await is_premium_for_platform(user_id, "instagram") and not await is_premium_for_platform(user_id, "x"):
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. ᴩʟᴇᴀꜱᴇ ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴏʀ X ᴩʀᴇᴍɪᴜᴍ ᴡɪᴛʜ /buypypremium.")

    user_states[user_id] = {"action": "waiting_for_platform_login"}
    await msg.reply("👤 ᴩʟᴇᴀꜱᴇ ꜱᴇʟᴇᴄᴛ ᴛʜᴇ ᴩʟᴀᴛғᴏʀᴍ yᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʟᴏɢɪɴ ᴛᴏ:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Instagram", callback_data="login_platform_instagram")],
        [InlineKeyboardButton("🐦 X", callback_data="login_platform_x")]
    ]))

@app.on_message(filters.command("buypypremium"))
@app.on_message(filters.regex("⭐ ᴩʀᴇᴍɪᴜᴍ"))
async def show_premium_options(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    premium_plans_text = (
        "⭐ **ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ᴩʀᴇᴍɪᴜᴍ!** ⭐\n\n"
        "ᴜɴʟᴏᴄᴋ ғᴜʟʟ ғᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜᴩʟᴏᴀᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ.\n\n"
        "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩʟᴀɴꜱ:**"
    )
    await msg.reply(premium_plans_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    user = await _get_user_data(user_id)
    if not user:
        return await msg.reply("yᴏᴜ ᴀʀᴇ ɴᴏᴛ ʀᴇɢɪꜱᴛᴇʀᴇᴅ ᴡɪᴛʜ ᴛʜᴇ ʙᴏᴛ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ /start.")

    if is_admin(user_id):
        return await msg.reply("👑 yᴏᴜ ᴀʀᴇ ᴛʜᴇ **ᴀᴅᴍɪɴ**. yᴏᴜ ʜᴀᴠᴇ ᴩᴇʀᴍᴀɴᴇɴᴛ ғᴜʟʟ ᴀᴄᴄᴇꜱꜱ ᴛᴏ ᴀʟʟ ғᴇᴀᴛᴜʀᴇꜱ!", parse_mode=enums.ParseMode.MARKDOWN)

    status_text = "⭐ **yᴏᴜʀ ᴩʀᴇᴍɪᴜᴍ ꜱᴛᴀᴛᴜꜱ:**\n\n"
    has_premium_any = False

    for platform in PREMIUM_PLATFORMS:
        if await is_premium_for_platform(user_id, platform):
            has_premium_any = True
            platform_premium = user.get("premium", {}).get(platform, {})
            premium_type = platform_premium.get("type")
            premium_until = platform_premium.get("until")

            status_text += f"**{platform.capitalize()} ᴩʀᴇᴍɪᴜᴍ:** "
            if premium_type == "lifetime":
                status_text += "🎉 **ʟɪғᴇᴛɪᴍᴇ!**\n"
            elif premium_until:
                remaining_time = premium_until - datetime.utcnow()
                days = remaining_time.days
                hours = remaining_time.seconds // 3600
                minutes = (remaining_time.seconds % 3600) // 60
                status_text += (
                    f"`{premium_type.replace('_', ' ').title()}` ᴇxᴩɪʀᴇꜱ ᴏɴ: "
                    f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`\n"
                    f"ᴛɪᴍᴇ ʀᴇᴍᴀɪɴɪɴɢ: `{days} ᴅᴀyꜱ, {hours} ʜᴏᴜʀꜱ, {minutes} ᴍɪɴᴜᴛᴇꜱ`\n"
                )
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
    if db is not None:
        await asyncio.to_thread(db.users.delete_one, {"_id": user_id})
        await asyncio.to_thread(db.settings.delete_one, {"_id": user_id})
        await asyncio.to_thread(db.sessions.delete_many, {"user_id": user_id})

    if user_id in user_states:
        del user_states[user_id]

    await query.answer("✅ yᴏᴜʀ ᴩʀᴏғɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ʀᴇꜱᴇᴛ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ /start ᴛᴏ ʙᴇɢɪɴ ᴀɢᴀɪɴ.", show_alert=True)
    await safe_edit_message(query.message, "✅ yᴏᴜʀ ᴩʀᴏғɪʟᴇ ʜᴀꜱ ʙᴇᴇɴ ʀᴇꜱᴇᴛ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ /start ᴛᴏ ʙᴇɢɪɴ ᴀɢᴀɪɴ.")

@app.on_message(filters.regex("⚙️ ꜱᴇᴛᴛɪɴɢꜱ"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("👤 Personal Settings", callback_data="user_settings_personal")]
        ])
        await msg.reply("👑 Admin, please choose which settings panel you'd like to access:", reply_markup=markup)
        return

    if await is_premium_for_platform(user_id, "instagram") or await is_premium_for_platform(user_id, "x"):
        await msg.reply(
            "⚙️ Welcome to your settings panel. Use the buttons below to adjust your preferences.",
            reply_markup=get_user_settings_markup(user_id)
        )
    else:
        return await msg.reply("❌ Premium access is required to access settings. Use /buypypremium to upgrade.")

@app.on_message(filters.regex("🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ") & filters.user(ADMIN_ID))
async def admin_panel_button_handler(_, msg):
    await msg.reply(
        "🛠 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛʜᴇ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ!\n\n"
        "ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ᴍᴀɴᴀɢᴇ ᴛʜᴇ ʙᴏᴛ.",
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_message(filters.regex("📤 ɪɴꜱᴛᴀ ʀᴇᴇʟ"))
@with_user_lock
async def initiate_instagram_reel_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ yᴏᴜʀ ᴀᴄᴄᴇꜱꜱ ʜᴀꜱ ʙᴇᴇɴ ᴅᴇɴɪᴇᴅ. ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʀᴇᴍɪᴜᴍ ᴛᴏ ᴜɴʟᴏᴄᴋ ʀᴇᴇʟꜱ ᴜᴩʟᴏᴀᴅ. /buypypremium.")

    sessions = await load_platform_sessions(user_id, "instagram")
    if not sessions:
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ ꜱᴇɴᴅ ᴠɪᴅᴇᴏ ғɪʟᴇ - ʀᴇᴇʟ ʀᴇᴀᴅy!!")
    user_states[user_id] = {"action": "waiting_for_instagram_reel_video", "platform": "instagram", "upload_type": "reel"}

@app.on_message(filters.regex("📸 ɪɴꜱᴛᴀ ᴩʜᴏᴛᴏ"))
@with_user_lock
async def initiate_instagram_photo_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("🚫 ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴜᴩʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴩʜᴏᴛᴏꜱ ᴩʟᴇᴀꜱᴇ ᴜᴩɢʀᴀᴅᴇ ᴩʀᴇᴍɪᴜᴍ /buypypremium.")

    sessions = await load_platform_sessions(user_id, "instagram")
    if not sessions:
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ ꜱᴇɴᴅ ᴩʜᴏᴛᴏ ғɪʟᴇ - ʀᴇᴀᴅy ғᴏʀ ɪɢ!.")
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
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)
    
    user_states[user_id] = {
        "action": "waiting_for_album_media",
        "platform": "instagram",
        "upload_type": "album",
        "media_paths": []
    }
    await msg.reply(
        "🗂️ **Album Mode**\n\n"
        "ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ʏᴏᴜʀ ᴩʜᴏᴛᴏꜱ ᴀɴᴅ ᴠɪᴅᴇᴏꜱ (ᴜᴩ ᴛᴏ 10). "
        "ᴏɴᴄᴇ ʏᴏᴜ ᴀʀᴇ ᴅᴏɴᴇ, ꜱᴇɴᴅ ᴛʜᴇ `/done` ᴄᴏᴍᴍᴀɴᴅ ᴛᴏ ᴄᴏɴᴛɪɴᴜᴇ."
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
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ ɪɴꜱᴛᴀɢʀᴀᴍ ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("⚡ ꜱᴇɴᴅ ᴀ ᴩʜᴏᴛᴏ ᴏʀ ᴠɪᴅᴇᴏ ғɪʟᴇ ғᴏʀ yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴛᴏʀy.")
    user_states[user_id] = {"action": "waiting_for_instagram_story", "platform": "instagram", "upload_type": "story"}

@app.on_message(filters.regex("🐦 X ᴩᴏꜱᴛ"))
@with_user_lock
async def initiate_x_post(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    if not await is_premium_for_platform(user_id, "x"):
        return await msg.reply("❌ X uploads are a Premium feature. Please upgrade with /buypypremium.")

    sessions = await load_platform_sessions(user_id, "x")
    if not sessions:
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ʟᴏɢɪɴ ᴛᴏ X ғɪʀꜱᴛ ᴜꜱɪɴɢ `/login`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("🐦 ꜱᴇɴᴅ yᴏᴜʀ ᴍᴇᴅɪᴀ ғᴏʀ ᴛʜᴇ X ᴩᴏꜱᴛ, ᴛʜᴇɴ ꜱᴇɴᴅ yᴏᴜʀ ᴛᴇxᴛ. (Photos/Videos/GIFs supported)")
    user_states[user_id] = {"action": "waiting_for_x_media", "platform": "x", "upload_type": "post", "media_paths": []}


@app.on_message(filters.regex("📊 ꜱᴛᴀᴛꜱ"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    
    if db is None:
        return await msg.reply("⚠️ Database is currently unavailable. Stats cannot be retrieved.")

    is_any_premium = any([await is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS])
    if not is_admin(user_id) and not is_any_premium:
        return await msg.reply("❌ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ. yᴏᴜ ɴᴇᴇᴅ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ғᴏʀ ᴀᴛ ʟᴇᴀꜱᴛ ᴏɴᴇ ᴩʟᴀᴛғᴏʀᴍ ᴛᴏ ᴠɪᴇᴡ ꜱᴛᴀᴛꜱ.")

    total_users = await asyncio.to_thread(db.users.count_documents, {})
    premium_counts = {platform: 0 for platform in PREMIUM_PLATFORMS}
    total_premium_users = 0
    
    pipeline = [
        {"$project": {
            "is_premium": {
                "$anyElementTrue": [
                    {"$or": [
                        {"$eq": [f"$premium.{p}.type", "lifetime"]},
                        {"$gt": [f"$premium.{p}.until", datetime.utcnow()]}
                    ]} for p in PREMIUM_PLATFORMS
                ]
            },
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

    result = await asyncio.to_thread(list, db.users.aggregate(pipeline))
    if result:
        total_premium_users = result[0].get('total_premium', 0)
        for p in PREMIUM_PLATFORMS:
            premium_counts[p] = result[0].get(f'{p}_premium', 0)

    total_uploads = await asyncio.to_thread(db.uploads.count_documents, {})
    
    stats_text = (
        "📊 **ʙᴏᴛ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ:**\n\n"
        f"**ᴜꜱᴇʀꜱ**\n"
        f"👥 ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ: `{total_users}`\n"
        f"👑 ᴀᴅᴍɪɴ ᴜꜱᴇʀꜱ: `{await asyncio.to_thread(db.users.count_documents, {'_id': ADMIN_ID})}`\n"
        f"⭐ ᴩʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ: `{total_premium_users}` (`{total_premium_users / total_users * 100 if total_users > 0 else 0:.2f}%`)\n"
    )
    for p in PREMIUM_PLATFORMS:
        stats_text += f"      - {p.capitalize()} Premium: `{premium_counts[p]}` (`{premium_counts[p] / total_users * 100 if total_users > 0 else 0:.2f}%`)\n"

    stats_text += (
        f"\n**ᴜᴩʟᴏᴀᴅꜱ**\n"
        f"📈 ᴛᴏᴛᴀʟ ᴜᴩʟᴏᴀᴅꜱ: `{total_uploads}`\n"
        f"🎬 ɪɴꜱᴛᴀɢʀᴀᴍ ʀᴇᴇʟꜱ: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'reel'})}`\n"
        f"📸 ɪɴꜱᴛᴀɢʀᴀᴍ ᴩᴏꜱᴛꜱ: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'post'})}`\n"
        f"⚡ ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴛᴏʀy: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'story'})}`\n"
        f"🗂️ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀʟʙᴜᴍꜱ: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'instagram', 'upload_type': 'album'})}`\n"
        f"🐦 X ᴩᴏꜱᴛꜱ: `{await asyncio.to_thread(db.uploads.count_documents, {'platform': 'x'})}`\n"
    )
    await msg.reply(stats_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_cmd(_, msg):
    if db is None:
        return await msg.reply("⚠️ Database is unavailable. Cannot fetch user list for broadcast.")
        
    if len(msg.text.split(maxsplit=1)) < 2:
        return await msg.reply("ᴜꜱᴀɢᴇ: `/broadcast <your message>`", parse_mode=enums.ParseMode.MARKDOWN)
    broadcast_message = msg.text.split(maxsplit=1)[1]
    users_cursor = await asyncio.to_thread(db.users.find, {})
    users = await asyncio.to_thread(list, users_cursor)
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

@app.on_message(filters.text & filters.private & ~filters.command(""))
@with_user_lock
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not state_data:
        return await msg.reply("ɪ ᴅᴏɴ'ᴛ ᴜɴᴅᴇʀꜱᴛᴀɴᴅ ᴛʜᴀᴛ ᴄᴏᴍᴍᴀɴᴅ. ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ ᴛʜᴇ ᴍᴇɴᴜ ʙᴜᴛᴛᴏɴꜱ ᴛᴏ ɪɴᴛᴇʀᴀᴄᴛ ᴡɪᴛʜ ᴍᴇ.")
    
    action = state_data.get("action")

    if action == "waiting_for_instagram_username":
        user_states[user_id]["username"] = msg.text
        user_states[user_id]["action"] = "waiting_for_instagram_password"
        return await msg.reply("🔑 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ **ᴩᴀꜱꜱᴡᴏʀᴅ**.")

    elif action == "waiting_for_instagram_password":
        username = user_states[user_id]["username"]
        password = msg.text
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
                await save_platform_session(user_id, "instagram", session_data, username)
                
                user_settings = await get_user_settings(user_id)
                user_settings["active_ig_username"] = username
                await save_user_settings(user_id, user_settings)

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
            finally:
                if user_id in user_states:
                    del user_states[user_id]

        task_tracker.create_task(safe_task_wrapper(login_task()), user_id=user_id, task_name="login_instagram")
        return

    elif action == "waiting_for_x_username":
        user_states[user_id]["username"] = msg.text
        user_states[user_id]["action"] = "waiting_for_x_password"
        return await msg.reply("🔑 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ X **ᴩᴀꜱꜱᴡᴏʀᴅ**.")
    
    elif action == "waiting_for_x_password":
        username = user_states[user_id]["username"]
        password = msg.text
        login_msg = await msg.reply("🔐 ᴀᴛᴛᴇᴍᴩᴛɪɴɢ X ʟᴏɢɪɴ...")

        async def login_task():
            try:
                # The twscrape library handles session management internally
                await asyncio.to_thread(x_api.add_account, username, password)
                
                user_settings = await get_user_settings(user_id)
                user_settings["active_x_username"] = username
                await save_user_settings(user_id, user_settings)
                
                await safe_edit_message(login_msg, "✅ X ʟᴏɢɪɴ ꜱᴜᴄᴄᴇꜱꜱғᴜʟ!")
                await send_log_to_channel(app, LOG_CHANNEL,
                    f"📝 ɴᴇᴡ X ʟᴏɢɪɴ\nᴜꜱᴇʀ: `{user_id}`\n"
                    f"ᴜꜱᴇʀɴᴀᴍᴇ: `{msg.from_user.username or 'N/A'}`\n"
                    f"X: `{username}`"
                )
                logger.info(f"X login successful for user {user_id} ({username}).")
            except Exception as e:
                await safe_edit_message(login_msg, f"❌ X ʟᴏɢɪɴ ғᴀɪʟᴇᴅ: {str(e)}. ᴩʟᴇᴀꜱᴇ ᴛʀy ᴀɢᴀɪɴ.")
                logger.error(f"X login failed for user {user_id} ({username}): {str(e)}")
            finally:
                if user_id in user_states:
                    del user_states[user_id]
        
        task_tracker.create_task(safe_task_wrapper(login_task()), user_id=user_id, task_name="login_x")

    elif action in ["waiting_for_caption_instagram", "waiting_for_caption_x"]:
        platform = "instagram" if action == "waiting_for_caption_instagram" else "x"
        caption = msg.text
        
        settings = await get_user_settings(user_id)
        settings[f"caption_{platform}"] = caption
        await save_user_settings(user_id, settings)
        
        await safe_edit_message(msg.reply_to_message, f"✅ Default caption for {platform.capitalize()} set to: `{caption}`", reply_markup=get_user_settings_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states:
            del user_states[user_id]
    
    elif action in ["waiting_for_hashtags_instagram"]:
        hashtags = msg.text
        settings = await get_user_settings(user_id)
        settings[f"hashtags_instagram"] = hashtags
        await save_user_settings(user_id, settings)
        await safe_edit_message(msg.reply_to_message, f"✅ Hashtags for Instagram set to: `{hashtags}`", reply_markup=get_user_settings_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states:
            del user_states[user_id]

    elif action == "waiting_for_caption":
        is_premium = await is_premium_for_platform(user_id, state_data["platform"])
        caption = msg.text
        if not is_premium and len(caption) > 280:
             return await msg.reply("❌ For free accounts, the caption limit is 280 characters. Please try a shorter caption.")
        
        file_info = state_data.get("file_info", {})
        file_info["custom_caption"] = caption
        state_data["file_info"] = file_info
        
        await safe_edit_message(msg.reply_to_message, f"📝 **Caption Set**\n\n`{caption}`\n\nWhat's next?", 
                                reply_markup=get_caption_markup(is_album=state_data['upload_type'] == 'album', is_x=(state_data['platform']=='x'), is_premium=is_premium), parse_mode=enums.ParseMode.MARKDOWN)
        state_data['action'] = "caption_set_waiting_for_options"
        user_states[user_id] = state_data

    elif action == "waiting_for_usertags_insta":
        file_info = state_data.get("file_info", {})
        usernames = [u.strip().replace("@", "") for u in msg.text.split(",") if u.strip()]
        file_info["usertags"] = usernames
        state_data["file_info"] = file_info
        await safe_edit_message(msg.reply_to_message, f"👥 **Users to tag:** `{', '.join(usernames)}`\n\nContinue with other options or upload now.",
                                reply_markup=get_caption_markup(is_album=state_data['upload_type'] == 'album'))
        state_data['action'] = "caption_set_waiting_for_options"
        user_states[user_id] = state_data

    elif action == "waiting_for_location_search_insta":
        file_info = state_data.get("file_info", {})
        location_search_term = msg.text
        await safe_edit_message(msg.reply_to_message, f"Searching for location: `{location_search_term}`...")
        
        async def search_location_task():
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            user_settings = await get_user_settings(user_id)
            active_username = user_settings.get("active_ig_username")
            session = await load_platform_session_data(user_id, "instagram", active_username)
            if not session:
                return await safe_edit_message(msg.reply_to_message, "❌ ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴩɪʀᴇᴅ. ᴩʟᴇᴀꜱᴇ `/login` ᴀɢᴀɪɴ.")
            user_upload_client.set_settings(session)
            
            try:
                locations = await asyncio.to_thread(user_upload_client.location_search, location_search_term)
                if not locations:
                    await safe_edit_message(msg.reply_to_message, f"📍 No locations found for `{location_search_term}`. Please try a different search term or cancel.", reply_markup=get_caption_markup())
                    user_states[user_id]["action"] = "waiting_for_location_search_insta"
                    return

                location_buttons = []
                for loc in locations[:5]:
                    location_buttons.append([InlineKeyboardButton(f"{loc.name} ({loc.address})", callback_data=f"select_location_{loc.pk}")])
                
                location_buttons.append([InlineKeyboardButton("❌ Cancel Location", callback_data="cancel_location_insta")])
                
                await safe_edit_message(msg.reply_to_message, "📍 **Select a location:**", reply_markup=InlineKeyboardMarkup(location_buttons))
                user_states[user_id]['action'] = "selecting_location_insta"
                user_states[user_id]['location_choices'] = {loc.pk: loc for loc in locations}

            except Exception as e:
                await safe_edit_message(msg.reply_to_message, f"❌ An error occurred while searching for locations: {str(e)}")
                user_states[user_id]['action'] = "caption_set_waiting_for_options"
                return

        task_tracker.create_task(safe_task_wrapper(search_location_task()), user_id=user_id, task_name="location_search")

    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}, "mode": "admin_add_premium"}
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
            await _update_global_setting("max_concurrent_uploads", new_limit)
            global upload_semaphore
            upload_semaphore = asyncio.Semaphore(new_limit)
            await msg.reply(f"✅ ᴍᴀxɪᴍᴜᴍ ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴩʟᴏᴀᴅꜱ ꜱᴇᴛ ᴛᴏ `{new_limit}`.", reply_markup=get_admin_global_settings_markup(), parse_mode=enums.ParseMode.MARKDOWN)
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
            await _update_global_setting("proxy_url", "")
            await msg.reply("✅ ʙᴏᴛ ᴩʀᴏxʏ ʜᴀꜱ ʙᴇᴇɴ ʀᴇᴍᴏᴠᴇᴅ.")
            logger.info(f"Admin {user_id} removed the global proxy.")
        else:
            await _update_global_setting("proxy_url", proxy_url)
            await msg.reply(f"✅ ʙᴏᴛ ᴩʀᴏxʏ ꜱᴇᴛ ᴛᴏ: `{proxy_url}`.")
            logger.info(f"Admin {user_id} set the global proxy to: {proxy_url}")
        if user_id in user_states:
            del user_states[user_id]
        if msg.reply_to_message:
            await safe_edit_message(msg.reply_to_message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=get_admin_global_settings_markup())

    elif isinstance(state_data, dict) and state_data.get("action") in ["waiting_for_event_title", "waiting_for_event_message"]:
        if not is_admin(user_id): return
        setting_key = "special_event_title" if action == "waiting_for_event_title" else "special_event_message"
        await _update_global_setting(setting_key, msg.text)
        await msg.reply(f"✅ Special event `{setting_key.split('_')[-1]}` has been updated!", reply_markup=get_admin_global_settings_markup())
        if user_id in user_states:
            del user_states[user_id]
    
    elif isinstance(state_data, dict) and state_data.get("action").startswith("waiting_for_payment_details_"):
        if not is_admin(user_id):
            return await msg.reply("❌ yᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴩᴇʀғᴏʀᴍ ᴛʜɪꜱ ᴀᴄᴛɪᴏɴ.")

        payment_method = action.replace("waiting_for_payment_details_", "")
        details = msg.text

        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings[payment_method] = details
        await _update_global_setting("payment_settings", new_payment_settings)

        await msg.reply(f"✅ ᴩᴀyᴍᴇɴᴛ ᴅᴇᴛᴀɪʟꜱ ғᴏʀ **{payment_method.upper()}** ᴜᴩᴅᴀᴛᴇᴅ.", reply_markup=payment_settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        if user_id in user_states:
            del user_states[user_id]

# === Callback Query Handlers ===

@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def personal_settings_hub_cb(_, query):
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ This is an admin-only button.", show_alert=True)
    
    await safe_edit_message(
        query.message,
        "⚙️ Welcome to your settings panel. Use the buttons below to adjust your preferences.",
        reply_markup=get_user_settings_markup(user_id)
    )

@app.on_callback_query(filters.regex("^cancel_upload$"))
async def cancel_upload_cb(_, query):
    user_id = query.from_user.id
    await query.answer("Upload cancelled.", show_alert=True)
    await safe_edit_message(query.message, "❌ **Upload Cancelled**\n\nYour operation has been successfully cancelled.")

    state_data = user_states.get(user_id, {})
    files_to_clean = []
    if "media_paths" in state_data:
        files_to_clean.extend(state_data["media_paths"])
    else:
        file_info = state_data.get("file_info", {})
        files_to_clean.append(file_info.get("downloaded_path"))
    
    cleanup_temp_files(files_to_clean)
    
    if user_id in user_states:
        del user_states[user_id]
    
    await task_tracker.cancel_all_user_tasks(user_id)
    logger.info(f"User {user_id} cancelled their upload.")

@app.on_callback_query(filters.regex("^skip_caption$"))
async def skip_caption_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    if not state_data or "file_info" not in state_data:
        return await query.answer("❌ Error: No upload process found to continue.", show_alert=True)

    await query.answer("✅ Using default caption...")
    file_info = state_data["file_info"]
    file_info["custom_caption"] = None
    
    if state_data['upload_type'] == 'album':
        return await query.message.reply("✅ Using default caption. Ready to upload album.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 ᴜᴩʟᴏᴀᴅ ɴᴏᴡ", callback_data="upload_now")]]))

    await safe_edit_message(query.message, "🚀 Preparing to upload with default caption...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 ᴜᴩʟᴏᴀᴅ ɴᴏᴡ", callback_data="upload_now")]]))
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
    
    if not state_data or state_data.get('action') != 'waiting_for_album_media':
        return await query.answer("❌ Error: Not in the right state to finalize album upload.", show_alert=True)

    media_paths = state_data.get('media_paths', [])
    if len(media_paths) < 1:
        return await query.answer("❌ Please send at least one media file for the album.", show_alert=True)

    file_info = {
        "platform": "instagram",
        "upload_type": "album",
        "media_paths": media_paths,
        "processing_msg": query.message
    }
    user_states[user_id] = {"action": "waiting_for_caption", "file_info": file_info}
    await safe_edit_message(
        query.message,
        "✅ Album files received. What caption do you want for your album?",
        reply_markup=get_caption_markup(is_album=True, is_premium=await is_premium_for_platform(user_id, "instagram")),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^tag_users_insta$"))
async def tag_users_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    is_premium = await is_premium_for_platform(user_id, "instagram")
    if not is_premium:
        return await query.answer("❌ This is a premium feature. Please upgrade to use it.", show_alert=True)

    if not state_data or state_data.get('action') != 'caption_set_waiting_for_options':
        return await query.answer("❌ Error: Please provide a caption first.", show_alert=True)
    
    user_states[user_id]['action'] = 'waiting_for_usertags_insta'
    await safe_edit_message(
        query.message,
        "👥 Please send a comma-separated list of Instagram usernames to tag (e.g., `user1, user2, user3`)."
    )

@app.on_callback_query(filters.regex("^add_location_insta$"))
async def add_location_cb(_, query):
    user_id = query.from_user.id
    state_data = user_states.get(user_id)
    is_premium = await is_premium_for_platform(user_id, "instagram")
    if not is_premium:
        return await query.answer("❌ This is a premium feature. Please upgrade to use it.", show_alert=True)

    if not state_data or state_data.get('action') not in ['caption_set_waiting_for_options', 'waiting_for_location_search_insta']:
        return await query.answer("❌ Error: Please provide a caption first.", show_alert=True)

    user_states[user_id]['action'] = 'waiting_for_location_search_insta'
    await safe_edit_message(
        query.message,
        "📍 Please send the name of the location you want to tag (e.g., `New York, New York`)."
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
    if "location" in file_info:
        del file_info["location"]
    
    await safe_edit_message(
        query.message,
        "📍 Location tagging cancelled. What's next?",
        reply_markup=get_caption_markup(is_album=state_data['upload_type'] == 'album')
    )
    state_data['action'] = 'caption_set_waiting_for_options'
    user_states[user_id] = state_data

@app.on_callback_query(filters.regex("^buypypremium$"))
async def buypypremium_cb(_, query):
    user_id = query.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if user_id in user_states and user_states[user_id].get("mode") == "admin_add_premium":
        del user_states[user_id]

    premium_plans_text = (
        "⭐ **ᴜᴩɢʀᴀᴅᴇ ᴛᴏ ᴩʀᴇᴍɪᴜᴍ!** ⭐\n\n"
        "ᴜɴʟᴏᴄᴋ ғᴜʟʟ ғᴇᴀᴛᴜʀᴇꜱ ᴀɴᴅ ᴜᴩʟᴏᴀᴅ ᴜɴʟɪᴍɪᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ ᴡɪᴛʜᴏᴜᴛ ʀᴇꜱᴛʀɪᴄᴛɪᴏɴꜱ.\n\n"
        "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩʟᴀɴꜱ:**"
    )
    await safe_edit_message(query.message, premium_plans_text, reply_markup=get_premium_plan_markup(user_id), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_plan_details_"))
async def show_plan_details_cb(_, query):
    user_id = query.from_user.id
    plan_key = query.data.split("show_plan_details_")[1]

    state_data = user_states.get(user_id, {})
    is_admin_adding_premium = (
        is_admin(user_id) and
        state_data.get("action") == "select_premium_plan_for_platforms"
    )

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

    plan_text += f"**ᴩʀɪᴄᴇ**: {price_string}\n\n"
    if is_admin_adding_premium:
        target_user_id = state_data.get('target_user_id', 'Unknown User')
        plan_text += f"Click below to grant this plan to user `{target_user_id}`."
    else:
        plan_text += "ᴛᴏ ᴩᴜʀᴄʜᴀꜱᴇ, ᴄʟɪᴄᴋ 'ʙᴜy ɴᴏᴡ' ᴏʀ ᴄʜᴇᴄᴋ ᴛʜᴇ ᴀᴠᴀɪʟᴀʙʟᴇ ᴩᴀyᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ."

    await safe_edit_message(
        query.message,
        plan_text,
        reply_markup=get_premium_details_markup(plan_key, is_admin_flow=is_admin_adding_premium),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^show_payment_methods$"))
async def show_payment_methods_cb(_, query):
    payment_methods_text = "**ᴀᴠᴀɪʟᴀʙʟᴇ ᴩᴀyᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅꜱ**\n\n"
    payment_methods_text += "ᴄʜᴏᴏꜱᴇ yᴏᴜʀ ᴩʀᴇғᴇʀʀᴇᴅ ᴍᴇᴛʜᴏᴅ ᴛᴏ ᴩʀᴏᴄᴇᴇᴅ ᴡɪᴛʜ ᴩᴀyᴍᴇɴᴛ."

    await safe_edit_message(query.message, payment_methods_text, reply_markup=get_payment_methods_markup(), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^show_payment_qr_google_play$"))
async def show_payment_qr_google_play_cb(_, query):
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
    text = (
        f"**ᴩᴜʀᴄʜᴀꜱᴇ ᴄᴏɴғɪʀᴍᴀᴛɪᴏɴ**\n\n"
        f"ᴩʟᴇᴀꜱᴇ ᴄᴏɴᴛᴀᴄᴛ **[ᴀᴅᴍɪɴ ᴛᴏᴍ](https://t.me/CjjTom)** ᴛᴏ ᴄᴏᴍᴩʟᴇᴛᴇ ᴛʜᴇ ᴩᴀyᴍᴇɴᴛ ᴩʀᴏᴄᴇꜱꜱ."
    )
    await safe_edit_message(query.message, text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^premiumdetails$"))
async def premium_details_cb(_, query):
    await query.message.reply("ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ ᴛʜᴇ `/premiumdetails` ᴄᴏᴍᴍᴀɴᴅ ɪɴꜱᴛᴇᴀᴅ ᴏғ ᴛʜɪꜱ ʙᴜᴛᴛᴏɴ.")

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

    event_status = "ON" if global_settings.get("special_event_toggle") else "OFF"
    max_uploads = global_settings.get("max_concurrent_uploads")
    proxy_url = global_settings.get("proxy_url")
    proxy_status_text = f"`{proxy_url}`" if proxy_url else "ɴᴏɴᴇ"
    compression_status = "ᴅɪꜱᴀʙʟᴇᴅ" if global_settings.get("no_compression_admin") else "ᴇɴᴀʙʟᴇᴅ"

    settings_text = (
        "⚙️ **ɢʟᴏʙᴀʟ ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ**\n\n"
        f"**📢 Special Event:** `{event_status}`\n"
        f"**✏️ Event Title:** `{global_settings.get('special_event_title')}`\n"
        f"**💬 Event Message:** `{global_settings.get('special_event_message')}`\n\n"
        f"**ᴍᴀx ᴄᴏɴᴄᴜʀʀᴇɴᴛ ᴜᴩʟᴏᴀᴅꜱ:** `{max_uploads}`\n"
        f"**ɢʟᴏʙᴀʟ ᴩʀᴏxʏ:** {proxy_status_text}\n"
        f"**ɢʟᴏʙᴀʟ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ:** `{compression_status}`\n"
    )

    await safe_edit_message(query.message, settings_text, reply_markup=get_admin_global_settings_markup(), parse_mode=enums.ParseMode.MARKDOWN)

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
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    await task_tracker.cancel_all_user_tasks(user_id)

    if user_id in user_states:
        del user_states[user_id]

    if data == "back_to_main_menu":
        try:
            await query.message.delete()
        except Exception:
            pass
        
        is_ig_premium = await is_premium_for_platform(user_id, "instagram")
        is_x_premium = await is_premium_for_platform(user_id, "x")
        premium_platforms = []
        if is_ig_premium: premium_platforms.append("instagram")
        if is_x_premium: premium_platforms.append("x")

        await app.send_message(
            query.message.chat.id,
            "🏠 ᴍᴀɪɴ ᴍᴇɴᴜ",
            reply_markup=get_main_keyboard(user_id, premium_platforms)
        )
    elif data == "back_to_settings":
        await safe_edit_message(
            query.message,
            "⚙️ Welcome to your settings panel. Use the buttons below to adjust your preferences.",
            reply_markup=get_user_settings_markup(user_id)
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
    elif data == "back_to_global":
        await global_settings_panel_cb(_, query)
    else:
        await query.answer("❌ ᴜɴᴋɴᴏᴡɴ ʙᴀᴄᴋ ᴀᴄᴛɪᴏɴ", show_alert=True)

@app.on_callback_query(filters.regex("^activate_trial_instagram$"))
async def activate_trial_instagram_cb(_, query):
    user_id = query.from_user.id
    user_first_name = query.from_user.first_name or "there"

    if await is_premium_for_platform(user_id, "instagram"):
        await query.answer("yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ ɪꜱ ᴀʟʀᴇᴀᴅy ᴀᴄᴛɪᴠᴇ! ᴇɴᴊᴏy yᴏᴜʀ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ.", show_alert=True)
        is_ig_premium = await is_premium_for_platform(user_id, "instagram")
        is_x_premium = await is_premium_for_platform(user_id, "x")
        premium_platforms = []
        if is_ig_premium: premium_platforms.append("instagram")
        if is_x_premium: premium_platforms.append("x")
        await safe_edit_message(query.message, "Welcome back!", reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)
        return

    trial_duration = timedelta(hours=6)
    premium_until = datetime.utcnow() + trial_duration
    
    user_premium_data = (await _get_user_data(user_id)).get("premium", {})
    user_premium_data["instagram"] = {
        "type": "6_hour_trial",
        "added_by": "callback_trial",
        "added_at": datetime.utcnow(),
        "until": premium_until
    }
    await _save_user_data(user_id, {"premium": user_premium_data})

    logger.info(f"User {user_id} activated a 6-hour Instagram trial.")
    await send_log_to_channel(app, LOG_CHANNEL, f"✨ ᴜꜱᴇʀ `{user_id}` ᴀᴄᴛɪᴠᴀᴛᴇᴅ ᴀ 6-ʜᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ.")

    await query.answer("✅ ғʀᴇᴇ 6-ʜᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴛʀɪᴀʟ ᴀᴄᴛɪᴠᴀᴛᴇᴅ! ᴇɴᴊᴏy!", show_alert=True)
    welcome_msg = (
        f"🎉 **ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ, {user_first_name}!**\n\n"
        f"yᴏᴜ ʜᴀᴠᴇ ᴀᴄᴛɪᴠᴀᴛᴇᴅ yᴏᴜʀ **6-ʜᴏᴜʀ ᴩʀᴇᴍɪᴜᴍ ᴛʀɪᴀʟ** ғᴏʀ **ɪɴꜱᴛᴀɢʀᴀᴍ**.\n\n"
        "yᴏᴜ ɴᴏᴡ ʜᴀᴠᴇ ᴀᴄᴄᴇꜱꜱ ᴛᴏ ᴜᴩʟᴏᴀᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴄᴏɴᴛᴇɴᴛ!\n\n"
        "ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ, ᴩʟᴇᴀꜱᴇ ʟᴏɢ ɪɴ ᴛᴏ yᴏᴜʀ ɪɴꜱᴛᴀɢʀᴀᴍ ᴀᴄᴄᴏᴜɴᴛ ᴡɪᴛʜ:\n"
        "`/login`\n\n"
        "ᴡᴀɴᴛ ᴍᴏʀᴇ ғᴇᴀᴛᴜʀᴇꜱ ᴀғᴛᴇʀ ᴛʜᴇ ᴛʀɪᴀʟ ᴇɴᴅꜱ? ᴄʜᴇᴄᴋ ᴏᴜᴛ ᴏᴜʀ ᴩᴀɪᴅ ᴩʟᴀɴꜱ ᴡɪᴛʜ /buypypremium."
    )
    is_ig_premium = await is_premium_for_platform(user_id, "instagram")
    is_x_premium = await is_premium_for_platform(user_id, "x")
    premium_platforms = []
    if is_ig_premium: premium_platforms.append("instagram")
    if is_x_premium: premium_platforms.append("x")
    await safe_edit_message(query.message, welcome_msg, reply_markup=get_main_keyboard(user_id, premium_platforms), parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^toggle_special_event$"))
async def toggle_special_event_cb(_, query):
    if not is_admin(query.from_user.id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
    
    current_status = global_settings.get("special_event_toggle", False)
    new_status = not current_status
    await _update_global_setting("special_event_toggle", new_status)
    
    status_text = "ON" if new_status else "OFF"
    await query.answer(f"Special Event toggled {status_text}.", show_alert=True)
    
    await global_settings_panel_cb(_, query)

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
    user_id = query.from_user.id
    if not is_admin(user_id):
        return await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)

    current_status = global_settings.get("no_compression_admin", False)
    new_status = not current_status
    await _update_global_setting("no_compression_admin", new_status)
    status_text = "ᴅɪꜱᴀʙʟᴇᴅ" if new_status else "ᴇɴᴀʙʟᴇᴅ"

    await query.answer(f"ɢʟᴏʙᴀʟ ᴄᴏᴍᴩʀᴇꜱꜱɪᴏɴ ᴛᴏɢɢʟᴇᴅ ᴛᴏ: {status_text}.", show_alert=True)
    await global_settings_panel_cb(_, query)

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
    
    if db is None:
        return await query.answer("⚠️ Database is unavailable. Cannot reset stats.", show_alert=True)

    result_uploads = await asyncio.to_thread(db.uploads.delete_many, {})
    await query.answer(f"✅ ᴀʟʟ ꜱᴛᴀᴛꜱ ʀᴇꜱᴇᴛ! Deleted {result_uploads.deleted_count} uploads.", show_alert=True)
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
                        f"      - **ɢᴩᴜ {i}:** `{gpu.name}`\n"
                        f"      - ʟᴏᴀᴅ: `{gpu.load*100:.1f}%`\n"
                        f"      - ᴍᴇᴍᴏʀy: `{gpu.memoryUsed}/{gpu.memoryTotal}` ᴍʙ\n"
                        f"      - ᴛᴇᴍᴩ: `{gpu.temperature}°ᴄ`\n"
                    )
            else:
                gpu_info = "ɴᴏ ɢᴩᴜ ғᴏᴜɴᴅ."
        except Exception:
            gpu_info = "ᴄᴏᴜʟᴅ ɴᴏᴛ ʀᴇᴛʀɪᴇᴠᴇ ɢᴩᴜ ɪɴғᴏ."
        system_stats_text += gpu_info
        await safe_edit_message(
            query.message,
            system_stats_text,
            reply_markup=get_admin_global_settings_markup(),
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await query.answer("❌ ғᴀɪʟᴇᴅ ᴛᴏ ʀᴇᴛʀɪᴇᴠᴇ ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ.", show_alert=True)
        logger.error(f"ᴇʀʀᴏʀ ʀᴇᴛʀɪᴇᴠɪɴɢ ꜱyꜱᴛᴇᴍ ꜱᴛᴀᴛꜱ ғᴏʀ ᴀᴅᴍɪɴ {user_id}: {e}")
        await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    await _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    
    if db is None:
        return await query.answer("⚠️ Database is unavailable. Cannot retrieve user list.", show_alert=True)

    users = await asyncio.to_thread(list, db.users.find({}))
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
        ig_sessions = await load_platform_sessions(user_id, "instagram")
        x_sessions = await load_platform_sessions(user_id, "x")
        
        insta_usernames = [s["username"] for s in ig_sessions]
        x_usernames = [s["username"] for s in x_sessions]

        added_at = user.get("added_at", "ɴ/ᴀ").strftime("%Y-%m-%d") if isinstance(user.get("added_at"), datetime) else "ɴ/ᴀ"
        last_active = user.get("last_active", "ɴ/ᴀ").strftime("%Y-%m-%d %H:%M") if isinstance(user.get("last_active"), datetime) else "ɴ/ᴀ"
        platform_statuses = []
        if user_id == ADMIN_ID:
            platform_statuses.append("👑 ᴀᴅᴍɪɴ")
        else:
            for platform in PREMIUM_PLATFORMS:
                if await is_premium_for_platform(user_id, platform):
                    platform_statuses.append(f"⭐ {platform.capitalize()}")
        
        status_line = " | ".join(platform_statuses) if platform_statuses else "❌ Free"

        user_list_text += (
            f"ɪᴅ: `{user_id}` | {status_line}\n"
            f"ɪɢ ᴀᴄᴄᴏᴜɴᴛꜱ: `{', '.join(insta_usernames) or 'ɴ/ᴀ'}`\n"
            f"X ᴀᴄᴄᴏᴜɴᴛꜱ: `{', '.join(x_usernames) or 'ɴ/ᴀ'}`\n"
            f"ᴀᴅᴅᴇᴅ: `{added_at}` | ʟᴀꜱᴛ ᴀᴄᴛɪᴠᴇ: `{last_active}`\n"
            "-----------------------------------\n"
        )
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "ᴜꜱᴇʀ ʟɪꜱᴛ ɪꜱ ᴛᴏᴏ ʟᴏɴɢ. ꜱᴇɴᴅɪɴɢ ᴀꜱ ᴀ ғɪʟᴇ...")
        with open("users.txt", "w", encoding="utf-8") as f:
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
    await _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
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
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
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
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
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

@app.on_callback_query(filters.regex("^grant_plan_"))
async def grant_plan_cb(_, query):
    user_id = query.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})

    if not is_admin(user_id):
        await query.answer("❌ ᴀᴅᴍɪɴ ᴀᴄᴄᴇꜱꜱ ʀᴇǫᴜɪʀᴇᴅ", show_alert=True)
        return
    
    if db is None:
        return await query.answer("⚠️ Database is unavailable. Cannot grant premium.", show_alert=True)

    state_data = user_states.get(user_id)

    if not isinstance(state_data, dict) or state_data.get("action") != "select_premium_plan_for_platforms":
        return await query.answer("❌ Error: State lost. Please start over.", show_alert=True)

    target_user_id = state_data["target_user_id"]
    selected_platforms = state_data["final_selected_platforms"]
    premium_plan_key = query.data.split("grant_plan_")[1]

    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("ɪɴᴠᴀʟɪᴅ ᴩʀᴇᴍɪᴜᴍ ᴩʟᴀɴ ꜱᴇʟᴇᴄᴛᴇᴅ.", show_alert=True)
        if user_id in user_states:
            del user_states[user_id]
        return await safe_edit_message(query.message, "🛠 ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", reply_markup=admin_markup)

    plan_details = PREMIUM_PLANS[premium_plan_key]
    
    target_user_data = await _get_user_data(target_user_id) or {"_id": target_user_id, "premium": {}}
    premium_data = target_user_data.get("premium", {})
    
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
        
        premium_data[platform] = platform_premium_data
    
    await _save_user_data(target_user_id, {"premium": premium_data})

    admin_confirm_text = f"✅ ᴩʀᴇᴍɪᴜᴍ ɢʀᴀɴᴛᴇᴅ ᴛᴏ ᴜꜱᴇʀ `{target_user_id}` ғᴏʀ:\n"
    user_msg_text = (
        f"🎉 **ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ!** 🎉\n\n"
        f"yᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ɢʀᴀɴᴛᴇᴅ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇꜱꜱ ғᴏʀ ᴛʜᴇ ғᴏʟʟᴏᴡɪɴɢ ᴩʟᴀᴛғᴏʀᴍꜱ:\n"
    )

    for platform in selected_platforms:
        updated_user = await _get_user_data(target_user_id)
        platform_data = updated_user.get("premium", {}).get(platform, {})
        confirm_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if platform_data.get("until"):
            confirm_line += f" (ᴇxᴩɪʀᴇꜱ: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')} ᴜᴛᴄ`)"
        admin_confirm_text += f"- {confirm_line}\n"
        user_msg_text += f"- {confirm_line}\n"

    user_msg_text += "\nᴇɴᴊᴏy yᴏᴜʀ ɴᴇᴡ ғᴇᴀᴛᴜʀᴇꜱ! ✨"

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
        await app.send_message(target_user_id, user_msg_text, parse_mode=enums.ParseMode.MARKDOWN)
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
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
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
    await _save_user_data(query.from_user.id, {"last_active": datetime.utcnow()})
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

    if db is None:
        return await query.answer("⚠️ Database is unavailable. Cannot retrieve stats.", show_alert=True)

    total_users = await asyncio.to_thread(db.users.count_documents, {})
    total_uploads = await asyncio.to_thread(db.uploads.count_documents, {})

    stats_text = (
        "📊 **ᴀᴅᴍɪɴ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ ᴩᴀɴᴇʟ**\n\n"
        f"**ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ**: `{total_users}`\n"
        f"**ᴛᴏᴛᴀʟ ᴜᴩʟᴏᴀᴅꜱ**: `{total_uploads}`\n\n"
        "ᴜꜱᴇ `/stats` ᴄᴏᴍᴍᴀɴᴅ ғᴏʀ ᴍᴏʀᴇ ᴅᴇᴛᴀɪʟᴇᴅ ꜱᴛᴀᴛꜱ."
    )

    await safe_edit_message(query.message, stats_text, reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^upload_type$"))
async def upload_type_cb(_, query):
    await safe_edit_message(
        query.message,
        "📌 ꜱᴇʟᴇᴄᴛ ᴛʜᴇ ᴅᴇғᴀᴜʟᴛ ᴜᴩʟᴏᴀᴅ ᴛyᴩᴇ:",
        reply_markup=upload_type_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_type_"))
async def set_upload_type_value_cb(_, query):
    user_id = query.from_user.id
    upload_type = query.data.replace("set_type_", "")
    settings = await get_user_settings(user_id)
    settings["upload_type_instagram"] = upload_type
    await save_user_settings(user_id, settings)
    await query.answer(f"✅ Default upload type set to {upload_type.capitalize()}", show_alert=True)
    await safe_edit_message(query.message, "⚙️ Welcome to your settings panel.", reply_markup=get_user_settings_markup(user_id))

@app.on_callback_query(filters.regex("^set_caption_"))
async def set_caption_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("_")[-1]
    user_states[user_id] = {"action": f"waiting_for_caption_{platform}"}
    await safe_edit_message(
        query.message,
        f"📝 ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɴᴇᴡ ᴅᴇғᴀᴜʟᴛ ᴄᴀᴩᴛɪᴏɴ ғᴏʀ {platform.capitalize()}."
    )

@app.on_callback_query(filters.regex("^set_hashtags_"))
async def set_hashtags_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("_")[-1]
    user_states[user_id] = {"action": f"waiting_for_hashtags_{platform}"}
    await safe_edit_message(
        query.message,
        f"🏷️ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ yᴏᴜʀ ɴᴇᴡ ᴅᴇғᴀᴜʟᴛ ʜᴀꜱʜᴛᴀɢꜱ ғᴏʀ {platform.capitalize()}. (e.g., `#hashtag1 #hashtag2`)"
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio_instagram$"))
async def set_aspect_ratio_cb(_, query):
    await safe_edit_message(
        query.message,
        "📐 ꜱᴇʟᴇᴄᴛ ᴛʜᴇ ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ ғᴏʀ yᴏᴜʀ ᴠɪᴅᴇᴏꜱ:",
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

    await query.answer(f"✅ ᴀꜱᴩᴇᴄᴛ ʀᴀᴛɪᴏ ꜱᴇᴛ ᴛᴏ {aspect_ratio}.", show_alert=True)
    await safe_edit_message(query.message, "⚙️ Welcome to your settings panel.", reply_markup=get_user_settings_markup(user_id))

@app.on_callback_query(filters.regex("^login_platform_"))
async def login_platform_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("_")[-1]

    if not await is_premium_for_platform(user_id, platform):
        return await query.answer("❌ This is a premium feature. Please upgrade to use it.", show_alert=True)

    sessions = await load_platform_sessions(user_id, platform)
    if sessions:
        accounts_list = [s['username'] for s in sessions]
        active_account = user_states.get(user_id, {}).get(f"active_{platform}_username")
        
        account_list_text = f"🔐 You are already logged into {len(accounts_list)} {platform.capitalize()} accounts. Your active account is: `{active_account or 'None'}`.\n\n"
        account_list_text += "Use the buttons below to switch accounts or add a new one."
        
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"➕ Add another account", callback_data=f"add_account_{platform}")],
            [InlineKeyboardButton("👤 Manage Accounts", callback_data=f"manage_{platform}_accounts")]
        ])
        await safe_edit_message(query.message, account_list_text, reply_markup=markup, parse_mode=enums.ParseMode.MARKDOWN)
    else:
        user_states[user_id] = {"action": f"waiting_for_{platform}_username", "platform": platform}
        await safe_edit_message(query.message, f"👤 Please send your {platform.capitalize()} **username**.")

@app.on_callback_query(filters.regex("^add_account_"))
async def add_account_cb(_, query):
    user_id = query.from_user.id
    platform = query.data.split("_")[-1]

    if not await is_premium_for_platform(user_id, platform):
        return await query.answer("❌ This is a premium feature. Please upgrade to use it.", show_alert=True)

    user_states[user_id] = {"action": f"waiting_for_{platform}_username", "platform": platform}
    await safe_edit_message(query.message, f"👤 Please send your {platform.capitalize()} **username**.")

@app.on_callback_query(filters.regex("^manage_ig_accounts$"))
async def manage_ig_accounts_cb(_, query):
    user_id = query.from_user.id
    sessions = await load_platform_sessions(user_id, "instagram")
    logged_in_accounts = [s['username'] for s in sessions]
    if not logged_in_accounts:
        await query.answer("You are not logged in to any Instagram account.", show_alert=True)
        return
    await safe_edit_message(query.message, "👤 **Your Instagram Accounts**\n\nSelect an account to make it active or log out.",
                            reply_markup=get_insta_account_markup(user_id, logged_in_accounts),
                            parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^select_ig_account_"))
async def select_ig_account_cb(_, query):
    user_id = query.from_user.id
    username = query.data.split("_")[-1]
    
    user_settings = await get_user_settings(user_id)
    user_settings["active_ig_username"] = username
    await save_user_settings(user_id, user_settings)
    
    sessions = await load_platform_sessions(user_id, "instagram")
    logged_in_accounts = [s['username'] for s in sessions]
    
    await query.answer(f"✅ @{username} is now your active Instagram account.", show_alert=True)
    await safe_edit_message(query.message, f"👤 **Your Instagram Accounts**\n\nActive account: `{username}`",
                            reply_markup=get_insta_account_markup(user_id, logged_in_accounts),
                            parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^logout_ig_account$"))
async def logout_ig_account_cb(_, query):
    user_id = query.from_user.id
    user_settings = await get_user_settings(user_id)
    active_username = user_settings.get("active_ig_username")
    
    if not active_username:
        return await query.answer("No active Instagram account to log out from.", show_alert=True)
        
    await delete_platform_session(user_id, "instagram", active_username)
    user_settings["active_ig_username"] = None
    await save_user_settings(user_id, user_settings)
    
    await query.answer(f"✅ Logged out from @{active_username}.", show_alert=True)
    sessions = await load_platform_sessions(user_id, "instagram")
    logged_in_accounts = [s['username'] for s in sessions]

    if logged_in_accounts:
        await safe_edit_message(query.message, "👤 **Your Instagram Accounts**\n\nActive account: `None`",
                                reply_markup=get_insta_account_markup(user_id, logged_in_accounts),
                                parse_mode=enums.ParseMode.MARKDOWN)
    else:
        await safe_edit_message(query.message, "✅ You have been logged out of all Instagram accounts.",
                                reply_markup=get_user_settings_markup(user_id))

@app.on_callback_query(filters.regex("^manage_x_accounts$"))
async def manage_x_accounts_cb(_, query):
    user_id = query.from_user.id
    sessions = await load_platform_sessions(user_id, "x")
    logged_in_accounts = [s['username'] for s in sessions]
    if not logged_in_accounts:
        await query.answer("You are not logged in to any X account.", show_alert=True)
        return
    await safe_edit_message(query.message, "👤 **Your X Accounts**\n\nSelect an account to make it active or log out.",
                            reply_markup=get_x_account_markup(user_id, logged_in_accounts),
                            parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^select_x_account_"))
async def select_x_account_cb(_, query):
    user_id = query.from_user.id
    username = query.data.split("_")[-1]
    
    user_settings = await get_user_settings(user_id)
    user_settings["active_x_username"] = username
    await save_user_settings(user_id, user_settings)
    
    sessions = await load_platform_sessions(user_id, "x")
    logged_in_accounts = [s['username'] for s in sessions]
    
    await query.answer(f"✅ @{username} is now your active X account.", show_alert=True)
    await safe_edit_message(query.message, f"👤 **Your X Accounts**\n\nActive account: `{username}`",
                            reply_markup=get_x_account_markup(user_id, logged_in_accounts),
                            parse_mode=enums.ParseMode.MARKDOWN)

@app.on_callback_query(filters.regex("^logout_x_account$"))
async def logout_x_account_cb(_, query):
    user_id = query.from_user.id
    user_settings = await get_user_settings(user_id)
    active_username = user_settings.get("active_x_username")
    
    if not active_username:
        return await query.answer("No active X account to log out from.", show_alert=True)
        
    await delete_platform_session(user_id, "x", active_username)
    user_settings["active_x_username"] = None
    await save_user_settings(user_id, user_settings)
    
    await query.answer(f"✅ Logged out from @{active_username}.", show_alert=True)
    sessions = await load_platform_sessions(user_id, "x")
    logged_in_accounts = [s['username'] for s in sessions]

    if logged_in_accounts:
        await safe_edit_message(query.message, "👤 **Your X Accounts**\n\nActive account: `None`",
                                reply_markup=get_x_account_markup(user_id, logged_in_accounts),
                                parse_mode=enums.ParseMode.MARKDOWN)
    else:
        await safe_edit_message(query.message, "✅ You have been logged out of all X accounts.",
                                reply_markup=get_user_settings_markup(user_id))

async def timeout_task(user_id, message_id):
    await asyncio.sleep(600)
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

@app.on_message(filters.media & filters.private)
@with_user_lock
async def handle_media_upload(_, msg):
    user_id = msg.from_user.id
    await _save_user_data(user_id, {"last_active": datetime.utcnow()})
    state_data = user_states.get(user_id)

    if is_admin(user_id) and state_data and state_data.get("action") == "waiting_for_google_play_qr" and msg.photo:
        qr_file_id = msg.photo.file_id
        new_payment_settings = global_settings.get("payment_settings", {})
        new_payment_settings["google_play_qr_file_id"] = qr_file_id
        await _update_global_setting("payment_settings", new_payment_settings)
        if user_id in user_states:
            del user_states[user_id]
        return await msg.reply("✅ ɢᴏᴏɢʟᴇ ᴩᴀy ǫʀ ᴄᴏᴅᴇ ɪᴍᴀɢᴇ ꜱᴜᴄᴄᴇꜱꜱғᴜʟʟy ꜱᴀᴠᴇᴅ!")

    if not state_data or state_data.get("action") not in [
        "waiting_for_instagram_reel_video",
        "waiting_for_instagram_photo_image",
        "waiting_for_instagram_story",
        "waiting_for_album_media",
        "waiting_for_x_media"
    ]:
        return await msg.reply("❌ ᴩʟᴇᴀꜱᴇ ᴜꜱᴇ ᴏɴᴇ ᴏғ ᴛʜᴇ ᴜᴩʟᴏᴀᴅ ʙᴜᴛᴛᴏɴꜱ ғɪʀꜱᴛ.")

    media = msg.video or msg.photo
    if not media:
        if msg.document:
            return await msg.reply("⚠️ ᴅᴏᴄᴜᴍᴇɴᴛꜱ ᴀʀᴇ ɴᴏᴛ ꜱᴜᴩᴩᴏʀᴛᴇᴅ. ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ a video or photo without compression.")
        return await msg.reply("❌ Unsupported media type.")

    if media.file_size > MAX_FILE_SIZE_BYTES:
        if user_id in user_states:
            del user_states[user_id]
        return await msg.reply(f"❌ ғɪʟᴇ ꜱɪᴢᴇ ᴇxᴄᴇᴇᴅꜱ ᴛʜᴇ ʟɪᴍɪᴛ ᴏғ `{MAX_FILE_SIZE_BYTES / (1024 * 1024):.2f}` ᴍʙ.")

    if state_data.get("action") in ["waiting_for_album_media", "waiting_for_x_media"]:
        
        if state_data.get("action") == "waiting_for_album_media" and len(state_data['media_paths']) >= 10:
            return await msg.reply("⚠️ You can only upload a maximum of 10 items in an album. Please send `/done` to finish.")
        
        processing_msg = await msg.reply("⏳ Downloading media...")
        file_path = await app.download_media(msg)
        state_data['media_paths'].append(file_path)
        
        if state_data.get("action") == "waiting_for_album_media":
            await safe_edit_message(
                processing_msg,
                f"✅ Downloaded file {len(state_data['media_paths'])} of your album. "
                f"Send more or use `/done` to finish."
            )
        else:
            await safe_edit_message(
                processing_msg,
                f"✅ Downloaded media for your X post. Now please send the text for your post."
            )
        return
    
    processing_msg = await msg.reply("⏳ ꜱᴛᴀʀᴛɪɴɢ ᴅᴏᴡɴʟᴏᴀᴅ...")
    file_info = {
        "file_id": media.file_id,
        "platform": state_data["platform"],
        "upload_type": state_data["upload_type"],
        "file_size": media.file_size,
        "processing_msg": processing_msg,
        "original_msg_id": msg.id,
        "downloaded_path": None,
        "usertags": [],
        "location": None
    }

    try:
        start_time = time.time()
        last_update_time = [0]
        
        task_tracker.create_task(
            monitor_progress_task(msg.chat.id, processing_msg.id, processing_msg),
            user_id=user_id,
            task_name="progress_monitor"
        )
        
        file_info["downloaded_path"] = await app.download_media(
            msg,
            progress=progress_callback_threaded,
            progress_args=("ᴅᴏᴡɴʟᴏᴀᴅ", processing_msg.id, msg.chat.id, start_time, last_update_time)
        )

        task_tracker.cancel_user_task(user_id, "progress_monitor")

        if file_info["upload_type"] == "story":
            user_states[user_id] = {"action": "finalizing_upload", "file_info": file_info}
            await start_upload_task(msg, file_info)
            return

        is_premium = await is_premium_for_platform(user_id, file_info['platform'])
        caption_text = "✅ ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴏᴍᴩʟᴇᴛᴇ. ᴡʜᴀᴛ ᴛɪᴛʟᴇ ᴅᴏ yᴏᴜ ᴡᴀɴᴛ ғᴏʀ yᴏᴜʀ ᴩᴏꜱᴛ?"
        if not is_premium:
            caption_text += "\n\n⚠️ As a free user, your caption is limited to 280 characters. You also cannot add tags or locations."

        caption_msg = await file_info["processing_msg"].reply_text(
            caption_text,
            reply_markup=get_caption_markup(is_album=False, is_x=False, is_premium=is_premium),
            reply_to_message_id=msg.id
        )
        file_info['processing_msg'] = caption_msg
        
        user_states[user_id] = {"action": "waiting_for_caption", "file_info": file_info}

        task_tracker.create_task(
            safe_task_wrapper(timeout_task(user_id, caption_msg.id)),
            user_id=user_id,
            task_name="timeout"
        )

    except asyncio.CancelledError:
        logger.info(f"ᴅᴏᴡɴʟᴏᴀᴅ ᴄᴀɴᴄᴇʟʟᴇᴅ ʙy ᴜꜱᴇʀ {user_id}.")
        cleanup_temp_files([file_info.get("downloaded_path")])
    except Exception as e:
        logger.error(f"ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ғɪʟᴇ ᴅᴏᴡɴʟᴏᴀᴅ ғᴏʀ ᴜꜱᴇʀ {user_id}: {e}")
        await safe_edit_message(file_info.get("processing_msg"), f"❌ ᴅᴏᴡɴʟᴏᴀᴅ ғᴀɪʟᴇᴅ: {str(e)}")
        cleanup_temp_files([file_info.get("downloaded_path")])
        if user_id in user_states:
            del user_states[user_id]

@app.on_message(filters.command("done") & filters.private)
@with_user_lock
async def handle_done_command(_, msg):
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)

    if not state_data or state_data.get('action') not in ['waiting_for_album_media', 'waiting_for_x_media']:
        return await msg.reply("❌ There is no active multi-media upload process. Please use the appropriate button to start.")

    media_paths = state_data.get('media_paths', [])
    if len(media_paths) < 1:
        return await msg.reply("❌ You must send at least one media file.")

    if state_data['platform'] == 'instagram':
        file_info = {
            "platform": "instagram",
            "upload_type": "album",
            "media_paths": media_paths,
            "processing_msg": await msg.reply("✅ Album files received. What caption do you want for your album?",
                                            reply_markup=get_caption_markup(is_album=True, is_premium=await is_premium_for_platform(user_id, "instagram")),
                                            parse_mode=enums.ParseMode.MARKDOWN)
        }
        user_states[user_id] = {"action": "waiting_for_caption", "file_info": file_info}
    
    elif state_data['platform'] == 'x':
        file_info = {
            "platform": "x",
            "upload_type": "post",
            "media_paths": media_paths,
            "processing_msg": await msg.reply("✅ Media files received for X post. What text do you want to add?",
                                            reply_markup=get_caption_markup(is_x=True, is_premium=await is_premium_for_platform(user_id, "x")),
                                            parse_mode=enums.ParseMode.MARKDOWN)
        }
        user_states[user_id] = {"action": "waiting_for_caption", "file_info": file_info}


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
        logger.info(f"Semaphore acquired for user {user_id}. Starting upload process.")
        
        files_to_clean = []
        try:
            # Prepare usertags and location for the upload
            usertags_to_add = []
            location_to_add = None
            
            # Caption
            user_settings = await get_user_settings(user_id)
            is_premium = await is_premium_for_platform(user_id, platform)
            
            default_caption = user_settings.get(f"caption_{platform}", f"Check out my new content on {platform.capitalize()}!")
            hashtags = user_settings.get(f"hashtags_{platform}", "")
            
            final_caption = file_info.get("custom_caption")
            if final_caption is None:
                final_caption = default_caption
            
            if hashtags and platform == "instagram":
                final_caption = f"{final_caption}\n\n{hashtags}"

            url = "ɴ/ᴀ"
            media_id = "ɴ/ᴀ"
            media_type_value = ""

            await safe_edit_message(processing_msg, f"🚀 **Uploading to {platform.capitalize()}...**", parse_mode=enums.ParseMode.MARKDOWN, reply_markup=get_progress_markup())
            
            if platform == "instagram":
                user_upload_client = InstaClient()
                user_upload_client.delay_range = [1, 3]
                
                proxy_url = global_settings.get("proxy_url")
                if proxy_url:
                    user_upload_client.set_proxy(proxy_url)
                elif INSTAGRAM_PROXY:
                    user_upload_client.set_proxy(INSTAGRAM_PROXY)
                
                active_username = user_settings.get("active_ig_username")
                if not active_username:
                    raise LoginRequired("No active Instagram account selected. Please login first.")

                session = await load_platform_session_data(user_id, "instagram", active_username)
                if not session:
                    raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴩɪʀᴇᴅ. Please re-login.")
                
                user_upload_client.set_settings(session)
                try:
                    await asyncio.to_thread(user_upload_client.get_timeline_feed)
                except LoginRequired:
                    raise LoginRequired("ɪɴꜱᴛᴀɢʀᴀᴍ ꜱᴇꜱꜱɪᴏɴ ᴇxᴩɪʀᴇᴅ. Please re-login.")

                if is_premium:
                    if file_info.get("usertags"):
                        for username in file_info["usertags"]:
                            try:
                                user_info = await asyncio.to_thread(user_upload_client.user_info_by_username, username)
                                usertags_to_add.append(Usertag(user=user_info, x=0.5, y=0.5))
                            except Exception as e:
                                logger.warning(f"Could not tag user {username}: {e}")
                    location_to_add = file_info.get("location")
                
                if upload_type == "album":
                    media_paths = file_info["media_paths"]
                    files_to_clean.extend(media_paths)
                    result = await asyncio.to_thread(user_upload_client.album_upload, media_paths, caption=final_caption, usertags=usertags_to_add, location=location_to_add)
                    url = f"https://instagram.com/p/{result.code}"
                    media_id = result.pk
                    media_type_value = result.media_type
                
                elif upload_type == "story":
                    file_path = file_info["downloaded_path"]
                    files_to_clean.append(file_path)
                    result = await asyncio.to_thread(user_upload_client.photo_upload_to_story if msg.photo else user_upload_client.video_upload_to_story, file_path)
                    url = f"https://instagram.com/stories/{user_upload_client.username}"
                    media_id = result.pk
                    media_type_value = result.media_type
                    
                elif upload_type == "reel":
                    file_path = file_info["downloaded_path"]
                    files_to_clean.append(file_path)
                    result = await asyncio.to_thread(user_upload_client.clip_upload, file_path, caption=final_caption, usertags=usertags_to_add, location=location_to_add)
                    url = f"https://instagram.com/reel/{result.code}"
                    media_id = result.pk
                    media_type_value = result.media_type
                
                elif upload_type == "post":
                    file_path = file_info["downloaded_path"]
                    files_to_clean.append(file_path)
                    result = await asyncio.to_thread(user_upload_client.photo_upload, file_path, caption=final_caption, usertags=usertags_to_add, location=location_to_add)
                    url = f"https://instagram.com/p/{result.code}"
                    media_id = result.pk
                    media_type_value = result.media_type
            
            elif platform == "x":
                # Assuming twscrape can handle media uploads directly
                active_username = user_settings.get("active_x_username")
                if not active_username:
                    raise LoginRequired("No active X account selected. Please login first.")

                # The `twscrape` library handles multi-account access automatically
                # after accounts are added with `add_account`.
                # We just need to make sure the active session is correctly managed.

                media_paths = file_info["media_paths"]
                files_to_clean.extend(media_paths)
                
                result = await asyncio.to_thread(x_api.tweet, text=final_caption, media=media_paths)
                
                url = f"https://x.com/{result.user.username}/status/{result.id}"
                media_id = result.id
                media_type_value = "X_POST"

            await _save_user_data(user_id, {
                "last_upload": {
                    "platform": platform,
                    "media_id": str(media_id), "url": url, "timestamp": datetime.utcnow()
                }
            })
            if db is not None:
                await asyncio.to_thread(db.uploads.insert_one, {
                    "user_id": user_id,
                    "media_id": str(media_id),
                    "media_type": str(media_type_value),
                    "platform": platform,
                    "upload_type": upload_type,
                    "timestamp": datetime.utcnow(),
                    "url": url,
                    "caption": final_caption,
                    "likes_count": None # For X, fetching likes is a different process
                })

            log_msg = (
                f"📤 ɴᴇᴡ {platform.capitalize()} {upload_type.capitalize()} ᴜᴩʟᴏᴀᴅ\n\n"
                f"👤 ᴜꜱᴇʀ: `{user_id}`\n"
                f"🔗 ᴜʀʟ: {url}\n"
                f"📅 {get_current_datetime()['date']}"
            )

            await safe_edit_message(processing_msg, f"✅ ᴜᴩʟᴏᴀᴅᴇᴅ ꜱᴜᴄᴄᴇꜱꜱғᴜʟʟy!\n\n{url}")
            await send_log_to_channel(app, LOG_CHANNEL, log_msg)

        except asyncio.CancelledError:
            logger.info(f"ᴜᴩʟᴏᴀᴅ ᴩʀᴏᴄᴇꜱꜱ ғᴏʀ ᴜꜱᴇʀ {user_id} ᴡᴀꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
            await safe_edit_message(processing_msg, "❌ ᴜᴩʟᴏᴀᴅ ᴩʀᴏᴄᴇꜱꜱ ᴄᴀɴᴄᴇʟʟᴇᴅ.")
        except LoginRequired:
            error_msg = f"❌ {platform.capitalize()} ʟᴏɢɪɴ ʀᴇǫᴜɪʀᴇᴅ. Your session might have expired. Please use `/login` again."
            await safe_edit_message(processing_msg, error_msg) if processing_msg else await msg.reply(error_msg)
            logger.error(f"LoginRequired during {platform} upload for user {user_id}")
        except ClientError as ce:
            error_msg = f"❌ {platform.capitalize()} ᴄʟɪᴇɴᴛ ᴇʀʀᴏʀ ᴅᴜʀɪɴɢ ᴜᴩʟᴏᴀᴅ: {ce}. ᴩʟᴇᴀꜱᴇ ᴛʀy ᴀɢᴀɪɴ ʟᴀᴛᴇʀ."
            await safe_edit_message(processing_msg, error_msg) if processing_msg else await msg.reply(error_msg)
            logger.error(f"ClientError during {platform} upload for user {user_id}: {ce}")
        except Exception as e:
            error_msg = f"❌ {platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ: {str(e)}"
            await safe_edit_message(processing_msg, error_msg) if processing_msg else await msg.reply(error_msg)
            logger.error(f"{platform.capitalize()} ᴜᴩʟᴏᴀᴅ ғᴀɪʟᴇᴅ ғᴏʀ {user_id}: {str(e)}", exc_info=True)
        finally:
            cleanup_temp_files(files_to_clean)
            if user_id in user_states:
                del user_states[user_id]
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
    """Runs the HTTP server in a separate thread."""
    try:
        server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
        logger.info("HTTP health check server started on port 8080.")
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server failed: {e}")

# === Main entry point: Combines setup and reliable run method ===
if __name__ == "__main__":
    os.makedirs("sessions", exist_ok=True)
    logger.info("Session directory ensured.")

    # --- Step 1: Initialize Task Tracker ---
    task_tracker = TaskTracker()
    logger.info("TaskTracker initialized.")

    # --- Step 2: Synchronous Setup ---
    logger.info("Attempting to connect to MongoDB...")
    try:
        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo.admin.command('ismaster')
        db = mongo.NowTok
        logger.info("✅ Connected to MongoDB successfully.")

        logger.info("Loading global settings...")
        settings_from_db = db.settings.find_one({"_id": "global_settings"})
        if settings_from_db:
            global_settings.update(settings_from_db)
        
        for key, value in DEFAULT_GLOBAL_SETTINGS.items():
            if key not in global_settings:
                global_settings[key] = value
                db.settings.update_one({"_id": "global_settings"}, {"$set": {key: value}}, upsert=True)
        
        logger.info("Global settings loaded.")

        MAX_CONCURRENT_UPLOADS = global_settings.get("max_concurrent_uploads")
        upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
        MAX_FILE_SIZE_BYTES = global_settings.get("max_file_size_mb") * 1024 * 1024

    except Exception as e:
        logger.critical(f"❌ DATABASE OR SETTINGS SETUP FAILED: {e}")
        logger.warning("Bot will run in a degraded mode without database features.")
        db = None

    # --- Step 3: Start Health Check Thread ---
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # --- Step 4: Run the Bot using the reliable app.run() method ---
    logger.info("Starting bot using app.run()...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed during app.run(): {e}", exc_info=True)
        sys.exit(1)

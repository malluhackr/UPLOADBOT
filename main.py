import os
import sys
import asyncio
import threading
import logging
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from pymongo import MongoClient
from pyrogram import Client, filters, enums
from pyrogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from instagrapi import Client as InstaClient
from instagrapi.exceptions import LoginRequired, ChallengeRequired, BadPassword, PleaseWaitFewMinutes, ClientError

# Import the new log handler (Ensure this file exists in the same directory)
from log_handler import send_log_to_channel
import subprocess

# === Load env ===

load_dotenv()
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

# Initialize MongoDB Client
try:
    mongo = MongoClient(MONGO_URI)
    db = mongo.instagram_bot # Using 'instagram_bot' database
    logging.info("Connected to MongoDB successfully.")
except Exception as e:
    logging.critical(f"Failed to connect to MongoDB: {e}")
    sys.exit(1)

# Configure logging to console and file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Output to console
        logging.FileHandler("bot.log")      # Output to file
    ]
)
logger = logging.getLogger("InstaUploadBot")

app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
insta_client = InstaClient()
insta_client.delay_range = [1, 3]  # More human-like behavior

# Create collections if not exists
# Added 'uploads' collection to track successful uploads for stats
required_collections = ["users", "settings", "sessions", "uploads", "scheduled_posts"]
for collection_name in required_collections:
    if collection_name not in db.list_collection_names():
        db.create_collection(collection_name)
        logger.info(f"Collection '{collection_name}' created.")

# State management for sequential user input
user_states = {} # {user_id: "action"}

# --- PREMIUM DEFINITIONS ---
PREMIUM_PLANS = {
    "1_hour_test": {"duration": timedelta(hours=1), "price": "Free"},
    "3_days": {"duration": timedelta(days=3), "price": "₹10"},
    "7_days": {"duration": timedelta(days=7), "price": "₹25"},
    "15_days": {"duration": timedelta(days=15), "price": "₹35"},
    "1_month": {"duration": timedelta(days=30), "price": "₹60"}, # Assuming 30 days for simplicity
    "3_months": {"duration": timedelta(days=90), "price": "₹150"}, # Assuming 90 days
    "1_year": {"duration": timedelta(days=365), "price": "Negotiable"},
    "lifetime": {"duration": None, "price": "Negotiable"} # None for lifetime
}

# Supported platforms for premium
PREMIUM_PLATFORMS = ["instagram", "tiktok"] # Added tiktok

# Keyboards

def get_main_keyboard(user_id):
    buttons = [
        [KeyboardButton("⚙️ Settings"), KeyboardButton("📊 Stats")]
    ]

    # Dynamically add upload buttons based on premium status for each platform
    is_instagram_premium = is_premium_for_platform(user_id, "instagram")
    is_tiktok_premium = is_premium_for_platform(user_id, "tiktok")

    upload_buttons_row = []
    if is_instagram_premium:
        upload_buttons_row.extend([KeyboardButton("📸 Insta Photo"), KeyboardButton("📤 Insta Reel")])
    if is_tiktok_premium:
        # Placeholder buttons for TikTok
        upload_buttons_row.extend([KeyboardButton("🎵 TikTok Video"), KeyboardButton("🖼️ TikTok Photo")])

    if upload_buttons_row:
        buttons.insert(0, upload_buttons_row) # Insert upload buttons at the top row

    # Add premium/admin specific buttons
    buttons.append([KeyboardButton("/buypypremium"), KeyboardButton("/premiumdetails")])
    if is_admin(user_id):
        buttons.append([KeyboardButton("🛠 Admin Panel"), KeyboardButton("🔄 Restart Bot")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
    [InlineKeyboardButton("📐 Aspect Ratio (Video)", callback_data="set_aspect_ratio")],
    [InlineKeyboardButton("🗜️ Toggle Compression", callback_data="toggle_compression")], # Added new button
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Manage Premium", callback_data="manage_premium")], # Changed callback to manage premium
    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_message")],
    [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_to_main_menu")] # Updated to main menu
])

upload_type_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Reel", callback_data="set_type_reel")],
    [InlineKeyboardButton("📷 Post", callback_data="set_type_post")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_settings")]
])

# New inline keyboard for aspect ratio selection
aspect_ratio_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("Original Aspect Ratio", callback_data="set_ar_original")],
    [InlineKeyboardButton("9:16 (Crop/Fit)", callback_data="set_ar_9_16")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_settings")]
])

# Inline keyboard for premium platform selection (Admin side)
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

# Inline keyboard for premium plan selection (Admin side)
def get_premium_plan_markup(selected_platforms):
    buttons = []
    for key, value in PREMIUM_PLANS.items():
        if value["duration"] is None: # Lifetime option
            buttons.append([InlineKeyboardButton(f"Lifetime ({value['price']})", callback_data=f"select_plan_{key}")])
        else:
            # Calculate price multiplier based on selected platforms
            price_multiplier = len(selected_platforms) if selected_platforms else 1
            display_price = value['price']
            if '₹' in display_price: # Assuming price is numeric with currency
                try:
                    base_price = float(display_price.replace('₹', '').strip())
                    calculated_price = base_price * price_multiplier
                    display_price = f"₹{int(calculated_price)}" # Display as integer for simplicity
                except ValueError:
                    pass # Keep original price if conversion fails
            
            buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()} ({display_price})", callback_data=f"select_plan_{key}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Platform Selection", callback_data="back_to_platform_selection")])
    return InlineKeyboardMarkup(buttons)


# === Helper Functions ===

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

def is_premium_for_platform(user_id, platform):
    """Checks if a user has active premium for a specific platform."""
    user = _get_user_data(user_id)
    if not user:
        return False
    
    # Admins are always premium for all platforms
    if user_id == ADMIN_ID:
        return True

    platform_premium = user.get("premium", {}).get(platform, {})
    premium_type = platform_premium.get("type")
    premium_until = platform_premium.get("until")

    # Check for 'lifetime' premium
    if premium_type == "lifetime":
        return True

    # Check if 'premium_until' exists and is in the future
    if premium_until and isinstance(premium_until, datetime) and premium_until > datetime.now():
        return True
    
    # If premium_until is in the past, or not set, or is_premium is False
    # we should explicitly clear premium_until and premium_type if it's expired
    if premium_type and premium_until and premium_until <= datetime.now():
        # Premium expired, update database by unsetting the fields
        db.users.update_one(
            {"_id": user_id},
            {"$unset": {f"premium.{platform}.type": "", f"premium.{platform}.until": ""}}
        )
        logger.info(f"Premium for {platform} expired for user {user_id}. Status updated in DB.")
        return False
    
    return False # Default to false if no valid premium found

def get_current_datetime():
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "UTC+5:30"
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
    """
    Saves TikTok session data to MongoDB (placeholder).
    In a real scenario, you'd integrate a TikTok library's session handling here.
    """
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"tiktok_session": session_data}},
        upsert=True
    )
    logger.info(f"TikTok (placeholder) session saved for user {user_id}")

async def load_tiktok_session(user_id):
    """
    Loads TikTok session data from MongoDB (placeholder).
    In a real scenario, you'd integrate a TikTok library's session handling here.
    """
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
    if "no_compression" not in settings: # Default to compression enabled (False for no_compression)
        settings["no_compression"] = False 
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
            # Verify session is still valid
            insta_client.get_timeline_feed()
            logger.info("Instagrapi session is valid for bot's client.")
            return True
        except LoginRequired:
            logger.warning("Instagrapi session expired for bot's client. Attempting fresh login.")
            insta_client.set_settings({}) # Clear expired settings
        except Exception as e:
            logger.error(f"Error loading instagrapi session for bot's client: {e}. Attempting fresh login.")
            insta_client.set_settings({}) # Clear potentially corrupted settings

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

# Placeholder for TikTok client and its functions
# IMPORTANT: This is a SIMULATION. Real TikTok API integration requires a separate library.
class TikTokClientPlaceholder:
    def __init__(self):
        self.is_logged_in = False
        self.username = None
        self.password = None
        self.session_data = None
        logger.info("TikTokClientPlaceholder initialized (SIMULATED).")

    async def login(self, username, password):
        logger.info(f"Simulating TikTok login for {username}...")
        await asyncio.sleep(3) # Simulate network delay
        if username and password:
            self.is_logged_in = True
            self.username = username
            self.password = password
            self.session_data = {"user": username, "timestamp": datetime.now().isoformat()}
            logger.info(f"Simulated TikTok login successful for {username}.")
            return True
        else:
            logger.warning(f"Simulated TikTok login failed for {username}.")
            raise LoginRequired("Simulated: Invalid username or password.")

    async def photo_upload(self, photo_path, caption):
        logger.info(f"Simulating TikTok photo upload for {self.username} with {photo_path} and caption: {caption}")
        await asyncio.sleep(5) # Simulate upload delay
        # Simulate a successful upload result (e.g., using a dummy code)
        return type('obj', (object,), {'code': 'tiktokphotoid123', 'media_type': 'image'})()

    async def clip_upload(self, video_path, caption):
        logger.info(f"Simulating TikTok video upload for {self.username} with {video_path} and caption: {caption}")
        await asyncio.sleep(7) # Simulate upload delay
        # Simulate a successful upload result (e.g., using a dummy code)
        return type('obj', (object,), {'code': 'tiktokvideoid456', 'media_type': 'video'})()

    def get_settings(self):
        return self.session_data

    def set_settings(self, session_data):
        self.session_data = session_data
        self.is_logged_in = True if session_data else False
        self.username = session_data.get("user") if session_data else None

    def get_timeline_feed(self):
        # Simulate a session check
        if not self.is_logged_in:
            raise LoginRequired("Simulated TikTok session expired.")
        logger.debug(f"Simulated TikTok session valid for {self.username}.")
        return True # Simulate successful session check

tiktok_client_placeholder = TikTokClientPlaceholder()

# === Message Handlers ===

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    _save_user_data(user_id, {"last_active": datetime.now()})

    user = _get_user_data(user_id)
    if not user:
        # New users start with no premium and an empty premium dict
        _save_user_data(user_id, {"_id": user_id, "premium": {}, "added_by": "self_start", "added_at": datetime.now()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")

    # Check if user has ANY premium or is admin
    has_any_premium = any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS)

    if not is_admin(user_id) and not has_any_premium:
        contact_admin_text = (
            f"👋 **Hi {user_first_name}!**\n\n"
            "**This Bot Lets You Upload Any Size Instagram Reels & Posts Directly From Telegram**.\n\n"
            "• **Unlock Full Premium Features**:\n"
            "• **Upload Unlimited Videos**\n"
            "• **Auto Captions & Hashtags**\n"
            "• **Reel Or Post Type Selection**\n"
            "• **TikTok Uploads (Coming Soon!)**\n\n" # Added TikTok mention
            "👤 Contact **[ADMIN TOM](https://t.me/CjjTom)** **To Upgrade Your Access**.\n"
            "🔐 **Your Data Is Fully ✅Encrypted**\n\n"
            f"🆔 Your User ID: `{user_id}`"
        )

        join_channel_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅𝗝𝗼𝗶𝗻 𝗢𝘂𝗿 𝗖𝗵𝗮𝗻𝗻𝗲𝗹✅", url="https://t.me/KeralaCaptain")]
        ])

        await app.send_photo(
            chat_id=msg.chat.id,
            photo="https://i.postimg.cc/SXDxJ92z/x.jpg",
            caption=contact_admin_text,
            reply_markup=join_channel_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        return

    # For premium or admin users
    welcome_msg = "🤖 **Welcome to Instagram & TikTok Upload Bot!**\n\n"
    if is_admin(user_id):
        welcome_msg += "🛠 You have **admin privileges**."
    else:
        platform_statuses = []
        for platform in PREMIUM_PLATFORMS:
            user_data = _get_user_data(user_id)
            platform_premium_data = user_data.get("premium", {}).get(platform, {})
            premium_type = platform_premium_data.get("type")
            premium_until = platform_premium_data.get("until")

            if premium_type == "lifetime":
                platform_statuses.append(f"👑 **Lifetime Premium** for **{platform.capitalize()}**!")
            elif premium_until and premium_until > datetime.now():
                remaining_time = premium_until - datetime.now()
                days = remaining_time.days
                hours = remaining_time.seconds // 3600
                platform_statuses.append(f"⭐ **{platform.capitalize()} Premium** expires in: `{days} days, {hours} hours`.")
            else:
                platform_statuses.append(f"Free user for **{platform.capitalize()}**.")
        
        welcome_msg += "\n\n" + "\n".join(platform_statuses)
            
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
    logger.info(f"User {msg.from_user.id} attempting Instagram login command.")

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
            logger.info(f"Applied proxy {INSTAGRAM_PROXY} to user {user_id}'s Instagram login attempt.")

        session = await load_instagram_session(user_id)
        if session:
            logger.info(f"Attempting to load existing Instagram session for user {user_id} (IG: {username}).")
            user_insta_client.set_settings(session)
            try:
                user_insta_client.get_timeline_feed()
                await login_msg.edit_text(f"✅ Already logged in to Instagram as `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                logger.info(f"Existing Instagram session for {user_id} is valid.")
                return
            except LoginRequired:
                logger.info(f"Existing Instagram session for {user_id} expired. Attempting fresh login.")
                user_insta_client.set_settings({})

        logger.info(f"Attempting fresh Instagram login for user {user_id} with username: {username}")
        user_insta_client.login(username, password)

        session_data = user_insta_client.get_settings()
        await save_instagram_session(user_id, session_data)

        _save_user_data(user_id, {"instagram_username": username})

        await login_msg.edit_text("✅ Instagram login successful!")
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
async def tiktok_login_cmd(_, msg):
    """Handles user TikTok login (simulated)."""
    logger.info(f"User {msg.from_user.id} attempting TikTok login command.")

    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to use TikTok features. Please upgrade to TikTok Premium with /buypypremium.")

    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Usage: `/tiktoklogin <tiktok_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 Attempting TikTok login (simulated)...")

    try:
        # Check for existing session first
        session = await load_tiktok_session(user_id)
        if session:
            tiktok_client_placeholder.set_settings(session)
            try:
                tiktok_client_placeholder.get_timeline_feed() # Simulate session validity check
                await login_msg.edit_text(f"✅ Already logged in to TikTok as `{username}` (simulated session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                logger.info(f"Existing simulated TikTok session for {user_id} is valid.")
                return
            except LoginRequired:
                logger.info(f"Existing simulated TikTok session for {user_id} expired. Attempting fresh login.")
                tiktok_client_placeholder.set_settings({}) # Clear expired settings

        await tiktok_client_placeholder.login(username, password)
        session_data = tiktok_client_placeholder.get_settings()
        await save_tiktok_session(user_id, session_data)

        _save_user_data(user_id, {"tiktok_username": username})

        await login_msg.edit_text("✅ TikTok login successful (simulated)!")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"📝 New TikTok login (Simulated)\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`\n"
            f"TikTok: `{username}`"
        )
        logger.info(f"TikTok login successful (simulated) for user {user_id} ({username}).")

    except Exception as e:
        await login_msg.edit_text(f"❌ TikTok login failed (simulated): {str(e)}. Please try again.")
        logger.error(f"Simulated TikTok Login Failed for user {user_id} ({username}): {e}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ TikTok Login Failed (Simulated) for user `{user_id}` (`{username}`): {e}")

@app.on_message(filters.command("buypypremium"))
async def buypypremium_cmd(_, msg):
    """Displays premium plans."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    premium_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions for Instagram and TikTok!\n\n"
        "**Available Plans:**\n"
        "• **1 Hour Test**: Free (Perfect for new users!)\n"
        "• **3 Days Premium**: `₹10`\n"
        "• **7 Days Premium**: `₹25`\n"
        "• **15 Days Premium**: `₹35`\n"
        "• **1 Month Premium**: `₹60`\n"
        "• **3 Months Premium**: `₹150`\n"
        "• **1 Year Premium**: `Negotiable`\n"
        "• **Lifetime Premium**: `Negotiable`\n\n"
        "**Note:** Price might vary based on the number of platforms you choose (Instagram, TikTok, or both).\n\n"
        "To purchase, please contact **[ADMIN TOM](https://t.me/CjjTom)**."
    )
    await msg.reply(premium_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    """Shows user's current premium status for all platforms."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

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
        elif premium_until and premium_until > datetime.now():
            remaining_time = premium_until - datetime.now()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            minutes = (remaining_time.seconds % 3600) // 60
            status_text += (
                f"`{premium_type.replace('_', ' ').title()}` expires on: "
                f"`{premium_until.strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"Time remaining: `{days} days, {hours} hours, {minutes} minutes`\n"
            )
            has_premium_any = True
        else:
            status_text += "😔 **Not Active.**\n"
        status_text += "\n" # Add a newline for separation

    if not has_premium_any:
        status_text += "Use /buypypremium to upgrade!"

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    """Displays the settings menu."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    # Users need to be premium for at least one platform to access settings, or be admin
    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ Not authorized. You need premium access for at least one platform to access settings.")

    current_settings = await get_user_settings(user_id)
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"

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
async def initiate_instagram_reel_upload(_, msg):
    """Initiates the process for uploading an Instagram Reel."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Not authorized to upload Instagram Reels. Please upgrade to Instagram Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝘃𝗶𝗱𝗲𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗲𝗹 𝗿𝗲𝗮𝗱𝘆!!")
    user_states[user_id] = "waiting_for_instagram_reel_video"

@app.on_message(filters.regex("📸 Insta Photo"))
async def initiate_instagram_photo_upload(_, msg):
    """Initiates the process for uploading an Instagram Photo."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "instagram"):
        return await msg.reply("❌ Not authorized to upload Instagram Photos. Please upgrade to Instagram Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ 𝗦𝗲𝗻𝗱 𝗽𝗵𝗼𝘁𝗼 𝗳𝗶𝗹𝗲 - 𝗿𝗲𝗮𝗱𝘆 𝗳𝗼𝗿 𝗜𝗚!.")
    user_states[user_id] = "waiting_for_instagram_photo_image"


@app.on_message(filters.regex("🎵 TikTok Video"))
async def initiate_tiktok_video_upload(_, msg):
    """Initiates the process for uploading a TikTok video (simulated)."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to upload TikTok videos. Please upgrade to TikTok Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ Please login to TikTok first using `/tiktoklogin <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for TikTok video upload! (Simulated) Please send me the video file.")
    user_states[user_id] = "waiting_for_tiktok_video"

@app.on_message(filters.regex("🖼️ TikTok Photo"))
async def initiate_tiktok_photo_upload(_, msg):
    """Initiates the process for uploading a TikTok photo (simulated)."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if not is_admin(user_id) and not is_premium_for_platform(user_id, "tiktok"):
        return await msg.reply("❌ Not authorized to upload TikTok photos. Please upgrade to TikTok Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if not user_data or not user_data.get("tiktok_username"):
        return await msg.reply("❌ TikTok session expired (simulated). Please login to TikTok first using `/tiktoklogin <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for TikTok photo upload! (Simulated) Please send me the image file.")
    user_states[user_id] = "waiting_for_tiktok_photo"

@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    """Displays bot usage statistics."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if not is_admin(user_id) and not any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        return await msg.reply("❌ Not authorized. You need premium access for at least one platform to view stats.")

    total_users = db.users.count_documents({})
    
    premium_counts = {platform: 0 for platform in PREMIUM_PLATFORMS}
    # Iterate through users to count premium per platform
    for user in db.users.find({}):
        for platform in PREMIUM_PLATFORMS:
            if is_premium_for_platform(user["_id"], platform): # Check for each platform
                premium_counts[platform] += 1

    admin_users = db.users.count_documents({"_id": ADMIN_ID})
    total_uploads = db.uploads.count_documents({})
    
    # Updated to count Instagram and TikTok uploads separately
    total_instagram_reel_uploads = db.uploads.count_documents({"platform": "instagram", "upload_type": "reel"})
    total_instagram_post_uploads = db.uploads.count_documents({"platform": "instagram", "upload_type": "post"})
    total_tiktok_video_uploads = db.uploads.count_documents({"platform": "tiktok", "upload_type": "video"})
    total_tiktok_photo_uploads = db.uploads.count_documents({"platform": "tiktok", "upload_type": "photo"})


    stats_text = (
        "📊 **Bot Statistics:**\n"
        f"👥 Total users: `{total_users}`\n"
    )
    for platform in PREMIUM_PLATFORMS:
        stats_text += f"⭐ {platform.capitalize()} Premium users: `{premium_counts[platform]}`\n"
    
    stats_text += (
        f"👑 Admin users: `{admin_users}`\n"
        f"📈 Total uploads: `{total_uploads}`\n"
        f"🎬 Total Instagram Reel uploads: `{total_instagram_reel_uploads}`\n"
        f"📸 Total Instagram Post uploads: `{total_instagram_post_uploads}`\n"
        f"🎵 Total TikTok Video uploads: `{total_tiktok_video_uploads}` (Simulated)\n"
        f"🖼️ Total TikTok Photo uploads: `{total_tiktok_photo_uploads}` (Simulated)"
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
            await asyncio.sleep(0.1) # Small delay to avoid flood waits
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to send broadcast to user {user['_id']}: {e}")

    await status_msg.edit_text(f"✅ Broadcast finished!\nSent to `{sent_count}` users, failed for `{failed_count}` users.")
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 Broadcast initiated by Admin `{msg.from_user.id}`\n"
        f"Sent: `{sent_count}`, Failed: `{failed_count}`"
    )

# --- State-Dependent Message Handlers ---

@app.on_message(filters.text & filters.private & ~filters.command(""))
async def handle_text_input(_, msg):
    """Handles text input based on current user state."""
    user_id = msg.from_user.id
    state_data = user_states.get(user_id)
    _save_user_data(user_id, {"last_active": datetime.now()})

    if state_data == "waiting_for_caption":
        caption = msg.text
        await save_user_settings(user_id, {"caption": caption})
        # Retrieve current settings to reflect updated compression status
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif state_data == "waiting_for_hashtags":
        hashtags = msg.text
        await save_user_settings(user_id, {"hashtags": hashtags})
        # Retrieve current settings to reflect updated compression status
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif isinstance(state_data, dict) and state_data.get("action") == "waiting_for_target_user_id_premium_management":
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            # Store target user ID and initialize selected platforms in state
            user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": {}}
            await msg.reply(
                f"✅ User ID `{target_user_id}` received. Select platforms for premium:",
                reply_markup=get_platform_selection_markup(user_id, user_states[user_id]["selected_platforms"]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
            user_states.pop(user_id, None) # Clear state on invalid input


# --- Callback Handlers ---

@app.on_callback_query(filters.regex("^upload_type$"))
async def upload_type_cb(_, query):
    """Callback to show upload type options."""
    _save_user_data(query.from_user.id, {"last_active": datetime.now()})
    await safe_edit_message(
        query.message,
        "📌 Select upload type:",
        reply_markup=upload_type_markup
    )

@app.on_callback_query(filters.regex("^set_type_"))
async def set_type_cb(_, query):
    """Callback to set the preferred upload type (Reel/Post)."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})
    upload_type = query.data.split("_")[-1]
    current_settings = await get_user_settings(user_id)
    current_settings["upload_type"] = upload_type
    await save_user_settings(user_id, current_settings)

    # Get current compression status for the settings menu update
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"

    await query.answer(f"✅ Upload type set to {upload_type.capitalize()}!", show_alert=False)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel\n\n🗜️ Compression is currently: **" + compression_status + "**\n\nUse the buttons below to adjust your preferences.",
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio$"))
async def set_aspect_ratio_cb(_, query):
    """Callback to show aspect ratio options."""
    _save_user_data(query.from_user.id, {"last_active": datetime.now()})
    await safe_edit_message(
        query.message,
        "📐 Select desired aspect ratio for videos:",
        reply_markup=aspect_ratio_markup
    )

@app.on_callback_query(filters.regex("^set_ar_"))
async def set_ar_cb(_, query):
    """Callback to set the preferred aspect ratio for videos."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})
    aspect_ratio_key_parts = query.data.split("_")[2:] # e.g., ['original'] or ['9', '16']
    aspect_ratio_value = "_".join(aspect_ratio_key_parts)

    current_settings = await get_user_settings(user_id)
    current_settings["aspect_ratio"] = aspect_ratio_value
    await save_user_settings(user_id, current_settings)

    display_text = "Original" if aspect_ratio_value == "original" else "9:16 (Crop/Fit)"
    
    # Get current compression status for the settings menu update
    compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"

    await query.answer(f"✅ Aspect ratio set to {display_text}!", show_alert=False)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel\n\n🗜️ Compression is currently: **" + compression_status + "**\n\nUse the buttons below to adjust your preferences.",
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_caption$"))
async def set_caption_cb(_, query):
    """Callback to prompt for new caption."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})
    user_states[user_id] = "waiting_for_caption"
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
    """Callback to prompt for new hashtags."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})
    user_states[user_id] = "waiting_for_hashtags"
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
    """Callback to toggle video compression setting."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    settings = await get_user_settings(user_id)
    current = settings.get("no_compression", False) # Default to False (compression enabled)
    new_setting = not current # Invert the setting
    settings["no_compression"] = new_setting
    await save_user_settings(user_id, settings)

    status = "OFF (Compression Enabled)" if not new_setting else "ON (Original Video)"
    await query.answer(f"🗜️ Compression is now {status}", show_alert=True)

    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel\n\n🗜️ Compression is currently: **" + status + "**\n\nUse the buttons below to adjust your preferences.",
        reply_markup=settings_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    """Callback to display the admin panel."""
    _save_user_data(query.from_user.id, {"last_active": datetime.now()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    await safe_edit_message(
        query.message,
        "🛠 Admin Panel",
        reply_markup=admin_markup
    )

@app.on_callback_query(filters.regex("^users_list$"))
async def users_list_cb(_, query):
    """Callback to display a list of all users."""
    _save_user_data(query.from_user.id, {"last_active": datetime.now()})
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
                    else: # Fallback, should not typically happen if is_premium_for_platform is True
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

    # Split long messages if necessary
    if len(user_list_text) > 4096:
        await safe_edit_message(query.message, "User list is too long. Sending as a file...")
        with open("users.txt", "w") as f:
            f.write(user_list_text.replace("`", "")) # Remove markdown for plain text file
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
    """Callback to prompt for user ID to manage premium."""
    _save_user_data(query.from_user.id, {"last_active": datetime.now()})
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    # Set state for admin to expect user ID for premium management
    user_states[query.from_user.id] = {"action": "waiting_for_target_user_id_premium_management"}
    await safe_edit_message(
        query.message,
        "➕ Please send the **User ID** to manage their premium access."
    )

@app.on_callback_query(filters.regex("^select_platform_"))
async def select_platform_cb(_, query):
    """Callback to select/deselect platforms for premium assignment."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

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
        selected_platforms.pop(platform_to_toggle) # Deselect
    else:
        selected_platforms[platform_to_toggle] = True # Select

    state_data["selected_platforms"] = selected_platforms
    user_states[user_id] = state_data # Update the state

    await safe_edit_message(
        query.message,
        f"✅ User ID `{state_data['target_user_id']}` received. Select platforms for premium:",
        reply_markup=get_platform_selection_markup(user_id, selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^confirm_platform_selection$"))
async def confirm_platform_selection_cb(_, query):
    """Callback to confirm selected platforms and proceed to plan selection."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

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
    state_data["final_selected_platforms"] = selected_platforms # Store for next step
    user_states[user_id] = state_data # Update the state

    await safe_edit_message(
        query.message,
        f"✅ Platforms selected: `{', '.join(platform.capitalize() for platform in selected_platforms)}`. Now, select a premium plan for user `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^select_plan_"))
async def select_plan_cb(_, query):
    """Callback to select premium plan and apply it to the user."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

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
    
    # Prepare update data for each selected platform
    update_query = {}
    for platform in selected_platforms:
        new_premium_until = None
        if plan_details["duration"] is not None:
            new_premium_until = datetime.now() + plan_details["duration"]

        platform_premium_data = {
            "type": premium_plan_key,
            "added_by": user_id,
            "added_at": datetime.now()
        }
        if new_premium_until:
            platform_premium_data["until"] = new_premium_until
        
        update_query[f"premium.{platform}"] = platform_premium_data

    # Perform the update on the target user
    db.users.update_one({"_id": target_user_id}, {"$set": update_query}, upsert=True)

    admin_confirm_text = (
        f"✅ Premium granted to user `{target_user_id}` for:\n"
    )
    for platform in selected_platforms:
        # Retrieve the updated data from MongoDB to reflect the exact state after update
        updated_user = _get_user_data(target_user_id)
        platform_data = updated_user.get("premium", {}).get(platform, {})
        
        confirm_line = f"**{platform.capitalize()}**: `{platform_data.get('type', 'N/A').replace('_', ' ').title()}`"
        if platform_data.get("until"):
            confirm_line += f" (Expires: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')}`)"
        admin_confirm_text += f"- {confirm_line}\n"

    await safe_edit_message(
        query.message,
        admin_confirm_text,
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer("Premium granted!", show_alert=False)
    user_states.pop(user_id, None) # Clear state

    # Notify the target user
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
                msg_line += f" (Expires: `{platform_data['until'].strftime('%Y-%m-%d %H:%M:%S')}`)"
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
async def back_to_platform_selection_cb(_, query):
    """Callback to go back to platform selection during premium management."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return
    
    state_data = user_states.get(user_id)
    if not isinstance(state_data, dict) or state_data.get("action") not in ["select_platforms_for_premium", "select_premium_plan_for_platforms"]:
        await query.answer("Error: Invalid state for back action. Please restart the process.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    target_user_id = state_data["target_user_id"]
    current_selected_platforms = state_data.get("selected_platforms", {}) # Keep previous selection
    
    user_states[user_id] = {"action": "select_platforms_for_premium", "target_user_id": target_user_id, "selected_platforms": current_selected_platforms}
    
    await safe_edit_message(
        query.message,
        f"✅ User ID `{target_user_id}` received. Select platforms for premium:",
        reply_markup=get_platform_selection_markup(user_id, current_selected_platforms),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    """Callback to prompt for broadcast message (redirects to command usage)."""
    _save_user_data(query.from_user.id, {"last_active": datetime.now()})
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
    """Callback to show personal user settings."""
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})
    
    if is_admin(user_id) or any(is_premium_for_platform(user_id, p) for p in PREMIUM_PLATFORMS):
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"

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
    """Callback to navigate back through menus."""
    data = query.data
    user_id = query.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    if data == "back_to_main_menu":
        # Delete the inline keyboard message and send a new one with reply keyboard
        await query.message.delete()
        await app.send_message(
            query.message.chat.id,
            "🏠 Main Menu",
            reply_markup=get_main_keyboard(user_id)
        )
    elif data == "back_to_settings":
        current_settings = await get_user_settings(user_id)
        compression_status = "OFF (Compression Enabled)" if not current_settings.get("no_compression") else "ON (Original Video)"

        settings_text = "⚙️ Settings Panel\n\n" \
                        f"🗜️ Compression is currently: **{compression_status}**\n\n" \
                        "Use the buttons below to adjust your preferences."
        await safe_edit_message(
            query.message,
            settings_text,
            reply_markup=settings_markup,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    user_states.pop(user_id, None)


@app.on_message(filters.video & filters.private)
async def handle_video_upload(_, msg):
    """Handles incoming video files for Instagram/TikTok uploads based on user state."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    state = user_states.get(user_id)

    # Determine target platform and upload type based on user state
    platform = None
    upload_type = None

    if state == "waiting_for_instagram_reel_video":
        platform = "instagram"
        upload_type = "reel"
    elif state == "waiting_for_tiktok_video":
        platform = "tiktok"
        upload_type = "video"
    else:
        return await msg.reply("❌ Please use the '📤 Insta Reel' or '🎵 TikTok Video' button first to initiate a video upload.")

    if not is_admin(user_id) and not is_premium_for_platform(user_id, platform):
        user_states.pop(user_id, None)
        return await msg.reply(f"❌ Not authorized to upload {platform.capitalize()} videos. Please upgrade to {platform.capitalize()} Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if platform == "instagram" and (not user_data or not user_data.get("instagram_username")):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Instagram session expired. Please login to Instagram first using `/login <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)
    elif platform == "tiktok" and (not user_data or not user_data.get("tiktok_username")):
        user_states.pop(user_id, None)
        return await msg.reply("❌ TikTok session expired (simulated). Please login to TikTok first using `/tiktoklogin <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    processing_msg = await msg.reply(f"⏳ Processing your {platform.capitalize()} video...")
    video_path = None
    transcoded_video_path = None

    try:
        await processing_msg.edit_text("⬇️ 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱𝗶𝗻𝗴 𝗥𝗲𝗲𝗹...")
        video_path = await msg.download()
        logger.info(f"Video downloaded to {video_path}")
        await processing_msg.edit_text("✅ 𝗥𝗲𝗲𝗹 𝗴𝗼𝘁. 𝗣𝗿𝗲𝗽𝗮𝗿𝗶𝗻𝗴 𝗳𝗼𝗿 𝘂𝗽𝗹𝗼𝗮𝗱...")

        settings = await get_user_settings(user_id)
        no_compression = settings.get("no_compression", False) # Get the compression setting
        aspect_ratio_setting = settings.get("aspect_ratio", "original")

        video_to_upload = video_path # Default to original if no compression or transcoding
        
        # Only transcode if compression is enabled or aspect ratio needs adjusting
        if not no_compression or aspect_ratio_setting != "original":
            await processing_msg.edit_text("🔄 Optimizing video (transcoding audio/video)... This may take a moment.")
            transcoded_video_path = f"{video_path}_transcoded.mp4"

            ffmpeg_command = [
                "ffmpeg",
                "-i", video_path,
                "-map_chapters", "-1", # Remove chapter metadata
                "-y", # Overwrite output file without asking
            ]

            if not no_compression:
                ffmpeg_command.extend([
                    "-c:v", "libx264", # Explicitly re-encode video to H.264
                    "-preset", "medium", # Quality/speed trade-off for video encoding
                    "-crf", "23", # Constant Rate Factor for quality (lower is better quality, larger file)
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "44100",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "faststart",
                ])
            else:
                # If no compression, use copy for video and audio but still allow aspect ratio filter
                ffmpeg_command.extend(["-c:v", "copy", "-c:a", "copy"])


            # Add aspect ratio specific filters only if not 'original'
            if aspect_ratio_setting == "9_16":
                # Ensure we add -vf only if it's not already added or if we are copying codecs
                if "-vf" not in ffmpeg_command:
                    ffmpeg_command.extend([
                        "-vf", "scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920),crop=1080:1920,setsar=1:1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                        "-s", "1080x1920" # Set output resolution explicitly (optional but good for consistency)
                    ])
                else: # if -vf is already present due to other options, append to it
                    # This case is less likely with current options but good practice
                    idx = ffmpeg_command.index("-vf") + 1
                    ffmpeg_command[idx] += ",scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920),crop=1080:1920,setsar=1:1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
                    ffmpeg_command.extend(["-s", "1080x1920"]) # Add resolution

            # Add output file to the command
            ffmpeg_command.append(transcoded_video_path)


            logger.info(f"Running FFmpeg command: {' '.join(ffmpeg_command)}")
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"FFmpeg transcoding failed for {video_path}: {stderr.decode()}")
                raise Exception(f"Video transcoding failed: {stderr.decode()}")
            else:
                logger.info(f"FFmpeg transcoding successful for {video_path}. Output: {transcoded_video_path}")
                video_to_upload = transcoded_video_path
                if os.path.exists(video_path):
                    os.remove(video_path)
                    logger.info(f"Deleted original downloaded video file: {video_path}")
        else:
            await processing_msg.edit_text("✅ 𝗢𝗿𝗶𝗴𝗶𝗻𝗮𝗹 𝘃𝗶𝗱𝗲𝗼. 𝗡𝗼 𝗰𝗼𝗺𝗽𝗿𝗲𝘀𝘀𝗶𝗼𝗻.")


        settings = await get_user_settings(user_id)
        caption = settings.get("caption", f"Check out my new {platform.capitalize()} content! 🎥")
        hashtags = settings.get("hashtags", "")
        if hashtags:
            caption = f"{caption}\n\n{hashtags}"

        url = "N/A"
        media_id = "N/A"
        media_type_value = ""

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            if INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
                logger.info(f"Applied proxy {INSTAGRAM_PROXY} for user {user_id}'s Instagram video upload.")

            session = await load_instagram_session(user_id)
            if not session:
                user_states.pop(user_id, None)
                return await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")
            user_upload_client.set_settings(session)
            try:
                user_upload_client.get_timeline_feed()
            except LoginRequired:
                await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")
                logger.error(f"LoginRequired during Instagram video upload (session check) for user {user_id}")
                await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Instagram video upload failed (Login Required - Pre-check)\nUser: `{user_id}`")
                return
            
            await processing_msg.edit_text("🚀 𝗨𝗽𝗹𝗼𝗮𝗱𝗶𝗻𝗴 𝗮𝘀 𝗥𝗲𝗲𝗹...")
            result = user_upload_client.clip_upload(video_to_upload, caption=caption)
            url = f"https://instagram.com/reel/{result.code}"
            media_id = result.pk
            media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type

        elif platform == "tiktok":
            # --- TikTok Upload (Simulated) ---
            tiktok_client_placeholder.set_settings(await load_tiktok_session(user_id))
            try:
                tiktok_client_placeholder.get_timeline_feed()
            except LoginRequired:
                await processing_msg.edit_text("❌ TikTok session expired (simulated). Please login again with `/tiktoklogin <username> <password>`.")
                logger.error(f"LoginRequired during TikTok video upload (simulated session check) for user {user_id}")
                await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ TikTok video upload failed (Simulated Login Required - Pre-check)\nUser: `{user_id}`")
                return

            await processing_msg.edit_text("🚀 Uploading video to TikTok (simulated)...")
            result = await tiktok_client_placeholder.clip_upload(video_to_upload, caption=caption)
            url = f"https://tiktok.com/@{tiktok_client_placeholder.username}/video/{result.code}" # Simulated URL
            media_id = result.code # Using code as a placeholder for media_id
            media_type_value = result.media_type

        db.uploads.insert_one({
            "user_id": user_id,
            "media_id": media_id,
            "media_type": media_type_value,
            "platform": platform, # Added platform field
            "upload_type": upload_type,
            "timestamp": datetime.now(),
            "url": url
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

    except LoginRequired:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} login required. Your session might have expired. Please use `/{platform}login <username> <password>` again.")
        logger.error(f"LoginRequired during {platform} video upload for user {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} video upload failed (Login Required)\nUser: `{user_id}`")
    except ClientError as ce: # Only relevant for instagrapi
        await processing_msg.edit_text(f"❌ {platform.capitalize()} client error during upload: {ce}. Please try again later.")
        logger.error(f"Instagrapi ClientError during {platform} video upload for user {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} video upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} video upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"{platform.capitalize()} video upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} Video Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
    finally:
        # Clean up both original and transcoded files
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            logger.info(f"Deleted original downloaded video file: {video_path}")
        if transcoded_video_path and os.path.exists(transcoded_video_path):
            os.remove(transcoded_video_path)
            logger.info(f"Deleted transcoded video file: {transcoded_video_path}")
        user_states.pop(user_id, None)

@app.on_message(filters.photo & filters.private)
async def handle_photo_upload(_, msg):
    """Handles incoming photo files for Instagram/TikTok uploads based on user state."""
    user_id = msg.from_user.id
    _save_user_data(user_id, {"last_active": datetime.now()})

    state = user_states.get(user_id)

    # Determine target platform and upload type based on user state
    platform = None
    upload_type = None

    if state == "waiting_for_instagram_photo_image":
        platform = "instagram"
        upload_type = "post"
    elif state == "waiting_for_tiktok_photo":
        platform = "tiktok"
        upload_type = "photo"
    else:
        return await msg.reply("❌ Please use the '📸 Insta Photo' or '🖼️ TikTok Photo' button first to initiate an image upload.")
    
    if not is_admin(user_id) and not is_premium_for_platform(user_id, platform):
        user_states.pop(user_id, None)
        return await msg.reply(f"❌ Not authorized to upload {platform.capitalize()} photos. Please upgrade to {platform.capitalize()} Premium with /buypypremium.")

    user_data = _get_user_data(user_id)
    if platform == "instagram" and (not user_data or not user_data.get("instagram_username")):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Instagram session expired. Please login to Instagram first using `/login <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)
    elif platform == "tiktok" and (not user_data or not user_data.get("tiktok_username")):
        user_states.pop(user_id, None)
        return await msg.reply("❌ TikTok session expired (simulated). Please login to TikTok first using `/tiktoklogin <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    processing_msg = await msg.reply(f"⏳ Processing your {platform.capitalize()} image...")
    photo_path = None

    try:
        await processing_msg.edit_text("⬇️ Downloading image...")
        photo_path = await msg.download()
        await processing_msg.edit_text(f"✅ Image downloaded. Uploading to {platform.capitalize()}...")

        settings = await get_user_settings(user_id)
        caption = settings.get("caption", f"Check out my new {platform.capitalize()} photo! 📸")
        hashtags = settings.get("hashtags", "")

        if hashtags:
            caption = f"{caption}\n\n{hashtags}"

        url = "N/A"
        media_id = "N/A"
        media_type_value = ""

        if platform == "instagram":
            user_upload_client = InstaClient()
            user_upload_client.delay_range = [1, 3]
            if INSTAGRAM_PROXY:
                user_upload_client.set_proxy(INSTAGRAM_PROXY)
                logger.info(f"Applied proxy {INSTAGRAM_PROXY} for user {user_id}'s Instagram photo upload.")

            session = await load_instagram_session(user_id)
            if not session:
                user_states.pop(user_id, None)
                return await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")
            user_upload_client.set_settings(session)
            try:
                user_upload_client.get_timeline_feed()
            except LoginRequired:
                await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")
                logger.error(f"LoginRequired during Instagram photo upload (session check) for user {user_id}")
                await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Instagram photo upload failed (Login Required - Pre-check)\nUser: `{user_id}`")
                return
            
            await processing_msg.edit_text("🚀 Uploading image as an Instagram Post...")
            result = user_upload_client.photo_upload(
                photo_path,
                caption=caption,
            )
            url = f"https://instagram.com/p/{result.code}"
            media_id = result.pk
            media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type

        elif platform == "tiktok":
            # --- TikTok Upload (Simulated) ---
            tiktok_client_placeholder.set_settings(await load_tiktok_session(user_id))
            try:
                tiktok_client_placeholder.get_timeline_feed()
            except LoginRequired:
                await processing_msg.edit_text("❌ TikTok session expired (simulated). Please login again with `/tiktoklogin <username> <password>`.")
                logger.error(f"LoginRequired during TikTok photo upload (simulated session check) for user {user_id}")
                await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ TikTok photo upload failed (Simulated Login Required - Pre-check)\nUser: `{user_id}`")
                return

            await processing_msg.edit_text("🚀 Uploading photo to TikTok (simulated)...")
            result = await tiktok_client_placeholder.photo_upload(photo_path, caption=caption)
            url = f"https://tiktok.com/@{tiktok_client_placeholder.username}/photo/{result.code}" # Simulated URL
            media_id = result.code # Using code as a placeholder for media_id
            media_type_value = result.media_type

        db.uploads.insert_one({
            "user_id": user_id,
            "media_id": media_id,
            "media_type": media_type_value,
            "platform": platform, # Added platform field
            "upload_type": upload_type,
            "timestamp": datetime.now(),
            "url": url
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

    except LoginRequired:
        await processing_msg.edit_text(f"❌ {platform.capitalize()} login required. Your session might have expired. Please use `/{platform}login <username> <password>` again.")
        logger.error(f"LoginRequired during {platform} photo upload for user {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} photo upload failed (Login Required)\nUser: `{user_id}`")
    except ClientError as ce: # Only relevant for instagrapi
        await processing_msg.edit_text(f"❌ {platform.capitalize()} client error during upload: {ce}. Please try again later.")
        logger.error(f"Instagrapi ClientError during {platform} photo upload for user {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ {platform.capitalize()} photo upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}`")
    except Exception as e:
        error_msg = f"❌ {platform.capitalize()} photo upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"{platform.capitalize()} photo upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ {platform.capitalize()} Photo Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
    finally:
        if photo_path and os.path.exists(photo_path):
            os.remove(photo_path)
            logger.info(f"Deleted local photo file: {photo_path}")
        user_states.pop(user_id, None)


# === HTTP Server ===

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    server.serve_forever()

if __name__ == "__main__":
    # Ensure sessions directory exists for instagrapi (and general temp files)
    os.makedirs("sessions", exist_ok=True)
    logger.info("Session directory ensured.")

    # Attempt to load/login the bot's own primary Instagram client
    # This is done only if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD are set in .env
    load_instagram_client_session()

    # Start health check server in a separate thread
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Health check server started on port 8080.")

    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}")
        sys.exit(1)

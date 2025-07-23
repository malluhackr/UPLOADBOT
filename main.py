import os
import sys
import asyncio
import threading
import logging
from datetime import datetime, timedelta # Import timedelta for duration calculations
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

# Import the new log handler
from log_handler import send_log_to_channel
import subprocess # Import subprocess module

# === Load env ===

load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002672967163")) # Double-check this ID!
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))

# Instagram Client Credentials (for the bot's own primary account, if any)
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

# Session file path for the bot's primary Instagram client
SESSION_FILE = "instagrapi_session.json"

# Initialize MongoDB Client
try:
    mongo = MongoClient(MONGO_URI)
    db = mongo.instagram_bot
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
required_collections = ["users", "settings", "sessions", "uploads", "scheduled_posts"] # Added scheduled_posts
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
    "lifetime": {"duration": None, "price": "Lifetime (Negotiable)"} # None for lifetime
}


# Keyboards

def get_main_keyboard(is_admin_user=False): # Renamed parameter to avoid conflict
    buttons = [
        [KeyboardButton("📤 Upload Reel"), KeyboardButton("📸 Upload Photo")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("📊 Stats")]
    ]
    # Add new premium buttons to main keyboard
    buttons.append([KeyboardButton("/buypypremium"), KeyboardButton("/premiumdetails")])
    if is_admin_user: # Use the renamed parameter
        buttons.append([KeyboardButton("🛠 Admin Panel"), KeyboardButton("🔄 Restart Bot")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
    [InlineKeyboardButton("📐 Aspect Ratio (Video)", callback_data="set_aspect_ratio")], # New button for aspect ratio
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Add Premium User", callback_data="add_premium_user")], # Renamed callback
    [InlineKeyboardButton("➖ Remove Premium User", callback_data="remove_premium_user")], # Renamed callback
    [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_message")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
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

# Inline keyboard for premium plan selection (Admin side)
def get_premium_plan_markup():
    buttons = []
    for key, value in PREMIUM_PLANS.items():
        if value["duration"] is None: # Lifetime option
            buttons.append([InlineKeyboardButton(f"Lifetime ({value['price']})", callback_data=f"set_premium_{key}")])
        else:
            buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()} ({value['price']})", callback_data=f"set_premium_{key}")])
    buttons.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


# === Helper Functions ===

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_premium_user(user_id):
    user = db.users.find_one({"_id": user_id})
    if not user:
        return False
    
    # Admins are always premium
    if user_id == ADMIN_ID:
        return True

    # Check for 'lifetime' premium
    if user.get("premium_type") == "lifetime":
        return True

    # Check if 'premium_until' exists and is in the future
    premium_until = user.get("premium_until")
    if premium_until and isinstance(premium_until, datetime) and premium_until > datetime.now():
        return True
    
    # If premium_until is in the past, or not set, or is_premium is False
    # we should explicitly set is_premium to False and clear premium_until if it's expired
    if user.get("is_premium") and premium_until and premium_until <= datetime.now():
        # Premium expired, update database
        db.users.update_one(
            {"_id": user_id},
            {"$set": {"is_premium": False}, "$unset": {"premium_until": "", "premium_type": ""}}
        )
        logger.info(f"Premium expired for user {user_id}. Status updated.")
        return False
    
    return False # Default to false if no valid premium found

def get_current_datetime():
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "UTC+5:30" # Assuming fixed timezone for logging, adjust as needed
    }

async def save_instagram_session(user_id, session_data):
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"session": session_data}},
        upsert=True
    )
    logger.info(f"Instagram session saved for user {user_id}")

async def load_instagram_session(user_id):
    session = db.sessions.find_one({"user_id": user_id})
    return session.get("session") if session else None

async def save_user_settings(user_id, settings):
    db.settings.update_one(
        {"_id": user_id},
        {"$set": settings},
        upsert=True
    )
    logger.info(f"User settings saved for user {user_id}")

async def get_user_settings(user_id):
    # Default aspect_ratio to 'original' if not set
    settings = db.settings.find_one({"_id": user_id}) or {}
    if "aspect_ratio" not in settings:
        settings["aspect_ratio"] = "original"
    return settings

async def safe_edit_message(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Couldn't edit message: {e}")

async def restart_bot(msg):
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


# === Message Handlers ===

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    # Update last_active for the user
    db.users.update_one(
        {"_id": user_id},
        {"$set": {"last_active": datetime.now()}},
        upsert=True
    )

    # Add user to DB if not exists
    user = db.users.find_one({"_id": user_id})
    if not user:
        # New users are free by default. Admin can grant premium.
        db.users.insert_one({"_id": user_id, "is_premium": False, "added_by": "self_start", "added_at": datetime.now()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")

    # Non-premium & non-admin users
    if not is_admin(user_id) and not is_premium_user(user_id):
        contact_admin_text = (
            f"👋 **Hi {user_first_name}!**\n\n"
            "**This Bot Lets You Upload Any Size Instagram Reels & Posts Directly From Telegram**.\n\n"
            "• **Unlock Full Premium Features**:\n"
            "• **Upload Unlimited Videos**\n"
            "• **Auto Captions & Hashtags**\n"
            "• **Reel Or Post Type Selection**\n\n"
            "👤 Contact **[ADMIN TOM](https://t.me/CjjTom)** **To Upgrade Your Access**.\n"
            "🔐 **Your Data Is Fully ✅Encrypted**\n\n"
            f"🆔 Your User ID: `{user_id}`"
        )

        join_channel_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅Join Our Channel✅", url="https://t.me/KeralaCaptain")]
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
    welcome_msg = "🤖 **Welcome to Instagram Upload Bot!**\n\n"
    if is_admin(user_id):
        welcome_msg += "🛠 You have **admin privileges**."
    elif is_premium_user(user_id): # Check explicitly for premium for custom message
        welcome_msg += "⭐ **You have premium access**."
        user = db.users.find_one({"_id": user_id})
        premium_until = user.get("premium_until")
        premium_type = user.get("premium_type")
        if premium_type == "lifetime":
            welcome_msg += "\n\n**👑 Lifetime Premium!**"
        elif premium_until:
            remaining_time = premium_until - datetime.now()
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            welcome_msg += f"\n\n**⭐ Premium expires in:** `{days} days, {hours} hours`."
            
    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(is_admin(user_id)), parse_mode=enums.ParseMode.MARKDOWN)


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
    logger.info(f"User {msg.from_user.id} attempting login command.")

    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to use this command.")

    args = msg.text.split()
    if len(args) < 3: # Expects /login <username> <password>
        return await msg.reply("Usage: `/login <instagram_username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 Attempting Instagram login...")

    try:
        user_insta_client = InstaClient()
        user_insta_client.delay_range = [1, 3]

        if INSTAGRAM_PROXY:
            user_insta_client.set_proxy(INSTAGRAM_PROXY)
            logger.info(f"Applied proxy {INSTAGRAM_PROXY} to user {user_id}'s login attempt.")

        session = await load_instagram_session(user_id)
        if session:
            logger.info(f"Attempting to load existing session for user {user_id} (IG: {username}).")
            user_insta_client.set_settings(session)
            try:
                user_insta_client.get_timeline_feed() # Test if session is still valid
                await login_msg.edit_text(f"✅ Already logged in to Instagram as `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                logger.info(f"Existing session for {user_id} is valid.")
                return
            except LoginRequired:
                logger.info(f"Existing session for {user_id} expired. Attempting fresh login.")
                user_insta_client.set_settings({}) # Clear expired settings

        logger.info(f"Attempting fresh Instagram login for user {user_id} with username: {username}")
        user_insta_client.login(username, password)

        session_data = user_insta_client.get_settings()
        await save_instagram_session(user_id, session_data)

        db.users.update_one(
            {"_id": user_id},
            {"$set": {"instagram_username": username}},
            upsert=True
        )

        await login_msg.edit_text("✅ Login successful!")
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
    except (LoginRequired, BadPassword) as e:
        await login_msg.edit_text(f"❌ Instagram login failed: {e}. Please check your credentials.")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ Instagram Login Failed for user `{user_id}` (`{username}`): {e}")
        logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
    except PleaseWaitFewMinutes:
        await login_msg.edit_text("⚠️ Instagram is asking to wait a few minutes before trying again. Please try after some time.")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Instagram 'Please Wait' for user `{user_id}` (`{username}`).")
        logger.warning(f"Instagram 'Please Wait' for user {user_id} ({username}).")
    except Exception as e:
        await login_msg.edit_text(f"❌ An unexpected error occurred during login: {str(e)}")
        logger.error(f"Unhandled error during login for {user_id} ({username}): {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"🔥 Critical Login Error for user `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.command("buypypremium"))
async def buypypremium_cmd(_, msg):
    """Displays premium plans."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    premium_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions.\n\n"
        "**Available Plans:**\n"
        "• **1 Hour Test**: Free (Perfect for new users!)\n"
        "• **3 Days Premium**: `₹10`\n"
        "• **7 Days Premium**: `₹25`\n"
        "• **15 Days Premium**: `₹35`\n"
        "• **1 Month Premium**: `₹60`\n"
        "• **3 Months Premium**: `₹150`\n"
        "• **1 Year Premium**: `Negotiable`\n"
        "• **Lifetime Premium**: `Negotiable`\n\n"
        "To purchase, please contact **[ADMIN TOM](https://t.me/CjjTom)**."
    )
    await msg.reply(premium_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    """Shows user's current premium status."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    user = db.users.find_one({"_id": user_id})
    if not user:
        return await msg.reply("You are not registered with the bot. Please use /start.")

    if is_admin(user_id):
        return await msg.reply("👑 You are the **Admin**. You have permanent full access!", parse_mode=enums.ParseMode.MARKDOWN)

    premium_until = user.get("premium_until")
    premium_type = user.get("premium_type")

    if premium_type == "lifetime":
        status_text = "🎉 You have **Lifetime Premium!** Enjoy unlimited uploads forever."
    elif premium_until and premium_until > datetime.now():
        remaining_time = premium_until - datetime.now()
        days = remaining_time.days
        hours = remaining_time.seconds // 3600
        minutes = (remaining_time.seconds % 3600) // 60
        status_text = (
            f"⭐ **Your Premium Status:**\n"
            f"Plan: `{premium_type.replace('_', ' ').title()}`\n"
            f"Expires on: `{premium_until.strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"Time remaining: `{days} days, {hours} hours, {minutes} minutes`"
        )
    else:
        status_text = "😔 You currently do not have active premium. Use /buypypremium to upgrade!"

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    """Displays the settings menu."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ User Settings", callback_data="user_settings_personal")]
        ])
    else:
        markup = settings_markup

    await msg.reply("⚙️ Settings Panel", reply_markup=markup)

@app.on_message(filters.regex("📤 Upload Reel"))
async def initiate_reel_upload(_, msg):
    """Initiates the process for uploading a video as a Reel."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for Reel upload! Please send me the video file.")
    user_states[user_id] = "waiting_for_reel_video" # Specific state for reel upload

@app.on_message(filters.regex("📸 Upload Photo"))
async def initiate_photo_upload(_, msg):
    """Initiates the process for uploading an image as a Post."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for Photo upload! Please send me the image file.")
    user_states[user_id] = "waiting_for_photo_image" # Specific state for photo upload


@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    """Displays bot usage statistics."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")

    total_users = db.users.count_documents({})
    premium_users = db.users.count_documents({"premium_until": {"$gt": datetime.now()}}) + db.users.count_documents({"premium_type": "lifetime"})
    admin_users = db.users.count_documents({"_id": ADMIN_ID})
    total_uploads = db.uploads.count_documents({})
    total_reel_uploads = db.uploads.count_documents({"upload_type": "reel"})
    total_post_uploads = db.uploads.count_documents({"upload_type": "post"})

    stats_text = (
        "📊 **Bot Statistics:**\n"
        f"👥 Total users: `{total_users}`\n"
        f"⭐ Premium users: `{premium_users}`\n"
        f"👑 Admin users: `{admin_users}`\n"
        f"📈 Total uploads: `{total_uploads}`\n"
        f"🎬 Total Reel uploads: `{total_reel_uploads}`\n"
        f"📸 Total Post uploads: `{total_post_uploads}`"
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
            # Skip sending to admin to avoid duplicate message for self
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
    state = user_states.get(user_id)
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if state == "waiting_for_caption":
        caption = msg.text
        await save_user_settings(user_id, {"caption": caption})
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif state == "waiting_for_hashtags":
        hashtags = msg.text
        await save_user_settings(user_id, {"hashtags": hashtags})
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif state == "waiting_for_add_premium_user_id": # New state for premium user addition
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            user_states[user_id] = f"waiting_for_premium_plan_for_{target_user_id}" # Store target user ID
            await msg.reply(
                f"✅ User ID `{target_user_id}` received. Now, select a premium plan for this user:",
                reply_markup=get_premium_plan_markup(),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
            user_states.pop(user_id, None) # Clear state on invalid input
    elif state == "waiting_for_remove_premium_user_id": # New state for premium user removal
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            if target_user_id == ADMIN_ID:
                await msg.reply("❌ Cannot remove the admin user.", reply_markup=admin_markup)
            else:
                # Remove premium status
                result = db.users.update_one(
                    {"_id": target_user_id},
                    {"$set": {"is_premium": False, "removed_by": user_id, "removed_at": datetime.now()},
                     "$unset": {"premium_until": "", "premium_type": ""}} # Clear premium details
                )
                if result.matched_count > 0:
                    await msg.reply(f"✅ User `{target_user_id}` has been removed from premium users.", reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)
                    await send_log_to_channel(app, LOG_CHANNEL, f"➖ Admin `{user_id}` removed premium user: `{target_user_id}`")
                else:
                    await msg.reply("⚠️ User not found in database.", reply_markup=admin_markup)
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
        user_states.pop(user_id, None) # Clear state after processing


# --- Callback Handlers ---

@app.on_callback_query(filters.regex("^upload_type$"))
async def upload_type_cb(_, query):
    """Callback to show upload type options."""
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    await safe_edit_message(
        query.message,
        "📌 Select upload type:",
        reply_markup=upload_type_markup
    )

@app.on_callback_query(filters.regex("^set_type_"))
async def set_type_cb(_, query):
    """Callback to set the preferred upload type (Reel/Post)."""
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    upload_type = query.data.split("_")[-1]
    current_settings = await get_user_settings(user_id)
    current_settings["upload_type"] = upload_type
    await save_user_settings(user_id, current_settings)

    await query.answer(f"✅ Upload type set to {upload_type.capitalize()}!", show_alert=False)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel",
        reply_markup=settings_markup
    )

@app.on_callback_query(filters.regex("^set_aspect_ratio$")) # New callback for aspect ratio
async def set_aspect_ratio_cb(_, query):
    """Callback to show aspect ratio options."""
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}})
    await safe_edit_message(
        query.message,
        "📐 Select desired aspect ratio for videos:",
        reply_markup=aspect_ratio_markup
    )

@app.on_callback_query(filters.regex("^set_ar_")) # New callback to set aspect ratio
async def set_ar_cb(_, query):
    """Callback to set the preferred aspect ratio for videos."""
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    aspect_ratio_key = query.data.split("_")[-2:] # e.g., ['9', '16'] or ['original']
    aspect_ratio_value = "_".join(aspect_ratio_key)

    current_settings = await get_user_settings(user_id)
    current_settings["aspect_ratio"] = aspect_ratio_value
    await save_user_settings(user_id, current_settings)

    display_text = "Original" if aspect_ratio_value == "original" else "9:16 (Crop/Fit)"
    await query.answer(f"✅ Aspect ratio set to {display_text}!", show_alert=False)
    await safe_edit_message(
        query.message,
        "⚙️ Settings Panel",
        reply_markup=settings_markup
    )

@app.on_callback_query(filters.regex("^set_caption$"))
async def set_caption_cb(_, query):
    """Callback to prompt for new caption."""
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
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
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    user_states[user_id] = "waiting_for_hashtags"
    current_settings = await get_user_settings(user_id)
    current_hashtags = current_settings.get("hashtags", "Not set")
    await safe_edit_message(
        query.message,
        f"🏷️ Please send the new hashtags for your uploads (e.g., #coding #bot).\n\n"
        f"Current hashtags: `{current_hashtags}`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
    """Callback to display the admin panel."""
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
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
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
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
        is_premium_status = is_premium_user(user_id) # Use the updated check
        instagram_username = user.get("instagram_username", "N/A")
        added_at = user.get("added_at", "N/A").strftime("%Y-%m-%d") if isinstance(user.get("added_at"), datetime) else "N/A"
        last_active = user.get("last_active", "N/A").strftime("%Y-%m-%d %H:%M") if isinstance(user.get("last_active"), datetime) else "N/A"
        premium_until = user.get("premium_until", "N/A")
        premium_type = user.get("premium_type", "N/A")

        status = ""
        if user_id == ADMIN_ID:
            status = "👑 Admin"
        elif premium_type == "lifetime":
            status = "⭐ Lifetime Premium"
        elif is_premium_status:
            status = f"⭐ Premium until: `{premium_until.strftime('%Y-%m-%d %H:%M')}`"
        else:
            status = "Free User"

        user_list_text += (
            f"ID: `{user_id}` | Status: {status}\n"
            f"IG: `{instagram_username}` | Added: `{added_at}` | Last Active: `{last_active}`\n"
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

@app.on_callback_query(filters.regex("^add_premium_user$")) # Renamed callback
async def add_premium_user_cb(_, query):
    """Callback to prompt for user ID to add as premium."""
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    user_states[query.from_user.id] = "waiting_for_add_premium_user_id" # New state
    await safe_edit_message(
        query.message,
        "➕ Please send the **User ID** to grant premium access to."
    )

@app.on_callback_query(filters.regex("^set_premium_")) # New callback for setting premium plan
async def set_premium_plan_cb(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    # Extract target user ID from state
    state_parts = user_states.get(user_id, "").split("_")
    if len(state_parts) != 5 or state_parts[0] != "waiting" or state_parts[3] != "for":
        await query.answer("Error: User selection lost. Please try 'Add Premium User' again.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    target_user_id = int(state_parts[4])
    premium_plan_key = query.data.split("set_premium_")[1]

    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("Invalid premium plan selected.", show_alert=True)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    plan_details = PREMIUM_PLANS[premium_plan_key]
    new_premium_until = None
    if plan_details["duration"] is not None:
        new_premium_until = datetime.now() + plan_details["duration"]

    # Update user's premium status
    update_data = {
        "is_premium": True,
        "premium_type": premium_plan_key,
        "added_by": user_id,
        "added_at": datetime.now()
    }
    if new_premium_until:
        update_data["premium_until"] = new_premium_until
    else: # For lifetime, remove premium_until if it exists
        update_data["$unset"] = {"premium_until": ""}

    db.users.update_one({"_id": target_user_id}, {"$set": update_data}, upsert=True)

    # Construct message for admin and target user
    admin_confirm_text = f"✅ Premium granted to user `{target_user_id}` for **{premium_plan_key.replace('_', ' ').title()}**."
    if new_premium_until:
        admin_confirm_text += f"\nExpires on: `{new_premium_until.strftime('%Y-%m-%d %H:%M:%S')}`"

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
            f"You have been granted **{premium_plan_key.replace('_', ' ').title()}** premium access!"
        )
        if new_premium_until:
            user_msg += f"\n\nYour premium will expire on: `{new_premium_until.strftime('%Y-%m-%d %H:%M:%S')}`."
        else:
            user_msg += "\n\nEnjoy **Lifetime** premium! ✨"
        
        await app.send_message(target_user_id, user_msg, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 Premium granted notification sent to `{target_user_id}` by Admin `{user_id}`. Plan: `{premium_plan_key}`"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {target_user_id} about premium: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ Failed to notify user `{target_user_id}` about premium. Error: `{str(e)}`"
        )


@app.on_callback_query(filters.regex("^remove_premium_user$")) # Renamed callback
async def remove_premium_user_cb(_, query):
    """Callback to prompt for user ID to remove from premium."""
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    user_states[query.from_user.id] = "waiting_for_remove_premium_user_id" # New state
    await safe_edit_message(
        query.message,
        "➖ Please send the **User ID** to remove premium access from."
    )

@app.on_callback_query(filters.regex("^broadcast_message$"))
async def broadcast_message_cb(_, query):
    """Callback to prompt for broadcast message."""
    db.users.update_one({"_id": query.from_user.id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    await safe_edit_message(
        query.message,
        "📢 Please send the message you want to broadcast to all users.\n\n"
        "Use `/broadcast <message>` command instead."
    )
    # The actual broadcast logic is in `broadcast_cmd`, this just guides the user
    # user_states[query.from_user.id] = "waiting_for_broadcast_message" # No longer needed here directly

@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def user_settings_personal_cb(_, query):
    """Callback to show personal user settings."""
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active
    if is_admin(user_id) or is_premium_user(user_id):
        await safe_edit_message(
            query.message,
            "⚙️ Your Personal Settings",
            reply_markup=settings_markup
        )
    else:
        await query.answer("❌ Not authorized.", show_alert=True)
        return

@app.on_callback_query(filters.regex("^back_to_"))
async def back_to_cb(_, query):
    """Callback to navigate back through menus."""
    data = query.data
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if data == "back_to_main_menu":
        # Delete the inline keyboard message and send a new one with reply keyboard
        await query.message.delete()
        await app.send_message(
            query.message.chat.id,
            "🏠 Main Menu",
            reply_markup=get_main_keyboard(is_admin(user_id))
        )
    elif data == "back_to_settings":
        await safe_edit_message(
            query.message,
            "⚙️ Settings Panel",
            reply_markup=settings_markup
        )
    user_states.pop(user_id, None)


@app.on_message(filters.video & filters.private)
async def handle_video_upload(_, msg):
    """Handles incoming video files for Instagram Reel uploads."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    # Ensure the user has initiated a Reel upload specifically
    if user_states.get(user_id) != "waiting_for_reel_video":
        return await msg.reply("❌ Please use the '📤 Upload Reel' button first to initiate a video upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Instagram session expired. Please login to Instagram first using `/login <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    processing_msg = await msg.reply("⏳ Processing your video...")
    video_path = None
    transcoded_video_path = None # New variable for the transcoded file

    try:
        await processing_msg.edit_text("⬇️ Downloading video...")
        video_path = await msg.download()
        logger.info(f"Video downloaded to {video_path}")
        await processing_msg.edit_text("✅ Video downloaded. Preparing for Instagram...")

        # --- NEW: Transcode Video Audio (and optionally video) using ffmpeg ---
        await processing_msg.edit_text("🔄 Optimizing video for Instagram (transcoding audio/video)... This may take a moment.")
        # Create a temporary file name for the transcoded video
        transcoded_video_path = f"{video_path}_transcoded.mp4"

        settings = await get_user_settings(user_id)
        aspect_ratio_setting = settings.get("aspect_ratio", "original") # Get user's aspect ratio preference

        # Base FFmpeg command
        ffmpeg_command = [
            "ffmpeg",
            "-i", video_path,
            "-c:v", "libx264", # Explicitly re-encode video to H.264
            "-preset", "medium", # Quality/speed trade-off for video encoding
            "-crf", "23", # Constant Rate Factor for quality (lower is better quality, larger file)
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-movflags", "faststart",
            "-map_chapters", "-1", # Remove chapter metadata
            "-y", # Overwrite output file without asking
        ]

        # Add aspect ratio specific filters
        if aspect_ratio_setting == "9_16":
            # Scale and pad/crop to 9:16 aspect ratio (1080x1920 is a common resolution for 9:16)
            # This complex filter attempts to intelligently resize.
            # scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920) scales video to fit 1080 width or 1920 height
            # crop=1080:1920 adjusts it to 9:16, potentially cropping edges
            # pad=1080:1920:(ow-iw)/2:(oh-ih)/2 adds black bars if video is wider than 9:16
            ffmpeg_command.extend([
                "-vf", "scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920),crop=1080:1920,setsar=1:1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-s", "1080x1920" # Set output resolution explicitly (optional but good for consistency)
            ])
        
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
            # If transcoding was successful, use the transcoded file for upload
            video_to_upload = transcoded_video_path
            # Clean up the original downloaded file to save space
            if os.path.exists(video_path):
                os.remove(video_path)
                logger.info(f"Deleted original downloaded video file: {video_path}")
        # --- END NEW FFmpeg Section ---


        settings = await get_user_settings(user_id)
        caption = settings.get("caption", "Check out my new content! 🎥")
        hashtags = settings.get("hashtags", "")

        if hashtags:
            caption = f"{caption}\n\n{hashtags}"

        upload_type = "reel" # Force to reel for video file uploads in this handler.

        user_upload_client = InstaClient()
        user_upload_client.delay_range = [1, 3]
        if INSTAGRAM_PROXY:
            user_upload_client.set_proxy(INSTAGRAM_PROXY)
            logger.info(f"Applied proxy {INSTAGRAM_PROXY} for user {user_id}'s video upload.")

        session = await load_instagram_session(user_id)
        if not session:
            user_states.pop(user_id, None)
            return await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")

        user_upload_client.set_settings(session)

        # Ensure the session is valid before proceeding to upload
        try:
            user_upload_client.get_timeline_feed()
        except LoginRequired:
            await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")
            logger.error(f"LoginRequired during video upload (session check) for user {user_id}")
            await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Video upload failed (Login Required - Pre-check)\nUser: `{user_id}`")
            return
        except ClientError as ce:
            await processing_msg.edit_text(f"❌ Instagram client error during session check: {ce}. Please try again.")
            logger.error(f"Instagrapi ClientError during video upload session check for user {user_id}: {ce}")
            await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Video upload failed (Client Error - Pre-check)\nUser: `{user_id}`\nError: `{ce}`")
            return
        except Exception as ex:
            await processing_msg.edit_text(f"❌ An unexpected error occurred during Instagram session check: {ex}. Please try again.")
            logger.error(f"Unexpected error during video upload session check for user {user_id}: {ex}")
            await send_log_to_channel(app, LOG_CHANNEL, f"🔥 Critical video upload error (Session Check)\nUser: `{user_id}`\nError: `{ex}`")
            return

        await processing_msg.edit_text("🚀 Uploading video as a Reel...")
        # Use the transcoded_video_path for upload
        result = user_upload_client.clip_upload(video_to_upload, caption=caption)
        url = f"https://instagram.com/reel/{result.code}"

        # Record successful upload
        media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type

        db.uploads.insert_one({
            "user_id": user_id,
            "media_id": result.pk,
            "media_type": media_type_value,
            "upload_type": upload_type,
            "timestamp": datetime.now(),
            "url": url
        })

        log_msg = (
            f"📤 New {upload_type.capitalize()} Upload\n\n"
            f"👤 User: `{user_id}`\n"
            f"📛 Username: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 URL: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await processing_msg.edit_text(f"✅ Uploaded successfully!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except LoginRequired:
        await processing_msg.edit_text("❌ Instagram login required. Your session might have expired. Please use `/login <username> <password>` again.")
        logger.error(f"LoginRequired during video upload for user {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Video upload failed (Login Required)\nUser: `{user_id}`")
    except ClientError as ce:
        await processing_msg.edit_text(f"❌ Instagram client error during upload: {ce}. Please try again later.")
        logger.error(f"Instagrapi ClientError during video upload for user {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Video upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}`")
    except Exception as e:
        error_msg = f"❌ Video upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"Video upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ Video Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
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
    """Handles incoming photo files for Instagram Post uploads."""
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}}) # Update last_active

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    # Ensure the user has initiated a Photo upload specifically
    if user_states.get(user_id) != "waiting_for_photo_image":
        return await msg.reply("❌ Please use the '📸 Upload Photo' button first to initiate an image upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Instagram session expired. Please login to Instagram first using `/login <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    processing_msg = await msg.reply("⏳ Processing your image...")
    photo_path = None

    try:
        await processing_msg.edit_text("⬇️ Downloading image...")
        photo_path = await msg.download()
        await processing_msg.edit_text("✅ Image downloaded. Uploading to Instagram...")

        settings = await get_user_settings(user_id)
        caption = settings.get("caption", "Check out my new photo! 📸")
        hashtags = settings.get("hashtags", "")

        if hashtags:
            caption = f"{caption}\n\n{hashtags}"

        upload_type = "post" # For `filters.photo`, it will ALWAYS be a post upload.

        user_upload_client = InstaClient()
        user_upload_client.delay_range = [1, 3]
        if INSTAGRAM_PROXY:
            user_upload_client.set_proxy(INSTAGRAM_PROXY)
            logger.info(f"Applied proxy {INSTAGRAM_PROXY} for user {user_id}'s photo upload.")

        session = await load_instagram_session(user_id)
        if not session:
            user_states.pop(user_id, None)
            return await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")

        user_upload_client.set_settings(session)

        # Ensure the session is valid before proceeding to upload
        try:
            user_upload_client.get_timeline_feed()
        except LoginRequired:
            await processing_msg.edit_text("❌ Instagram session expired. Please login again with `/login <username> <password>`.")
            logger.error(f"LoginRequired during photo upload (session check) for user {user_id}")
            await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Photo upload failed (Login Required - Pre-check)\nUser: `{user_id}`")
            return
        except ClientError as ce:
            await processing_msg.edit_text(f"❌ Instagram client error during session check: {ce}. Please try again.")
            logger.error(f"Instagrapi ClientError during photo upload session check for user {user_id}: {ce}")
            await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Photo upload failed (Client Error - Pre-check)\nUser: `{user_id}`\nError: `{ce}`")
            return
        except Exception as ex:
            await processing_msg.edit_text(f"❌ An unexpected error occurred during Instagram session check: {ex}. Please try again.")
            logger.error(f"Unexpected error during photo upload session check for user {user_id}: {ex}")
            await send_log_to_channel(app, LOG_CHANNEL, f"🔥 Critical photo upload error (Session Check)\nUser: `{user_id}`\nError: `{ex}`")
            return

        await processing_msg.edit_text("🚀 Uploading image as a Post...")
        result = user_upload_client.photo_upload(
            photo_path,
            caption=caption,
        )
        url = f"https://instagram.com/p/{result.code}"

        # Record successful upload
        media_type_value = result.media_type.value if hasattr(result.media_type, 'value') else result.media_type

        db.uploads.insert_one({
            "user_id": user_id,
            "media_id": result.pk,
            "media_type": media_type_value,
            "upload_type": upload_type,
            "timestamp": datetime.now(),
            "url": url
        })

        log_msg = (
            f"📤 New {upload_type.capitalize()} Upload\n\n"
            f"👤 User: `{user_id}`\n"
            f"📛 Username: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 URL: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await processing_msg.edit_text(f"✅ Uploaded successfully!\n\n{url}")
        await send_log_to_channel(app, LOG_CHANNEL, log_msg)

    except LoginRequired:
        await processing_msg.edit_text("❌ Instagram login required. Your session might have expired. Please use `/login <username> <password>` again.")
        logger.error(f"LoginRequired during photo upload for user {user_id}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Photo upload failed (Login Required)\nUser: `{user_id}`")
    except ClientError as ce:
        await processing_msg.edit_text(f"❌ Instagram client error during upload: {ce}. Please try again later.")
        logger.error(f"Instagrapi ClientError during photo upload for user {user_id}: {ce}")
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Photo upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}`")
    except Exception as e:
        error_msg = f"❌ Photo upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"Photo upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ Photo Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
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
    # Ensure sessions directory exists
    os.makedirs("sessions", exist_ok=True)
    logger.info("Session directory ensured.")

    # Attempt to load/login the bot's own primary Instagram client
    # This is done only if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD are set in .env
    load_instagram_client_session()

    # Start health check server
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Health check server started on port 8080.")

    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}")
        sys.exit(1)

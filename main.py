# bot.py (COMPLETELY FIXED & ENHANCED VERSION)

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

# Import the new log handler (ASSUMES log_handler.py EXISTS AND HAS send_log_to_channel)
from log_handler import send_log_to_channel
import subprocess

# === Load env ===

load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002672967163"))
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200")) # IMPORTANT: UPDATE THIS TO YOUR ACTUAL ADMIN ID

# Instagram Client Credentials (for the bot's own primary account, if any)
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

# Session file path for the bot's primary Instagram client
SESSION_FILE = "instagrapi_session.json"

# Initialize MongoDB Client
try:
    mongo = MongoClient(MONGO_URI)
    db = mongo.instagram_bot # Using 'instagram_bot' database name
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
# Using a dictionary for states, Pyrogram doesn't have a built-in ConversationHandler like PTB.
# We'll enhance this to store more context for admin operations.
user_states = {} # {user_id: {"state": "action", "data": {}}}

# --- PREMIUM DEFINITIONS ---
# Added 'platforms' to premium plans to denote what's included
PREMIUM_PLANS = {
    "1_hour_test": {"duration": timedelta(hours=1), "price": "Free", "platforms": ["instagram"]}, # Default to IG for now
    "3_days": {"duration": timedelta(days=3), "price": "₹10", "platforms": ["instagram"]},
    "7_days": {"duration": timedelta(days=7), "price": "₹25", "platforms": ["instagram"]},
    "15_days": {"duration": timedelta(days=15), "price": "₹35", "platforms": ["instagram"]},
    "1_month": {"duration": timedelta(days=30), "price": "₹60", "platforms": ["instagram"]},
    "3_months": {"duration": timedelta(days=90), "price": "₹150", "platforms": ["instagram"]},
    "1_year": {"duration": timedelta(days=365), "price": "Negotiable", "platforms": ["instagram"]},
    "lifetime": {"duration": None, "price": "Lifetime (Negotiable)", "platforms": ["instagram"]}
}

# --- KEYBOARDS ---

def get_main_keyboard(is_admin_user=False):
    buttons = [
        [KeyboardButton("📤 Upload Reel"), KeyboardButton("📸 Upload Photo")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("📊 Stats")],
        [KeyboardButton("💰 Buy Premium"), KeyboardButton("✨ My Premium")] # Renamed for clarity
    ]
    if is_admin_user:
        buttons.append([KeyboardButton("🛠 Admin Panel"), KeyboardButton("🔄 Restart Bot")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="settings_upload_type")], # Prefixed with settings_
    [InlineKeyboardButton("📝 Caption", callback_data="settings_set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="settings_set_hashtags")],
    [InlineKeyboardButton("📐 Aspect Ratio (Video)", callback_data="settings_set_aspect_ratio")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="admin_users_list")],
    [InlineKeyboardButton("➕ Add Premium User", callback_data="admin_add_premium_user")],
    [InlineKeyboardButton("➖ Remove Premium User", callback_data="admin_remove_premium_user")],
    [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast_message")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
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

# New: Inline keyboard for premium platform selection (Admin side)
def get_premium_platform_markup():
    buttons = [
        [InlineKeyboardButton("Instagram Only", callback_data="premium_platform_instagram")],
        [InlineKeyboardButton("TikTok Only (Placeholder)", callback_data="premium_platform_tiktok")],
        [InlineKeyboardButton("Both (Instagram & TikTok)", callback_data="premium_platform_both")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_operation")]
    ]
    return InlineKeyboardMarkup(buttons)

# New: Inline keyboard for premium plan selection (Admin side)
def get_premium_plan_markup():
    buttons = []
    for key, value in PREMIUM_PLANS.items():
        if value["duration"] is None: # Lifetime option
            buttons.append([InlineKeyboardButton(f"👑 Lifetime ({value['price']})", callback_data=f"set_plan_{key}")])
        else:
            # Using title() for better display
            buttons.append([InlineKeyboardButton(f"{key.replace('_', ' ').title()} ({value['price']})", callback_data=f"set_plan_{key}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_operation")])
    return InlineKeyboardMarkup(buttons)

# --- HELPER FUNCTIONS ---

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_premium_user(user_id, platform=None):
    """
    Checks if a user is premium, optionally for a specific platform.
    Automatically handles expiry.
    """
    user = db.users.find_one({"_id": user_id})
    if not user:
        return False

    # Admins always have all premium features
    if user_id == ADMIN_ID:
        return True

    # Check for general premium status if no specific platform is requested
    if platform is None:
        # If 'is_premium' flag is explicitly false, or no premium_type/premium_until, it's not premium
        if not user.get("is_premium", False) and not user.get("premium_type"):
            return False
        
        # Check for 'lifetime' premium
        if user.get("premium_type") == "lifetime":
            return True

        # Check for time-bound premium
        premium_until = user.get("premium_until")
        if premium_until and isinstance(premium_until, datetime):
            if premium_until > datetime.now():
                return True
            else:
                # Premium expired, update database
                db.users.update_one(
                    {"_id": user_id},
                    {"$set": {"is_premium": False}, "$unset": {"premium_until": "", "premium_type": "", "is_instagram_premium": "", "is_tiktok_premium": ""}}
                )
                logger.info(f"Premium expired for user {user_id}. Status updated.")
                return False
        return False # No valid premium found

    # Check for specific platform premium
    if platform == "instagram":
        if not user.get("is_instagram_premium", False):
            return False
    elif platform == "tiktok":
        if not user.get("is_tiktok_premium", False):
            return False
    else:
        logger.warning(f"Unknown platform requested for premium check: {platform}")
        return False

    # If platform check passed, now check general premium validity (expiry/lifetime)
    return is_premium_user(user_id, platform=None) # Re-use general check for expiry/lifetime

def get_current_datetime_info():
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "UTC+5:30" # Adjust as needed
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
    settings = db.settings.find_one({"_id": user_id}) or {}
    if "aspect_ratio" not in settings:
        settings["aspect_ratio"] = "original"
    return settings

async def safe_edit_message(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Couldn't edit message: {e}")

async def restart_bot_process(msg):
    dt = get_current_datetime_info()
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


# === MESSAGE HANDLERS ===

@app.on_message(filters.command("start"))
async def start_command(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    db.users.update_one(
        {"_id": user_id},
        {"$set": {"last_active": datetime.now()}},
        upsert=True
    )

    user = db.users.find_one({"_id": user_id})
    if not user:
        db.users.insert_one({"_id": user_id, "is_premium": False, "is_instagram_premium": False, "is_tiktok_premium": False, "added_by": "self_start", "added_at": datetime.now()})
        logger.info(f"New user {user_id} added to database via start command.")
        await send_log_to_channel(app, LOG_CHANNEL, f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")

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

    welcome_msg = "🤖 **Welcome to Instagram Upload Bot!**\n\n"
    if is_admin(user_id):
        welcome_msg += "🛠 You have **admin privileges**."
    elif is_premium_user(user_id):
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


@app.on_message(filters.command("restart") & filters.user(ADMIN_ID))
async def restart_command(_, msg):
    await restart_bot_process(msg)

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    logger.info(f"User {msg.from_user.id} attempting login command.")
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id, platform="instagram"): # Check for Instagram premium
        return await msg.reply("❌ Not authorized to use this command. Requires Instagram premium.")

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
            logger.info(f"Applied proxy {INSTAGRAM_PROXY} to user {user_id}'s login attempt.")

        session = await load_instagram_session(user_id)
        if session:
            logger.info(f"Attempting to load existing session for user {user_id} (IG: {username}).")
            user_insta_client.set_settings(session)
            try:
                user_insta_client.get_timeline_feed()
                await login_msg.edit_text(f"✅ Already logged in to Instagram as `{username}` (session reloaded).", parse_mode=enums.ParseMode.MARKDOWN)
                logger.info(f"Existing session for {user_id} is valid.")
                return
            except LoginRequired:
                logger.info(f"Existing session for {user_id} expired. Attempting fresh login.")
                user_insta_client.set_settings({})

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

@app.on_message(filters.regex("💰 Buy Premium")) # New button for premium info
async def buy_premium_button_handler(_, msg):
    await buypypremium_cmd(_, msg)

@app.on_message(filters.command("buypypremium"))
async def buypypremium_cmd(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    premium_text = (
        "⭐ **Upgrade to Premium!** ⭐\n\n"
        "Unlock full features and upload unlimited content without restrictions.\n\n"
        "**Available Plans:**\n"
    )
    for key, value in PREMIUM_PLANS.items():
        if value["duration"] is None:
            premium_text += f"• **{key.replace('_', ' ').title()}**: {value['price']} (Platforms: {', '.join([p.capitalize() for p in value['platforms']])})\n"
        else:
            duration_str = ""
            if value["duration"].days > 0:
                duration_str += f"{value['duration'].days} Days"
            elif value["duration"].seconds // 3600 > 0:
                duration_str += f"{value['duration'].seconds // 3600} Hours"
            premium_text += f"• **{duration_str} Premium**: {value['price']} (Platforms: {', '.join([p.capitalize() for p in value['platforms']])})\n"
        
    premium_text += (
        "\nTo purchase, please contact **[ADMIN TOM](https://t.me/CjjTom)**."
    )
    await msg.reply(premium_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("✨ My Premium")) # New button for premium details
async def my_premium_button_handler(_, msg):
    await premium_details_cmd(_, msg)

@app.on_message(filters.command("premiumdetails"))
async def premium_details_cmd(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    user = db.users.find_one({"_id": user_id})
    if not user:
        return await msg.reply("You are not registered with the bot. Please use /start.")

    if is_admin(user_id):
        return await msg.reply("👑 You are the **Admin**. You have permanent full access to all features!", parse_mode=enums.ParseMode.MARKDOWN)

    premium_until = user.get("premium_until")
    premium_type = user.get("premium_type")
    is_ig_premium = user.get("is_instagram_premium", False)
    is_tiktok_premium = user.get("is_tiktok_premium", False)

    if premium_type == "lifetime":
        status_text = "🎉 You have **Lifetime Premium!** Enjoy unlimited uploads forever.\n\n"
    elif premium_until and premium_until > datetime.now():
        remaining_time = premium_until - datetime.now()
        days = remaining_time.days
        hours = remaining_time.seconds // 3600
        minutes = (remaining_time.seconds % 3600) // 60
        status_text = (
            f"⭐ **Your Premium Status:**\n"
            f"Plan: `{premium_type.replace('_', ' ').title()}`\n"
            f"Expires on: `{premium_until.strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"Time remaining: `{days} days, {hours} hours, {minutes} minutes`\n\n"
        )
    else:
        status_text = "😔 You currently do not have active premium. Use **Buy Premium** to upgrade!\n\n"

    status_text += "**Platform Access:**\n"
    status_text += f"Instagram: {'✅ Active' if is_ig_premium else '❌ Inactive'}\n"
    status_text += f"TikTok: {'✅ Active' if is_tiktok_premium else '❌ Inactive'}\n"

    await msg.reply(status_text, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized. Settings are for premium users only.")

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ User Settings", callback_data="user_settings_personal")]
        ])
    else:
        markup = settings_markup

    await msg.reply("⚙️ Settings Panel", reply_markup=markup)

@app.on_message(filters.regex("📤 Upload Reel"))
async def initiate_reel_upload(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    if not is_admin(user_id) and not is_premium_user(user_id, platform="instagram"): # Check for Instagram premium
        return await msg.reply("❌ Not authorized to upload Reels. Requires Instagram premium.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for Reel upload! Please send me the video file.")
    user_states[user_id] = {"state": "waiting_for_reel_video"} # Use dict for state

@app.on_message(filters.regex("📸 Upload Photo"))
async def initiate_photo_upload(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    if not is_admin(user_id) and not is_premium_user(user_id, platform="instagram"): # Check for Instagram premium
        return await msg.reply("❌ Not authorized to upload Photos. Requires Instagram premium.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`", parse_mode=enums.ParseMode.MARKDOWN)

    await msg.reply("✅ Ready for Photo upload! Please send me the image file.")
    user_states[user_id] = {"state": "waiting_for_photo_image"} # Use dict for state


@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")

    total_users = db.users.count_documents({})
    # Count premium users by checking if they are admin or have valid premium_until/lifetime
    active_premium_users = 0
    for user in db.users.find({}):
        if is_admin(user["_id"]) or is_premium_user(user["_id"]):
            active_premium_users += 1

    total_uploads = db.uploads.count_documents({})
    total_reel_uploads = db.uploads.count_documents({"upload_type": "reel"})
    total_post_uploads = db.uploads.count_documents({"upload_type": "post"})

    stats_text = (
        "📊 **Bot Statistics:**\n"
        f"👥 Total users: `{total_users}`\n"
        f"⭐ Active Premium users: `{active_premium_users}`\n"
        f"👑 Admin users (counted in active premium): `{1 if is_admin(ADMIN_ID) else 0}`\n" # Admin is only one user
        f"📈 Total uploads: `{total_uploads}`\n"
        f"🎬 Total Reel uploads: `{total_reel_uploads}`\n"
        f"📸 Total Post uploads: `{total_post_uploads}`"
    )
    await msg.reply(stats_text, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.regex("🛠 Admin Panel"))
async def admin_panel_button_handler(_, msg):
    await admin_panel_cmd(_, msg)

@app.on_message(filters.command("admin") & filters.user(ADMIN_ID))
async def admin_panel_cmd(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await msg.reply(
        "🛠 Welcome to the Admin Panel!",
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    user_states.pop(user_id, None) # Clear any ongoing user state when entering admin panel

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_ID))
async def broadcast_cmd(_, msg):
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
    await send_log_to_channel(app, LOG_CHANNEL,
        f"📢 Broadcast initiated by Admin `{msg.from_user.id}`\n"
        f"Sent: `{sent_count}`, Failed: `{failed_count}`"
    )

# --- STATE-DEPENDENT MESSAGE HANDLERS ---

@app.on_message(filters.text & filters.private & ~filters.command(""))
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state_info = user_states.get(user_id)
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    if not state_info or "state" not in state_info:
        # If no specific state, act as a general fallback or return to main menu
        await msg.reply("I'm not sure what to do with that. Please use the menu buttons or commands.")
        return

    current_state = state_info["state"]

    if current_state == "waiting_for_caption":
        caption = msg.text
        await save_user_settings(user_id, {"caption": caption})
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif current_state == "waiting_for_hashtags":
        hashtags = msg.text
        await save_user_settings(user_id, {"hashtags": hashtags})
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup, parse_mode=enums.ParseMode.MARKDOWN)
        user_states.pop(user_id, None)
    elif current_state == "waiting_for_add_premium_user_id":
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            # Store target user ID in user_states data for the next step
            user_states[user_id] = {"state": "waiting_for_premium_platform_selection", "target_user_id": target_user_id}
            await msg.reply(
                f"✅ User ID `{target_user_id}` received. Now, select the platform(s) for premium access:",
                reply_markup=get_premium_platform_markup(),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
            user_states.pop(user_id, None)
    elif current_state == "waiting_for_remove_premium_user_id":
        if not is_admin(user_id):
            return await msg.reply("❌ You are not authorized to perform this action.")
        try:
            target_user_id = int(msg.text)
            if target_user_id == ADMIN_ID:
                await msg.reply("❌ Cannot remove the admin user.", reply_markup=admin_markup)
            else:
                user_to_remove = db.users.find_one({"_id": target_user_id})
                if user_to_remove:
                    # Explicitly set premium fields to False/None
                    db.users.update_one(
                        {"_id": target_user_id},
                        {"$set": {
                            "is_premium": False,
                            "is_instagram_premium": False,
                            "is_tiktok_premium": False,
                            "removed_by": user_id,
                            "removed_at": datetime.now()
                        },
                        "$unset": {"premium_until": "", "premium_type": ""}} # Clear premium details
                    )
                    await msg.reply(f"✅ User `{target_user_id}` has been removed from premium users.", reply_markup=admin_markup, parse_mode=enums.ParseMode.MARKDOWN)
                    await send_log_to_channel(app, LOG_CHANNEL, f"➖ Admin `{user_id}` removed premium for user: `{target_user_id}`")
                    # Optionally notify the removed user
                    try:
                        await app.send_message(target_user_id, "😔 Your premium access has been revoked by an admin.")
                    except Exception as e:
                        logger.warning(f"Failed to notify {target_user_id} about premium removal: {e}")
                else:
                    await msg.reply("⚠️ User not found in database.", reply_markup=admin_markup)
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.")
        finally:
            user_states.pop(user_id, None) # Always clear state after attempt
    else:
        # Fallback for unhandled text in non-specific states
        await msg.reply("I'm not expecting text input right now. Please use the menu buttons or commands.")


# --- CALLBACK HANDLERS ---

@app.on_callback_query(filters.regex("^settings_"))
async def settings_callback_handler(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer()

    if not is_admin(user_id) and not is_premium_user(user_id):
        await query.message.reply("❌ Not authorized. Settings are for premium users only.")
        return

    if query.data == "settings_upload_type":
        await safe_edit_message(
            query.message,
            "📌 Select upload type:",
            reply_markup=upload_type_markup
        )
    elif query.data == "settings_set_caption":
        user_states[user_id] = {"state": "waiting_for_caption"}
        current_settings = await get_user_settings(user_id)
        current_caption = current_settings.get("caption", "Not set")
        await safe_edit_message(
            query.message,
            f"📝 Please send the new caption for your uploads.\n\n"
            f"Current caption: `{current_caption}`",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif query.data == "settings_set_hashtags":
        user_states[user_id] = {"state": "waiting_for_hashtags"}
        current_settings = await get_user_settings(user_id)
        current_hashtags = current_settings.get("hashtags", "Not set")
        await safe_edit_message(
            query.message,
            f"🏷️ Please send the new hashtags for your uploads (e.g., #coding #bot).\n\n"
            f"Current hashtags: `{current_hashtags}`",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif query.data == "settings_set_aspect_ratio":
        await safe_edit_message(
            query.message,
            "📐 Select desired aspect ratio for videos:",
            reply_markup=aspect_ratio_markup
        )
    elif query.data == "user_settings_personal": # For admin's personal settings from main settings menu
        await safe_edit_message(
            query.message,
            "⚙️ Your Personal Settings",
            reply_markup=settings_markup
        )


@app.on_callback_query(filters.regex("^set_type_"))
async def set_type_cb(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer()

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

@app.on_callback_query(filters.regex("^set_ar_"))
async def set_ar_cb(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer()

    aspect_ratio_key = query.data.split("_")[-2:]
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

@app.on_callback_query(filters.regex("^admin_"))
async def admin_callback_handler(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer()

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    if query.data == "admin_users_list":
        await users_list_cb(_, query) # Re-use existing handler
    elif query.data == "admin_add_premium_user":
        user_states[user_id] = {"state": "waiting_for_add_premium_user_id"}
        await safe_edit_message(
            query.message,
            "➕ Please send the **Telegram User ID** to grant premium access to."
        )
    elif query.data == "admin_remove_premium_user":
        user_states[user_id] = {"state": "waiting_for_remove_premium_user_id"}
        await safe_edit_message(
            query.message,
            "➖ Please send the **Telegram User ID** to remove premium access from."
        )
    elif query.data == "admin_broadcast_message":
        await safe_edit_message(
            query.message,
            "📢 Please send the message you want to broadcast to all users by using the command: `/broadcast <your message>`"
        )
    # The main admin_panel_cb is now directly invoked by button or command.

@app.on_callback_query(filters.regex("^premium_platform_"))
async def premium_platform_selection_cb(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer()

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    state_info = user_states.get(user_id)
    if not state_info or state_info.get("state") != "waiting_for_premium_platform_selection" or "target_user_id" not in state_info["data"]:
        await query.answer("Error: User selection lost. Please try 'Add Premium User' again.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    target_user_id = state_info["data"]["target_user_id"]
    platform_choice = query.data.split("premium_platform_")[1]

    # Store platform choice in user_states data for the next step
    state_info["data"]["selected_platform"] = platform_choice
    user_states[user_id] = state_info

    await safe_edit_message(
        query.message,
        f"You selected **{platform_choice.replace('_', ' ').title()}**.\n"
        f"Now, select a premium plan for user `{target_user_id}`:",
        reply_markup=get_premium_plan_markup(),
        parse_mode=enums.ParseMode.MARKDOWN
    )

@app.on_callback_query(filters.regex("^set_plan_"))
async def set_premium_plan_cb(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer()

    if not is_admin(user_id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    state_info = user_states.get(user_id)
    if not state_info or state_info.get("state") != "waiting_for_premium_platform_selection" or "target_user_id" not in state_info["data"] or "selected_platform" not in state_info["data"]:
        await query.answer("Error: User selection lost. Please try 'Add Premium User' again.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    target_user_id = state_info["data"]["target_user_id"]
    selected_platform_scope = state_info["data"]["selected_platform"] # e.g., 'instagram', 'tiktok', 'both'
    premium_plan_key = query.data.split("set_plan_")[1]

    if premium_plan_key not in PREMIUM_PLANS:
        await query.answer("Invalid premium plan selected.", show_alert=True)
        user_states.pop(user_id, None)
        return await safe_edit_message(query.message, "🛠 Admin Panel", reply_markup=admin_markup)

    plan_details = PREMIUM_PLANS[premium_plan_key]
    new_premium_until = None
    if plan_details["duration"] is not None:
        new_premium_until = datetime.now() + plan_details["duration"]

    # Initialize platform specific premium flags
    is_instagram_premium = False
    is_tiktok_premium = False

    if selected_platform_scope == "instagram":
        is_instagram_premium = True
    elif selected_platform_scope == "tiktok":
        is_tiktok_premium = True
    elif selected_platform_scope == "both":
        is_instagram_premium = True
        is_tiktok_premium = True
    
    # Update user's premium status
    update_data = {
        "is_premium": True, # General premium flag
        "premium_type": premium_plan_key,
        "is_instagram_premium": is_instagram_premium,
        "is_tiktok_premium": is_tiktok_premium,
        "added_by": user_id,
        "added_at": datetime.now()
    }
    if new_premium_until:
        update_data["premium_until"] = new_premium_until
    else:
        # For lifetime, explicitly remove premium_until if it exists
        db.users.update_one({"_id": target_user_id}, {"$unset": {"premium_until": ""}})

    db.users.update_one({"_id": target_user_id}, {"$set": update_data}, upsert=True)

    admin_confirm_text = (
        f"✅ Premium granted to user `{target_user_id}` for **{premium_plan_key.replace('_', ' ').title()}**.\n"
        f"Platforms: {selected_platform_scope.replace('_', ' ').title()}"
    )
    if new_premium_until:
        admin_confirm_text += f"\nExpires on: `{new_premium_until.strftime('%Y-%m-%d %H:%M:%S')}`"
    else:
        admin_confirm_text += "\nExpiry: Indefinite (Lifetime)"

    await safe_edit_message(
        query.message,
        admin_confirm_text,
        reply_markup=admin_markup,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer("Premium granted!", show_alert=False)
    user_states.pop(user_id, None)

    # Notify the target user
    try:
        user_msg = (
            f"🎉 **Congratulations!** 🎉\n\n"
            f"You have been granted **{premium_plan_key.replace('_', ' ').title()}** premium access!\n"
            f"Platforms: {'✅ Instagram' if is_instagram_premium else ''} {'✅ TikTok' if is_tiktok_premium else ''}"
        )
        if new_premium_until:
            user_msg += f"\n\nYour premium will expire on: `{new_premium_until.strftime('%Y-%m-%d %H:%M:%S')}`."
        else:
            user_msg += "\n\nEnjoy **Lifetime** premium! ✨"
        
        await app.send_message(target_user_id, user_msg, parse_mode=enums.ParseMode.MARKDOWN)
        await send_log_to_channel(app, LOG_CHANNEL,
            f"💰 Premium granted notification sent to `{target_user_id}` by Admin `{user_id}`. "
            f"Plan: `{premium_plan_key}`, Platforms: `{selected_platform_scope}`"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {target_user_id} about premium: {e}")
        await send_log_to_channel(app, LOG_CHANNEL,
            f"⚠️ Failed to notify user `{target_user_id}` about premium. Error: `{str(e)}`"
        )

@app.on_callback_query(filters.regex("^cancel_admin_operation$"))
async def cancel_admin_operation_cb(_, query):
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer("Operation cancelled.", show_alert=True)
    user_states.pop(user_id, None)
    await safe_edit_message(
        query.message,
        "🛠 Admin Panel",
        reply_markup=admin_markup
    )

@app.on_callback_query(filters.regex("^back_to_"))
async def back_to_cb(_, query):
    data = query.data
    user_id = query.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})
    await query.answer() # Acknowledge the callback

    if data == "back_to_main_menu":
        await query.message.delete() # Delete the inline keyboard message
        await app.send_message(
            query.message.chat.id,
            "🏠 Main Menu",
            reply_markup=get_main_keyboard(is_admin(user_id)) # Send new message with reply keyboard
        )
    elif data == "back_to_settings":
        await safe_edit_message(
            query.message,
            "⚙️ Settings Panel",
            reply_markup=settings_markup
        )
    user_states.pop(user_id, None) # Clear state on navigation back


@app.on_message(filters.video & filters.private)
async def handle_video_upload(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    state_info = user_states.get(user_id)
    if not state_info or state_info.get("state") != "waiting_for_reel_video":
        return await msg.reply("❌ Please use the '📤 Upload Reel' button first to initiate a video upload.")

    if not is_admin(user_id) and not is_premium_user(user_id, platform="instagram"):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Not authorized to upload Reels. Requires Instagram premium.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Instagram session expired. Please login to Instagram first using `/login <username> <password>`.", parse_mode=enums.ParseMode.MARKDOWN)

    processing_msg = await msg.reply("⏳ Processing your video...")
    video_path = None
    transcoded_video_path = None

    try:
        await processing_msg.edit_text("⬇️ Downloading video...")
        video_path = await msg.download()
        logger.info(f"Video downloaded to {video_path}")
        await processing_msg.edit_text("✅ Video downloaded. Preparing for Instagram...")

        await processing_msg.edit_text("🔄 Optimizing video for Instagram (transcoding audio/video)... This may take a moment.")
        transcoded_video_path = f"{video_path}_transcoded.mp4"

        settings = await get_user_settings(user_id)
        aspect_ratio_setting = settings.get("aspect_ratio", "original")

        ffmpeg_command = [
            "ffmpeg",
            "-i", video_path,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-movflags", "faststart",
            "-map_chapters", "-1",
            "-y",
        ]

        if aspect_ratio_setting == "9_16":
            # Scale and pad/crop to 9:16 (1080x1920)
            ffmpeg_command.extend([
                "-vf", "scale=if(gt(a,9/16),1080,-1):if(gt(a,9/16),-1,1920),crop=1080:1920,setsar=1:1,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-s", "1080x1920"
            ])
        
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

        settings = await get_user_settings(user_id)
        caption = settings.get("caption", "Check out my new content! 🎥")
        hashtags = settings.get("hashtags", "")

        if hashtags:
            caption = f"{caption}\n\n{hashtags}"

        upload_type = "reel"

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
        result = user_upload_client.clip_upload(video_to_upload, caption=caption)
        url = f"https://instagram.com/reel/{result.code}"

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
            f"📅 {get_current_datetime_info()['date']}"
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
        await send_log_to_channel(app, LOG_CHANNEL, f"⚠️ Video upload failed (Client Error)\nUser: `{user_id}`\nError: `{ce}``")
    except Exception as e:
        error_msg = f"❌ Video upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"Video upload failed for {user_id}: {str(e)}")
        await send_log_to_channel(app, LOG_CHANNEL, f"❌ Video Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            logger.info(f"Deleted original downloaded video file: {video_path}")
        if transcoded_video_path and os.path.exists(transcoded_video_path):
            os.remove(transcoded_video_path)
            logger.info(f"Deleted transcoded video file: {transcoded_video_path}")
        user_states.pop(user_id, None) # Clear state regardless of success/failure

@app.on_message(filters.photo & filters.private)
async def handle_photo_upload(_, msg):
    user_id = msg.from_user.id
    db.users.update_one({"_id": user_id}, {"$set": {"last_active": datetime.now()}})

    state_info = user_states.get(user_id)
    if not state_info or state_info.get("state") != "waiting_for_photo_image":
        return await msg.reply("❌ Please use the '📸 Upload Photo' button first to initiate an image upload.")

    if not is_admin(user_id) and not is_premium_user(user_id, platform="instagram"):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Not authorized to upload Photos. Requires Instagram premium.")

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

        upload_type = "post"

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
            f"📅 {get_current_datetime_info()['date']}"
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
        user_states.pop(user_id, None) # Clear state regardless of success/failure


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


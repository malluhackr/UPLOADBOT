import os
import sys
import asyncio
import threading
import logging
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from pymongo import MongoClient
from pyrogram import Client, filters
from pyrogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from instagrapi import Client as InstaClient
from instagrapi.exceptions import LoginRequired, ChallengeRequired

# === Load env ===

load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002805592130")) # Double-check this ID!
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))

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
if "users" not in db.list_collection_names():
    db.create_collection("users")
    logger.info("Collection 'users' created.")
if "settings" not in db.list_collection_names():
    db.create_collection("settings")
    logger.info("Collection 'settings' created.")
if "sessions" not in db.list_collection_names():
    db.create_collection("sessions")
    logger.info("Collection 'sessions' created.")

# State management for sequential user input
user_states = {} # {user_id: "action"}

# Keyboards

def get_main_keyboard(is_admin=False):
    buttons = [
        [KeyboardButton("📤 Upload Reel"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("📊 Stats")]
    ]
    if is_admin:
        buttons.append([KeyboardButton("🛠 Admin Panel"), KeyboardButton("🔄 Restart Bot")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Add User", callback_data="add_user")],
    [InlineKeyboardButton("➖ Remove User", callback_data="remove_user")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")]
])

upload_type_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Reel", callback_data="set_type_reel")],
    [InlineKeyboardButton("📷 Post", callback_data="set_type_post")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_settings")]
])

# === Helper Functions ===

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_premium_user(user_id):
    user = db.users.find_one({"_id": user_id})
    return user and user.get("is_premium", False)

def get_current_datetime():
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "UTC+5:30" # Assuming fixed timezone for logging, adjust as needed
    }

async def log_to_channel(message):
    try:
        # Check if the channel ID is valid before sending
        if not isinstance(LOG_CHANNEL, int) or LOG_CHANNEL == 0: # Check for default/invalid
             logger.warning("LOG_CHANNEL ID is not set or invalid. Skipping channel log.")
             return

        await app.send_message(LOG_CHANNEL, message)
        logger.info(f"Logged to channel: {message}")
    except Exception as e:
        logger.error(f"Failed to log to channel {LOG_CHANNEL}: {e}") # More specific error
        # No await query.answer() here, as this function is for background logging

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
    return db.settings.find_one({"_id": user_id}) or {}

async def safe_edit_message(message, text, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
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
    await log_to_channel(restart_msg_log)
    await msg.reply("✅ Bot is restarting...")
    await asyncio.sleep(2) # Give a bit more time for the message to send
    
    try:
        logger.info("Executing os.execv to restart process...")
        # This will replace the current process with a new one running the script.
        # Ensure your deployment environment supports this (e.g., it doesn't immediately kill the new process).
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"Failed to execute restart via os.execv: {e}")
        await log_to_channel(f"❌ Restart failed for {msg.from_user.id}: {str(e)}")
        await msg.reply(f"❌ Failed to restart bot: {str(e)}")

# === Message Handlers ===

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    user_first_name = msg.from_user.first_name or "there"

    # Add user to DB if not exists
    user = db.users.find_one({"_id": user_id})
    if not user:
        db.users.insert_one({"_id": user_id, "is_premium": False, "added_by": "self_start"})
        logger.info(f"New user {user_id} added to database via start command.")
        await log_to_channel(f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")

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

        # Send welcome image with info
        await app.send_photo(
    chat_id=msg.chat.id,
    photo="https://i.postimg.cc/SXDxJ92z/x.jpg",  # updated image link
    caption=contact_admin_text,
    reply_markup=join_channel_markup
        )
        return

    # For premium or admin users
    welcome_msg = "🤖 Welcome to Instagram Upload Bot!\n\n"
    welcome_msg += "🛠 You have admin privileges." if is_admin(user_id) else "⭐ You have premium access."

    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(is_admin(user_id)))

    # If premium/admin
    welcome_msg = "🤖 Welcome to Instagram Upload Bot!\n\n"
    welcome_msg += "🛠 You have admin privileges." if is_admin(user_id) else "⭐ You have premium access."

    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(is_admin(user_id)))
    
    welcome_msg = "🤖 Welcome to Instagram Upload Bot!\n\n"
    if is_admin(user_id):
        welcome_msg += "🛠 You have admin privileges."
    else:
        welcome_msg += "⭐ You have premium access."

    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(is_admin(user_id)))



    welcome_msg = "🤖 Welcome to Instagram Upload Bot!\n\n"
    if is_admin(user_id):
        welcome_msg += "🛠 You have admin privileges."
    else:
        welcome_msg += "⭐ You have premium access."

    await msg.reply(welcome_msg, reply_markup=get_main_keyboard(is_admin(user_id)))

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Admin access required.")

    restarting_msg = await msg.reply("♻️ Restarting bot...")
    await asyncio.sleep(1)  # Ensure message is sent
    await restart_bot(msg)

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    # Added debug prints for clearer console output during testing
    print(f"DEBUG: login_cmd function entered by user {msg.from_user.id}")
    logger.info(f"User {msg.from_user.id} attempting login command.")

    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        print(f"DEBUG: User {user_id} not authorized for login.")
        return await msg.reply("❌ Not authorized to use this command.")

    args = msg.text.split()
    # Debug print the received arguments
    print(f"DEBUG: Received args for login: {args}")
    if len(args) < 3: # Expects /login <username> <password>
        print(f"DEBUG: User {user_id} sent invalid login format. Expected 3 args, got {len(args)}.")
        return await msg.reply("Usage: `/login <instagram_username> <password>`")

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 Attempting Instagram login...")

    try:
        session = await load_instagram_session(user_id)
        if session:
            logger.info(f"Attempting to load existing session for user {user_id} (IG: {username}).")
            insta_client.set_settings(session)
            try:
                # Verify session is still valid by making a simple request
                insta_client.get_timeline_feed()
                await login_msg.edit_text(f"✅ Already logged in to Instagram as `{username}` (session reloaded).")
                logger.info(f"Existing session for {user_id} is valid.")
                return
            except LoginRequired:
                logger.info(f"Existing session for {user_id} expired. Attempting fresh login.")
                insta_client.set_settings({}) # Clear expired settings

        logger.info(f"Attempting fresh Instagram login for user {user_id} with username: {username}")
        insta_client.login(username, password)

        session_data = insta_client.get_settings()
        await save_instagram_session(user_id, session_data)

        db.users.update_one(
            {"_id": user_id},
            {"$set": {"instagram_username": username}},
            upsert=True
        )

        await login_msg.edit_text("✅ Login successful!")
        await log_to_channel(
            f"📝 New Instagram login\nUser: `{user_id}`\n"
            f"Username: `{msg.from_user.username or 'N/A'}`\n"
            f"Instagram: `{username}`"
        )
        logger.info(f"Instagram login successful for user {user_id} ({username}).")

    except ChallengeRequired:
        await login_msg.edit_text("🔐 Instagram requires challenge verification. Please complete it in the Instagram app and try again.")
        await log_to_channel(f"⚠️ Instagram Challenge Required for user `{user_id}` (`{username}`).")
        logger.warning(f"Instagram Challenge Required for user {user_id} ({username}).")
    except LoginRequired as e:
        await login_msg.edit_text(f"❌ Instagram login failed: {e}. Please check your credentials.")
        await log_to_channel(f"❌ Instagram Login Failed for user `{user_id}` (`{username}`): {e}")
        logger.error(f"Instagram Login Failed for user {user_id} ({username}): {e}")
    except Exception as e:
        await login_msg.edit_text(f"❌ An unexpected error occurred during login: {str(e)}")
        logger.error(f"Unhandled error during login for {user_id} ({username}): {str(e)}")
        await log_to_channel(f"🔥 Critical Login Error for user `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
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
async def initiate_upload(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`.")

    await msg.reply("✅ Ready for upload! Please send me the video file.")
    user_states[user_id] = "waiting_for_video"

@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")

    total_users = db.users.count_documents({})
    premium_users = db.users.count_documents({"is_premium": True})
    admin_users = db.users.count_documents({"_id": ADMIN_ID})

    stats_text = (
        "📊 Bot Statistics:\n"
        f"Total users: {total_users}\n"
        f"Premium users: {premium_users}\n"
        f"Admin users: {admin_users}"
    )
    await msg.reply(stats_text)

# === State-Dependent Message Handlers ===

# FIXED: filters.command() now includes parentheses
@app.on_message(filters.text & filters.private & ~filters.command(""))
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state = user_states.get(user_id)

    if state == "waiting_for_caption":
        caption = msg.text
        await save_user_settings(user_id, {"caption": caption})
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup)
        user_states.pop(user_id, None)
    elif state == "waiting_for_hashtags":
        hashtags = msg.text
        await save_user_settings(user_id, {"hashtags": hashtags})
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup)
        user_states.pop(user_id, None)
    elif state == "waiting_for_add_user_id":
        try:
            target_user_id = int(msg.text)
            db.users.update_one(
                {"_id": target_user_id},
                {"$set": {"is_premium": True, "added_by": user_id, "added_at": datetime.now()}},
                upsert=True
            )
            await msg.reply(f"✅ User `{target_user_id}` has been added as a premium user.", reply_markup=admin_markup)
            await log_to_channel(f"➕ Admin `{user_id}` added premium user: `{target_user_id}`")
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.", reply_markup=admin_markup)
        user_states.pop(user_id, None)
    elif state == "waiting_for_remove_user_id":
        try:
            target_user_id = int(msg.text)
            if target_user_id == ADMIN_ID:
                await msg.reply("❌ Cannot remove the admin user.", reply_markup=admin_markup)
            else:
                result = db.users.update_one(
                    {"_id": target_user_id},
                    {"$set": {"is_premium": False, "removed_by": user_id, "removed_at": datetime.now()}}
                )
                if result.matched_count > 0:
                    await msg.reply(f"✅ User `{target_user_id}` has been removed from premium users.", reply_markup=admin_markup)
                    await log_to_channel(f"➖ Admin `{user_id}` removed premium user: `{target_user_id}`")
                else:
                    await msg.reply("⚠️ User not found in database.", reply_markup=admin_markup)
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.", reply_markup=admin_markup)
        user_states.pop(user_id, None)
    # The 'else' block from previous iterations is now handled correctly by the filter itself.
    # No need for an explicit 'if not filters.command(msg)' check here anymore.

# === Callback Handlers ===

@app.on_callback_query(filters.regex("^upload_type$"))
async def upload_type_cb(_, query):
    await safe_edit_message(
        query.message,
        "📌 Select upload type:",
        reply_markup=upload_type_markup
    )

@app.on_callback_query(filters.regex("^set_type_"))
async def set_type_cb(_, query):
    user_id = query.from_user.id
    upload_type = query.data.split("_")[-1]
    current_settings = await get_user_settings(user_id)
    current_settings["upload_type"] = upload_type
    await save_user_settings(user_id, current_settings)
    await safe_edit_message(query.message, f"✅ Upload type set to **{upload_type.capitalize()}**.")
    await asyncio.sleep(1)
    await safe_edit_message(query.message, "⚙️ Settings Panel", reply_markup=settings_markup)

@app.on_callback_query(filters.regex("^set_caption$"))
async def set_caption_cb(_, query):
    user_id = query.from_user.id
    user_states[user_id] = "waiting_for_caption"
    current_settings = await get_user_settings(user_id)
    current_caption = current_settings.get("caption", "Not set")
    await safe_edit_message(
        query.message,
        f"📝 Please send the new caption for your uploads.\n\n"
        f"Current caption: `{current_caption}`"
    )

@app.on_callback_query(filters.regex("^set_hashtags$"))
async def set_hashtags_cb(_, query):
    user_id = query.from_user.id
    user_states[user_id] = "waiting_for_hashtags"
    current_settings = await get_user_settings(user_id)
    current_hashtags = current_settings.get("hashtags", "Not set")
    await safe_edit_message(
        query.message,
        f"🏷️ Please send the new hashtags for your uploads (e.g., #coding #bot).\n\n"
        f"Current hashtags: `{current_hashtags}`"
    )

@app.on_callback_query(filters.regex("^admin_panel$"))
async def admin_panel_cb(_, query):
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
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    users = list(db.users.find({}))
    if not users:
        await safe_edit_message(query.message, "👥 No users found in the database.", reply_markup=admin_markup)
        return

    user_list_text = "👥 **All Users:**\n\n"
    for user in users:
        user_id = user["_id"]
        is_premium = user.get("is_premium", False)
        instagram_username = user.get("instagram_username", "N/A")
        status = "⭐ Premium" if is_premium else "Free"
        if user_id == ADMIN_ID:
            status = "👑 Admin"

        user_list_text += f"ID: `{user_id}` | Status: {status} | IG: `{instagram_username}`\n"

    await safe_edit_message(query.message, user_list_text, reply_markup=admin_markup)

@app.on_callback_query(filters.regex("^add_user$"))
async def add_user_cb(_, query):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    user_states[query.from_user.id] = "waiting_for_add_user_id"
    await safe_edit_message(
        query.message,
        "➕ Please send the User ID to add as a premium user."
    )

@app.on_callback_query(filters.regex("^remove_user$"))
async def remove_user_cb(_, query):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Admin access required", show_alert=True)
        return

    user_states[query.from_user.id] = "waiting_for_remove_user_id"
    await safe_edit_message(
        query.message,
        "➖ Please send the User ID to remove from premium users."
    )

@app.on_callback_query(filters.regex("^user_settings_personal$"))
async def user_settings_personal_cb(_, query):
    user_id = query.from_user.id
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
    data = query.data
    user_id = query.from_user.id

    if data == "back_to_main_menu":
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

# === Video Upload Handler ===

@app.on_message(filters.video & filters.private)
async def handle_video(_, msg):
    user_id = msg.from_user.id

    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    if user_states.get(user_id) != "waiting_for_video":
        return await msg.reply("❌ Please use the '📤 Upload Reel' button first to initiate an upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        user_states.pop(user_id, None)
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`.")

    processing_msg = await msg.reply("⏳ Processing your video...")
    video_path = None

    try:
        await processing_msg.edit_text("⬇️ Downloading video...")
        video_path = await msg.download()
        await processing_msg.edit_text("✅ Video downloaded. Uploading to Instagram...")

        settings = await get_user_settings(user_id)
        caption = settings.get("caption", "Check out my new content! 🎥")
        hashtags = settings.get("hashtags", "")

        if hashtags:
            caption = f"{caption}\n\n{hashtags}"

        upload_type = settings.get("upload_type", "reel") # Default to reel

        session = await load_instagram_session(user_id)
        if not session:
            user_states.pop(user_id, None)
            return await processing_msg.edit_text("❌ Instagram session expired. Please login again with /login")

        insta_client.set_settings(session)

        result = None
        url = None

        if upload_type == "reel":
            await processing_msg.edit_text("🚀 Uploading as a Reel...")
            result = insta_client.clip_upload(
                video_path,
                caption=caption,
                thumbnail=video_path,
            )
            url = f"https://instagram.com/reel/{result.code}"
        else:
            await processing_msg.edit_text("📸 Uploading as a Post...")
            result = insta_client.photo_upload(
                video_path,
                caption=caption
            )
            url = f"https://instagram.com/p/{result.code}"

        log_msg = (
            f"📤 New {upload_type.capitalize()} Upload\n\n"
            f"👤 User: `{user_id}`\n"
            f"📛 Username: `{msg.from_user.username or 'N/A'}`\n"
            f"🔗 URL: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )

        await processing_msg.edit_text(f"✅ Uploaded successfully!\n\n{url}")
        await log_to_channel(log_msg)

    except LoginRequired:
        await processing_msg.edit_text("❌ Instagram login required. Your session might have expired. Please use `/login` again.")
        logger.error(f"LoginRequired during upload for user {user_id}")
        await log_to_channel(f"⚠️ Upload failed (Login Required)\nUser: `{user_id}`")
    except Exception as e:
        error_msg = f"❌ Upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"Upload failed for {user_id}: {str(e)}")
        await log_to_channel(f"❌ Upload Failed\nUser: `{user_id}`\nError: `{error_msg}`")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            logger.info(f"Deleted local video file: {video_path}")
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

    # Start health check server
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Health check server started on port 8080.")

    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}")
        sys.exit(1)


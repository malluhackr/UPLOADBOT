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
BOT_TOKEN = os.getenv("TELEGRAM_BOT_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002750394644"))
# Ensure this MONGO_URI is correct and accessible
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))

# Initialize MongoDB Client
try:
    mongo = MongoClient(MONGO_URI)
    db = mongo.instagram_bot
    logger.info("Connected to MongoDB successfully.")
except Exception as e:
    logger.critical(f"Failed to connect to MongoDB: {e}")
    sys.exit(1) # Exit if cannot connect to DB

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
# These checks are generally not needed with PyMongo as insert/update operations create collections if they don't exist,
# but keeping them for explicit clarity.
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
        buttons.append([KeyboardButton("🛠 Admin Panel"), KeyboardButton("🔄 Restart Bot")]) # Added Admin Panel button
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")] # Changed callback_data for clarity
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Add User", callback_data="add_user")],
    [InlineKeyboardButton("➖ Remove User", callback_data="remove_user")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main_menu")] # Changed callback_data for clarity
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
        await app.send_message(LOG_CHANNEL, message)
        logger.info(f"Logged to channel: {message}")
    except Exception as e:
        logger.error(f"Failed to log to channel: {e}")

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
    restart_msg = (
        "🔄 Bot Restarted Successfully!\n\n"
        f"📅 Date: {dt['date']}\n"
        f"⏰ Time: {dt['time']}\n"
        f"🌐 Timezone: {dt['timezone']}\n"
        f"👤 By: {msg.from_user.mention} (ID: `{msg.from_user.id}`)"
    )
    await log_to_channel(restart_msg)
    await msg.reply("✅ Bot is restarting...")
    # It's generally better to use a tool like systemd or a process manager
    # for proper bot restarts in a production environment.
    os.execv(sys.executable, [sys.executable] + sys.argv)

# === Message Handlers ===

@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id

    # Add user to DB if not exists (as a non-premium by default)
    user = db.users.find_one({"_id": user_id})
    if not user:
        db.users.insert_one({"_id": user_id, "is_premium": False, "added_by": "self_start"})
        logger.info(f"New user {user_id} added to database via start command.")
        await log_to_channel(f"🌟 New user started bot: `{user_id}` (`{msg.from_user.username or 'N/A'}`)")


    if not is_admin(user_id) and not is_premium_user(user_id):
        contact_admin = (
            "🔒 This bot is for premium users only.\n\n"
            f"📨 Contact Admin: `{ADMIN_ID}`\n"
            "Please send your request with your user ID."
        )
        return await msg.reply(contact_admin, reply_markup=ReplyKeyboardRemove())

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
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to use this command.")

    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Usage: `/login <instagram_username> <password>`")

    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 Attempting Instagram login...")

    try:
        # Try to login
        # Check if a session already exists and try to load it first
        session = await load_instagram_session(user_id)
        if session:
            insta_client.set_settings(session)
            # Verify session is still valid
            try:
                insta_client.get_timeline_feed()
                await login_msg.edit_text(f"✅ Already logged in to Instagram as `{username}` (session reloaded).")
                return
            except LoginRequired:
                logger.info(f"Existing session for {user_id} expired. Attempting fresh login.")
                insta_client.set_settings({}) # Clear expired settings

        insta_client.login(username, password)

        # Save session to database
        session_data = insta_client.get_settings()
        await save_instagram_session(user_id, session_data)

        # Update user record
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

    except ChallengeRequired:
        await login_msg.edit_text("🔐 Instagram requires challenge verification. Please complete it in the Instagram app and try again.")
        await log_to_channel(f"⚠️ Instagram Challenge Required for user `{user_id}` (`{username}`).")
    except LoginRequired as e:
        await login_msg.edit_text(f"❌ Instagram login failed: {e}. Please check your credentials.")
        await log_to_channel(f"❌ Instagram Login Failed for user `{user_id}` (`{username}`): {e}")
    except Exception as e:
        await login_msg.edit_text(f"❌ An unexpected error occurred during login: {str(e)}")
        logger.error(f"Login error for {user_id} ({username}): {str(e)}")
        await log_to_channel(f"🔥 Critical Login Error for user `{user_id}` (`{username}`): {str(e)}")

@app.on_message(filters.regex("⚙️ Settings"))
async def settings_menu(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")

    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ User Settings", callback_data="user_settings_personal")] # Renamed callback for clarity
        ])
    else:
        markup = settings_markup

    await msg.reply("⚙️ Settings Panel", reply_markup=markup)

@app.on_message(filters.regex("📤 Upload Reel")) # Handle direct button click for upload
async def initiate_upload(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized to upload.")

    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using `/login <username> <password>`.")

    await msg.reply("✅ Ready for upload! Please send me the video file.")
    user_states[user_id] = "waiting_for_video" # Set state to expect a video

@app.on_message(filters.regex("📊 Stats"))
async def show_stats(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")

    total_users = db.users.count_documents({})
    premium_users = db.users.count_documents({"is_premium": True})
    admin_users = db.users.count_documents({"_id": ADMIN_ID}) # Admins are also users

    stats_text = (
        "📊 Bot Statistics:\n"
        f"Total users: {total_users}\n"
        f"Premium users: {premium_users}\n"
        f"Admin users: {admin_users}"
    )
    await msg.reply(stats_text)


# === State-Dependent Message Handlers ===

@app.on_message(filters.text & filters.private & ~filters.command)
async def handle_text_input(_, msg):
    user_id = msg.from_user.id
    state = user_states.get(user_id)

    if state == "waiting_for_caption":
        caption = msg.text
        await save_user_settings(user_id, {"caption": caption})
        await msg.reply(f"✅ Caption set to: `{caption}`", reply_markup=settings_markup)
        user_states.pop(user_id, None) # Clear state
    elif state == "waiting_for_hashtags":
        hashtags = msg.text
        await save_user_settings(user_id, {"hashtags": hashtags})
        await msg.reply(f"✅ Hashtags set to: `{hashtags}`", reply_markup=settings_markup)
        user_states.pop(user_id, None) # Clear state
    elif state == "waiting_for_add_user_id":
        try:
            target_user_id = int(msg.text)
            # Add user to DB and set as premium
            db.users.update_one(
                {"_id": target_user_id},
                {"$set": {"is_premium": True, "added_by": user_id, "added_at": datetime.now()}},
                upsert=True
            )
            await msg.reply(f"✅ User `{target_user_id}` has been added as a premium user.", reply_markup=admin_markup)
            await log_to_channel(f"➕ Admin `{user_id}` added premium user: `{target_user_id}`")
        except ValueError:
            await msg.reply("❌ Invalid User ID. Please send a valid number.", reply_markup=admin_markup)
        user_states.pop(user_id, None) # Clear state
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
        user_states.pop(user_id, None) # Clear state
    else:
        # If no specific state is set, this is a general text message
        if not filters.command(msg): # Avoid replying to commands twice
            pass # Or handle with a generic "I don't understand" message

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
    upload_type = query.data.split("_")[-1] # Corrected split
    current_settings = await get_user_settings(user_id)
    current_settings["upload_type"] = upload_type
    await save_user_settings(user_id, current_settings) # Save updated settings
    await safe_edit_message(query.message, f"✅ Upload type set to **{upload_type.capitalize()}**.") # Capitalize for better display
    await asyncio.sleep(1) # Give time for user to read
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

@app.on_callback_query(filters.regex("^user_settings_personal$")) # New callback for individual user settings
async def user_settings_personal_cb(_, query):
    user_id = query.from_user.id
    # Only allow regular premium users to access their own settings directly.
    # Admin can go to admin panel.
    if is_admin(user_id):
        # Admins also have user settings, but they accessed via "User Settings" button
        await safe_edit_message(
            query.message,
            "⚙️ Your Personal Settings",
            reply_markup=settings_markup
        )
    elif is_premium_user(user_id):
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

    if data == "back_to_main_menu": # Corrected callback_data
        
if __name__ == "__main__":
    # Ensure sessions directory exists
    # This line (and subsequent lines in this block) MUST be indented.
    # Make sure there are exactly 4 spaces (or one tab, but 4 spaces is standard) before this line.
    os.makedirs("sessions", exist_ok=True) 
    logger.info("Session directory ensured.") # Added for better logging clarity in the startup sequence

    # Start health check server
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Health check server started on port 8080.")

    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.critical(f"Bot crashed: {str(e)}")
        sys.exit(1)

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
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL_ID", "-1002750394644"))
MONGO_URI = os.getenv("MONGO_DB", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))

mongo = MongoClient(MONGO_URI)
db = mongo.instagram_bot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("InstaUploadBot")

app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
insta_client = InstaClient()
insta_client.delay_range = [1, 3]  # More human-like behavior

# Create collections if not exists
if "users" not in db.list_collection_names():
    db.create_collection("users")
if "settings" not in db.list_collection_names():
    db.create_collection("settings")
if "sessions" not in db.list_collection_names():
    db.create_collection("sessions")

# Keyboards
def get_main_keyboard(is_admin=False):
    buttons = [
        [KeyboardButton("📤 Upload Reel"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("📊 Stats")]
    ]
    if is_admin:
        buttons.append([KeyboardButton("🔄 Restart Bot")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, selective=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="set_hashtags")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Add User", callback_data="add_user")],
    [InlineKeyboardButton("➖ Remove User", callback_data="remove_user")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
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
        "timezone": "UTC+5:30"
    }

async def log_to_channel(message):
    try:
        await app.send_message(LOG_CHANNEL, message)
    except Exception as e:
        logger.error(f"Failed to log to channel: {e}")

async def save_instagram_session(user_id, session_data):
    db.sessions.update_one(
        {"user_id": user_id},
        {"$set": {"session": session_data}},
        upsert=True
    )

async def load_instagram_session(user_id):
    session = db.sessions.find_one({"user_id": user_id})
    return session.get("session") if session else None

async def save_user_settings(user_id, settings):
    db.settings.update_one(
        {"_id": user_id},
        {"$set": settings},
        upsert=True
    )

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
        f"👤 By: {msg.from_user.mention}"
    )
    await log_to_channel(restart_msg)
    await msg.reply("✅ Bot is restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# === Message Handlers ===
@app.on_message(filters.command("start"))
async def start(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        contact_admin = (
            "🔒 This bot is for premium users only.\n\n"
            f"📨 Contact Admin: {ADMIN_ID}\n"
            "Please send your request with your user ID"
        )
        return await msg.reply(contact_admin, reply_markup=ReplyKeyboardRemove())
    
    welcome_msg = "🤖 Welcome to Instagram Upload Bot!\n\n"
    if is_admin(user_id):
        welcome_msg += "🛠 You have admin privileges"
    else:
        welcome_msg += "⭐ You have premium access"
    
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
        return await msg.reply("❌ Not authorized.")
    
    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Usage: /login <instagram_username> <password>")
    
    username, password = args[1], args[2]
    login_msg = await msg.reply("🔐 Attempting Instagram login...")
    
    try:
        # Try to login
        insta_client.login(username, password)
        
        # Save session to database
        session = insta_client.get_settings()
        await save_instagram_session(user_id, session)
        
        # Update user record
        db.users.update_one(
            {"_id": user_id},
            {"$set": {"instagram_username": username}},
            upsert=True
        )
        
        await login_msg.edit_text("✅ Login successful!")
        await log_to_channel(
            f"📝 New Instagram login\nUser: {user_id}\n"
            f"Username: {msg.from_user.username or 'N/A'}"
        )
    except ChallengeRequired:
        await login_msg.edit_text("🔐 Instagram requires challenge verification. Please complete it in the Instagram app.")
    except LoginRequired as e:
        await login_msg.edit_text(f"❌ Login failed: {e}")
    except Exception as e:
        await login_msg.edit_text(f"❌ Error: {str(e)}")
        logger.error(f"Login error for {user_id}: {str(e)}")

@app.on_message(filters.command("settings"))
async def settings(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return await msg.reply("❌ Not authorized.")
    
    if is_admin(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ User Settings", callback_data="user_settings")]
        ])
    else:
        markup = settings_markup
    
    await msg.reply("⚙️ Settings Panel", reply_markup=markup)

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
    
    await save_user_settings(user_id, {"upload_type": upload_type})
    await safe_edit_message(query.message, f"✅ Upload type set to {upload_type}")

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

@app.on_callback_query(filters.regex("^back_to"))
async def back_to_cb(_, query):
    data = query.data
    user_id = query.from_user.id
    
    if data == "back_to_main":
        await query.message.delete()
        await query.message._client.send_message(
            query.message.chat.id,
            "🏠 Main Menu",
            reply_markup=get_main_keyboard(is_admin(user_id)))
    elif data == "back_to_settings":
        await safe_edit_message(
            query.message,
            "⚙️ Settings Panel",
            reply_markup=settings_markup
        )

# === Video Upload Handler ===
@app.on_message(filters.video)
async def handle_video(_, msg):
    user_id = msg.from_user.id
    if not is_admin(user_id) and not is_premium_user(user_id):
        return
    
    # Check Instagram credentials
    user_data = db.users.find_one({"_id": user_id})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please login to Instagram first using /login")
    
    processing_msg = await msg.reply("⏳ Processing your video...")
    
    try:
        # Download video
        video_path = await msg.download()
        
        # Get user settings
        settings = await get_user_settings(user_id)
        caption = settings.get("caption", "Check out my new content! 🎥")
        upload_type = settings.get("upload_type", "reel")
        
        # Load Instagram session
        session = await load_instagram_session(user_id)
        if not session:
            return await processing_msg.edit_text("❌ Instagram session expired. Please login again with /login")
        
        insta_client.set_settings(session)
        
        # Upload based on type
        if upload_type == "reel":
            result = insta_client.clip_upload(
                video_path,
                caption=caption,
                thumbnail=video_path  # Use first frame as thumbnail
            )
            url = f"https://instagram.com/reel/{result.code}"
        else:
            result = insta_client.photo_upload(
                video_path,
                caption=caption
            )
            url = f"https://instagram.com/p/{result.code}"
        
        # Log the upload
        log_msg = (
            f"📤 New {upload_type.capitalize()} Upload\n\n"
            f"👤 User: {user_id}\n"
            f"📛 Username: {msg.from_user.username or 'N/A'}\n"
            f"🔗 URL: {url}\n"
            f"📅 {get_current_datetime()['date']}"
        )
        
        await processing_msg.edit_text(f"✅ Uploaded successfully!\n\n{url}")
        await log_to_channel(log_msg)
        
    except Exception as e:
        error_msg = f"❌ Upload failed: {str(e)}"
        await processing_msg.edit_text(error_msg)
        logger.error(f"Upload failed for {user_id}: {str(e)}")
        await log_to_channel(f"Upload Failed\nUser: {user_id}\nError: {error_msg}")
    finally:
        if 'video_path' in locals() and os.path.exists(video_path):
            os.remove(video_path)

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
    
    # Start health check server
    threading.Thread(target=run_server, daemon=True).start()
    
    logger.info("Starting bot...")
    try:
        app.run()
    except Exception as e:
        logger.error(f"Bot crashed: {str(e)}")
        sys.exit(1)

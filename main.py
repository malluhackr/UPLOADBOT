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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Bot")

app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
insta_client = InstaClient()
user_settings = {}

# Create necessary collections if they don't exist
if "users" not in db.list_collection_names():
    db.create_collection("users")
if "settings" not in db.list_collection_names():
    db.create_collection("settings")

# Keyboards
main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📤 Upload Reel"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("📊 Stats"), KeyboardButton("🔄 Restart Bot")]
    ],
    resize_keyboard=True,
    selective=True
)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("🔀 Aspect Ratio", callback_data="aspect_ratio")],
    [InlineKeyboardButton("📝 Caption", callback_data="caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="hashtags")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Add User", callback_data="add_user")],
    [InlineKeyboardButton("➖ Remove User", callback_data="remove_user")],
    [InlineKeyboardButton("📊 User Limits", callback_data="user_limits")],
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
    await app.send_message(LOG_CHANNEL, message)

async def save_user_settings(user_id, settings):
    db.settings.update_one(
        {"_id": user_id},
        {"$set": settings},
        upsert=True
    )

async def get_user_settings(user_id):
    return db.settings.find_one({"_id": user_id}) or {}

# === Message Handlers ===
@app.on_message(filters.command("start"))
async def start(_, msg):
    if not is_admin(msg.from_user.id) and not is_premium_user(msg.from_user.id):
        contact_admin = "📨 Contact Admin for premium access:\n\n" \
                      f"👤 Admin ID: {ADMIN_ID}\n" \
                      "💬 Send your request with your user ID"
        return await msg.reply(
            "🔒 This bot is for premium users only.\n\n" + contact_admin,
            reply_markup=ReplyKeyboardRemove()
        )
    
    welcome_msg = "🤖 Welcome to Instagram Upload Bot!\n\n"
    if is_admin(msg.from_user.id):
        welcome_msg += "🛠 You have admin privileges"
    else:
        welcome_msg += "⭐ You have premium access"
    
    await msg.reply(welcome_msg, reply_markup=main_keyboard)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Admin access required.")
    
    dt = get_current_datetime()
    restart_msg = (
        "🔄 Bot Restarted Successfully!\n\n"
        f"📅 Date: {dt['date']}\n"
        f"⏰ Time: {dt['time']}\n"
        f"🌐 Timezone: {dt['timezone']}\n"
        f"👤 By: {msg.from_user.mention}"
    )
    
    await msg.reply("♻️ Restarting bot...")
    await log_to_channel(restart_msg)
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command("stats"))
async def stats(_, msg):
    if not is_admin(msg.from_user.id) and not is_premium_user(msg.from_user.id):
        return await msg.reply("❌ Not authorized.")
    
    total_users = db.users.count_documents({"is_premium": True})
    active_users = db.users.count_documents({"is_premium": True, "last_active": {"$exists": True}})
    
    stats_msg = (
        "📊 Bot Statistics\n\n"
        f"👥 Total Premium Users: {total_users}\n"
        f"🟢 Active Users: {active_users}\n"
        f"🛠 Admin: {msg.from_user.mention}"
    )
    
    await msg.reply(stats_msg)

# === Callback Handlers ===
@app.on_callback_query()
async def cb_handler(_, query):
    uid = query.from_user.id
    if not is_admin(uid) and not is_premium_user(uid):
        await query.answer("❌ Not authorized", show_alert=True)
        return
    
    if query.data == "upload_type":
        await query.message.edit(
            "📌 Select upload type:",
            reply_markup=upload_type_markup
        )
    elif query.data.startswith("set_type_"):
        upload_type = query.data.split("_")[-1]
        await save_user_settings(uid, {"upload_type": upload_type})
        await query.message.edit(f"✅ Upload type set to {upload_type}")
    elif query.data == "back_to_settings":
        await query.message.edit("⚙️ Settings Panel", reply_markup=settings_markup)
    elif query.data == "admin_panel":
        if not is_admin(uid):
            await query.answer("❌ Admin access required", show_alert=True)
            return
        await query.message.edit("🛠 Admin Panel", reply_markup=admin_markup)
    elif query.data == "users_list":
        if not is_admin(uid):
            await query.answer("❌ Admin access required", show_alert=True)
            return
        
        users = list(db.users.find({"is_premium": True}))
        if not users:
            await query.message.edit("No premium users found.", reply_markup=admin_markup)
            return
        
        users_list = "👥 Premium Users:\n\n"
        for user in users[:10]:  # Show first 10 users
            users_list += f"🆔 {user['_id']}\n"
        
        await query.message.edit(users_list, reply_markup=admin_markup)
    elif query.data == "add_user":
        if not is_admin(uid):
            await query.answer("❌ Admin access required", show_alert=True)
            return
        
        await query.message.edit(
            "➕ Add Premium User\n\n"
            "Send the user's Telegram ID to add them as premium.\n"
            "Example: <code>/adduser 123456789</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ])
        )
    elif query.data == "back_to_main":
        await query.message.delete()
        await query.message._client.send_message(
            query.message.chat.id,
            "🏠 Main Menu",
            reply_markup=main_keyboard
        )

# === Video Upload Handler ===
@app.on_message(filters.video)
async def handle_video(_, msg):
    uid = msg.from_user.id
    if not is_admin(uid) and not is_premium_user(uid):
        return
    
    # Check if user has Instagram credentials
    user_data = db.users.find_one({"_id": uid})
    if not user_data or not user_data.get("instagram_username"):
        return await msg.reply("❌ Please set your Instagram credentials first using /login")
    
    await msg.reply("⏳ Processing your video...")
    
    try:
        # Download video
        video_path = await msg.download()
        
        # Get user settings
        settings = await get_user_settings(uid)
        caption = settings.get("caption", "Check out my new reel! 🎥")
        upload_type = settings.get("upload_type", "reel")
        
        # Load Instagram session
        session_file = f"sessions/insta_session_{uid}.json"
        if not os.path.exists(session_file):
            return await msg.reply("❌ Instagram session expired. Please login again with /login")
        
        insta_client.load_settings(session_file)
        
        # Upload based on type
        if upload_type == "reel":
            result = insta_client.clip_upload(video_path, caption=caption)
            url = f"https://instagram.com/reel/{result.code}"
        else:
            result = insta_client.photo_upload(video_path, caption=caption)
            url = f"https://instagram.com/p/{result.code}"
        
        # Log the upload
        log_msg = (
            f"📤 New {upload_type.capitalize()} Upload\n\n"
            f"👤 User: {uid}\n"
            f"📛 Username: {msg.from_user.username or 'N/A'}\n"
            f"🔗 URL: {url}\n"
            f"📅 Date: {get_current_datetime()['date']}"
        )
        
        await msg.reply(f"✅ Successfully uploaded!\n\n{url}")
        await log_to_channel(log_msg)
        
    except Exception as e:
        error_msg = f"❌ Upload failed: {str(e)}"
        await msg.reply(error_msg)
        await log_to_channel(f"Upload Failed\nUser: {uid}\nError: {error_msg}")
    finally:
        if 'video_path' in locals() and os.path.exists(video_path):
            os.remove(video_path)

# === HTTP Server ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    httpd = HTTPServer(('0.0.0.0', 8080), Handler)
    httpd.serve_forever()

if __name__ == "__main__":
    # Create sessions directory if not exists
    if not os.path.exists("sessions"):
        os.makedirs("sessions")
    
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Bot Running...")
    app.run()

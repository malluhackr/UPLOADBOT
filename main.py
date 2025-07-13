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

# Keyboards
main_keyboard = ReplyKeyboardMarkup([
    [KeyboardButton("📄 Upload Reel"), KeyboardButton("⚙️ Settings")],
    [KeyboardButton("📊 Stats"), KeyboardButton("🔄 Restart Bot")]
], resize_keyboard=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("📌 Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("🔀 Aspect Ratio", callback_data="aspect_ratio")],
    [InlineKeyboardButton("📝 Caption", callback_data="caption")],
    [InlineKeyboardButton("🏷️ Hashtags", callback_data="hashtags")],
])

admin_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("👥 Users List", callback_data="users_list")],
    [InlineKeyboardButton("➕ Add User", callback_data="add_user")],
    [InlineKeyboardButton("➖ Remove User", callback_data="remove_user")],
    [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
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

# === Basic Handlers ===
@app.on_message(filters.command("start"))
async def start(_, msg):
    if not is_admin(msg.from_user.id) and not is_premium_user(msg.from_user.id):
        return await msg.reply("❌ Not authorized.")
    
    if is_admin(msg.from_user.id):
        await msg.reply("👋 Welcome Admin!", reply_markup=main_keyboard)
    else:
        await msg.reply("👋 Welcome Premium User!", reply_markup=main_keyboard)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Unauthorized.")
    
    dt = get_current_datetime()
    restart_msg = (
        "🔄 Bot Restarted Successfully!\n\n"
        f"📅 Date: {dt['date']}\n"
        f"⏰ Time: {dt['time']}\n"
        f"🌐 Timezone: {dt['timezone']}"
    )
    
    await msg.reply("♻️ Restarting...")
    await log_to_channel(restart_msg)
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    if not is_admin(msg.from_user.id) and not is_premium_user(msg.from_user.id):
        return await msg.reply("❌ Not authorized.")
    
    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Use: /login <username> <password>")
    
    username, password = args[1], args[2]
    await msg.reply("🔐 Logging into Instagram...")
    
    try:
        insta_client.login(username, password)
        insta_client.dump_settings(f"insta_session_{msg.from_user.id}.json")
        await msg.reply("✅ Login successful!")
        
        # Log to channel
        log_msg = (
            f"📝 New Instagram Login\n\n"
            f"👤 User ID: {msg.from_user.id}\n"
            f"📛 Username: {msg.from_user.username or 'N/A'}\n"
            f"🕒 Time: {get_current_datetime()['time']}"
        )
        await log_to_channel(log_msg)
    except Exception as e:
        await msg.reply(f"❌ Login failed: {e}")

@app.on_message(filters.command("settings"))
async def settings(_, msg):
    if not is_admin(msg.from_user.id) and not is_premium_user(msg.from_user.id):
        return await msg.reply("❌ Unauthorized")
    
    if is_admin(msg.from_user.id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Admin Panel", callback_data="admin_panel")],
            [InlineKeyboardButton("⚙️ User Settings", callback_data="user_settings")]
        ])
    else:
        markup = settings_markup
    
    await msg.reply("⚙️ Settings Panel", reply_markup=markup)

@app.on_message(filters.command("admin"))
async def admin_panel(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Admin access required.")
    await msg.reply("🛠 Admin Panel", reply_markup=admin_markup)

@app.on_message(filters.command("adduser"))
async def add_user_cmd(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ Admin access required.")
    
    args = msg.text.split()
    if len(args) < 2:
        return await msg.reply("Use: /adduser <user_id>")
    
    try:
        user_id = int(args[1])
        db.users.update_one(
            {"_id": user_id},
            {"$set": {"is_premium": True}},
            upsert=True
        )
        await msg.reply(f"✅ User {user_id} added as premium!")
        await log_to_channel(f"🎖 New Premium User\n\nUser ID: {user_id}\nAdded by: {msg.from_user.id}")
    except ValueError:
        await msg.reply("❌ Invalid user ID. Must be a number.")

# === Callback Handlers ===
@app.on_callback_query()
async def cb_handler(_, query):
    uid = query.from_user.id
    user_settings.setdefault(uid, {})
    
    if query.data == "upload_type":
        user_settings[uid]["step"] = "set_upload_type"
        await query.message.edit(
            "Select upload type:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Reels", callback_data="set_type_reel")],
                [InlineKeyboardButton("Post", callback_data="set_type_post")]
            ])
        )
    elif query.data.startswith("set_type"):
        upload_type = query.data.split("_")[-1]
        db.settings.update_one(
            {"_id": uid},
            {"$set": {"upload_type": upload_type}},
            upsert=True
        )
        await query.message.edit(f"✅ Upload type set to {upload_type}")
    elif query.data == "admin_panel":
        if not is_admin(uid):
            await query.answer("❌ Access denied", show_alert=True)
            return
        await query.message.edit("🛠 Admin Panel", reply_markup=admin_markup)
    elif query.data == "add_user":
        if not is_admin(uid):
            await query.answer("❌ Access denied", show_alert=True)
            return
        await query.message.edit(
            "To add a user, send:\n\n"
            "<code>/adduser USER_ID</code>\n\n"
            "Where USER_ID is the Telegram ID of the user you want to add.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
            ])
        )
    elif query.data == "back_to_main":
        await query.message.edit("⚙️ Settings Panel", reply_markup=settings_markup)

# === Video Handler ===
@app.on_message(filters.video)
async def handle_video(_, msg):
    uid = msg.from_user.id
    if not is_admin(uid) and not is_premium_user(uid):
        return
    
    video = await msg.download()
    await msg.reply("⏳ Uploading Reel...\nUpload Task Reels\n┃ [▓▓▓▓▓▦□□□□□□] 51.19%")
    
    try:
        caption = db.settings.find_one({"_id": uid}).get("caption", "")
        session_file = f"insta_session_{uid}.json"
        
        if os.path.exists(session_file):
            insta_client.load_settings(session_file)
            result = insta_client.clip_upload(video, caption=caption)
            await msg.reply(f"✅ Uploaded: https://instagram.com/reel/{result.code}")
            
            log_msg = (
                f"📤 New Upload\n\n"
                f"👤 User: {uid}\n"
                f"📛 Username: {msg.from_user.username or 'N/A'}\n"
                f"📅 Date: {get_current_datetime()['date']}\n"
                f"⏰ Time: {get_current_datetime()['time']}\n"
                f"📝 Caption: {caption[:100]}..."
            )
            await log_to_channel(log_msg)
            await app.send_video(LOG_CHANNEL, video, caption=log_msg)
        else:
            await msg.reply("❌ Instagram session not found. Please login first with /login")
    except Exception as e:
        await msg.reply(f"❌ Failed to upload: {e}")
        await log_to_channel(f"❌ Upload Failed\n\nUser: {uid}\nError: {str(e)}")
    finally:
        if os.path.exists(video):
            os.remove(video)

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
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Bot Running...")
    app.run()

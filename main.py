# === Imports ===
import os
import sys
import time
import asyncio
import logging
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from instagrapi import Client as InstaClient
from pymongo import MongoClient
from dotenv import load_dotenv

# === Load Environment ===
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "24026226"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "76b243b66cf12f8b7a603daef8859837")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "-1002750394644"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

# === Initialize Clients ===
app = Client("insta_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
db = MongoClient(MONGO_URI).bot_users
insta = InstaClient()

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Default Settings ===
def get_user(uid):
    user = db.users.find_one({"_id": uid})
    if not user:
        db.users.insert_one({"_id": uid, "upload_type": "reel", "aspect_ratio": "9:16", "caption": "", "hashtags": ""})
        return get_user(uid)
    return user

# === Keyboards ===
def main_keyboard():
    return ReplyKeyboardMarkup([
        ["\U0001F4F9 Upload Reel", "\U0001F527 Settings"],
        ["\U0001F4CA Status", "\U0001F504 Restart"]
    ], resize_keyboard=True)

def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Upload Type", callback_data="set_upload_type")],
        [InlineKeyboardButton("Aspect Ratio", callback_data="set_aspect")],
        [InlineKeyboardButton("Caption", callback_data="set_caption")],
        [InlineKeyboardButton("Hashtags", callback_data="set_hashtag")],
        [InlineKeyboardButton("\u2B05 Back", callback_data="back")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add User", callback_data="add_user"), InlineKeyboardButton("Remove User", callback_data="remove_user")],
        [InlineKeyboardButton("View Logins", callback_data="view_logins")],
        [InlineKeyboardButton("\u2B05 Back", callback_data="back")]
    ])

# === Handlers ===
@app.on_message(filters.command("start"))
async def start(_, msg: Message):
    user = get_user(msg.from_user.id)
    if msg.from_user.id != ADMIN_ID and not db.authorized.find_one({"_id": msg.from_user.id}):
        await msg.reply("❌ This bot is for premium users only. Contact admin.")
        return
    await msg.reply("👋 Welcome! Upload reels to Instagram.", reply_markup=main_keyboard())

@app.on_message(filters.command("restart") & filters.user(ADMIN_ID))
async def restart(_, msg: Message):
    now = datetime.datetime.now()
    dt = now.strftime("%Y-%m-%d")
    tm = now.strftime("%H:%M:%S")
    await msg.reply("♻️ Restarting bot...")
    await app.send_message(LOG_CHANNEL, f"✅ Bot Restarted Successfully!\n\n📅 Date: {dt}\n⏰ Time: {tm}\n🌐 Timezone: UTC+5:30")
    os.execv(sys.executable, ['python'] + sys.argv)

@app.on_message(filters.regex("^\U0001F527 Settings$"))
async def show_settings(_, msg: Message):
    await msg.reply("⚙️ Manage your preferences:", reply_markup=settings_keyboard())

@app.on_message(filters.regex("^\U0001F4CA Status$"))
async def status(_, msg: Message):
    uid = msg.from_user.id
    user = get_user(uid)
    text = f"📊 Your Settings:\n\nUpload Type: {user['upload_type']}\nAspect Ratio: {user['aspect_ratio']}\nCaption: {user['caption']}\nHashtags: {user['hashtags']}"
    await msg.reply(text)

@app.on_callback_query()
async def callback_handler(_, query: CallbackQuery):
    uid = query.from_user.id
    if query.data == "back":
        await query.message.delete()
        return

    if query.data.startswith("set_"):
        step = query.data.replace("set_", "")
        db.temp.update_one({"_id": uid}, {"$set": {"step": step}}, upsert=True)
        await query.message.reply(f"✍️ Send new {step} now.")

    if query.data == "add_user" and uid == ADMIN_ID:
        db.temp.update_one({"_id": uid}, {"$set": {"step": "add_user"}}, upsert=True)
        await query.message.reply("Send the Telegram ID to add:")

    if query.data == "remove_user" and uid == ADMIN_ID:
        db.temp.update_one({"_id": uid}, {"$set": {"step": "remove_user"}}, upsert=True)
        await query.message.reply("Send the Telegram ID to remove:")

    if query.data == "view_logins" and uid == ADMIN_ID:
        all_users = db.authorized.find()
        text = "👤 Authorized Users:\n" + "\n".join([str(u['_id']) for u in all_users])
        await query.message.reply(text)

@app.on_message(filters.text)
async def text_input(_, msg: Message):
    uid = msg.from_user.id
    step_data = db.temp.find_one({"_id": uid})
    if not step_data: return
    step = step_data.get("step")

    if step == "caption":
        db.users.update_one({"_id": uid}, {"$set": {"caption": msg.text}})
        await msg.reply("✅ Caption updated.")

    elif step == "hashtag":
        db.users.update_one({"_id": uid}, {"$set": {"hashtags": msg.text}})
        await msg.reply("✅ Hashtags updated.")

    elif step == "aspect":
        db.users.update_one({"_id": uid}, {"$set": {"aspect_ratio": msg.text}})
        await msg.reply("✅ Aspect ratio updated.")

    elif step == "upload_type":
        db.users.update_one({"_id": uid}, {"$set": {"upload_type": msg.text}})
        await msg.reply("✅ Upload type updated.")

    elif step == "add_user" and uid == ADMIN_ID:
        db.authorized.insert_one({"_id": int(msg.text)})
        await msg.reply("✅ User added.")

    elif step == "remove_user" and uid == ADMIN_ID:
        db.authorized.delete_one({"_id": int(msg.text)})
        await msg.reply("✅ User removed.")

    db.temp.delete_one({"_id": uid})

# === Uptime Server ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    httpd = HTTPServer(('0.0.0.0', 8080), Handler)
    httpd.serve_forever()

# === Start Everything ===
th = threading.Thread(target=run_server, daemon=True)
th.start()
app.run()

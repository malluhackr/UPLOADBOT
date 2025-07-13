

import os
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from instagrapi import Client as InstaClient
from dotenv import load_dotenv
from pymongo import MongoClient

# === Load .env ===
load_dotenv()
API_ID = int(os.getenv("API_ID", "24026226"))
API_HASH = os.getenv("API_HASH", "76b243b66cf12f8b7a603daef8859837")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM")
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "-1002750394644"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "7898534200"))
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

# === MongoDB ===
mongo_client = MongoClient(MONGO_URL)
db = mongo_client["igbot"]
users = db["users"]

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Pyrogram Client ===
app = Client("upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# === Instagram Client ===
insta = InstaClient()
def safe_instagram_login():
    if INSTAGRAM_PROXY:
        insta.set_proxy(INSTAGRAM_PROXY)
    insta.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)

# === Settings Cache ===
user_settings = {}

# === Menus ===
def get_main_menu():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("\U0001F4C4 Upload Reel"), KeyboardButton("/settings")]],
        resize_keyboard=True
    )

def get_settings_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Upload Type", callback_data="upload_type")],
        [InlineKeyboardButton("Aspect Ratio", callback_data="aspect_ratio")],
        [InlineKeyboardButton("Caption", callback_data="caption")],
        [InlineKeyboardButton("Hashtags", callback_data="hashtags")],
        [InlineKeyboardButton("Back", callback_data="main")]
    ])

# === Bot Commands ===
@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return await message.reply("⛔ Access Denied")
    users.update_one({"_id": user_id}, {"$set": {"chat_id": user_id}}, upsert=True)
    await message.reply("👋 Welcome to Reel Bot!", reply_markup=get_main_menu())

@app.on_message(filters.command("restart"))
async def restart(client, message):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("⛔ Not allowed")
    await message.reply("♻️ Restarting...")
    os.execv(sys.executable, ['python'] + sys.argv)

@app.on_message(filters.command("settings"))
async def settings(client, message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.reply("⚙️ Settings:", reply_markup=get_settings_markup())

# === Button Interactions ===
@app.on_callback_query()
async def callback_query(client, callback):
    uid = callback.from_user.id
    user_settings.setdefault(uid, {
        "upload_type": "reel",
        "aspect_ratio": "9:16",
        "caption": "",
        "hashtags": ""
    })
    data = callback.data
    if data == "upload_type":
        user_settings[uid]["upload_type"] = "post" if user_settings[uid]["upload_type"] == "reel" else "reel"
        await callback.answer(f"Now: {user_settings[uid]['upload_type'].upper()}")
    elif data == "aspect_ratio":
        user_settings[uid]["aspect_ratio"] = "1:1" if user_settings[uid]["aspect_ratio"] == "9:16" else "9:16"
        await callback.answer(f"Aspect Ratio: {user_settings[uid]['aspect_ratio']}")
    elif data == "caption":
        await callback.message.reply("✍️ Send new caption")
        user_settings[uid]["step"] = "caption"
    elif data == "hashtags":
        await callback.message.reply("🏷️ Send new hashtags")
        user_settings[uid]["step"] = "hashtags"
    elif data == "main":
        await callback.message.reply("Main menu", reply_markup=get_main_menu())

# === Text Input Settings ===
@app.on_message(filters.text)
async def text_input(client, message):
    uid = message.from_user.id
    if uid != ADMIN_ID:
        return
    if user_settings.get(uid, {}).get("step") == "caption":
        user_settings[uid]["caption"] = message.text
        await message.reply("✅ Caption updated")
    elif user_settings.get(uid, {}).get("step") == "hashtags":
        user_settings[uid]["hashtags"] = message.text
        await message.reply("✅ Hashtags updated")
    user_settings[uid]["step"] = None

# === Upload Flow ===
@app.on_message(filters.text & filters.regex("^\U0001F4C4 Upload Reel$"))
async def ask_video(client, message):
    await message.reply("📥 Send the video now", reply_markup=ReplyKeyboardRemove())
    user_settings[message.from_user.id]["step"] = "awaiting_video"

@app.on_message(filters.video)
async def receive_video(client, message):
    uid = message.from_user.id
    if uid != ADMIN_ID:
        return
    if user_settings.get(uid, {}).get("step") != "awaiting_video":
        return
    user_settings[uid]["step"] = None
    video_path = await message.download()
    await message.reply("📤 Uploading to Instagram...")

    try:
        safe_instagram_login()
        caption = f"{user_settings[uid]['caption']}\n\n{user_settings[uid]['hashtags']}"
        await message.reply("Upload Task Reels\n┃ [■■■■■■▦□□□□□□] 51.19%")
        upload_result = insta.clip_upload(video_path, caption=caption)
        await message.reply("✅ Uploaded successfully")
        await app.send_video(LOG_CHANNEL, video_path, caption=f"Log from Admin:\n{caption}")
    except Exception as e:
        await message.reply(f"❌ Failed: {e}")

# === HTTP Keepalive ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()
app.run()

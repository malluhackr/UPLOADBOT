import os
import sys
import asyncio
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from pymongo import MongoClient
from pyrogram import Client, filters
from pyrogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
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

main_keyboard = ReplyKeyboardMarkup([
    [KeyboardButton("\ud83d\udcc4 Upload Reel"), KeyboardButton("\u2699\ufe0f Settings")],
    [KeyboardButton("\ud83d\udcca Stats"), KeyboardButton("\ud83d\udd04 Restart Bot")]
], resize_keyboard=True)

settings_markup = InlineKeyboardMarkup([
    [InlineKeyboardButton("\ud83d\udccc Upload Type", callback_data="upload_type")],
    [InlineKeyboardButton("\ud83d\udd00 Aspect Ratio", callback_data="aspect_ratio")],
    [InlineKeyboardButton("\ud83d\udcdd Caption", callback_data="caption")],
    [InlineKeyboardButton("\ud83c\udff7\ufe0f Hashtags", callback_data="hashtags")],
])

# === Basic Handlers ===
@app.on_message(filters.command("start"))
async def start(_, msg):
    if msg.from_user.id != ADMIN_ID:
        return await msg.reply("\u274c Not authorized.")
    await msg.reply("\ud83d\udc4b Welcome to Reels Bot!", reply_markup=main_keyboard)

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    if msg.from_user.id != ADMIN_ID:
        return await msg.reply("\u274c Unauthorized.")
    await msg.reply("\u267b\ufe0f Restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command("login"))
async def login_cmd(_, msg):
    if msg.from_user.id != ADMIN_ID:
        return await msg.reply("\u274c Not authorized.")
    args = msg.text.split()
    if len(args) < 3:
        return await msg.reply("Use: /login <username> <password>")
    username, password = args[1], args[2]
    await msg.reply("\ud83d\udd10 Logging into Instagram...")
    try:
        insta_client.login(username, password)
        insta_client.dump_settings("insta_session.json")
        await msg.reply("\u2705 Login successful!")
    except Exception as e:
        await msg.reply(f"\u274c Login failed: {e}")

@app.on_message(filters.command("settings"))
async def settings(_, msg):
    if msg.from_user.id != ADMIN_ID:
        return await msg.reply("\u274c Unauthorized")
    await msg.reply("\u2699\ufe0f Settings Panel", reply_markup=settings_markup)

@app.on_callback_query()
async def cb_handler(_, query):
    uid = query.from_user.id
    user_settings.setdefault(uid, {})
    if query.data == "upload_type":
        user_settings[uid]["step"] = "set_upload_type"
        await query.message.edit("Select upload type:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Reels", callback_data="set_type_reel")],
            [InlineKeyboardButton("Post", callback_data="set_type_post")]
        ]))
    elif query.data.startswith("set_type"):
        upload_type = query.data.split("_")[-1]
        db.settings.update_one({"_id": uid}, {"$set": {"upload_type": upload_type}}, upsert=True)
        await query.message.edit(f"\u2705 Upload type set to {upload_type}")

@app.on_message(filters.video)
async def handle_video(_, msg):
    uid = msg.from_user.id
    if uid != ADMIN_ID:
        return
    video = await msg.download()
    await msg.reply("\u23f3 Uploading Reel...\nUpload Task Reels\n┃ [\\u2588\\u2588\\u2588\\u2588\\u2588▦□□□□□□] 51.19%")
    try:
        caption = db.settings.find_one({"_id": uid}).get("caption", "")
        insta_client.load_settings("insta_session.json")
        result = insta_client.clip_upload(video, caption=caption)
        await msg.reply(f"\u2705 Uploaded: https://instagram.com/reel/{result.code}")
        await app.send_video(LOG_CHANNEL, video, caption=f"Log:\nUploaded by: {uid}\n{caption}")
    except Exception as e:
        await msg.reply(f"\u274c Failed to upload: {e}")

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

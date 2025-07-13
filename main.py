# === Telegram to Instagram Upload Bot ===
# Features: Login, Reels upload, MongoDB, Custom settings, Log channel, Advanced UI

import os
import time
import asyncio
import logging
import threading
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import (ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, Message)
from instagrapi import Client as InstaClient
from pymongo import MongoClient

# === Load ENV (hardcoded for production as per request) ===
API_ID = 24026226
API_HASH = "76b243b66cf12f8b7a603daef8859837"
BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
LOG_CHANNEL = -1002750394644
ADMIN_ID = 7898534200
MONGO_URI = "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# === Bot Init ===
app = Client("insta_upload_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# === MongoDB Setup ===
mongo = MongoClient(MONGO_URI)
db = mongo["instabot"]
auth_users = db["authorized_users"]
bot_logs = db["logs"]

# === Insta Setup ===
insta = InstaClient()
session_file = "insta_session.json"
def safe_login():
    if os.path.exists(session_file):
        insta.load_settings(session_file)
    insta.login(os.getenv("INSTAGRAM_USERNAME", ""), os.getenv("INSTAGRAM_PASSWORD", ""))
    insta.dump_settings(session_file)

# === State ===
user_states = {}
def is_admin(user_id):
    return user_id == ADMIN_ID or auth_users.find_one({"user_id": user_id})

# === Button Layouts ===
def settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Upload Type", callback_data="set_type")],
        [InlineKeyboardButton("🎞️ Aspect Ratio", callback_data="set_ratio")],
        [InlineKeyboardButton("📝 Caption", callback_data="set_caption")],
        [InlineKeyboardButton("#️⃣ Hashtag", callback_data="set_hashtag")],
        [InlineKeyboardButton("🔙 Back", callback_data="back")]
    ])

def main_menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📤 Upload Reel")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("♻️ Restart")]
    ], resize_keyboard=True)

# === Bot Commands ===
@app.on_message(filters.command("start"))
async def start(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply("⛔ You are not authorized.")
    await m.reply("👋 Welcome to Instagram Upload Bot!", reply_markup=main_menu())

@app.on_message(filters.command("restart"))
async def restart(_, m: Message):
    if not is_admin(m.from_user.id): return
    await m.reply("♻️ Restarting bot...")
    os.execv(sys.executable, ['python'] + sys.argv)

@app.on_message(filters.command("settings") | filters.text("⚙️ Settings"))
async def settings(_, m: Message):
    if not is_admin(m.from_user.id): return
    await m.reply("⚙️ Bot Settings:", reply_markup=settings_menu())

@app.on_callback_query()
async def callback_handler(_, call):
    uid = call.from_user.id
    if not is_admin(uid): return await call.answer("Not allowed")
    state = user_states.get(uid, {})
    if call.data == "set_type":
        user_states[uid] = {"setting": "type"}
        await call.message.reply("Select Upload Type:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Reel", callback_data="type_reel"), InlineKeyboardButton("📷 Post", callback_data="type_post")]
        ]))
    elif call.data == "set_ratio":
        user_states[uid] = {"setting": "ratio"}
        await call.message.reply("Select Aspect Ratio:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("9:16", callback_data="ratio_9_16"), InlineKeyboardButton("1:1", callback_data="ratio_1_1")]
        ]))
    elif call.data.startswith("type_"):
        user_states[uid]["upload_type"] = call.data.split("_")[1]
        await call.answer(f"Set to {user_states[uid]['upload_type']}")
    elif call.data.startswith("ratio_"):
        user_states[uid]["aspect_ratio"] = call.data.split("_")[1].replace("_", ":")
        await call.answer(f"Aspect Ratio: {user_states[uid]['aspect_ratio']}")
    elif call.data == "back":
        await call.message.reply("🔙 Back to Menu", reply_markup=main_menu())

@app.on_message(filters.text("📤 Upload Reel"))
async def ask_video(_, m):
    if not is_admin(m.from_user.id): return
    user_states[m.chat.id] = {"step": "video"}
    await m.reply("🎥 Send your reel video.")

@app.on_message(filters.video)
async def handle_video(_, m):
    state = user_states.get(m.chat.id)
    if not state or state.get("step") != "video": return
    path = await m.download()
    user_states[m.chat.id].update({"video_path": path, "step": "caption"})
    await m.reply("📝 Send caption text.")

@app.on_message(filters.text & ~filters.command)
async def handle_caption(_, m):
    uid = m.chat.id
    state = user_states.get(uid)
    if not state or state.get("step") != "caption": return
    caption = m.text
    video_path = state["video_path"]
    await m.reply("🚀 Uploading to Instagram...", quote=True)

    # Fake upload progress
    for i in range(0, 101, 10):
        bar = "[" + "█" * (i // 10) + "▒" + "░" * ((100 - i) // 10) + f"] {i}%"
        await m.reply(f"Upload Task Reels\n┃ {bar}")
        await asyncio.sleep(0.3)

    try:
        safe_login()
        insta.clip_upload(video_path, caption=caption)
        await m.reply("✅ Uploaded to Instagram!")
        await app.send_video(LOG_CHANNEL, video_path, caption=f"Log:
{caption}")
        bot_logs.insert_one({"uid": uid, "caption": caption, "time": time.time()})
    except Exception as e:
        await m.reply(f"❌ Upload Failed: {e}")

    user_states.pop(uid, None)

# === Keepalive ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Alive")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', 8080), Handler).serve_forever(), daemon=True).start()

# === Start Bot ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()

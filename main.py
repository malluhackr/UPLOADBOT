import os
import threading
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from pymongo import MongoClient
from pyrogram import Client, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton
from instagrapi import Client as InstaClient
from dotenv import load_dotenv

# === LOAD ENV ===
load_dotenv()

# === TELEGRAM & INSTAGRAM CONFIG ===
TELEGRAM_API_ID = 24026226
TELEGRAM_API_HASH = "76b243b66cf12f8b7a603daef8859837"
TELEGRAM_BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
LOG_CHANNEL_ID = -1002750394644
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")

# === MONGO DB SETUP ===
MONGO_URL = "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
mongo_client = MongoClient(MONGO_URL)
db = mongo_client["insta_bot"]
auth_users = db["authorized_users"]

# === INSTAGRAM & TELEGRAM CLIENT ===
insta_client = InstaClient()
app = Client("upload_bot", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, bot_token=TELEGRAM_BOT_TOKEN)

main_menu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("\ud83d\udcc4 /settings")],
        [KeyboardButton("\ud83d\udcf4 Upload a Reel")]
    ], resize_keyboard=True
)

user_states = {}

# === AUTHORIZATION ===
def is_authorized(user_id):
    return auth_users.find_one({"user_id": user_id}) is not None

# === SAFE INSTAGRAM LOGIN ===
def safe_instagram_login():
    if INSTAGRAM_PROXY:
        insta_client.set_proxy(INSTAGRAM_PROXY)
    insta_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)

# === COMMAND: /start ===
@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(f"\u26d4 Not authorized.\nYour ID: {user_id}")
        return
    await message.reply("\ud83d\udc4b Welcome! Choose an option:", reply_markup=main_menu)

# === COMMAND: /restart ===
@app.on_message(filters.command("restart"))
async def restart_bot(client, message):
    await message.reply("\u267b\ufe0f Restarting bot...")
    os.execv(__file__, [__file__])

# === COMMAND: /login ===
@app.on_message(filters.command("login"))
async def login_instagram(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply("\u26d4 You are not authorized.")
        return

    args = message.text.split(maxsplit=2)
    if len(args) != 3:
        await message.reply("\u2757 Usage: /login username password")
        return

    username, password = args[1], args[2]
    await message.reply("\ud83d\udd10 Logging into Instagram...")

    try:
        def do_login():
            temp_client = InstaClient()
            if INSTAGRAM_PROXY:
                temp_client.set_proxy(INSTAGRAM_PROXY)
            temp_client.login(username, password)
            temp_client.dump_settings("insta_settings.json")

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(do_login)
            future.result(timeout=30)

        await message.reply("\u2705 Instagram login successful.")
    except concurrent.futures.TimeoutError:
        await message.reply("\u274c Login timeout. Try again later.")
    except Exception as e:
        await message.reply(f"\u274c Login failed: {e}")

# === COMMAND: /settings ===
@app.on_message(filters.command("settings"))
async def settings_cmd(client, message):
    await message.reply(
        "\ud83d\udcc4 Settings:
- Upload Type: [Reel/Post]
- Reel Mode: [Single/Multiple]
- Caption: default
- Hashtags: default
- Aspect Ratio: 9:16\n\n(Editable settings coming soon!)"
    )

# === Upload Prompt ===
@app.on_message(filters.text & filters.regex("^\ud83d\udcf4 Upload a Reel$"))
async def upload_prompt(client, message):
    user_states[message.chat.id] = {"step": "awaiting_video"}
    await message.reply("\ud83c\udfa5 Send your reel video now.")

# === Handle Video ===
@app.on_message(filters.video)
async def handle_video(client, message):
    user_id = message.chat.id
    if not is_authorized(user_id):
        await message.reply("\u26d4 Unauthorized.")
        return

    state = user_states.get(user_id)
    if not state or state.get("step") != "awaiting_video":
        await message.reply("\u2757 Click \ud83d\udcf4 Upload a Reel first.")
        return

    file_path = await message.download()
    user_states[user_id] = {"step": "awaiting_title", "file_path": file_path}
    await message.reply("\ud83d\udd8d\ufe0f Now send the title.")

@app.on_message(filters.text & filters.create(lambda _, __, m: user_states.get(m.chat.id, {}).get("step") == "awaiting_title"))
async def handle_title(client, message):
    user_id = message.chat.id
    user_states[user_id]["title"] = message.text
    user_states[user_id]["step"] = "awaiting_hashtags"
    await message.reply("\ud83c\udff7\ufe0f Now send hashtags.")

@app.on_message(filters.text & filters.create(lambda _, __, m: user_states.get(m.chat.id, {}).get("step") == "awaiting_hashtags"))
async def handle_hashtags(client, message):
    user_id = message.chat.id
    title = user_states[user_id].get("title", "")
    hashtags = message.text.strip()
    file_path = user_states[user_id]["file_path"]
    caption = f"{title}\n\n{hashtags}"

    try:
        safe_instagram_login()
        insta_client.clip_upload(file_path, caption)
        await message.reply("\u2705 Uploaded to Instagram!")
        await app.send_message(LOG_CHANNEL_ID, f"\ud83d\udcf9 Reel uploaded by `{user_id}`\nTitle: {title}")
    except Exception as e:
        await message.reply(f"\u274c Upload failed: {e}")

    user_states.pop(user_id)

# === KEEP SERVER ALIVE ===
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', 8080), Handler).serve_forever(), daemon=True).start()

# === START BOT ===
app.run()

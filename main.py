import os
import threading
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from pyrogram import Client, filters
from pyrogram.types import ReplyKeyboardMarkup, KeyboardButton
from instagrapi import Client as InstaClient
from dotenv import load_dotenv
from pymongo import MongoClient

# === LOAD ENV ===
load_dotenv()
TELEGRAM_API_ID = 24026226
TELEGRAM_API_HASH = "76b243b66cf12f8b7a603daef8859837"
TELEGRAM_BOT_TOKEN = "7821394616:AAEXNOE-hOB_nBp6Vfoms27sqcXNF3cKDCM"
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "")
LOG_CHANNEL_ID = -1002750394644
MONGO_URL = "mongodb+srv://cristi7jjr:tRjSVaoSNQfeZ0Ik@cluster0.kowid.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

AUTHORIZED_USERS_FILE = "authorized_users.txt"
SESSION_FILE = "insta_settings.json"

mongo_client = MongoClient(MONGO_URL)
db = mongo_client["ig_bot"]
user_states = {}

insta_client = InstaClient()
app = Client("upload_bot", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, bot_token=TELEGRAM_BOT_TOKEN)

main_menu = ReplyKeyboardMarkup(
    [
        [KeyboardButton("\ud83d\udcf4 Upload a Reel")],
        [KeyboardButton("\ud83d\udcf4 Upload Multiple Reels")],
        [KeyboardButton("\ud83d\udcc4 Settings"), KeyboardButton("\ud83d\udd04 Restart")]
    ],
    resize_keyboard=True
)

def is_authorized(user_id):
    try:
        with open(AUTHORIZED_USERS_FILE, "r") as file:
            return str(user_id) in file.read().splitlines()
    except FileNotFoundError:
        return False

def safe_instagram_login():
    if INSTAGRAM_PROXY:
        insta_client.set_proxy(INSTAGRAM_PROXY)
    if os.path.exists(SESSION_FILE):
        insta_client.load_settings(SESSION_FILE)
    insta_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
    insta_client.dump_settings(SESSION_FILE)

@app.on_message(filters.command("start"))
async def start(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply(f"\u26d4 Not authorized.\n\ud83c\udd94 Your ID: {user_id}")
        return
    await message.reply("\ud83d\udc4b Welcome! Choose an option below:", reply_markup=main_menu)

@app.on_message(filters.command("restart"))
async def restart_cmd(client, message):
    await message.reply("\u23f3 Restarting bot...")
    os.execv(__file__, ["python"] + sys.argv)

@app.on_message(filters.command("login"))
async def login_instagram(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply("\u26d4 You are not authorized.")
        return

    try:
        args = message.text.split(maxsplit=2)
        if len(args) != 3:
            await message.reply("\u2757 Usage: /login username password")
            return

        username, password = args[1], args[2]
        await message.reply("\ud83d\udd10 Logging into Instagram...")

        def do_login():
            temp_client = InstaClient()
            if INSTAGRAM_PROXY:
                temp_client.set_proxy(INSTAGRAM_PROXY)
            temp_client.login(username, password)
            temp_client.dump_settings(SESSION_FILE)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(do_login)
            future.result(timeout=20)

        await message.reply("\u2705 Instagram login successful and session saved.")
    except concurrent.futures.TimeoutError:
        await message.reply("\u274c Login timeout. Proxy/Instagram may be slow or blocked.")
    except Exception as e:
        await message.reply(f"\u274c Login failed: {e}")

@app.on_message(filters.text & filters.regex("^\ud83d\udcf4 Upload a Reel$"))
async def upload_prompt(client, message):
    user_states[message.chat.id] = {"step": "awaiting_video"}
    await message.reply("\ud83c\udfa5 Send your reel video now.")

@app.on_message(filters.video)
async def handle_video(client, message):
    user_id = message.chat.id
    if not is_authorized(user_id):
        await message.reply("\u26d4 You are not authorized.")
        return

    state = user_states.get(user_id)
    if not state or state.get("step") != "awaiting_video":
        await message.reply("\u2757 Click \ud83d\udcf4 Upload a Reel first.")
        return

    file_path = await message.download()
    user_states[user_id] = {"step": "awaiting_title", "file_path": file_path}
    await message.reply("\ud83d\udd8d\ufe0f Now send the title for your reel.")

@app.on_message(filters.text & filters.create(lambda _, __, m: user_states.get(m.chat.id, {}).get("step") == "awaiting_title"))
async def handle_title(client, message):
    user_id = message.chat.id
    user_states[user_id]["title"] = message.text
    user_states[user_id]["step"] = "awaiting_hashtags"
    await message.reply("\ud83c\udff7\ufe0f Now send hashtags (e.g. #funny #reel).")

@app.on_message(filters.text & filters.create(lambda _, __, m: user_states.get(m.chat.id, {}).get("step") == "awaiting_hashtags"))
async def handle_hashtags(client, message):
    user_id = message.chat.id
    title = user_states[user_id].get("title", "")
    hashtags = message.text.strip()
    file_path = user_states[user_id]["file_path"]
    caption = f"{title}\n\n{hashtags}"

    try:
        safe_instagram_login()
        await message.reply("\u23f3 Uploading reel...")
        insta_client.clip_upload(file_path, caption)
        await message.reply("\u2705 Uploaded to Instagram!")
        await app.send_message(LOG_CHANNEL_ID, f"User {user_id} uploaded a reel.")
    except Exception as e:
        await message.reply(f"\u274c Upload failed: {e}")

    user_states.pop(user_id)

@app.on_message(filters.text & filters.regex("^\ud83d\udcc4 Settings$"))
async def show_settings(client, message):
    await message.reply(
        "\ud83d\udcc4 Settings:\n\n"
        "• Reel Type: Single\n"
        "• Uploading Type: Reels\n"
        "• Caption: Default\n"
        "• Hashtags: Default\n"
        "• Aspect Ratio: 9:16"
    )

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_server():
    httpd = HTTPServer(('0.0.0.0', 8080), Handler)
    httpd.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

app.run()

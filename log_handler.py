import logging
import asyncio
from pyrogram import enums  # ✅ Import enums here
from pyrogram.errors import FloodWait, RPCError

logger = logging.getLogger("InstaUploadBot")

async def send_log_to_channel(app, log_channel_id, message):
    """Send log message to a Telegram channel."""
    try:
        await app.send_message(
            chat_id=log_channel_id,
            text=message,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except FloodWait as e:
        logger.warning(f"FloodWait while logging: Sleeping for {e.value} seconds.")
        await asyncio.sleep(e.value)
        return await send_log_to_channel(app, log_channel_id, message)
    except RPCError as e:
        logger.error(f"Failed to log to channel {log_channel_id} (RPCError): {type(e).__name__}: {e}")
    except Exception as e:
        logger.error(f"Failed to log to channel {log_channel_id} (General Error): {type(e).__name__}: {e}")

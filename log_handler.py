import logging
from pyrogram.errors import PeerIdInvalid, RPCError # Import specific Pyrogram errors
from pyrogram import Client # Import Client for type hinting

# Get the logger instance. It's good practice to ensure this matches the name used in main.py.
# If 'InstaUploadBot' logger is already configured in main.py, this will retrieve it.
logger = logging.getLogger("InstaUploadBot")

# Ensure handlers are present. This block is good for standalone testing,
# but in a typical Pyrogram application, the main script's logging.basicConfig
# will usually handle the initial configuration.
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    # Re-get the logger to ensure it has the newly configured handlers
    logger = logging.getLogger("InstaUploadBot")

async def send_log_to_channel(app_client: Client, log_channel_id: int, message: str):
    """
    Sends a message to the specified Telegram log channel.

    Args:
        app_client (pyrogram.Client): The Pyrogram Client instance.
        log_channel_id (int): The ID of the Telegram channel to send the log to.
        message (str): The message string to send.
    """
    try:
        # Essential check: Ensure the LOG_CHANNEL_ID is a valid integer (e.g., -100...)
        # Added a check for chat_id being None or 0 explicitly, as getenv might return None or empty string
        # before conversion to int. Though your main.py converts it, it's a good defensive check here too.
        if not isinstance(log_channel_id, int) or log_channel_id == 0:
            logger.warning("LOG_CHANNEL_ID is not set or invalid. Skipping channel log.")
            return

        # It's good practice to explicitly define parse_mode if you expect Markdown
        # This aligns with how you're using it in main.py's `send_log_to_channel` calls.
        await app_client.send_message(chat_id=log_channel_id, text=message, parse_mode=enums.ParseMode.MARKDOWN) # Assuming markdown is desired for logs
        logger.info(f"Logged to channel (ID: {log_channel_id}): {message[:100]}...") # Log first 100 chars to avoid very long console logs

    except PeerIdInvalid:
        # Specific error for invalid ID or permissions
        logger.error(
            f"Failed to log to channel {log_channel_id}: Peer ID is invalid. "
            "Please ensure the LOG_CHANNEL_ID in your .env is correct (starts with -100) "
            "and that the bot has 'Post Messages' permission in the channel."
        )
    except RPCError as e:
        # General Pyrogram RPC errors
        logger.error(f"Failed to log to channel {log_channel_id} (RPC Error): {e}")
    except Exception as e:
        # Any other unexpected errors
        logger.error(f"Failed to log to channel {log_channel_id} (General Error): {e}")


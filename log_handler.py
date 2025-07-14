import logging
from pyrogram.errors import PeerIdInvalid, RPCError # Import specific Pyrogram errors

# Get the logger instance from main.py, or define a basic one if this file is run standalone for testing
logger = logging.getLogger("InstaUploadBot")
if not logger.handlers: # Configure basic logging if not already configured (e.g., for direct testing)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("InstaUploadBot") # Re-get after basicConfig

async def send_log_to_channel(app_client, log_channel_id, message):
    """
    Sends a message to the specified Telegram log channel.

    Args:
        app_client (pyrogram.Client): The Pyrogram Client instance.
        log_channel_id (int): The ID of the Telegram channel to send the log to.
        message (str): The message string to send.
    """
    try:
        # Essential check: Ensure the LOG_CHANNEL_ID is a valid integer (e.g., -100...)
        if not isinstance(log_channel_id, int) or log_channel_id == 0:
             logger.warning("LOG_CHANNEL ID is not set or invalid in .env. Skipping channel log.")
             return

        await app_client.send_message(chat_id=log_channel_id, text=message)
        logger.info(f"Logged to channel: {message}")
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


import logging

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
    except Exception as e:
        logger.error(f"Failed to send log to channel {log_channel_id}: {e}")


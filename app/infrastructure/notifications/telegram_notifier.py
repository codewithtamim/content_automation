"""Send notifications to admin via Telegram Bot API."""

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def notify_admin(
    bot_token: str,
    admin_chat_id: str,
    message: str,
) -> bool:
    """
    Send a message to the admin via Telegram.

    Args:
        bot_token: Telegram bot token.
        admin_chat_id: Admin's Telegram chat ID.
        message: Message text to send.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not bot_token or not admin_chat_id:
        logger.warning("Cannot notify admin: missing bot_token or admin_chat_id")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps({"chat_id": admin_chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            logger.warning("Telegram API returned status %s", resp.status)
            return False
    except Exception as e:
        logger.exception("Failed to notify admin via Telegram: %s", e)
        return False

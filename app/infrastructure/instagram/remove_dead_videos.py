"""Remove Instagram Reels with 0 views older than N days."""

import logging
from datetime import datetime, timedelta, timezone

from instagrapi import Client

logger = logging.getLogger(__name__)


def remove_dead_videos(
    username: str,
    password: str,
    min_age_days: int = 1,
    max_reels: int = 100,
) -> tuple[int, list[str]]:
    """
    Find and delete Reels with 0 views that were uploaded more than min_age_days ago.

    Args:
        username: Instagram username.
        password: Instagram password.
        min_age_days: Minimum age in days before a 0-view reel is considered "dead".
        max_reels: Maximum number of reels to scan (pagination limit).

    Returns:
        (deleted_count, list of deleted media codes/URLs).
    """
    client = Client()
    try:
        client.login(username, password)
    except Exception as e:
        logger.exception("Instagram login failed for %s: %s", username, e)
        raise

    user_id = client.user_id_from_username(username)
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    deleted_count = 0
    deleted_codes: list[str] = []

    try:
        clips = client.user_clips(str(user_id), amount=max_reels)
    except Exception as e:
        logger.exception("Failed to fetch clips for %s: %s", username, e)
        raise

    for media in clips:
        try:
            info = client.media_info(media.pk)
            view_count = getattr(info, "view_count", None)
            taken_at = getattr(info, "taken_at", None)

            if taken_at is None:
                continue
            if not isinstance(taken_at, datetime) and hasattr(taken_at, "replace"):
                taken_at = taken_at.replace(tzinfo=timezone.utc) if taken_at.tzinfo is None else taken_at
            elif taken_at.tzinfo is None:
                taken_at = taken_at.replace(tzinfo=timezone.utc)

            if taken_at > cutoff:
                continue
            if view_count is not None and view_count != 0:
                continue

            try:
                client.media_delete(media.pk)
                deleted_count += 1
                code = getattr(info, "code", None) or getattr(media, "code", None) or str(media.pk)
                deleted_codes.append(code)
                logger.info("Deleted dead reel %s (0 views, %s)", code, taken_at)
            except Exception as e:
                logger.warning("Failed to delete media %s: %s", media.pk, e)
        except Exception as e:
            logger.warning("Failed to get info for media %s: %s", media.pk, e)

    return deleted_count, deleted_codes

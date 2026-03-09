"""Instagram uploader using instagrapi."""

import logging
from pathlib import Path

from instagrapi import Client

logger = logging.getLogger(__name__)


class InstagramUploader:
    """Upload videos to Instagram as Reels using instagrapi."""

    def __init__(
        self,
        username: str,
        password: str,
        session_path: str | Path | None = None,
    ):
        self.client = Client()
        self.username = username
        self.password = password
        self._logged_in = False
        self._session_path = Path(session_path) if session_path else None

    def _ensure_logged_in(self) -> None:
        """Login to Instagram. Uses persisted session if available to avoid re-login."""
        if self._logged_in:
            return

        if self._session_path:
            session_file = self._session_path / f"instagram_{self.username}.json"
            session_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.client.load_settings(session_file)
                self.client.login(self.username, self.password)
                self._logged_in = True
                logger.debug("Instagram session loaded for %s", self.username)
                return
            except Exception as e:
                logger.warning("Could not load Instagram session for %s: %s", self.username, e)

        self.client.login(self.username, self.password)
        self._logged_in = True

        if self._session_path:
            try:
                session_file = self._session_path / f"instagram_{self.username}.json"
                self.client.dump_settings(session_file)
                logger.debug("Instagram session saved for %s", self.username)
            except Exception as e:
                logger.warning("Could not save Instagram session for %s: %s", self.username, e)

    def upload_reel(
        self,
        video_path: str,
        caption: str,
    ) -> None:
        """
        Upload a video as an Instagram Reel.

        Args:
            video_path: Path to the video file.
            caption: Caption with title and hashtags.
        """
        self._ensure_logged_in()
        self.client.clip_upload(path=video_path, caption=caption)

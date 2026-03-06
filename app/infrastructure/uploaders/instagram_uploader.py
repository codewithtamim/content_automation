"""Instagram uploader using instagrapi."""

from instagrapi import Client


class InstagramUploader:
    """Upload videos to Instagram as Reels using instagrapi."""

    def __init__(self, username: str, password: str):
        self.client = Client()
        self.username = username
        self.password = password
        self._logged_in = False

    def _ensure_logged_in(self) -> None:
        """Login to Instagram if not already logged in."""
        if not self._logged_in:
            self.client.login(self.username, self.password)
            self._logged_in = True

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

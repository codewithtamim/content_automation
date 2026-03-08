"""yt-dlp video downloader with metadata extraction."""

import logging
import subprocess
from pathlib import Path
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)


def _convert_to_mp4(path: str) -> str:
    """Convert video to mp4 using ffmpeg if not already mp4. Returns path to mp4 file."""
    p = Path(path)
    if p.suffix.lower() == ".mp4":
        return path
    mp4_path = p.with_suffix(".mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-c", "copy", str(mp4_path)],
            check=True,
            capture_output=True,
        )
        p.unlink(missing_ok=True)
        return str(mp4_path)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("ffmpeg conversion failed: %s. Using original file.", e)
        return path


class YtDlpDownloader:
    """Download videos using yt-dlp and extract metadata."""

    def __init__(
        self,
        storage_path: str = "/tmp/videos",
        cookies_path: Optional[str] = None,
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.cookies_path = Path(cookies_path) if (cookies_path and cookies_path.strip()) else None

    def download(
        self,
        url: str,
        job_id: int,
    ) -> tuple[str, Optional[str], Optional[list[str]]]:
        """
        Download video from URL and extract metadata.

        Args:
            url: Video URL (YouTube, Instagram, etc.).
            job_id: Job ID for naming the output file.

        Returns:
            Tuple of (local_path, original_title, original_tags).
        """
        output_template = str(self.storage_path / f"{job_id}.%(ext)s")

        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "recodevideo": "mp4",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }
        if self.cookies_path and self.cookies_path.exists() and self.cookies_path.stat().st_size > 0:
            ydl_opts["cookiefile"] = str(self.cookies_path)
            logger.info("Using cookies from %s", self.cookies_path)
        elif self.cookies_path:
            logger.warning(
                "Cookies file missing or empty at %s. YouTube may block downloads. "
                "Upload cookies via bot: Manage credentials → Upload YouTube cookies",
                self.cookies_path,
            )

        extracted_info = {}

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                extracted_info["title"] = info.get("title")
                extracted_info["tags"] = info.get("tags") or []

        # Find the downloaded file
        output_path = None
        for ext in ["mp4", "webm", "mkv", "m4a", "3gp", "flv"]:
            candidate = self.storage_path / f"{job_id}.{ext}"
            if candidate.exists():
                output_path = str(candidate)
                break

        if not output_path:
            raise RuntimeError(f"Download failed: no output file found for job {job_id}")

        # Convert to mp4 if needed (e.g. webm for Instagram compatibility)
        output_path = _convert_to_mp4(output_path)

        title = extracted_info.get("title")
        tags = extracted_info.get("tags")
        if tags and not isinstance(tags, list):
            tags = [str(t) for t in tags] if tags else None

        return output_path, title, tags

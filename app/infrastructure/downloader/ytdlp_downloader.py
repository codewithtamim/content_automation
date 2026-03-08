"""yt-dlp video downloader with metadata extraction."""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)

# EU consent cookies: CONSENT (legacy) + SOCS (current) = "accept all"
_CONSENT_COOKIES = [
    # domain, path, name, value
    (".youtube.com", "/", "CONSENT", "YES+1"),
    (".youtube.com", "/", "SOCS", "CAISHAg"),
    (".consent.youtube.com", "/", "CONSENT", "YES+1"),
    (".consent.youtube.com", "/", "SOCS", "CAISHAg"),
]


def _ensure_consent_cookies(cookies_path: Optional[Path]) -> tuple[Optional[Path], bool]:
    """
    Return (cookies_file_path, is_temp).
    When user has cookies: use them as-is. When no cookies: create temp file with EU consent only.
    """
    if cookies_path and cookies_path.exists() and cookies_path.stat().st_size > 0:
        return (cookies_path, False)

    expiration = "9999999999"
    consent_lines = [
        f"{domain}\tTRUE\t{path}\tTRUE\t{expiration}\t{name}\t{value}"
        for domain, path, name, value in _CONSENT_COOKIES
    ]
    merged = "# HTTP Cookie File\n" + "\n".join(consent_lines) + "\n"

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(merged)
        return (Path(path), True)
    except OSError:
        return (None, False)


def _convert_to_mp4(path: str) -> str:
    """Convert video to mp4 using ffmpeg if not already mp4. Returns path to mp4 file."""
    p = Path(path)
    if p.suffix.lower() == ".mp4":
        return path
    mp4_path = p.with_suffix(".mp4")
    # Try stream copy first (fast). If that fails (e.g. webm VP8/VP9), re-encode to H.264.
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-c", "copy", str(mp4_path)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-c:a", "aac", str(mp4_path)],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("ffmpeg conversion failed: %s. Using original file.", e)
            return path
    p.unlink(missing_ok=True)
    return str(mp4_path)


class YtDlpDownloader:
    """Download videos using yt-dlp and extract metadata."""

    def __init__(
        self,
        storage_path: str = "/tmp/videos",
        cookies_path: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.cookies_path = Path(cookies_path) if (cookies_path and cookies_path.strip()) else None
        self.proxy = proxy.strip() if (proxy and proxy.strip()) else None

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
        cookies_to_use, is_temp = _ensure_consent_cookies(self.cookies_path)
        has_user_cookies = cookies_to_use is not None and not is_temp

        ydl_opts = {
            # Download whatever format is available (webm, mp4, etc). ffmpeg converts to mp4 after.
            "format": "best",
            "outtmpl": output_template,
            "logger": logger,
            "extract_flat": False,
            **({"proxy": self.proxy} if self.proxy else {}),
            # Avoid web client - triggers "Sign in to confirm" on datacenter/VPS IPs.
            # Use tv/android clients (no PO token required). tv_embedded first when we have cookies.
            "extractor_args": {
                "youtube": {
                    "player_client": (
                        ["tv_embedded", "tv", "tv_simply", "android_vr", "android"]
                        if has_user_cookies
                        else ["tv", "tv_simply", "android_vr", "android"]
                    ),
                    "player_skip": ["webpage", "configs"],
                },
            },
        }
        try:
            if cookies_to_use:
                ydl_opts["cookiefile"] = str(cookies_to_use)
                logger.info("Using cookies from %s", cookies_to_use)
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

        finally:
            if is_temp and cookies_to_use:
                try:
                    cookies_to_use.unlink(missing_ok=True)
                except OSError:
                    pass

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

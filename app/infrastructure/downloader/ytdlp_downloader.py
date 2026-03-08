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


def _ensure_consent_cookies(cookies_path: Optional[Path]) -> Optional[Path]:
    """
    Return a cookies file path with EU consent cookies (CONSENT, SOCS) added.
    Merges with existing cookies or creates a minimal file. Uses a temp file to avoid mutating the original.
    """
    expiration = "9999999999"  # Far future
    consent_lines = [
        f"{domain}\tTRUE\t{path}\tTRUE\t{expiration}\t{name}\t{value}"
        for domain, path, name, value in _CONSENT_COOKIES
    ]

    if cookies_path and cookies_path.exists() and cookies_path.stat().st_size > 0:
        try:
            text = cookies_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            out_lines = []
            for line in lines:
                if line.startswith("#") or not line.strip():
                    out_lines.append(line)
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name, domain = parts[5], parts[0]
                    if name in ("CONSENT", "SOCS") and "youtube" in domain:
                        continue  # Drop existing consent cookies, we'll add fresh ones
                out_lines.append(line)
            merged = "\n".join(out_lines).rstrip()
            if not merged.endswith("\n") and merged:
                merged += "\n"
            merged += "\n".join(consent_lines) + "\n"
        except OSError as e:
            logger.warning("Could not read cookies file %s: %s. Using consent-only.", cookies_path, e)
            merged = "# HTTP Cookie File\n" + "\n".join(consent_lines) + "\n"
    else:
        merged = "# HTTP Cookie File\n" + "\n".join(consent_lines) + "\n"

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(merged)
        return Path(path)
    except OSError:
        return cookies_path


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
            # Prefer clients that don't require PO Token (avoids challenge_required)
            # Skip webpage to avoid consent_required (EU cookie consent / age verification)
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv_embedded", "tv", "tv_simply", "android_vr", "android"],
                    "player_skip": ["webpage", "configs"],
                },
            },
        }
        cookies_to_use = _ensure_consent_cookies(self.cookies_path)
        try:
            if cookies_to_use:
                ydl_opts["cookiefile"] = str(cookies_to_use)
                logger.info("Using cookies from %s (with EU consent)", cookies_to_use)
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
            if cookies_to_use:
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

"""yt-dlp video downloader with metadata extraction."""

import logging
import subprocess
from pathlib import Path
from typing import Optional

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

logger = logging.getLogger(__name__)


def _is_format_unavailable_error(error: Exception) -> bool:
    """Return True when yt-dlp failed due to unavailable requested format."""
    err_msg = str(error).lower()
    needles = (
        "requested format is not available",
        "format is not available",
        "requested format not available",
        "no video formats found",
        "no suitable format",
        "unable to download",
        "no formats found",
        "format not available",
    )
    return any(needle in err_msg for needle in needles)


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

        base_opts = {
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "check_formats": False,
        }
        if self.cookies_path and self.cookies_path.exists() and self.cookies_path.stat().st_size > 0:
            base_opts["cookiefile"] = str(self.cookies_path)

        extracted_info = {}

        def postprocessor_hook(info):
            extracted_info["title"] = info.get("title")
            extracted_info["tags"] = info.get("tags") or []

        # (format, extractor_args) — try in order; format=None means omit format key.
        # Prioritise clients that do NOT require a PO (Proof-of-Origin) Token.
        # Use "worst" as fallback — download whatever format is available, then convert to mp4.
        retry_combinations = [
            (None, None),
            ("bv*+ba/b", None),
            ("b", None),
            ("bv*+ba/b", {"youtube": {"player_client": "tv"}}),
            ("b", {"youtube": {"player_client": "tv"}}),
            ("worst", None),
            ("worst", {"youtube": {"player_client": "tv"}}),
            ("worstvideo+worstaudio/worst", None),
            ("bv*+ba/b", {"youtube": {"player_client": "web_embedded"}}),
            ("b", {"youtube": {"player_client": "web_embedded"}}),
            ("worst", {"youtube": {"player_client": "web_embedded"}}),
            ("bv*+ba/b", {"youtube": {"player_client": "android_vr"}}),
            ("worst", {"youtube": {"player_client": "android_vr"}}),
            ("18", None),  # 360p single-file MP4
            ("17", None),  # 144p single-file MP4 last resort
        ]

        last_error = None
        downloaded = False
        for fmt, extractor_args in retry_combinations:
            # Clean up any partial files from previous attempt
            for ext in ["mp4", "webm", "mkv", "m4a", "3gp", "flv"]:
                (self.storage_path / f"{job_id}.{ext}").unlink(missing_ok=True)

            ydl_opts = dict(base_opts)
            if fmt is not None:
                ydl_opts["format"] = fmt
            if extractor_args is not None:
                ydl_opts["extractor_args"] = extractor_args

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        extracted_info["title"] = info.get("title")
                        extracted_info["tags"] = info.get("tags") or []
                downloaded = True
                last_error = None
                break
            except (DownloadError, ExtractorError) as e:
                last_error = e
                if _is_format_unavailable_error(e):
                    label = f"format={fmt!r}" if fmt else "default format"
                    if extractor_args:
                        yt_args = extractor_args.get("youtube", {})
                        player_client = yt_args.get("player_client", "unknown")
                        label += f" ({player_client})"
                    logger.info("%s failed, retrying: %s", label, e)
                    continue
                raise

        if not downloaded and last_error is not None:
            raise last_error

        # Find the downloaded file (yt-dlp may use different extensions; worst can yield 3gp, flv)
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

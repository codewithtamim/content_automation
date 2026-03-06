"""yt-dlp video downloader with metadata extraction."""

import os
from pathlib import Path
from typing import Optional

import yt_dlp


class YtDlpDownloader:
    """Download videos using yt-dlp and extract metadata."""

    def __init__(self, storage_path: str = "/tmp/videos"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

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
            "format": "best[ext=mp4]/best",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
        }

        extracted_info = {}

        def postprocessor_hook(info):
            extracted_info["title"] = info.get("title")
            extracted_info["tags"] = info.get("tags") or []

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                extracted_info["title"] = info.get("title")
                extracted_info["tags"] = info.get("tags") or []

        # Find the downloaded file (yt-dlp may use different extensions)
        output_path = None
        for ext in ["mp4", "webm", "mkv", "m4a"]:
            candidate = self.storage_path / f"{job_id}.{ext}"
            if candidate.exists():
                output_path = str(candidate)
                break

        if not output_path:
            raise RuntimeError(f"Download failed: no output file found for job {job_id}")

        title = extracted_info.get("title")
        tags = extracted_info.get("tags")
        if tags and not isinstance(tags, list):
            tags = [str(t) for t in tags] if tags else None

        return output_path, title, tags

"""yt-dlp video downloader with metadata extraction."""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

logger = logging.getLogger(__name__)


def _get_js_runtimes() -> dict:
    """Find available JS runtimes for yt-dlp. Returns {runtime: {path: ...}}."""
    runtimes = {}
    for name in ("node", "nodejs", "deno"):
        path = shutil.which(name)
        if path:
            key = "node" if name == "nodejs" else name
            if key not in runtimes:
                runtimes[key] = {"path": path}
    return runtimes


def _convert_to_mp4(path: str) -> str:
    """Convert video to mp4 using ffmpeg if not already mp4."""
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
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    path,
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "aac",
                    str(mp4_path),
                ],
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
        max_resolution: Optional[int] = None,
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.cookies_path = (
            Path(cookies_path) if (cookies_path and cookies_path.strip()) else None
        )

        self.proxy = proxy.strip() if (proxy and proxy.strip()) else None
        self.max_resolution = max_resolution

    def download(
        self,
        url: str,
        job_id: int,
    ) -> tuple[str, Optional[str], Optional[list[str]]]:
        """
        Download video from URL and extract metadata.
        Downloads best available format, then converts to mp4 via ffmpeg.
        """
        output_template = str(self.storage_path / f"{job_id}.%(ext)s")
        opts = {
            "outtmpl": output_template,
            "noplaylist": True,
            "logger": logger,
            "merge_output_format": "mp4",
        }
        if self.max_resolution and self.max_resolution > 0:
            opts["format"] = (
                f"best[ext=mp4][height<={self.max_resolution}]/"
                f"best[height<={self.max_resolution}]/"
                f"bestvideo[height<={self.max_resolution}]+bestaudio/"
                f"bestvideo[height<={self.max_resolution}]/best[height<={self.max_resolution}]/best"
            )
        else:
            opts["format"] = "best[ext=mp4]/best"
        js_runtimes = _get_js_runtimes()
        if js_runtimes:
            opts["js_runtimes"] = js_runtimes

        has_cookies = (
            self.cookies_path
            and self.cookies_path.exists()
            and self.cookies_path.stat().st_size > 0
        )
        if has_cookies:
            opts["cookiefile"] = str(self.cookies_path)

        if self.proxy:
            opts["proxy"] = self.proxy

        extracted_info = {}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    extracted_info["title"] = info.get("title")
                    extracted_info["tags"] = info.get("tags") or []
        except (DownloadError, ExtractorError) as e:
            err_msg = str(e).lower()
            if has_cookies and (
                "signature solving failed" in err_msg
                or "requested format is not available" in err_msg
                or "only images are available" in err_msg
            ):
                logger.warning(
                    "Download failed with cookies (signature/format issue), retrying without cookies: %s",
                    e,
                )
                opts.pop("cookiefile", None)
                for f in self.storage_path.glob(f"{job_id}.*"):
                    f.unlink(missing_ok=True)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        extracted_info["title"] = info.get("title")
                        extracted_info["tags"] = info.get("tags") or []
            else:
                raise

        output_path = None
        for ext in ["mp4", "webm", "mkv", "m4a", "3gp", "flv"]:
            candidate = self.storage_path / f"{job_id}.{ext}"
            if candidate.exists():
                output_path = str(candidate)
                break

        if not output_path:
            raise RuntimeError(f"Download failed: no output file found for job {job_id}")

        output_path = _convert_to_mp4(output_path)

        title = extracted_info.get("title")
        tags = extracted_info.get("tags")
        if tags and not isinstance(tags, list):
            tags = [str(t) for t in tags] if tags else None

        return output_path, title, tags

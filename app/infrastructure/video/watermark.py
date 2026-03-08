"""Add a logo watermark to videos using ffmpeg."""

import logging
import os
from pathlib import Path

import ffmpeg

logger = logging.getLogger(__name__)


def add_watermark(video_path: str, logo_path: str) -> str:
    """
    Overlay a logo at the bottom-center of a video.

    The logo is scaled to 15% of the video width, then placed centered
    horizontally with 20 px padding from the bottom edge.

    Args:
        video_path: Path to the input video (must exist).
        logo_path: Path to the logo PNG (must exist).

    Returns:
        Path to the watermarked video (same as video_path; the original is
        replaced in-place).
    """
    p = Path(video_path)
    out_path = p.with_stem(p.stem + "_wm")

    video = ffmpeg.input(video_path)
    logo = ffmpeg.input(logo_path)

    # Scale logo to 15% of the video width, keep aspect ratio
    logo_scaled = logo.filter("scale", w="trunc(main_w*0.15/2)*2", h="-1")

    overlaid = ffmpeg.overlay(
        video.video,
        logo_scaled,
        x="(main_w-overlay_w)/2",
        y="main_h-overlay_h-20",
    )

    out = ffmpeg.output(
        overlaid,
        video.audio,
        str(out_path),
        vcodec="libx264",
        acodec="aac",
        loglevel="error",
    )

    try:
        out.overwrite_output().run()
    except ffmpeg.Error as e:
        logger.error("Watermark failed: %s", e.stderr)
        raise

    # Replace original with watermarked version
    os.replace(str(out_path), video_path)
    logger.info("Watermarked video: %s", video_path)
    return video_path

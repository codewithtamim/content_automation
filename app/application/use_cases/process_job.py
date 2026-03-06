"""Process job use case - orchestrates download, AI metadata, and upload."""

import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional

from app.domain.entities.video_job import VideoJob
from app.infrastructure.database.repository import VideoJobRepository
from app.infrastructure.downloader.ytdlp_downloader import YtDlpDownloader
from app.infrastructure.uploaders.instagram_uploader import InstagramUploader


def _build_caption(title: str, tags: list[str]) -> str:
    """Build caption/description with title and hashtags."""
    hashtags = " ".join(f"#{t.replace('#', '')}" for t in tags) if tags else ""
    return f"{title} {hashtags}".strip()


def process_job(
    job_id: int,
    repository: VideoJobRepository,
    downloader: YtDlpDownloader,
    metadata_client=None,
    instagram_uploader: InstagramUploader = None,
    generate_metadata_fn: Optional[Callable[[str, list[str]], dict]] = None,
    delete_after_upload: bool = True,
) -> VideoJob:
    """
    Process a single video job: download, generate metadata, upload.

    Args:
        job_id: Job ID to process.
        repository: Video job repository.
        downloader: Video downloader.
        metadata_client: Deprecated. Use generate_metadata_fn instead.
        instagram_uploader: Instagram uploader.
        generate_metadata_fn: Callable(title, tags) -> dict with title, tags. Used if provided.
        delete_after_upload: Whether to delete local file after upload.

    Returns:
        Updated VideoJob entity.
    """
    job = repository.get_by_id(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    if job.status != "pending":
        raise ValueError(f"Job {job_id} is not pending (status: {job.status})")

    try:
        # 1. Download
        job.status = "downloading"
        repository.update(job)

        local_path, original_title, original_tags = downloader.download(job.original_url, job_id)
        job.local_path = local_path
        job.original_title = original_title
        job.original_tags = original_tags or []
        repository.update(job)

        # 2. Generate metadata
        job.status = "metadata_generating"
        repository.update(job)

        if generate_metadata_fn:
            metadata = generate_metadata_fn(original_title or "", original_tags or [])
        elif metadata_client:
            metadata = metadata_client.generate_metadata(
                title=original_title or "",
                tags=original_tags or [],
            )
        else:
            raise ValueError("Either generate_metadata_fn or metadata_client must be provided")
        job.generated_title = metadata["title"]
        job.generated_tags = metadata["tags"]
        repository.update(job)

        # 3. Upload
        job.status = "uploading"
        repository.update(job)

        caption = _build_caption(job.generated_title, job.generated_tags or [])

        instagram_uploader.upload_reel(local_path, caption)

        # 4. Success
        job.status = "completed"
        job.error_message = None
        repository.update(job)

    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)
        repository.update(job)
        raise
    finally:
        # Cleanup local file
        if job.local_path and delete_after_upload and os.path.exists(job.local_path):
            try:
                os.remove(job.local_path)
            except OSError:
                pass

    return job

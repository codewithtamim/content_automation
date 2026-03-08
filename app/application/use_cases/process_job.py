"""Process job use case - orchestrates download, AI metadata, and upload."""

import logging
import os
from collections.abc import Callable
from typing import Optional

from sqlalchemy.orm import sessionmaker

from app.domain.entities.video_job import VideoJob
from app.infrastructure.database.repository import VideoJobRepository
from app.infrastructure.database.session import get_db_session, retry_on_locked
from app.infrastructure.downloader.ytdlp_downloader import YtDlpDownloader
from app.infrastructure.uploaders.instagram_uploader import InstagramUploader
from app.infrastructure.video.watermark import add_watermark

logger = logging.getLogger(__name__)


def _build_caption(title: str, tags: list[str]) -> str:
    """Build caption/description with title and hashtags."""
    hashtags = " ".join(f"#{t.replace('#', '')}" for t in tags) if tags else ""
    return f"{title} {hashtags}".strip()


def _update_job_status(SessionLocal, job_id: int, **kwargs) -> VideoJob:
    """Short-lived update: open session, update, commit, close. Retries on database locked."""

    def _do_update():
        with get_db_session(SessionLocal) as session:
            repo = VideoJobRepository(session)
            job = repo.get_by_id(job_id)
            if not job:
                raise ValueError(f"Job {job_id} not found")
            for key, value in kwargs.items():
                setattr(job, key, value)
            repo.update(job)
            return job

    return retry_on_locked(_do_update)


def process_job(
    job_id: int,
    repository: Optional[VideoJobRepository],
    downloader: YtDlpDownloader,
    metadata_client=None,
    instagram_uploader: InstagramUploader = None,
    generate_metadata_fn: Optional[Callable[[str, list[str]], dict]] = None,
    delete_after_upload: bool = True,
    logo_path: Optional[str] = None,
    SessionLocal: sessionmaker | None = None,
) -> VideoJob:
    """
    Process a single video job: download, watermark, generate metadata, upload.

    Uses short-lived DB sessions for each update to avoid holding connections during
    long operations (download, upload), which prevents "database is locked" errors.
    Pass SessionLocal to enable short-lived sessions.

    Args:
        job_id: Job ID to process.
        repository: Video job repository (used for initial fetch when SessionLocal provided).
        downloader: Video downloader.
        metadata_client: Deprecated. Use generate_metadata_fn instead.
        instagram_uploader: Instagram uploader.
        generate_metadata_fn: Callable(title, tags) -> dict with title, tags. Used if provided.
        delete_after_upload: Whether to delete local file after upload.
        logo_path: Path to logo PNG for watermarking. Skipped if None.
        SessionLocal: Session factory for short-lived updates. When provided, avoids DB locks.

    Returns:
        Updated VideoJob entity.
    """
    session_factory = SessionLocal
    use_short_sessions = session_factory is not None

    if use_short_sessions:
        with get_db_session(session_factory) as session:
            repo = VideoJobRepository(session)
            job = repo.get_by_id(job_id)
    else:
        job = repository.get_by_id(job_id)

    if not job:
        raise ValueError(f"Job {job_id} not found")
    if job.status != "pending":
        raise ValueError(f"Job {job_id} is not pending (status: {job.status})")

    def _update(**kwargs):
        if use_short_sessions:
            return _update_job_status(session_factory, job_id, **kwargs)
        for k, v in kwargs.items():
            setattr(job, k, v)
        repository.update(job)
        return job

    local_path: Optional[str] = None
    try:
        # 1. Download (status update in short tx, then download without holding session)
        _update(status="downloading")
        local_path, original_title, original_tags = downloader.download(job.original_url, job_id)
        _update(
            local_path=local_path, original_title=original_title,
            original_tags=original_tags or [],
        )
        job.local_path = local_path
        job.original_title = original_title
        job.original_tags = original_tags or []

        # 2. Watermark
        if logo_path and os.path.exists(logo_path):
            _update(status="watermarking")
            add_watermark(local_path, logo_path)
            logger.info("Watermark applied to job %s", job_id)
        else:
            logger.warning("Logo not found at %s, skipping watermark", logo_path)

        # 3. Generate metadata
        _update(status="metadata_generating")
        if generate_metadata_fn:
            metadata = generate_metadata_fn(original_title or "", original_tags or [])
        elif metadata_client:
            metadata = metadata_client.generate_metadata(
                title=original_title or "",
                tags=original_tags or [],
            )
        else:
            raise ValueError("Either generate_metadata_fn or metadata_client must be provided")
        _update(generated_title=metadata["title"], generated_tags=metadata["tags"])
        job.generated_title = metadata["title"]
        job.generated_tags = metadata["tags"]

        # 4. Upload
        _update(status="uploading")
        caption = _build_caption(job.generated_title, job.generated_tags or [])
        instagram_uploader.upload_reel(local_path, caption)

        # 5. Success
        _update(status="completed", error_message=None)
        job.status = "completed"
        job.error_message = None

    except Exception as e:
        try:
            _update(status="failed", error_message=str(e))
        except Exception:
            pass
        job.status = "failed"
        job.error_message = str(e)
        raise
    finally:
        if local_path and delete_after_upload and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass

    return job

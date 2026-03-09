"""Prep job use case - download, watermark, generate metadata for scheduled videos."""

import logging
import os
from collections.abc import Callable
from typing import Optional

from sqlalchemy.orm import sessionmaker

from app.domain.entities.video_job import VideoJob
from app.infrastructure.database.repository import VideoJobRepository
from app.infrastructure.database.session import get_db_session, retry_on_locked
from app.infrastructure.downloader.ytdlp_downloader import YtDlpDownloader
from app.infrastructure.video.watermark import add_watermark

logger = logging.getLogger(__name__)

# Type for batch metadata: (title, tags) -> dict
BatchMetadataFn = Callable[[list[tuple[str, list[str]]]], list[dict]]


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


def prep_job(
    job_id: int,
    repository: Optional[VideoJobRepository],
    downloader: YtDlpDownloader,
    generate_metadata_fn: Callable[[str, list[str]], dict],
    logo_path: Optional[str] = None,
    SessionLocal: sessionmaker | None = None,
) -> VideoJob:
    """
    Pre-process a scheduled video job: download, watermark, generate metadata.
    Sets status to ready_to_upload. Does NOT upload or delete the file.

    Args:
        job_id: Job ID to prep.
        repository: Video job repository (used when SessionLocal not provided).
        downloader: Video downloader.
        generate_metadata_fn: Callable(title, tags) -> dict with title, tags.
        logo_path: Path to logo PNG for watermarking. Skipped if None.
        SessionLocal: Session factory for short-lived updates.

    Returns:
        Updated VideoJob entity with status ready_to_upload.
    """
    session_factory = SessionLocal
    use_short_sessions = session_factory is not None

    if use_short_sessions:
        with get_db_session(session_factory) as session:
            repo = VideoJobRepository(session)
            job = repo.get_by_id(job_id)
    elif repository:
        job = repository.get_by_id(job_id)
    else:
        raise ValueError("Either SessionLocal or repository must be provided")

    if not job:
        raise ValueError(f"Job {job_id} not found")
    if job.status != "pending":
        raise ValueError(f"Job {job_id} is not pending (status: {job.status})")
    if not job.schedule_time:
        raise ValueError(f"Job {job_id} has no schedule_time - prep is for scheduled jobs only")

    def _update(**kwargs):
        if use_short_sessions:
            return _update_job_status(session_factory, job_id, **kwargs)
        for k, v in kwargs.items():
            setattr(job, k, v)
        repository.update(job)
        return job

    local_path: Optional[str] = None
    try:
        _update(status="downloading")
        local_path, original_title, original_tags = downloader.download(job.original_url, job_id)
        _update(
            local_path=local_path,
            original_title=original_title,
            original_tags=original_tags or [],
        )
        job.local_path = local_path
        job.original_title = original_title
        job.original_tags = original_tags or []

        if logo_path and os.path.exists(logo_path):
            _update(status="watermarking")
            add_watermark(local_path, logo_path)
            logger.info("Watermark applied to job %s", job_id)
        else:
            logger.warning("Logo not found at %s, skipping watermark", logo_path)

        _update(status="metadata_generating")
        metadata = generate_metadata_fn(original_title or "", original_tags or [])
        _update(generated_title=metadata["title"], generated_tags=metadata["tags"])
        job.generated_title = metadata["title"]
        job.generated_tags = metadata["tags"]

        _update(status="ready_to_upload", error_message=None)
        job.status = "ready_to_upload"
        job.error_message = None

    except Exception as e:
        try:
            _update(status="failed", error_message=str(e))
        except Exception:
            pass
        job.status = "failed"
        job.error_message = str(e)
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass
        raise

    return job


def prep_jobs_batch(
    job_ids: list[int],
    repository: Optional[VideoJobRepository],
    downloader: YtDlpDownloader,
    generate_metadata_batch_fn: BatchMetadataFn,
    logo_path: Optional[str] | None = None,
    SessionLocal: sessionmaker | None = None,
) -> list[VideoJob]:
    """
    Pre-process multiple scheduled jobs: download, watermark, then batch metadata.
    Uses a single Gemini API call for all jobs to save cost.

    Args:
        job_ids: List of job IDs to prep.
        repository: Used when SessionLocal not provided.
        downloader: Video downloader.
        generate_metadata_batch_fn: Callable that takes list of (title, tags) and returns list of dicts.
        logo_path: Path to logo PNG. Skipped if None.
        SessionLocal: Session factory for short-lived updates.

    Returns:
        List of updated VideoJob entities with status ready_to_upload.
    """
    if not job_ids:
        return []

    session_factory = SessionLocal
    use_short_sessions = session_factory is not None

    def _update(jid: int, **kwargs) -> VideoJob:
        def _do():
            with get_db_session(session_factory) as session:
                repo = VideoJobRepository(session)
                j = repo.get_by_id(jid)
                if not j:
                    raise ValueError(f"Job {jid} not found")
                for k, v in kwargs.items():
                    setattr(j, k, v)
                repo.update(j)
                return j
        return retry_on_locked(_do)

    jobs_data: list[tuple[VideoJob, str, str | None, list[str] | None]] = []
    failed_ids: list[int] = []

    for job_id in job_ids:
        if use_short_sessions:
            with get_db_session(session_factory) as session:
                repo = VideoJobRepository(session)
                job = repo.get_by_id(job_id)
        else:
            job = repository.get_by_id(job_id) if repository else None

        if not job or job.status != "pending" or not job.schedule_time:
            if job and job.status != "pending":
                failed_ids.append(job_id)
            continue

        local_path: Optional[str] = None
        try:
            _update(job_id, status="downloading")
            local_path, original_title, original_tags = downloader.download(job.original_url, job_id)
            _update(
                job_id,
                local_path=local_path,
                original_title=original_title,
                original_tags=original_tags or [],
            )
            if logo_path and os.path.exists(logo_path):
                _update(job_id, status="watermarking")
                add_watermark(local_path, logo_path)
            jobs_data.append((job, local_path, original_title, original_tags or []))
        except Exception as e:
            logger.exception("Prep batch: job %s download/watermark failed: %s", job_id, e)
            _update(job_id, status="failed", error_message=str(e)[:500])
            failed_ids.append(job_id)
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass

    if not jobs_data:
        return []

    # Batch metadata for all successful downloads
    items = [(j[2] or "", j[3] or []) for j in jobs_data]
    try:
        for job, _, _, _ in jobs_data:
            _update(job.id, status="metadata_generating")
        metadata_list = generate_metadata_batch_fn(items)
    except Exception as e:
        logger.exception("Prep batch: metadata failed: %s", e)
        for job, local_path, _, _ in jobs_data:
            _update(job.id, status="failed", error_message=str(e)[:500])
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass
        raise

    # Update each job with metadata and set ready_to_upload
    result_jobs = []
    for i, (job, local_path, _, _) in enumerate(jobs_data):
        meta = metadata_list[i] if i < len(metadata_list) else {"title": job.original_title or "Viral Video", "tags": ["viral", "fyp", "trending", "foryou", "viral"]}
        _update(
            job.id,
            generated_title=meta["title"],
            generated_tags=meta["tags"],
            status="ready_to_upload",
            error_message=None,
        )
        job.generated_title = meta["title"]
        job.generated_tags = meta["tags"]
        job.status = "ready_to_upload"
        job.local_path = local_path
        result_jobs.append(job)

    return result_jobs

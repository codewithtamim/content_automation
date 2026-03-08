"""Background worker for processing scheduled video jobs."""

import logging
import threading
import time
from datetime import datetime, timezone

from app.application.use_cases.process_job import process_job
from app.infrastructure.ai.gemini_client import generate_metadata_with_failover
from app.infrastructure.database.repository import (
    GeminiKeyRepository,
    InstagramAccountRepository,
    VideoJobRepository,
)
from app.infrastructure.database.session import get_db_session
from app.infrastructure.downloader.ytdlp_downloader import YtDlpDownloader
from app.infrastructure.uploaders.instagram_uploader import InstagramUploader

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
UPLOAD_DELAY_SECONDS = 30  # Delay between uploads to avoid rate limits


def run_worker(
    SessionLocal,
    video_storage_path: str,
    gemini_model: str = "gemini-2.5-flash",
    yt_cookies_path: str = "cookies.txt",
    stop_event: threading.Event | None = None,
) -> None:
    """
    Run the background worker loop.

    Loads Gemini keys and Instagram accounts from DB. Processes jobs using
    credentials from DB.
    """
    downloader = YtDlpDownloader(
        storage_path=video_storage_path,
        cookies_path=yt_cookies_path,
    )

    if stop_event is None:
        stop_event = threading.Event()

    while not stop_event.is_set():
        try:
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                gemini_repo = GeminiKeyRepository(session)
                insta_repo = InstagramAccountRepository(session)

                gemini_keys_data = gemini_repo.list_all_ordered()
                gemini_keys = [enc for _, enc in gemini_keys_data]

                now = datetime.now(timezone.utc)
                pending = repo.get_pending_jobs(now)

                for job in pending:
                    if stop_event.is_set():
                        break
                    try:
                        if not job.instagram_account_id:
                            logger.error(
                                "Job %s has no Instagram account. Re-create the job with an account.",
                                job.id,
                            )
                            job.status = "failed"
                            job.error_message = "No Instagram account configured. Re-create the job."
                            repo.update(job)
                            continue

                        account = insta_repo.get_by_id(job.instagram_account_id)
                        if not account:
                            logger.error("Job %s: Instagram account %s not found", job.id, job.instagram_account_id)
                            job.status = "failed"
                            job.error_message = f"Instagram account {job.instagram_account_id} not found"
                            repo.update(job)
                            continue

                        username, password = account
                        instagram_uploader = InstagramUploader(username=username, password=password)

                        if not gemini_keys:
                            logger.error("No Gemini API keys configured. Add keys via bot.")
                            job.status = "failed"
                            job.error_message = "No Gemini API keys configured"
                            repo.update(job)
                            continue

                        logger.info("Processing job %s: %s", job.id, job.original_url)

                        def _generate_metadata(title: str, tags: list[str]):
                            return generate_metadata_with_failover(
                                gemini_keys, title, tags, model_name=gemini_model
                            )

                        process_job(
                            job_id=job.id,
                            repository=repo,
                            downloader=downloader,
                            metadata_client=None,
                            instagram_uploader=instagram_uploader,
                            generate_metadata_fn=_generate_metadata,
                        )
                        logger.info("Job %s completed successfully", job.id)
                        time.sleep(UPLOAD_DELAY_SECONDS)
                    except Exception as e:
                        logger.exception("Job %s failed: %s", job.id, e)

        except Exception as e:
            logger.exception("Worker iteration failed: %s", e)

        for _ in range(POLL_INTERVAL_SECONDS):
            if stop_event.is_set():
                break
            time.sleep(1)
    logger.info("Worker stopped")

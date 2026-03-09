"""Background worker for processing scheduled video jobs."""

import logging
import threading
import time
from datetime import datetime, timezone

from app.application.use_cases.prep_job import prep_job
from app.application.use_cases.process_job import process_job
from app.infrastructure.ai.gemini_client import generate_metadata_with_failover
from app.infrastructure.notifications.telegram_notifier import notify_admin
from app.infrastructure.database.repository import (
    GeminiKeyRepository,
    InstagramAccountRepository,
    VideoJobRepository,
)
from app.infrastructure.database.session import get_db_session, retry_on_locked
from app.infrastructure.config_paths import get_cookies_path
from app.infrastructure.downloader.ytdlp_downloader import YtDlpDownloader
from app.infrastructure.uploaders.instagram_uploader import InstagramUploader

logger = logging.getLogger(__name__)

UPLOAD_DELAY_SECONDS = 30  # Delay between uploads to avoid rate limits


def _notify_admin_job_failed(
    job_id: int,
    original_url: str,
    error_message: str,
    submitted_by_username: str | None,
    admin_chat_id: str | None,
    bot_token: str | None,
) -> None:
    """Notify admin via Telegram when a job fails."""
    if not admin_chat_id or not bot_token:
        return
    submitter = f"@{submitted_by_username}" if submitted_by_username else "Unknown"
    msg = (
        f"Job {job_id} failed\n\n"
        f"Submitted by: {submitter}\n"
        f"URL: {original_url}\n\n"
        f"Error: {error_message}"
    )
    if any(
        x in error_message.lower()
        for x in ("sign in to confirm", "cookies", "challenge_required", "consent_required")
    ):
        msg += "\n\nTip: Upload fresh YouTube cookies via Manage credentials → Upload YouTube cookies"
    reply_markup = {"inline_keyboard": [[{"text": "Retry", "callback_data": f"retry_job_{job_id}"}]]}
    notify_admin(bot_token, admin_chat_id, msg, reply_markup=reply_markup)


def _notify_admin_job_completed(
    job_id: int,
    original_url: str,
    generated_title: str | None,
    submitted_by_username: str | None,
    admin_chat_id: str | None,
    bot_token: str | None,
) -> None:
    """Notify admin via Telegram when a job completes successfully."""
    if not admin_chat_id or not bot_token:
        return
    submitter = f"@{submitted_by_username}" if submitted_by_username else "Unknown"
    title_line = f"Title: {generated_title}\n" if generated_title else ""
    msg = (
        f"Job {job_id} completed\n\n"
        f"Submitted by: {submitter}\n"
        f"URL: {original_url}\n"
        f"{title_line}"
        f"Uploaded to Instagram."
    )
    notify_admin(bot_token, admin_chat_id, msg)


def run_worker(
    SessionLocal,
    engine,
    pause_event: threading.Event | None = None,
    video_storage_path: str = "",
    gemini_model: str = "gemini-2.5-flash",
    yt_cookies_path: str = "cookies.txt",
    yt_proxy: str | None = None,
    stop_event: threading.Event | None = None,
    admin_telegram_chat_id: str | None = None,
    telegram_bot_token: str | None = None,
    prep_scheduled_videos: bool = True,
    prep_hours_before_schedule: int = 24,
    prep_min_schedule_ahead_minutes: int = 5,
    poll_interval_idle_seconds: int = 60,
    poll_interval_active_seconds: int = 30,
) -> None:
    """
    Run the background worker loop.

    Loads Gemini keys and Instagram accounts from DB. Processes jobs using
    credentials from DB. Optionally pre-processes scheduled jobs (download,
    watermark, metadata) ahead of schedule_time.
    """
    cookies_path = get_cookies_path(yt_cookies_path)
    logger.info("Worker cookies path: %s", cookies_path)

    downloader = YtDlpDownloader(
        storage_path=video_storage_path,
        cookies_path=str(cookies_path),
        proxy=yt_proxy,
    )

    if stop_event is None:
        stop_event = threading.Event()
    if pause_event is None:
        pause_event = threading.Event()

    while not stop_event.is_set():
        while pause_event.is_set():
            time.sleep(0.5)
        prep_done = False
        pending = []
        try:
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                gemini_repo = GeminiKeyRepository(session)
                insta_repo = InstagramAccountRepository(session)

                gemini_keys_data = gemini_repo.list_all_ordered()
                gemini_keys = [enc for _, enc in gemini_keys_data]

                now = datetime.now(timezone.utc)
                pending = repo.get_pending_jobs(now)
                jobs_for_prep = (
                    repo.get_jobs_for_prep(
                        now,
                        min_schedule_ahead_minutes=prep_min_schedule_ahead_minutes,
                        max_schedule_ahead_hours=prep_hours_before_schedule,
                    )
                    if prep_scheduled_videos and gemini_keys
                    else []
                )

            # Priority 1: Prep scheduled jobs (if enabled and any need prep)
            prep_done = False
            if jobs_for_prep and not pending:
                prep_job_obj = jobs_for_prep[0]
                if stop_event.is_set():
                    break
                try:
                    with get_db_session(SessionLocal) as session:
                        repo = VideoJobRepository(session)
                        insta_repo = InstagramAccountRepository(session)
                        account = insta_repo.get_by_id(prep_job_obj.instagram_account_id)
                        if not account or not prep_job_obj.instagram_account_id:
                            err = "No Instagram account configured for prep job"
                            logger.error("Job %s: %s", prep_job_obj.id, err)
                            prep_job_obj.status = "failed"
                            prep_job_obj.error_message = err
                            repo.update(prep_job_obj)
                            _notify_admin_job_failed(
                                prep_job_obj.id, prep_job_obj.original_url, err,
                                prep_job_obj.submitted_by_username,
                                admin_telegram_chat_id, telegram_bot_token,
                            )
                        else:
                            username, password, watermark_path = account[0], account[1], account[2]

                            def _generate_metadata(title: str, tags: list[str]):
                                return generate_metadata_with_failover(
                                    gemini_keys, title, tags, model_name=gemini_model
                                )

                            prep_job(
                                job_id=prep_job_obj.id,
                                repository=None,
                                downloader=downloader,
                                generate_metadata_fn=_generate_metadata,
                                logo_path=watermark_path,
                                SessionLocal=SessionLocal,
                            )
                            logger.info("Job %s pre-processed (ready to upload at schedule time)", prep_job_obj.id)
                            prep_done = True
                except Exception as e:
                    logger.exception("Prep job %s failed: %s", prep_job_obj.id, e)
                    try:
                        def _mark_failed():
                            with get_db_session(SessionLocal) as session:
                                repo = VideoJobRepository(session)
                                failed_job = repo.get_by_id(prep_job_obj.id)
                                if failed_job:
                                    failed_job.status = "failed"
                                    failed_job.error_message = str(e)[:500]
                                    repo.update(failed_job)
                        retry_on_locked(_mark_failed)
                    except Exception:
                        pass
                    _notify_admin_job_failed(
                        prep_job_obj.id, prep_job_obj.original_url, str(e),
                        prep_job_obj.submitted_by_username,
                        admin_telegram_chat_id, telegram_bot_token,
                    )

            # Priority 2: Upload jobs (pending or ready_to_upload, schedule_time <= now)
            if not prep_done and pending:
                job = pending[0]
                if stop_event.is_set():
                    pass  # Will break at end of loop
                else:
                    account_data = None
                    try:
                        with get_db_session(SessionLocal) as session:
                            repo = VideoJobRepository(session)
                            insta_repo = InstagramAccountRepository(session)

                            if not job.instagram_account_id:
                                err = "No Instagram account configured. Re-create the job."
                                logger.error(
                                    "Job %s has no Instagram account. Re-create the job with an account.",
                                    job.id,
                                )
                                job.status = "failed"
                                job.error_message = err
                                repo.update(job)
                                _notify_admin_job_failed(
                                    job.id, job.original_url, err,
                                    job.submitted_by_username,
                                    admin_telegram_chat_id, telegram_bot_token,
                                )
                            elif not (account := insta_repo.get_by_id(job.instagram_account_id)):
                                err = f"Instagram account {job.instagram_account_id} not found"
                                logger.error("Job %s: Instagram account %s not found", job.id, job.instagram_account_id)
                                job.status = "failed"
                                job.error_message = err
                                repo.update(job)
                                _notify_admin_job_failed(
                                    job.id, job.original_url, err,
                                    job.submitted_by_username,
                                    admin_telegram_chat_id, telegram_bot_token,
                                )
                            elif not gemini_keys:
                                err = "No Gemini API keys configured"
                                logger.error("No Gemini API keys configured. Add keys via bot.")
                                job.status = "failed"
                                job.error_message = err
                                repo.update(job)
                                _notify_admin_job_failed(
                                    job.id, job.original_url, err,
                                    job.submitted_by_username,
                                    admin_telegram_chat_id, telegram_bot_token,
                                )
                            else:
                                account_data = (account[0], account[1], account[2])  # username, password, watermark_path

                        if account_data:
                            username, password, watermark_path = account_data
                            instagram_uploader = InstagramUploader(username=username, password=password)

                            logger.info("Processing job %s: %s", job.id, job.original_url)

                            def _generate_metadata(title: str, tags: list[str]):
                                return generate_metadata_with_failover(
                                    gemini_keys, title, tags, model_name=gemini_model
                                )

                            completed_job = process_job(
                                job_id=job.id,
                                repository=None,
                                downloader=downloader,
                                metadata_client=None,
                                instagram_uploader=instagram_uploader,
                                generate_metadata_fn=_generate_metadata,
                                logo_path=watermark_path,
                                SessionLocal=SessionLocal,
                            )
                            logger.info("Job %s completed successfully", job.id)
                            _notify_admin_job_completed(
                                completed_job.id,
                                completed_job.original_url,
                                completed_job.generated_title,
                                completed_job.submitted_by_username,
                                admin_telegram_chat_id,
                                telegram_bot_token,
                            )
                            time.sleep(UPLOAD_DELAY_SECONDS)
                    except Exception as e:
                        logger.exception("Job %s failed: %s", job.id, e)
                        marked = False
                        try:
                            def _mark_failed():
                                with get_db_session(SessionLocal) as session:
                                    repo = VideoJobRepository(session)
                                    failed_job = repo.get_by_id(job.id)
                                    if failed_job:
                                        failed_job.status = "failed"
                                        failed_job.error_message = str(e)[:500]
                                        repo.update(failed_job)
                            retry_on_locked(_mark_failed)
                            marked = True
                        except Exception as db_err:
                            logger.exception("Could not update job %s to failed: %s", job.id, db_err)
                            try:
                                from sqlalchemy import text
                                with engine.connect() as conn:
                                    now_str = datetime.now(timezone.utc).isoformat()
                                    conn.execute(
                                        text(
                                            "UPDATE video_jobs SET status='failed', error_message=:err, updated_at=:now WHERE id=:jid"
                                        ),
                                        {"err": str(e)[:500], "jid": job.id, "now": now_str},
                                    )
                                    conn.commit()
                                marked = True
                            except Exception as raw_err:
                                logger.exception("Raw SQL fallback failed for job %s: %s", job.id, raw_err)
                        if not marked:
                            logger.warning("Job %s could not be marked failed - will retry next poll", job.id)
                            time.sleep(5)
                        _notify_admin_job_failed(
                            job.id, job.original_url, str(e),
                            job.submitted_by_username,
                            admin_telegram_chat_id, telegram_bot_token,
                        )

        except Exception as e:
            logger.exception("Worker iteration failed: %s", e)

        # Adaptive poll: shorter when jobs are ready, longer when idle
        poll_seconds = poll_interval_active_seconds if (prep_done or pending) else poll_interval_idle_seconds
        for _ in range(poll_seconds):
            if stop_event.is_set():
                break
            time.sleep(1)
    logger.info("Worker stopped")

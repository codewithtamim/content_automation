"""Immediate prep: download, watermark, cache metadata when admin creates scheduled jobs."""

import logging
import threading
from typing import Any

from app.application.use_cases.prep_job import prep_job
from app.infrastructure.ai.gemini_client import generate_metadata_with_failover
from app.infrastructure.config_paths import get_cookies_path
from app.infrastructure.database.repository import (
    GeminiKeyRepository,
    InstagramAccountRepository,
    VideoJobRepository,
)
from app.infrastructure.database.session import get_db_session, retry_on_locked
from app.infrastructure.downloader.ytdlp_downloader import YtDlpDownloader
from app.infrastructure.notifications.telegram_notifier import notify_admin

logger = logging.getLogger(__name__)


def _notify_prep_failed(
    job_id: int,
    original_url: str,
    error_message: str,
    submitted_by_username: str | None,
    admin_chat_id: str | None,
    bot_token: str | None,
) -> None:
    """Notify admin when immediate prep fails."""
    if not admin_chat_id or not bot_token:
        return
    submitter = f"@{submitted_by_username}" if submitted_by_username else "Unknown"
    msg = (
        f"Prep failed for job {job_id}\n\n"
        f"Submitted by: {submitter}\n"
        f"URL: {original_url}\n\n"
        f"Error: {error_message}\n\n"
        f"The job will retry when you click Retry, or the worker may pick it up."
    )
    if any(
        x in error_message.lower()
        for x in ("sign in to confirm", "cookies", "challenge_required", "consent_required")
    ):
        msg += "\n\nTip: Upload fresh YouTube cookies via Manage credentials."
    reply_markup = {"inline_keyboard": [[{"text": "Retry", "callback_data": f"retry_job_{job_id}"}]]}
    notify_admin(bot_token, admin_chat_id, msg, reply_markup=reply_markup)


def _run_immediate_prep_sync(
    job_ids: list[int],
    SessionLocal: Any,
    video_storage_path: str,
    cookies_path: str,
    yt_proxy: str | None,
    gemini_model: str,
    admin_chat_id: str | None,
    telegram_bot_token: str | None,
) -> None:
    """
    Run prep (download, watermark, metadata) for scheduled jobs in a background thread.
    Called immediately when admin creates scheduled jobs - videos are ready before schedule_time.
    """
    if not job_ids:
        return
    cookies_path_resolved = str(get_cookies_path(cookies_path))
    downloader = YtDlpDownloader(
        storage_path=video_storage_path,
        cookies_path=cookies_path_resolved,
        proxy=yt_proxy,
    )
    for job_id in job_ids:
        try:
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                gemini_repo = GeminiKeyRepository(session)
                insta_repo = InstagramAccountRepository(session)
                job = repo.get_by_id(job_id)
                if not job or job.status != "pending" or not job.schedule_time:
                    continue
                if not job.instagram_account_id:
                    err = "No Instagram account configured"
                    job.status = "failed"
                    job.error_message = err
                    repo.update(job)
                    _notify_prep_failed(
                        job_id, job.original_url, err,
                        job.submitted_by_username,
                        admin_chat_id, telegram_bot_token,
                    )
                    continue
                account = insta_repo.get_by_id(job.instagram_account_id)
                if not account:
                    err = f"Instagram account {job.instagram_account_id} not found"
                    job.status = "failed"
                    job.error_message = err
                    repo.update(job)
                    _notify_prep_failed(
                        job_id, job.original_url, err,
                        job.submitted_by_username,
                        admin_chat_id, telegram_bot_token,
                    )
                    continue
                gemini_keys_data = gemini_repo.list_all_ordered()
                gemini_keys = [k for _, k in gemini_keys_data]
                if not gemini_keys:
                    err = "No Gemini API keys configured"
                    job.status = "failed"
                    job.error_message = err
                    repo.update(job)
                    _notify_prep_failed(
                        job_id, job.original_url, err,
                        job.submitted_by_username,
                        admin_chat_id, telegram_bot_token,
                    )
                    continue
                username, password, watermark_path = account[0], account[1], account[2]

            def _generate_metadata(title: str, tags: list[str]):
                return generate_metadata_with_failover(
                    gemini_keys, title, tags, model_name=gemini_model
                )

            prep_job(
                job_id=job_id,
                repository=None,
                downloader=downloader,
                generate_metadata_fn=_generate_metadata,
                logo_path=watermark_path,
                SessionLocal=SessionLocal,
            )
            logger.info("Job %s prepped immediately (ready for upload at schedule time)", job_id)
        except Exception as e:
            logger.exception("Immediate prep failed for job %s: %s", job_id, e)
            try:
                def _mark_failed():
                    with get_db_session(SessionLocal) as session:
                        repo = VideoJobRepository(session)
                        j = repo.get_by_id(job_id)
                        if j:
                            j.status = "failed"
                            j.error_message = str(e)[:500]
                            repo.update(j)
                retry_on_locked(_mark_failed)
            except Exception:
                pass
            with get_db_session(SessionLocal) as session:
                repo = VideoJobRepository(session)
                j = repo.get_by_id(job_id)
                if j:
                    _notify_prep_failed(
                        job_id, j.original_url, str(e),
                        j.submitted_by_username,
                        admin_chat_id, telegram_bot_token,
                    )


def start_immediate_prep(
    job_ids: list[int],
    bot_data: dict,
) -> None:
    """
    Start immediate prep in a background thread. Non-blocking.
    Call this right after creating scheduled jobs.
    """
    if not job_ids:
        return
    prep_config = bot_data.get("prep_config")
    if not prep_config:
        logger.warning("prep_config not in bot_data - skipping immediate prep")
        return
    thread = threading.Thread(
        target=_run_immediate_prep_sync,
        kwargs={
            "job_ids": job_ids,
            "SessionLocal": bot_data["SessionLocal"],
            "video_storage_path": prep_config["video_storage_path"],
            "cookies_path": prep_config["cookies_path"],
            "yt_proxy": prep_config.get("yt_proxy"),
            "gemini_model": prep_config.get("gemini_model", "gemini-2.5-flash"),
            "admin_chat_id": bot_data.get("admin_chat_id"),
            "telegram_bot_token": bot_data.get("telegram_bot_token"),
        },
        daemon=True,
    )
    thread.start()
    logger.info("Started immediate prep for %d job(s) in background", len(job_ids))

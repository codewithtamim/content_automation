"""Application entrypoint - starts Telegram bot and background worker."""

import logging
import signal
import sys
import threading
from pathlib import Path

from config import get_settings
from app.infrastructure.database.session import create_engine_and_session, init_db
from app.infrastructure.scheduler.worker import run_worker
from app.interfaces.telegram_bot.bot import create_application

logger = logging.getLogger(__name__)

# Global stop event for worker (set on SIGTERM)
_worker_stop_event = threading.Event()


def main() -> None:
    """Start the application."""
    settings = get_settings()

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )

    engine, SessionLocal = create_engine_and_session(settings.database_url)
    init_db(engine)
    logger.info("Database initialized")

    # Start worker in background thread (credentials loaded from DB)
    worker_thread = threading.Thread(
        target=run_worker,
        kwargs={
            "SessionLocal": SessionLocal,
            "video_storage_path": settings.video_storage_path,
            "gemini_model": settings.gemini_model,
            "yt_cookies_path": settings.yt_cookies_path,
            "stop_event": _worker_stop_event,
            "admin_telegram_chat_id": settings.admin_telegram_chat_id,
            "telegram_bot_token": settings.telegram_bot_token,
        },
        daemon=False,
    )
    worker_thread.start()
    logger.info("Worker thread started")

    # Resolve cookies path (same logic as worker)
    cookies_path = Path(settings.yt_cookies_path)
    if not cookies_path.is_absolute():
        project_root = Path(__file__).resolve().parents[1]
        cookies_path = project_root / settings.yt_cookies_path

    # Create Telegram bot
    app = create_application(
        bot_token=settings.telegram_bot_token,
        admin_chat_id=settings.admin_telegram_chat_id,
        admin_username=settings.admin_telegram_username,
        SessionLocal=SessionLocal,
        cookies_path=str(cookies_path),
    )

    def _sigterm_handler(signum, frame):
        logger.info("Received SIGTERM, shutting down gracefully...")
        _worker_stop_event.set()
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        logger.info("Starting Telegram bot (Instagram only)")
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        logger.warning("run_polling returned unexpectedly - bot stopped")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        _worker_stop_event.set()
        worker_thread.join(timeout=30)
        if worker_thread.is_alive():
            logger.warning("Worker did not stop within timeout")
    except Exception as e:
        logger.exception("Application error: %s", e)
        _worker_stop_event.set()
        worker_thread.join(timeout=10)
        raise


if __name__ == "__main__":
    main()

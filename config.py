"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str
    admin_telegram_chat_id: str
    admin_telegram_username: str

    # Telegram mode: "polling" (default) or "webhook"
    telegram_mode: str = "polling"

    # Webhook (when telegram_mode=webhook). Required: webhook_secret_token.
    webhook_port: int = 8080
    webhook_url_path: str = "webhook"
    webhook_secret_token: str | None = None
    webhook_public_url: str | None = None

    # Database (SQLite file)
    database_url: str = "sqlite:///data/tiktok_automation.db"

    # AI model (keys stored in DB, set via bot)
    gemini_model: str = "gemini-2.5-flash"

    # Batch size for Gemini metadata (N jobs = 1 API call). Max 30 recommended.
    gemini_metadata_batch_size: int = 30

    # Persist metadata cache to DB (survives restarts, saves Gemini cost)
    use_metadata_db_cache: bool = True

    # Storage
    video_storage_path: str = "data/videos"

    # Instagram session persistence (per-account JSON files)
    instagram_session_path: str = "data/sessions"

    # YouTube cookies (optional, for bypassing bot detection)
    # Path to Netscape-format cookies file. Default: cookies.txt in project root
    yt_cookies_path: str = "cookies.txt"

    # Optional: HTTP proxy for yt-dlp (e.g. http://user:pass@host:port). Helps with datacenter IP blocks.
    yt_proxy: str | None = None

    # Max video resolution for downloads (e.g. 720, 1080). None = best available.
    yt_max_resolution: int | None = None

    # Pre-processing for scheduled videos (download/watermark/metadata ahead of schedule_time)
    prep_scheduled_videos: bool = True
    prep_hours_before_schedule: int = 24  # Only prep jobs within this window
    prep_min_schedule_ahead_minutes: int = 5  # Don't prep if schedule_time is within this many minutes

    # Worker polling
    poll_interval_idle_seconds: int = 60
    poll_interval_active_seconds: int = 30

    # Optional default Instagram account (skip picker when only one account or this is set)
    default_instagram_account_id: int | None = None

    # Logging
    log_level: str = "INFO"

    # Run VACUUM during low activity (e.g. when idle for N worker iterations). 0 = disabled.
    db_vacuum_idle_iterations: int = 0


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()

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

    # Database (SQLite file)
    database_url: str = "sqlite:///data/tiktok_automation.db"

    # AI model (keys stored in DB, set via bot)
    gemini_model: str = "gemini-2.5-flash"

    # Storage
    video_storage_path: str = "data/videos"

    # YouTube cookies (optional, for bypassing bot detection)
    # Path to Netscape-format cookies file. Default: cookies.txt in project root
    yt_cookies_path: str = "cookies.txt"

    # Optional: HTTP proxy for yt-dlp (e.g. http://user:pass@host:port). Helps with datacenter IP blocks.
    yt_proxy: str | None = None

    # Logging
    log_level: str = "INFO"


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()

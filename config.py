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

    # Database
    database_url: str

    # AI model (keys stored in DB, set via bot)
    gemini_model: str = "gemini-2.5-flash"

    # Storage
    video_storage_path: str = "/tmp/videos"

    # YouTube cookies (optional, for bypassing bot detection)
    # Path to Netscape-format cookies file. Default: cookies.txt in project root
    yt_cookies_path: str = "cookies.txt"

    # Logging
    log_level: str = "INFO"


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()

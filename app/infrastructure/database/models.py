"""SQLAlchemy database models."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""

    pass


class VideoJobModel(Base):
    """SQLAlchemy model for video_jobs table."""

    __tablename__ = "video_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    original_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    instagram_account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("instagram_accounts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    schedule_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    original_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    original_tags: Mapped[Optional[list]] = mapped_column(ARRAY(Text), nullable=True)
    generated_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    generated_tags: Mapped[Optional[list]] = mapped_column(ARRAY(Text), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_by_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SubAdminModel(Base):
    """SQLAlchemy model for sub_admins table."""

    __tablename__ = "sub_admins"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    permissions: Mapped[Optional[list]] = mapped_column(ARRAY(Text), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GeminiKeyModel(Base):
    """SQLAlchemy model for gemini_keys table."""

    __tablename__ = "gemini_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class InstagramAccountModel(Base):
    """SQLAlchemy model for instagram_accounts table."""

    __tablename__ = "instagram_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

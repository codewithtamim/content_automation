"""Video job repository - CRUD operations and domain mapping."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from app.domain.entities.video_job import VideoJob
from app.infrastructure.database.models import (
    GeminiKeyModel,
    InstagramAccountModel,
    SubAdminModel,
    VideoJobModel,
)


def _model_to_entity(model: VideoJobModel) -> VideoJob:
    """Map SQLAlchemy model to domain entity."""
    return VideoJob(
        id=model.id,
        original_url=model.original_url,
        platform=model.platform,
        instagram_account_id=model.instagram_account_id,
        status=model.status,
        schedule_time=model.schedule_time,
        local_path=model.local_path,
        original_title=model.original_title,
        original_tags=list(model.original_tags) if model.original_tags else None,
        generated_title=model.generated_title,
        generated_tags=list(model.generated_tags) if model.generated_tags else None,
        error_message=model.error_message,
        submitted_by_username=model.submitted_by_username,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _entity_to_model(entity: VideoJob) -> VideoJobModel:
    """Map domain entity to SQLAlchemy model (for create/update)."""
    return VideoJobModel(
        id=entity.id or 0,
        original_url=entity.original_url,
        platform=entity.platform,
        instagram_account_id=entity.instagram_account_id,
        status=entity.status,
        schedule_time=entity.schedule_time,
        local_path=entity.local_path,
        original_title=entity.original_title,
        original_tags=entity.original_tags,
        generated_title=entity.generated_title,
        generated_tags=entity.generated_tags,
        error_message=entity.error_message,
        submitted_by_username=entity.submitted_by_username,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


class VideoJobRepository:
    """Repository for video job persistence."""

    def __init__(self, session: Session):
        self.session = session

    def create(self, job: VideoJob) -> VideoJob:
        """Create a new video job and return it with id."""
        model = VideoJobModel(
            original_url=job.original_url,
            platform=job.platform,
            instagram_account_id=job.instagram_account_id,
            status=job.status,
            schedule_time=job.schedule_time,
            local_path=job.local_path,
            original_title=job.original_title,
            original_tags=job.original_tags,
            generated_title=job.generated_title,
            generated_tags=job.generated_tags,
            error_message=job.error_message,
            submitted_by_username=job.submitted_by_username,
        )
        self.session.add(model)
        self.session.flush()
        return _model_to_entity(model)

    def get_by_id(self, job_id: int) -> Optional[VideoJob]:
        """Get a video job by id."""
        model = self.session.get(VideoJobModel, job_id)
        return _model_to_entity(model) if model else None

    def update(self, job: VideoJob) -> VideoJob:
        """Update an existing video job."""
        model = self.session.get(VideoJobModel, job.id)
        if not model:
            raise ValueError(f"Job {job.id} not found")
        model.status = job.status
        model.instagram_account_id = job.instagram_account_id
        model.schedule_time = job.schedule_time
        model.local_path = job.local_path
        model.original_title = job.original_title
        model.original_tags = job.original_tags
        model.generated_title = job.generated_title
        model.generated_tags = job.generated_tags
        model.error_message = job.error_message
        model.submitted_by_username = job.submitted_by_username
        model.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return _model_to_entity(model)

    def get_pending_jobs(self, now: datetime) -> list[VideoJob]:
        """Get jobs that are pending and ready to process (no schedule or schedule <= now)."""
        stmt = (
            select(VideoJobModel)
            .where(
                and_(
                    VideoJobModel.status == "pending",
                    or_(
                        VideoJobModel.schedule_time.is_(None),
                        VideoJobModel.schedule_time <= now,
                    ),
                )
            )
            .order_by(VideoJobModel.schedule_time, VideoJobModel.created_at)
        )
        result = self.session.execute(stmt)
        models = result.scalars().all()
        return [_model_to_entity(m) for m in models]

    def get_all_pending_and_scheduled(self) -> list[VideoJob]:
        """Get all jobs with status pending (for viewing scheduled tasks)."""
        stmt = (
            select(VideoJobModel)
            .where(VideoJobModel.status == "pending")
            .order_by(VideoJobModel.schedule_time, VideoJobModel.created_at)
        )
        result = self.session.execute(stmt)
        models = result.scalars().all()
        return [_model_to_entity(m) for m in models]


# Sub-admin permission constants
PERM_UPLOAD_VIDEOS = "upload_videos"
PERM_SCHEDULE_UPLOADS = "schedule_uploads"
PERM_VIEW_SCHEDULED_TASKS = "view_scheduled_tasks"
PERM_MANAGE_ADMINS = "manage_admins"
PERM_MANAGE_CREDS = "manage_creds"

ALL_PERMISSIONS = [
    PERM_UPLOAD_VIDEOS,
    PERM_SCHEDULE_UPLOADS,
    PERM_VIEW_SCHEDULED_TASKS,
    PERM_MANAGE_ADMINS,
    PERM_MANAGE_CREDS,
]


class SubAdminRepository:
    """Repository for sub-admin persistence."""

    def __init__(self, session: Session):
        self.session = session

    def add(self, username: str, permissions: list[str] | None = None) -> SubAdminModel:
        """Add a sub-admin. Username is normalized (lowercase, no @)."""
        normalized = username.strip().lower().lstrip("@")
        if not normalized:
            raise ValueError("Username cannot be empty")
        perms = permissions if permissions is not None else ALL_PERMISSIONS
        model = SubAdminModel(username=normalized, permissions=perms)
        self.session.add(model)
        self.session.flush()
        return model

    def remove(self, username: str) -> bool:
        """Remove a sub-admin by username. Returns True if removed."""
        normalized = username.strip().lower().lstrip("@")
        stmt = delete(SubAdminModel).where(SubAdminModel.username == normalized)
        result = self.session.execute(stmt)
        return result.rowcount > 0

    def list_all(self) -> list[tuple[str, list[str]]]:
        """Return all sub-admins as (username, permissions) tuples."""
        stmt = select(SubAdminModel.username, SubAdminModel.permissions).order_by(
            SubAdminModel.username
        )
        result = self.session.execute(stmt)
        rows = result.fetchall()
        return [
            (row[0], list(row[1]) if row[1] else ALL_PERMISSIONS)
            for row in rows
        ]

    def get_permissions(self, username: str) -> list[str] | None:
        """Return permissions for a sub-admin, or None if not found."""
        normalized = username.strip().lower().lstrip("@")
        stmt = select(SubAdminModel.permissions).where(SubAdminModel.username == normalized)
        result = self.session.execute(stmt)
        row = result.first()
        if row is None or row[0] is None:
            return None
        return list(row[0])

    def exists(self, username: str) -> bool:
        """Check if username is a sub-admin."""
        normalized = username.strip().lower().lstrip("@")
        stmt = select(SubAdminModel).where(SubAdminModel.username == normalized)
        result = self.session.execute(stmt)
        return result.scalars().first() is not None




class GeminiKeyRepository:
    """Repository for Gemini API key persistence."""

    def __init__(self, session: Session):
        self.session = session

    def add(self, api_key_encrypted: str, priority: int = 0) -> GeminiKeyModel:
        """Add a Gemini API key (encrypted)."""
        model = GeminiKeyModel(api_key_encrypted=api_key_encrypted, priority=priority)
        self.session.add(model)
        self.session.flush()
        return model

    def list_all_ordered(self) -> list[tuple[int, str]]:
        """Return (id, encrypted_key) tuples ordered by priority ascending."""
        stmt = (
            select(GeminiKeyModel.id, GeminiKeyModel.api_key_encrypted)
            .order_by(GeminiKeyModel.priority, GeminiKeyModel.id)
        )
        result = self.session.execute(stmt)
        return [(row[0], row[1]) for row in result.fetchall()]

    def remove(self, key_id: int) -> bool:
        """Remove a Gemini key by id. Returns True if removed."""
        model = self.session.get(GeminiKeyModel, key_id)
        if not model:
            return False
        self.session.delete(model)
        self.session.flush()
        return True


class InstagramAccountRepository:
    """Repository for Instagram account persistence."""

    def __init__(self, session: Session):
        self.session = session

    def add(
        self, username: str, password_encrypted: str, watermark_path: str | None = None,
    ) -> InstagramAccountModel:
        """Add an Instagram account (password encrypted)."""
        model = InstagramAccountModel(
            username=username,
            password_encrypted=password_encrypted,
            watermark_path=watermark_path,
        )
        self.session.add(model)
        self.session.flush()
        return model

    def list_all(self) -> list[tuple[int, str, str | None]]:
        """Return (id, username, watermark_path) tuples."""
        stmt = select(
            InstagramAccountModel.id,
            InstagramAccountModel.username,
            InstagramAccountModel.watermark_path,
        ).order_by(InstagramAccountModel.username)
        result = self.session.execute(stmt)
        return [(row[0], row[1], row[2]) for row in result.fetchall()]

    def get_by_id(self, account_id: int) -> Optional[tuple[str, str, str | None]]:
        """Get (username, password_encrypted, watermark_path) by id."""
        model = self.session.get(InstagramAccountModel, account_id)
        if not model:
            return None
        return (model.username, model.password_encrypted, model.watermark_path)

    def update_watermark(self, account_id: int, watermark_path: str | None) -> bool:
        """Set or clear the watermark path for an account. Returns True if found."""
        model = self.session.get(InstagramAccountModel, account_id)
        if not model:
            return False
        model.watermark_path = watermark_path
        self.session.flush()
        return True

    def remove(self, account_id: int) -> bool:
        """Remove an Instagram account by id. Returns True if removed."""
        model = self.session.get(InstagramAccountModel, account_id)
        if not model:
            return False
        stmt = (
            update(VideoJobModel)
            .where(VideoJobModel.instagram_account_id == account_id)
            .values(instagram_account_id=None)
        )
        self.session.execute(stmt)
        self.session.delete(model)
        self.session.flush()
        return True

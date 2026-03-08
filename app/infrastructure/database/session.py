"""Database session factory and connection management."""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.database.models import (
    Base,
    GeminiKeyModel,
    InstagramAccountModel,
    SubAdminModel,
    VideoJobModel,
)


def create_engine_and_session(database_url: str):
    """Create engine and session factory."""
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal


def init_db(engine) -> None:
    """Create all tables and run migrations for existing databases."""
    Base.metadata.create_all(bind=engine)

    # Migration: add instagram_account_id to video_jobs if missing (for existing DBs)
    from sqlalchemy import text

    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'video_jobs' AND column_name = 'instagram_account_id'
                """
            )
        )
        if result.fetchone() is None:
            conn.execute(
                text(
                    "ALTER TABLE video_jobs ADD COLUMN instagram_account_id INTEGER "
                    "REFERENCES instagram_accounts(id)"
                )
            )
            conn.commit()

    # Migration: add submitted_by_username to video_jobs if missing (for existing DBs)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'video_jobs' AND column_name = 'submitted_by_username'
                """
            )
        )
        if result.fetchone() is None:
            conn.execute(text("ALTER TABLE video_jobs ADD COLUMN submitted_by_username VARCHAR(255)"))
            conn.commit()

    # Migration: add permissions to sub_admins if missing (for existing DBs)
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'sub_admins' AND column_name = 'permissions'
                """
            )
        )
        if result.fetchone() is None:
            conn.execute(text("ALTER TABLE sub_admins ADD COLUMN permissions TEXT[]"))
            # Existing sub-admins get full access (all permissions)
            conn.execute(
                text(
                    "UPDATE sub_admins SET permissions = ARRAY['upload_videos','schedule_uploads',"
                    "'view_scheduled_tasks','manage_admins','manage_creds']"
                )
            )
            conn.commit()


@contextmanager
def get_db_session(SessionLocal: sessionmaker) -> Generator[Session, None, None]:
    """Context manager for database sessions."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

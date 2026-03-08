"""Database session factory and connection management."""

import json
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.database.models import Base


def _table_has_column(conn, table: str, column: str) -> bool:
    """Check if a SQLite table has a given column using PRAGMA."""
    result = conn.execute(text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result.fetchall())


def create_engine_and_session(database_url: str):
    """Create engine and session factory."""
    connect_args = {"check_same_thread": False}
    if database_url.startswith("sqlite"):
        connect_args["timeout"] = 60  # Wait up to 60s for lock (avoids "database is locked")
    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if database_url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")  # 60s in ms
            cursor.close()

        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal


def init_db(engine) -> None:
    """Create all tables and run migrations for existing databases."""
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        if not _table_has_column(conn, "video_jobs", "instagram_account_id"):
            conn.execute(text(
                "ALTER TABLE video_jobs ADD COLUMN instagram_account_id INTEGER "
                "REFERENCES instagram_accounts(id)"
            ))
            conn.commit()

        if not _table_has_column(conn, "video_jobs", "submitted_by_username"):
            conn.execute(text("ALTER TABLE video_jobs ADD COLUMN submitted_by_username VARCHAR(255)"))
            conn.commit()

        if not _table_has_column(conn, "instagram_accounts", "watermark_path"):
            conn.execute(text("ALTER TABLE instagram_accounts ADD COLUMN watermark_path VARCHAR(1024)"))
            conn.commit()

        if not _table_has_column(conn, "sub_admins", "permissions"):
            conn.execute(text("ALTER TABLE sub_admins ADD COLUMN permissions TEXT"))
            all_perms = json.dumps([
                "upload_videos", "schedule_uploads",
                "view_scheduled_tasks", "manage_admins", "manage_creds",
            ])
            conn.execute(text("UPDATE sub_admins SET permissions = :perms"), {"perms": all_perms})
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

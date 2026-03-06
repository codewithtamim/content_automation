"""Create job use case."""

import re
from datetime import datetime
from typing import Optional

from app.domain.entities.video_job import VideoJob
from app.infrastructure.database.repository import VideoJobRepository


URL_PATTERN = re.compile(
    r"^https?://[^\s]{10,}$",
    re.IGNORECASE,
)


def parse_urls(text: str) -> list[str]:
    """Parse URLs from comma or newline separated text."""
    urls = []
    for part in re.split(r"[\s,]+", text.strip()):
        part = part.strip()
        if part and URL_PATTERN.match(part):
            urls.append(part)
    return urls


def create_job(
    repository: VideoJobRepository,
    urls: list[str],
    platform: str = "instagram",
    schedule_time: Optional[datetime] = None,
    instagram_account_id: Optional[int] = None,
) -> list[int]:
    """
    Create video jobs for each URL.

    Args:
        repository: Video job repository.
        urls: List of video URLs.
        platform: Target platform (instagram).
        schedule_time: Optional scheduled upload time.
        instagram_account_id: Instagram account to use for upload.

    Returns:
        List of created job IDs.
    """
    now = datetime.now()
    job_ids = []
    for url in urls:
        job = VideoJob(
            id=None,
            original_url=url,
            platform=platform,
            instagram_account_id=instagram_account_id,
            status="pending",
            schedule_time=schedule_time,
            local_path=None,
            original_title=None,
            original_tags=None,
            generated_title=None,
            generated_tags=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        created = repository.create(job)
        job_ids.append(created.id)
    return job_ids

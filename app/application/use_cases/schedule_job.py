"""Schedule job use case."""

from datetime import datetime

from app.infrastructure.database.repository import VideoJobRepository


def schedule_job(
    repository: VideoJobRepository,
    job_id: int,
    schedule_time: datetime,
) -> bool:
    """
    Set schedule time for an existing job.

    Args:
        repository: Video job repository.
        job_id: Job ID to schedule.
        schedule_time: When to process the job.

    Returns:
        True if job was found and updated, False otherwise.
    """
    job = repository.get_by_id(job_id)
    if not job or job.status != "pending":
        return False
    job.schedule_time = schedule_time
    repository.update(job)
    return True

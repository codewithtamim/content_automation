"""Video job domain entity."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


@dataclass
class VideoJob:
    """Domain entity representing a video upload job."""

    id: Optional[int]
    original_url: str
    platform: Literal["instagram"]
    instagram_account_id: Optional[int]
    status: Literal[
        "pending",
        "downloading",
        "metadata_generating",
        "uploading",
        "completed",
        "failed",
    ]
    schedule_time: Optional[datetime]
    local_path: Optional[str]
    original_title: Optional[str]
    original_tags: Optional[list[str]]
    generated_title: Optional[str]
    generated_tags: Optional[list[str]]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

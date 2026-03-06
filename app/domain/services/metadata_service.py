"""Metadata service protocol for AI-generated titles and tags."""

from typing import Protocol


class MetadataService(Protocol):
    """Abstract interface for AI metadata generation (dependency inversion)."""

    def generate_metadata(self, title: str, tags: list[str]) -> dict:
        """
        Generate viral short-video title and hashtags from original metadata.

        Args:
            title: Original video title.
            tags: Original video tags.

        Returns:
            Dict with keys: "title" (str), "tags" (list[str]).
        """
        ...

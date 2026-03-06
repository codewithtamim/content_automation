"""Gemini AI client for metadata generation."""

import json
import re

from google import genai
from pydantic import BaseModel


class GeneratedMetadata(BaseModel):
    """Schema for AI-generated metadata response."""

    title: str
    tags: list[str]


class GeminiMetadataClient:
    """Gemini API client implementing MetadataService protocol."""

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash"):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def generate_metadata(self, title: str, tags: list[str]) -> dict:
        """
        Generate viral short-video title and hashtags from original metadata.

        Args:
            title: Original video title.
            tags: Original video tags.

        Returns:
            Dict with keys: "title" (str), "tags" (list[str]).
        """
        prompt = f"""You are a social media expert. Given the following video metadata, generate an optimized viral title and 5 hashtags for Instagram Reels.

Original title: {title or "Unknown"}
Original tags: {", ".join(tags) if tags else "None"}

Respond with ONLY a valid JSON object, no other text:
{{"title": "Optimized viral title for short video", "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}}

Rules:
- Title: catchy, under 100 chars, optimized for engagement
- Tags: 5 relevant hashtags without # symbol, lowercase
- Return valid JSON only"""

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
        )

        if not response.text:
            return self._fallback_metadata(title, tags)

        try:
            # Extract JSON from response (handle markdown code blocks)
            text = response.text.strip()
            # Remove markdown code blocks if present
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            parsed = GeneratedMetadata(
                title=data.get("title", title or "Viral Video"),
                tags=data.get("tags", tags or ["viral", "fyp", "trending", "foryou", "viral"])[:5],
            )
            return {"title": parsed.title, "tags": parsed.tags}
        except (json.JSONDecodeError, Exception):
            pass

        return self._fallback_metadata(title, tags)

    def _fallback_metadata(self, title: str, tags: list[str]) -> dict:
        """Fallback when AI response cannot be parsed."""
        fallback_tags = ["viral", "fyp", "trending", "foryou", "viral"]
        if tags:
            fallback_tags = [str(t).replace("#", "").lower() for t in tags[:5]]
            while len(fallback_tags) < 5:
                fallback_tags.append("viral")
        return {
            "title": title or "Viral Video",
            "tags": fallback_tags[:5],
        }


def generate_metadata_with_failover(
    api_keys: list[str],
    title: str,
    tags: list[str],
    model_name: str = "gemini-2.5-flash",
) -> dict:
    """
    Try each API key in order until one succeeds.
    Raises RuntimeError if all keys fail.
    """
    errors = []
    for key in api_keys:
        try:
            client = GeminiMetadataClient(api_key=key, model_name=model_name)
            return client.generate_metadata(title, tags)
        except Exception as e:
            errors.append(str(e))
            continue
    raise RuntimeError(f"All Gemini API keys failed: {'; '.join(errors[-3:])}")

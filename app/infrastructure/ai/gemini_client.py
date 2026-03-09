"""Gemini AI client for metadata generation."""

import hashlib
import json
import re
from collections import OrderedDict

from google import genai
from pydantic import BaseModel

# In-memory cache for metadata by (title, tags) - avoids duplicate Gemini calls
_METADATA_CACHE: OrderedDict = OrderedDict()
_METADATA_CACHE_MAX = 200


def _cache_key(title: str, tags: list[str]) -> str:
    """Create cache key from title and tags."""
    tags_str = ",".join(sorted(str(t) for t in (tags or [])))
    raw = f"{title or ''}|{tags_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


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
    db_cache_get=None,
    db_cache_set=None,
) -> dict:
    """
    Try each API key in order until one succeeds.
    Uses in-memory cache and optional DB cache to avoid duplicate Gemini calls.
    Raises RuntimeError if all keys fail.
    """
    key = _cache_key(title, tags)
    if key in _METADATA_CACHE:
        _METADATA_CACHE.move_to_end(key)
        return _METADATA_CACHE[key]

    if db_cache_get:
        try:
            cached = db_cache_get(key)
            if cached:
                if len(_METADATA_CACHE) >= _METADATA_CACHE_MAX:
                    _METADATA_CACHE.popitem(last=False)
                _METADATA_CACHE[key] = cached
                _METADATA_CACHE.move_to_end(key)
                return cached
        except Exception:
            pass

    errors = []
    for api_key in api_keys:
        try:
            client = GeminiMetadataClient(api_key=api_key, model_name=model_name)
            result = client.generate_metadata(title, tags)
            if len(_METADATA_CACHE) >= _METADATA_CACHE_MAX:
                _METADATA_CACHE.popitem(last=False)
            _METADATA_CACHE[key] = result
            _METADATA_CACHE.move_to_end(key)
            if db_cache_set:
                try:
                    db_cache_set(key, result["title"], result["tags"])
                except Exception:
                    pass
            return result
        except Exception as e:
            errors.append(str(e))
            continue
    raise RuntimeError(f"All Gemini API keys failed: {'; '.join(errors[-3:])}")


def generate_metadata_batch_with_failover(
    api_keys: list[str],
    items: list[tuple[str, list[str]]],
    model_name: str = "gemini-2.5-flash",
    db_cache_get=None,
    db_cache_set=None,
) -> list[dict]:
    """
    Generate metadata for multiple videos in a single Gemini API call.
    Saves API cost when processing batch/scheduled uploads.

    Args:
        api_keys: List of Gemini API keys (tried in order).
        items: List of (title, tags) tuples, one per video.
        model_name: Gemini model name.

    Returns:
        List of dicts with "title" and "tags" keys, same order as items.
    """
    if not items:
        return []

    # Check cache for each item; collect uncached indices
    results: list[dict | None] = [None] * len(items)
    uncached: list[tuple[int, str, list[str]]] = []
    for i, (title, tags) in enumerate(items):
        key = _cache_key(title, tags)
        if key in _METADATA_CACHE:
            _METADATA_CACHE.move_to_end(key)
            results[i] = _METADATA_CACHE[key]
        elif db_cache_get:
            try:
                cached = db_cache_get(key)
                if cached:
                    results[i] = cached
                    if len(_METADATA_CACHE) >= _METADATA_CACHE_MAX:
                        _METADATA_CACHE.popitem(last=False)
                    _METADATA_CACHE[key] = cached
                    _METADATA_CACHE.move_to_end(key)
                    continue
            except Exception:
                pass
            uncached.append((i, title, tags))
        else:
            uncached.append((i, title, tags))

    if not uncached:
        return results

    # Build batch prompt for uncached items
    lines = []
    for idx, (orig_i, orig_title, orig_tags) in enumerate(uncached):
        lines.append(f"  {idx + 1}. Title: {orig_title or 'Unknown'}; Tags: {', '.join(orig_tags) if orig_tags else 'None'}")
    prompt_lines = "\n".join(lines)

    prompt = f"""You are a social media expert. Given the following {len(uncached)} video metadata entries, generate an optimized viral title and 5 hashtags for each, for Instagram Reels.

Videos:
{prompt_lines}

Respond with ONLY a valid JSON array, no other text. One object per video, in the same order:
[{{"title": "...", "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}}, ...]

Rules:
- Title: catchy, under 100 chars, optimized for engagement
- Tags: 5 relevant hashtags without # symbol, lowercase
- Return valid JSON array only, same length as input"""

    errors = []
    for api_key in api_keys:
        try:
            client = GeminiMetadataClient(api_key=api_key, model_name=model_name)
            response = client.client.models.generate_content(
                model=client.model_name,
                contents=prompt,
            )
            if not response.text:
                raise ValueError("Empty response")
            text = response.text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            if not isinstance(data, list) or len(data) < len(uncached):
                raise ValueError(f"Expected list of {len(uncached)} items, got {len(data) if isinstance(data, list) else 'non-list'}")
            for j, (orig_i, orig_title, orig_tags) in enumerate(uncached):
                if j < len(data):
                    obj = data[j]
                    parsed = GeneratedMetadata(
                        title=obj.get("title", orig_title or "Viral Video"),
                        tags=(obj.get("tags") or ["viral", "fyp", "trending", "foryou", "viral"])[:5],
                    )
                    res = {"title": parsed.title, "tags": parsed.tags}
                    results[orig_i] = res
                    # Cache it
                    key = _cache_key(orig_title or "", orig_tags or [])
                    if len(_METADATA_CACHE) >= _METADATA_CACHE_MAX:
                        _METADATA_CACHE.popitem(last=False)
                    _METADATA_CACHE[key] = res
                    _METADATA_CACHE.move_to_end(key)
                    if db_cache_set:
                        try:
                            db_cache_set(key, res["title"], res["tags"])
                        except Exception:
                            pass
                else:
                    results[orig_i] = client._fallback_metadata(orig_title or "", orig_tags or [])
            return results
        except Exception as e:
            errors.append(str(e))
            continue
    raise RuntimeError(f"All Gemini API keys failed for batch: {'; '.join(errors[-3:])}")

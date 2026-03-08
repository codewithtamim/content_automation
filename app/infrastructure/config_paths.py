"""Shared path resolution for config files (cookies, etc.)."""

from pathlib import Path


def get_cookies_path(yt_cookies_path: str = "cookies.txt") -> Path:
    """
    Resolve the absolute path to the cookies file.
    Uses project root (parent of app/) so bot upload and worker use the same path.
    """
    path = Path(yt_cookies_path)
    if path.is_absolute():
        return path
    # app/infrastructure/config_paths.py -> project root is parents[2]
    project_root = Path(__file__).resolve().parents[2]
    return (project_root / yt_cookies_path).resolve()

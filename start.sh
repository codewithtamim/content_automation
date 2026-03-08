#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "=== TiktokAutomation - Termux Setup ==="

# ── Install system packages ──────────────────────────────────────────
# Native libs from Termux repos (prebuilt, fast install).
# - rust: needed to compile pydantic-core, cryptography, etc.
# - python-numpy/pillow/cryptography: avoid slow source builds
pkg update -y && pkg install -y \
    python \
    ffmpeg \
    git \
    binutils \
    rust \
    python-numpy \
    python-pillow \
    python-cryptography \
    python-pip

# ── Virtual environment ──────────────────────────────────────────────
# --system-site-packages lets pip reuse numpy/pillow/cryptography from pkg
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python -m venv --system-site-packages venv
fi
source venv/bin/activate

# ── Python dependencies ──────────────────────────────────────────────
pip install --upgrade pip

# Step 1: Core deps (pure-python wheels, installs fast)
pip install \
    "python-telegram-bot>=22.6" \
    "yt-dlp[default]>=2026.3.3" \
    "sqlalchemy>=2.0.48" \
    "ffmpeg-python>=0.2.0" \
    "pydantic-settings>=2.13.1"

# Step 2: Packages that need C/Rust compilation (Rust installed above)
pip install pycryptodomex pydantic

# Step 3: google-genai (depends on pydantic which is now installed)
pip install "google-genai>=1.66.0"

# Step 4: instagrapi without moviepy (moviepy pulls numpy source build;
# instagrapi only uses moviepy for video thumbnails which we don't need)
pip install instagrapi --no-deps
pip install PySocks requests

# ── Environment file ─────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "================================================"
    echo " .env file created from .env.example"
    echo " Please edit it with your Telegram bot token"
    echo " and other settings, then re-run this script."
    echo ""
    echo "   nano .env"
    echo "================================================"
    exit 1
fi

# ── Data directories ─────────────────────────────────────────────────
mkdir -p data

# ── Run ──────────────────────────────────────────────────────────────
echo "Starting bot..."
PYTHONPATH=. python -m app.main

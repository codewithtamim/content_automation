#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "=== TiktokAutomation - Termux Setup ==="

# ── Install system packages ──────────────────────────────────────────
pkg update -y && pkg install -y \
    python \
    ffmpeg \
    git \
    binutils \
    python-numpy \
    python-pillow \
    python-cryptography \
    python-pip

# ── Virtual environment ──────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python -m venv --system-site-packages venv
fi
source venv/bin/activate

# ── Python dependencies ──────────────────────────────────────────────
pip install --upgrade pip

# Install deps in two passes to avoid getting stuck compiling numpy etc.
#
# Pass 1: Install everything EXCEPT instagrapi (which drags in moviepy →
#          numpy/pillow source builds that hang on Termux).
pip install \
    "python-telegram-bot>=22.6" \
    "yt-dlp[default]>=2026.3.3" \
    "google-genai>=1.66.0" \
    "sqlalchemy>=2.0.48" \
    "ffmpeg-python>=0.2.0" \
    "pydantic-settings>=2.13.1"

# Pass 2: Install instagrapi's own deps that need C compilation, one by
#          one so we can control flags.
pip install pycryptodomex

# Pass 3: Install instagrapi but tell pip the heavy native packages are
#          already satisfied — don't download or rebuild them.
pip install instagrapi --no-deps

# Install the remaining pure-python deps that instagrapi actually needs
# at runtime (skip moviepy — instagrapi only uses it for video thumbnails
# which we don't need; our videos already have thumbnails).
pip install PySocks pydantic requests

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

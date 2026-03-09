#!/data/data/com.termux/files/usr/bin/bash
set -e

cd "$(dirname "$0")"

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
    nodejs \
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

# ── Set Android API level for Rust/maturin builds ────────────────────
# maturin (used by pydantic-core) needs this to compile on Termux.
export ANDROID_API_LEVEL=$(getprop ro.build.version.sdk)

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
pip install PySocks requests tzdata

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

# ── Clear app bytecode cache (force fresh run) ───────────────────────
find . -path ./venv -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ── Set webhook (when TELEGRAM_MODE=webhook) ─────────────────────────
if grep -q '^TELEGRAM_MODE=webhook' .env 2>/dev/null; then
    echo "Setting Telegram webhook..."
    PYTHONPATH=. python scripts/set_webhook.py set 2>/dev/null || true
fi

# ── Run (PYTHONDONTWRITEBYTECODE=1 prevents writing .pyc, always uses .py) ─
echo "Starting bot..."
export PYTHONDONTWRITEBYTECODE=1
PYTHONPATH=. python -m app.main

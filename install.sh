#!/data/data/com.termux/files/usr/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== TiktokAutomation - Install & Start (background) ==="

# ── Install system packages ──────────────────────────────────────────
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
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python -m venv --system-site-packages venv
fi
source venv/bin/activate

# ── Set Android API level for Rust/maturin builds ────────────────────
export ANDROID_API_LEVEL=$(getprop ro.build.version.sdk)

# ── Python dependencies ──────────────────────────────────────────────
pip install --upgrade pip
pip install \
    "python-telegram-bot>=22.6" \
    "yt-dlp[default]>=2026.3.3" \
    "sqlalchemy>=2.0.48" \
    "ffmpeg-python>=0.2.0" \
    "pydantic-settings>=2.13.1"
pip install pycryptodomex pydantic
pip install "google-genai>=1.66.0"
pip install instagrapi --no-deps
pip install PySocks requests tzdata

# ── Environment file ─────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "================================================"
    echo " .env file created from .env.example"
    echo " Please edit it with your Telegram bot token"
    echo " and other settings, then re-run install.sh"
    echo ""
    echo "   nano .env"
    echo "================================================"
    exit 1
fi

# ── Data directories ─────────────────────────────────────────────────
mkdir -p data

# ── Stop existing instance if running ───────────────────────────────
if [ -f "data/app.pid" ]; then
    OLD_PID=$(cat data/app.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing instance (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
    fi
    rm -f data/app.pid
fi

# ── Run in background ────────────────────────────────────────────────
echo "Starting bot in background..."
nohup bash -c "source venv/bin/activate && PYTHONPATH=. python -m app.main" >> data/app.log 2>&1 &
echo $! > data/app.pid
echo "Bot started (PID $(cat data/app.pid)). Logs: data/app.log"
echo "To stop: bash stop.sh"

#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "=== TiktokAutomation - Termux Setup ==="

# ── Install system packages ──────────────────────────────────────────
# numpy, pillow, and cryptography are much faster to install via pkg
# than compiling from source with pip
pkg update -y && pkg install -y \
    python \
    ffmpeg \
    git \
    python-numpy \
    python-pillow \
    python-cryptography

# ── Virtual environment ──────────────────────────────────────────────
# --system-site-packages lets the venv use numpy/pillow/cryptography
# installed via pkg (avoids slow source builds)
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python -m venv --system-site-packages venv
fi
source venv/bin/activate

# ── Python dependencies ──────────────────────────────────────────────
pip install --upgrade pip
pip install -r requirements.txt

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

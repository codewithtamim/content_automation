#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "=== TiktokAutomation - Termux Setup ==="

# ── Install system packages ──────────────────────────────────────────
# Heavy native packages (numpy, pillow, cryptography) come from Termux
# repos as prebuilt binaries — building from source takes forever on ARM.
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
# --system-site-packages lets pip see numpy/pillow/cryptography from pkg
# so it won't try to compile them again.
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python -m venv --system-site-packages venv
fi
source venv/bin/activate

# ── Python dependencies ──────────────────────────────────────────────
pip install --upgrade pip

# constraints-termux.txt pins numpy/pillow/cryptography to the exact
# versions installed by pkg, preventing pip from downloading newer
# source tarballs and getting stuck compiling C code for hours.
pip install -c constraints-termux.txt -r requirements.txt

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

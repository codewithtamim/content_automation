#!/data/data/com.termux/files/usr/bin/bash

cd "$(dirname "$0")"

if [ ! -f "data/app.pid" ]; then
    echo "No PID file found. Bot may not be running."
    exit 0
fi

PID=$(cat data/app.pid)
if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping bot (PID $PID)..."
    kill "$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null
    echo "Bot stopped."
else
    echo "Process $PID not running."
fi
rm -f data/app.pid

#!/data/data/com.termux/files/usr/bin/bash
# Clear all video jobs. Run with app STOPPED.
# Usage: ./clear_jobs.sh

cd "$(dirname "$0")"
DB="data/tiktok_automation.db"

if [ ! -f "$DB" ]; then
    echo "Database not found: $DB"
    exit 1
fi

echo "Stopping any processes using the DB..."
./stop.sh 2>/dev/null
sleep 2

echo "Clearing all jobs (timeout 60s)..."
sqlite3 "$DB" <<EOF
.timeout 60000
PRAGMA wal_checkpoint(TRUNCATE);
DELETE FROM video_jobs;
SELECT 'Deleted ' || changes() || ' rows';
EOF
echo "Done."

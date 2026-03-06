#!/bin/bash
# Deploy TiktokAutomation to remote server via rsync + SSH
# Usage: SSH_DEPLOY_PASS='your_password' ./deploy.sh
# Or run and enter password when prompted (remove expect blocks for manual mode)

set -e
HOST="root@72.60.130.115"
REMOTE_DIR="/root/TiktokAutomation"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS="${SSH_DEPLOY_PASS:-}"

if [ -z "$PASS" ]; then
  echo "Set SSH_DEPLOY_PASS env var or enter password when prompted."
fi

echo "=== Deploying to $HOST ==="

# Sync project files (exclude venv, cache, git)
if [ -n "$PASS" ]; then
  expect -c "
set timeout 120
spawn rsync -avz --progress -e \"ssh -o StrictHostKeyChecking=no\" \
  --exclude '.venv' --exclude 'venv' --exclude '__pycache__' --exclude '.git' \
  --exclude '*.pyc' --exclude '.cursor' \
  \"$LOCAL_DIR/\" \"$HOST:$REMOTE_DIR/\"
expect { \"password:\" { send \"$PASS\r\"; exp_continue }; \"yes/no\" { send \"yes\r\"; exp_continue }; eof }
"
else
  rsync -avz -e "ssh -o StrictHostKeyChecking=no" \
    --exclude '.venv' --exclude 'venv' --exclude '__pycache__' --exclude '.git' \
    --exclude '*.pyc' --exclude '.cursor' \
    "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"
fi

# Copy .env
if [ -n "$PASS" ]; then
  expect -c "
set timeout 30
spawn scp -o StrictHostKeyChecking=no \"$LOCAL_DIR/.env\" \"$HOST:$REMOTE_DIR/.env\"
expect { \"password:\" { send \"$PASS\r\"; exp_continue }; eof }
"
else
  scp -o StrictHostKeyChecking=no "$LOCAL_DIR/.env" "$HOST:$REMOTE_DIR/.env"
fi

# SSH in and run docker compose
if [ -n "$PASS" ]; then
  expect -c "
set timeout 300
spawn ssh -o StrictHostKeyChecking=no $HOST \"cd $REMOTE_DIR && docker compose down 2>/dev/null; docker compose build --no-cache app && docker compose up -d\"
expect { \"password:\" { send \"$PASS\r\"; exp_continue }; eof }
wait
"
else
  ssh -o StrictHostKeyChecking=no $HOST "cd $REMOTE_DIR && docker compose down 2>/dev/null; docker compose build --no-cache app && docker compose up -d"
fi

echo "=== Deploy complete. Check: ssh $HOST 'cd $REMOTE_DIR && docker compose logs -f app' ==="

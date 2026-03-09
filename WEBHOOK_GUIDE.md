# Telegram Webhook Setup Guide

Your bot is configured for **webhook mode** with cloudflared. Follow these steps:

## 1. Start cloudflared tunnel (Termux)

Run this **before** starting the bot. It exposes your localhost to the internet:

```bash
cloudflared tunnel --url http://localhost:8080
```



> If the cloudflared URL changes (e.g. after restart), update `WEBHOOK_PUBLIC_URL` in `.env` and re-run `install.sh` or `start.sh`.

## 2. Start the bot

`install.sh` and `start.sh` will **automatically set the webhook** when `TELEGRAM_MODE=webhook`:

```bash
# Background (install.sh)
bash install.sh

# Foreground (start.sh)
bash start.sh
```

The scripts call `scripts/set_webhook.py set` before starting, using `WEBHOOK_PUBLIC_URL` and `WEBHOOK_SECRET_TOKEN` from `.env`.

## 3. Manual webhook commands (optional)

```bash
source venv/bin/activate

# Set webhook (uses .env)
python scripts/set_webhook.py set

# Set webhook with custom URL
python scripts/set_webhook.py set https://your-new-url.trycloudflare.com/webhook

# Check current webhook
python scripts/set_webhook.py info

# Delete webhook (switch back to polling)
python scripts/set_webhook.py delete
```

## 4. Required .env variables

- `WEBHOOK_PUBLIC_URL` — your cloudflared URL + `/webhook` (e.g. `https://xxx.trycloudflare.com/webhook`)
- `WEBHOOK_SECRET_TOKEN` — random token (generate with `openssl rand -hex 32`)
- `WEBHOOK_PORT` — 8080 (must match cloudflared `--url`)

## 5. Order of operations

1. Start **cloudflared** first (in one terminal)
2. Run **install.sh** or **start.sh** (in another terminal)

Cloudflared must be running before the bot starts, or Telegram will fail to deliver updates.

#!/usr/bin/env python3
"""Helper script to set or delete the Telegram bot webhook. Reads from .env."""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

# Add project root to path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

from config import get_settings


def _get(token: str, method: str) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        headers={"User-Agent": "TiktokAutomation-set-webhook"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def set_webhook(token: str, url: str, secret_token: str | None = None) -> dict:
    params = f"url={urllib.parse.quote(url)}"
    if secret_token:
        params += f"&secret_token={urllib.parse.quote(secret_token)}"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setWebhook?{params}",
        headers={"User-Agent": "TiktokAutomation-set-webhook"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def delete_webhook(token: str) -> dict:
    return _get(token, "deleteWebhook")


def get_webhook_info(token: str) -> dict:
    return _get(token, "getWebhookInfo")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set or delete Telegram bot webhook")
    sub = parser.add_subparsers(dest="cmd", required=True)

    set_parser = sub.add_parser("set", help="Set webhook URL")
    set_parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="Webhook URL (default: WEBHOOK_PUBLIC_URL from .env)",
    )
    set_parser.add_argument(
        "--secret-token",
        default=None,
        help="Secret token (default: WEBHOOK_SECRET_TOKEN from .env)",
    )

    sub.add_parser("delete", help="Delete webhook")
    sub.add_parser("info", help="Show current webhook info")

    args = parser.parse_args()
    settings = get_settings()
    token = settings.telegram_bot_token

    if args.cmd == "set":
        url = args.url or settings.webhook_public_url
        secret = args.secret_token or settings.webhook_secret_token
        if not url:
            print("Provide URL as argument or set WEBHOOK_PUBLIC_URL in .env", file=sys.stderr)
            sys.exit(1)
        result = set_webhook(token, url, secret)
        print(result)
        if result.get("ok"):
            print("Webhook set successfully.")
    elif args.cmd == "delete":
        result = delete_webhook(token)
        print(result)
        if result.get("ok"):
            print("Webhook deleted.")
    elif args.cmd == "info":
        result = get_webhook_info(token)
        data = result.get("result", {})
        url = data.get("url") or "(none)"
        print(f"Webhook URL: {url}")
        if data.get("pending_update_count"):
            print(f"Pending updates: {data['pending_update_count']}")


if __name__ == "__main__":
    main()

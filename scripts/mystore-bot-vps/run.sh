#!/usr/bin/env bash
# MyStore Telegram Admin Bot Runner
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo "🤖 Starting MyStore Telegram Admin Bot..."
PYTHONUNBUFFERED=1 python3 -u main.py

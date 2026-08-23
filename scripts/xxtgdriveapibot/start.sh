#!/usr/bin/env bash
# TG Drive MTProto Bot Starter Script

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "🚀 Setting up TG Drive Bot Environment..."
echo "========================================"

python3 -m pip install -r requirements.txt

echo "========================================"
echo "⚡ Starting Bot Engine..."
echo "========================================"

python3 main.py

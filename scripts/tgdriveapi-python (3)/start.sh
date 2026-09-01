#!/bin/bash
# TG Drive Python MTProto Engine Startup Script for VPS & Runners

echo "=================================================="
echo "🚀 Setting up TG Drive Python MTProto Engine..."
echo "=================================================="

# Export Ngrok Authtoken environment variable
export NGROK_AUTHTOKEN="${NGROK_AUTHTOKEN:-3IBGFjZrUBgDgqY1Hn3EIU20BXL_7DSo3P6LkVk1CrgNkg95Q}"

# Setup Virtual Environment if not exists
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# Install dependencies
pip install --no-cache-dir -r requirements.txt

# Start Uvicorn Server (app.py handles permanent tunnel launch and prints public URL)
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000

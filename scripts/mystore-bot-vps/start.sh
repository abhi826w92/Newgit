#!/usr/bin/env bash
# ==============================================================================
# MyStore Telegram Admin Bot - VPS Production Startup Script
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 [MyStore VPS Deployer] Initializing bot environment..."

# 1. Check Python3 & Pip
if ! command -v python3 &> /dev/null; then
    echo "📦 Installing Python3 and tools..."
    apt-get update && apt-get install -y python3 python3-pip g++ curl || true
fi

# 2. Install / Verify Python Dependencies
if [ -f "requirements.txt" ]; then
    echo "📦 [MyStore] Installing/verifying python dependencies..."
    pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt || true
fi

# 3. Compile Native C++ Acceleration Core if g++ is available
if command -v g++ &> /dev/null && [ -f "cpp_core/engine.cpp" ]; then
    echo "⚡ [MyStore] Compiling Native C++ Acceleration Core..."
    mkdir -p ~/.mystore_native
    g++ -O3 -shared -fPIC cpp_core/engine.cpp -o ~/.mystore_native/libengine.so 2>/dev/null || true
fi

# 4. Auto-Restart Loop (Daemon protection against unexpected crashes / restarts)
echo "🤖 [MyStore] Starting bot with auto-restart watchdog loop..."
while true; do
    echo "🟢 [$(date)] Launching MyStore Admin Bot..."
    PYTHONUNBUFFERED=1 python3 -u main.py || true
    echo "⚠️ [$(date)] Bot stopped or crashed. Restarting automatically in 3 seconds..."
    sleep 3
done

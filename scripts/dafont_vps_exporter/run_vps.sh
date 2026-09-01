#!/usr/bin/env bash
set -e

CHAT_ID="${1:--1003887776900}"
THREADS="${2:-64}"
LIMIT="${3:-0}" # 0 means ALL fonts

echo "=========================================================="
echo "    🚀 DAFONT ULTRA-FAST C++ VPS EXPORTER & BOT UPLOADER  "
echo "=========================================================="
echo "[*] Target Telegram Chat ID: $CHAT_ID"
echo "[*] Worker Threads: $THREADS"
echo "[*] Download Limit: $( [ "$LIMIT" -eq 0 ] && echo "ALL FONTS" || echo "$LIMIT" )"
echo "=========================================================="

# 1. Install System Dependencies if running as root
if [ "$(id -u)" -eq 0 ]; then
    echo "[*] Installing build dependencies..."
    apt-get update -qq && apt-get install -y -qq \
        build-essential \
        g++ \
        libcurl4-openssl-dev \
        libsqlite3-dev \
        libzip-dev \
        zlib1g-dev \
        python3 \
        zip \
        curl > /dev/null 2>&1
    echo "[✓] Dependencies installed."
fi

# 2. Compile Native C++ High-Performance Engine
echo "[*] Compiling C++ High-Performance Engine (-O3 optimized)..."
g++ -O3 -std=c++17 src/fast_font_downloader.cpp -o fast_font_downloader -lcurl -lsqlite3 -lzip -lz -lpthread
chmod +x fast_font_downloader
echo "[✓] Engine compiled successfully: ./fast_font_downloader"

# 3. Crawl All DaFont Alphabet Sections (A-Z, #)
echo ""
echo "[*] Step 1/4: Indexing DaFont master alphabet catalog..."
./fast_font_downloader --crawl

# 4. Download Fonts in Parallel
echo ""
echo "[*] Step 2/4: Mass Parallel Downloading and In-Memory Extraction..."
if [ "$LIMIT" -gt 0 ]; then
    ./fast_font_downloader --download --limit "$LIMIT" --threads "$THREADS"
else
    ./fast_font_downloader --download --threads "$THREADS"
fi

# 5. Bundle and Upload to Telegram Bot
echo ""
echo "[*] Step 3/4: Packaging and Uploading Font Archives to Telegram Bot..."
python3 bot_uploader.py "$CHAT_ID" "dafont_archive"

# 6. Automatic Data Wipe / Self-Cleanup
echo ""
echo "[*] Step 4/4: Wiping downloaded data and freeing VPS storage..."
python3 auto_cleanup.py

echo ""
echo "=========================================================="
echo "    🎉 ALL TASKS COMPLETE! ARCHIVES SENT & VPS CLEANED    "
echo "=========================================================="

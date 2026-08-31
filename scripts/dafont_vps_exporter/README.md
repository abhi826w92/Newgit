# 🚀 DaFont Ultra-Fast C++ VPS Exporter & Telegram Bot Uploader

This standalone package is designed to run on any Linux VPS (Ubuntu/Debian) to crawl and export fonts from DaFont at 100x speed using native C++, bundle them into multi-part zip archives (< 45 MB chunks), upload them to your Telegram bot, and automatically clean up all server storage when done.

---

## 📦 Package Contents

* **`src/fast_font_downloader.cpp`**: Multi-threaded C++ engine (`libcurl` + `libzip` + `sqlite3`) with in-memory stream decompression.
* **`bot_uploader.py`**: Telegram Bot chunked multi-part uploader.
* **`auto_cleanup.py`**: Automatic post-upload data wipe script.
* **`run_vps.sh`**: One-command master runner.

---

## ⚙️ Telegram Bot Setup

* **Bot Token**: `8486999738:AAEXkcxrILtF2AH2YfPesT1vwUAhPKiRVYs` (`@Test02639bot`)
* **To send to a Channel**: Add `@Test02639bot` as an **Administrator** with "Post Messages" permission in your channel.
* **To send to your Private Chat**: Send `/start` to `@Test02639bot` and use your numeric Chat ID.

---

## 🛠️ Quick Start on Your VPS

### 1. Upload & Unzip on VPS
```bash
unzip dafont_vps_exporter.zip -d dafont_vps_exporter
cd dafont_vps_exporter
```

### 2. Run with One Command
```bash
# Syntax: bash run_vps.sh [CHAT_ID] [THREADS] [LIMIT]
# Default: sends to your channel with 64 threads for ALL fonts
bash run_vps.sh -1003887776900 64 0
```

### 3. Custom Run Examples
```bash
# Export top 10,000 fonts with 128 threads:
bash run_vps.sh -1003887776900 128 10000

# Export to your personal Telegram User ID:
bash run_vps.sh 123456789 64 0
```

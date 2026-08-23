# 🚀 TG Drive Telegram Bot (Python)

A full-featured Telegram Cloud Storage Bot powered by the **TG Drive REST API** and **python-telegram-bot (v22)**.

## ✨ Features
- 🔑 **Multi-User Authentication**: Each user enters their personal API key from `tgdriveo.pages.dev/#/developer`.
- 📁 **File Explorer**: Browse files with page navigation, view file metadata, size, upload date, and direct download links.
- 📤 **Instant Uploads**: Send any Photo, Video, Audio, Document, Voice, or APK directly to the bot to upload to TG Drive.
- 🔍 **Fast Search**: Search files & folders by query (`/search <query>`).
- 📂 **Virtual Folders**: Create and manage folders.
- ⭐ **Favorites / Starred**: Mark files as favorite for quick access.
- 📊 **Storage Statistics**: View total files, folders, and storage breakdown.
- 🗑️ **Trash & Recycle Bin**: Restore files or empty bin.

## 🚀 How to Run

1. **Install Requirements:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure `.env`:**
   Check `.env` to verify your `BOT_TOKEN` and `API_BASE_URL`.

3. **Start the Bot:**
   ```bash
   python3 main.py
   ```

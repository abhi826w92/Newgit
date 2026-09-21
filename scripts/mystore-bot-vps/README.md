# 🤖 MyStore Telegram Admin Bot

Full-featured Python Telegram Admin Bot for **MyStore** PWA.

## 🌟 Key Capabilities
- **Only 1 Command UI (`/start`)**: 100% interactive inline buttons & step-by-step wizard.
- **Firebase Realtime Database Sync**: Instant live update on the website whenever an app is added, edited, or deleted.
- **GitHub Releases API Uploader**: Send an `.apk` file directly in Telegram chat to auto-upload to GitHub Releases and generate direct download links.
- **Two-Tier Security**: Telegram Admin ID whitelist + Shared Security Token in `.env`.
- **Live Analytics**: Real-time download clicks and visitor counts from Firebase.
- **1-Tap Cloud Backup**: Generates and sends full `apps.json` database backup to Telegram.

## 🚀 How to Run the Bot

### 1. In Terminal (Interactive)
```bash
cd /storage/emulated/0/Zworkspace/appstore/bot
./run.sh
# or
python3 main.py
```

### 2. In Background (Daemon)
```bash
nohup python3 /storage/emulated/0/Zworkspace/appstore/bot/main.py > /storage/emulated/0/Zworkspace/appstore/bot/bot.log 2>&1 &
```

## ⚙️ Configuration (`bot/.env`)
- `TELEGRAM_BOT_TOKEN`: Bot token from @BotFather
- `ADMIN_IDS`: Comma-separated authorized Telegram user IDs
- `GITHUB_TOKEN`: GitHub Personal Access Token (PAT) with `repo` scope
- `GITHUB_OWNER`: GitHub username (`myreleasesrpo0272`)
- `GITHUB_REPO`: Target repository for releases (`appstore`)
- `FIREBASE_DATABASE_URL`: Firebase Realtime Database endpoint
- `ADMIN_SECURITY_TOKEN`: Shared secret token

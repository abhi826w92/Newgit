#!/usr/bin/env bash
# ==============================================================================
# MyStore Telegram Admin Bot - 1-Click VPS Auto-Installer & Systemd Setup
# Run: bash install_vps.sh
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/root/mystore-bot"

echo "=================================================================="
echo "🚀 MyStore Telegram Admin Bot - VPS Production Setup"
echo "=================================================================="

# 1. Update OS packages and install Python & C++ compiler
echo "📦 Updating system packages and installing dependencies..."
apt-get update -y
apt-get install -y python3 python3-pip g++ curl cron zip unzip

# 2. Copy files to standard production directory
if [ "$SCRIPT_DIR" != "$TARGET_DIR" ]; then
    echo "📁 Deploying bot files to $TARGET_DIR..."
    mkdir -p "$TARGET_DIR"
    cp -r "$SCRIPT_DIR"/* "$TARGET_DIR"/
    cp "$SCRIPT_DIR"/.env* "$TARGET_DIR"/ 2>/dev/null || true
    cd "$TARGET_DIR"
fi

# 3. Install Python requirements
echo "📦 Installing Python requirements..."
pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

# 4. Compile C++ Core Engine
echo "⚡ Compiling C++ Core Acceleration Engine..."
mkdir -p /root/.mystore_native
g++ -O3 -shared -fPIC cpp_core/engine.cpp -o /root/.mystore_native/libengine.so || true

# 5. Install Systemd Service (Starts on Boot + Auto-Restart on Crash)
echo "⚙️ Configuring Systemd Background Service..."
chmod +x "$TARGET_DIR"/start.sh
chmod +x "$TARGET_DIR"/bin/cloudflared 2>/dev/null || true
sed -i "s|/root/mystore-bot|$TARGET_DIR|g" mystore-bot.service
cp mystore-bot.service /etc/systemd/system/mystore-bot.service

systemctl daemon-reload
systemctl enable mystore-bot.service
systemctl restart mystore-bot.service

# 6. Optional: Setup Cronjob to periodic restart (e.g. every 1 hour / 30 mins)
# To clean memory & keep bot 100% fresh forever
(crontab -l 2>/dev/null | grep -v "mystore-bot" ; echo "0 * * * * systemctl restart mystore-bot.service >/dev/null 2>&1") | crontab -

echo "=================================================================="
echo "🎉 DEPLOYMENT COMPLETE & BOT IS NOW RUNNING 24/7!"
echo "=================================================================="
echo "• Status Check : systemctl status mystore-bot"
echo "• View Logs    : journalctl -u mystore-bot -f"
echo "• Restart Bot  : systemctl restart mystore-bot"
echo "• Stop Bot     : systemctl stop mystore-bot"
echo "• Auto-Start   : ✅ Enabled on Boot (survives any VPS restart)"
echo "• Cron Restart : ✅ Enabled (every 1 hour auto-refresh)"
echo "=================================================================="

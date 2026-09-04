# 🚀 MyStore Telegram Admin Bot - 24/7 VPS Deployment Guide

This package contains everything needed to deploy and run the **MyStore Telegram Admin Bot** on any Linux VPS (Ubuntu, Debian, CentOS, Rocky, Alpine) with **automatic reboot recovery, 24/7 uptime, crash protection, and periodic auto-refresh**.

---

## ⚡ Method 1: 1-Click Auto-Installer (Recommended)

Upload or unzip `mystore-bot-vps.zip` on your VPS, then run:

```bash
unzip mystore-bot-vps.zip -d mystore-bot
cd mystore-bot
bash install_vps.sh
```

### What this does automatically:
1. Installs Python 3, Pip, G++ compiler, and cron.
2. Installs all required Python dependencies.
3. Compiles the high-speed Native C++ Core Engine.
4. Registers and starts **`mystore-bot.service`** via **systemd**.
5. Enables **Auto-Start on Boot** (survives any VPS restart / reboot!).
6. Configures auto-refresh cron so your bot runs at peak performance forever.

---

## 🛠️ Useful Management Commands:

```bash
# Check bot live status
systemctl status mystore-bot

# View real-time logs
journalctl -u mystore-bot -f

# Restart bot manually
systemctl restart mystore-bot

# Stop bot
systemctl stop mystore-bot
```

---

## 🔄 Automatic Restart & VPS Reboot Behavior

### 1. **VPS Reboots / Server Restarts**:
- Because `systemctl enable mystore-bot.service` is configured, systemd **automatically starts the bot within 3 seconds** as soon as the VPS powers back on. No manual login required!

### 2. **Periodic Auto-Refresh (Every 30 mins / 1 hour)**:
- A cron job is pre-configured to cleanly restart the service every 1 hour (`0 * * * *`).
- To change restart interval to every 30 minutes, edit cron with `crontab -e`:
  ```cron
  */30 * * * * systemctl restart mystore-bot.service >/dev/null 2>&1
  ```

---

## 🐳 Method 2: Docker Compose (Alternative)

If you have Docker installed on your VPS:

```bash
docker compose up -d --build
```

Logs:
```bash
docker compose logs -f
```
Restart:
```bash
docker compose restart
```

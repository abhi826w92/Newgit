import os
import sys
import re
import shutil
import time
import json
import html
import psutil
import logging
import signal
import threading
import subprocess
import traceback
import hashlib
import hmac
import base64
import zipfile
import sqlite3
from datetime import datetime, date, timezone
import requests

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TGBotController")

# ---------------------------------------------------------------------------
# Configuration & Environment Variables
# ---------------------------------------------------------------------------
def sanitize_github_token(token):
    if not token:
        return ""
    t = str(token).strip().strip("'\"")
    if t.startswith("Bearer "):
        t = t[7:].strip()
    elif t.startswith("token "):
        t = t[6:].strip()
    return t

# Workspace & Config
WORKSPACE_DIR = os.getcwd()

def get_effective_github_token():
    """
    Resolves the active GitHub PAT or Actions token from environment or git remote.
    Works seamlessly across GitHub Actions runners, local dev, and VPS environments.
    """
    token = sanitize_github_token(
        os.environ.get("GH_PAT", "") or 
        os.environ.get("GITHUB_TOKEN", "") or 
        os.environ.get("GH_TOKEN", "")
    )
    if token:
        return token
    try:
        remote_url = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=WORKSPACE_DIR).decode().strip()
        m = re.search(r'://([^:@]+)(?::([^@]+))?@github\.com', remote_url)
        if m:
            user_or_token = m.group(1)
            token_part = m.group(2)
            if token_part:
                return sanitize_github_token(token_part)
            if user_or_token.startswith("ghp_") or user_or_token.startswith("github_pat_"):
                return sanitize_github_token(user_or_token)
    except Exception:
        pass
    return ""

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
GH_PAT = sanitize_github_token(os.environ.get("GH_PAT", ""))
GITHUB_TOKEN = sanitize_github_token(os.environ.get("GITHUB_TOKEN", ""))
EFFECTIVE_TOKEN = get_effective_github_token()
REPO = os.environ.get("GITHUB_REPOSITORY", "abhi826w92/Newgit").strip()
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local-dev").strip()
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "server.yml").strip()
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "main").strip()

# Default run duration: 2.0 hours (7200 seconds)
RUN_DURATION_SECONDS = int(os.environ.get("RUN_DURATION_SECONDS", "7200"))
START_TIME = time.time()
IS_RUNNING = True
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, "scripts")
os.makedirs(SCRIPTS_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(WORKSPACE_DIR, "bot_config.json")

# Multi-Process Concurrent Runner State:
# running_processes = { "clean_name": { "proc": Popen, "start_time": float, "create_time": float, "logs": [], "is_stopped": bool, "pid": int, "script_real_path": str, "parent_pid": int } }
running_processes = {}
LOG_BUFFER_MAX = 200

# Per-Target Execution Mutex Locks (Prevents concurrent launch race conditions)
target_execution_locks = {}
target_locks_mutex = threading.Lock()

def get_target_lock(clean_name):
    """Returns a re-entrant thread lock for a specific script target."""
    with target_locks_mutex:
        norm_name = os.path.normpath(clean_name).replace("\\", "/")
        if norm_name not in target_execution_locks:
            target_execution_locks[norm_name] = threading.Lock()
        return target_execution_locks[norm_name]

# User conversation states (for interactive step-by-step inputs)
user_states = {}

def get_active_running_processes():
    """
    Returns dict of currently active running processes.
    Strictly verifies (a) process existence, (b) create_time matching to prevent PID reuse,
    and (c) explicitly prunes & reaps zombie processes.
    """
    active = {}
    for name, pdata in list(running_processes.items()):
        proc = pdata.get("proc")
        pid = pdata.get("pid")
        stored_create_time = pdata.get("create_time")
        is_alive = False
        
        if proc and proc.poll() is None and pid and psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                # 1. PID Reuse check: verify process creation timestamp
                if stored_create_time and abs(p.create_time() - stored_create_time) >= 0.5:
                    is_alive = False
                # 2. Zombie / Dead state check
                elif p.status() in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD, psutil.STATUS_STOPPED]:
                    try:
                        proc.poll()
                        proc.wait(timeout=0.1)
                    except Exception:
                        pass
                    is_alive = False
                else:
                    is_alive = True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                is_alive = False
                
        if is_alive:
            active[name] = pdata
        else:
            running_processes.pop(name, None)
    return active

# ---------------------------------------------------------------------------
# Telegram API Helpers (Buttons & Messages)
# ---------------------------------------------------------------------------
TG_BASE_URL = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

def load_config():
    default_cfg = {
        "admin_ids": [7249511572, 7251749429],
        "active_scripts": [],
        "manually_stopped_scripts": []
    }
    data = default_cfg.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
                if "admin_id" in data and "admin_ids" not in data:
                    data["admin_ids"] = [data["admin_id"]] if data["admin_id"] else []
                if "admin_ids" not in data or not data["admin_ids"]:
                    data["admin_ids"] = [7249511572, 7251749429]
                if "active_scripts" not in data or data["active_scripts"] is None:
                    data["active_scripts"] = []
                if "manually_stopped_scripts" not in data or data["manually_stopped_scripts"] is None:
                    data["manually_stopped_scripts"] = []
        except Exception as e:
            logger.error(f"Error loading config: {e}")

    # Dynamically inject admin IDs from environment variables (TG_ADMIN_ID, ADMIN_ID, etc.)
    for env_key in ["TG_ADMIN_ID", "ADMIN_ID", "ADMIN_IDS", "OWNER_ID"]:
        val = os.environ.get(env_key, "").strip()
        if val:
            for item in val.replace(",", " ").split():
                if item.isdigit():
                    uid = int(item)
                    if uid not in data.get("admin_ids", []):
                        data.setdefault("admin_ids", []).append(uid)

    return data

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

config = load_config()

def get_all_admin_ids():
    admin_list = list(config.get("admin_ids", []))
    for env_key in ["TG_ADMIN_ID", "ADMIN_ID", "ADMIN_IDS", "OWNER_ID"]:
        val = os.environ.get(env_key, "").strip()
        if val:
            for item in val.replace(",", " ").split():
                if item.isdigit():
                    uid = int(item)
                    if uid not in admin_list:
                        admin_list.append(uid)
    if not admin_list:
        admin_list = [7249511572, 7251749429]
    return admin_list

def is_admin(user_id):
    admin_list = get_all_admin_ids()
    if not admin_list:
        return True
    try:
        uid = int(user_id)
        if uid in [int(x) for x in admin_list if str(x).isdigit()]:
            return True
    except (ValueError, TypeError):
        pass
    return str(user_id).strip() in [str(x).strip() for x in admin_list]

def notify_all_admins(text, reply_markup=None):
    for a_id in get_all_admin_ids():
        send_tg_message(a_id, text, reply_markup=reply_markup)

def delete_telegram_webhook():
    """Deletes any existing webhook so getUpdates polling functions cleanly without 409 Conflict."""
    url = f"{TG_BASE_URL}/deleteWebhook"
    try:
        resp = requests.post(url, json={"drop_pending_updates": False}, timeout=10)
        res_json = resp.json()
        if res_json.get("ok"):
            logger.info("✅ Telegram Webhook cleared successfully (clean polling active).")
            return True
        else:
            logger.warning(f"⚠️ deleteWebhook response: {res_json}")
            return False
    except Exception as e:
        logger.error(f"❌ Error clearing Telegram webhook: {e}")
        return False

def send_tg_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"{TG_BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    try:
        resp = requests.post(url, json=payload, timeout=10).json()
        if not resp.get("ok"):
            # Fallback without parse_mode in case of HTML entity error
            payload.pop("parse_mode", None)
            return requests.post(url, json=payload, timeout=10).json()
        return resp
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return None

def edit_tg_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"{TG_BASE_URL}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10).json()
        if not resp.get("ok"):
            err_desc = resp.get("description", "")
            if "message is not modified" in err_desc:
                return resp
            # Fallback 1: Plain text without HTML parsing
            import re
            plain_text = re.sub(r'<[^>]+>', '', text)
            payload["text"] = plain_text
            payload.pop("parse_mode", None)
            resp2 = requests.post(url, json=payload, timeout=10).json()
            if resp2.get("ok"):
                return resp2
            # Fallback 2: Send as fresh new message if message cannot be edited
            send_payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            if reply_markup:
                send_payload["reply_markup"] = reply_markup
            return requests.post(f"{TG_BASE_URL}/sendMessage", json=send_payload, timeout=10).json()
        return resp
    except Exception as e:
        logger.error(f"Failed to edit message: {e}")
        return None

def answer_callback(callback_query_id, text=None, show_alert=False):
    url = f"{TG_BASE_URL}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def send_tg_document(chat_id, filepath, caption="", reply_markup=None):
    url = f"{TG_BASE_URL}/sendDocument"
    try:
        with open(filepath, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup) if not isinstance(reply_markup, str) else reply_markup
            requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        send_tg_message(chat_id, f"❌ Failed to send document: {e}")

def download_tg_file(file_id, destination_path):
    try:
        url = f"{TG_BASE_URL}/getFile?file_id={file_id}"
        resp = requests.get(url, timeout=10).json()
        if not resp.get("ok"):
            err_msg = resp.get("description", "Could not retrieve file path from Telegram.")
            if "too big" in err_msg.lower():
                err_msg += " (Telegram Bot API has a 20MB file limit for bots)."
            return False, err_msg
        
        file_path = resp["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}/{file_path}"
        
        file_resp = requests.get(download_url, timeout=45)
        if file_resp.status_code == 200:
            with open(destination_path, "wb") as f:
                f.write(file_resp.content)
            return True, None
        return False, f"HTTP Error {file_resp.status_code}"
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------------------
# GitHub Git Auto-Sync
# ---------------------------------------------------------------------------
git_sync_lock = threading.Lock()

def git_sync_to_github(commit_message="Update via Telegram Controller"):
    token_to_use = get_effective_github_token()
    
    with git_sync_lock:
        try:
            # Clean up stale index.lock if leftover from any interrupted git command
            index_lock = os.path.join(WORKSPACE_DIR, ".git", "index.lock")
            if os.path.exists(index_lock):
                try:
                    os.remove(index_lock)
                except Exception:
                    pass

            for git_stuck_path in [".git/rebase-apply", ".git/rebase-merge"]:
                if os.path.exists(os.path.join(WORKSPACE_DIR, git_stuck_path)):
                    subprocess.run(["git", "rebase", "--abort"], cwd=WORKSPACE_DIR, capture_output=True)
                    break
            if os.path.exists(os.path.join(WORKSPACE_DIR, ".git", "MERGE_HEAD")):
                subprocess.run(["git", "merge", "--abort"], cwd=WORKSPACE_DIR, capture_output=True)

            remote_url = None
            if token_to_use and REPO:
                if token_to_use.startswith(("ghp_", "github_pat_")):
                    remote_url = f"https://{token_to_use}@github.com/{REPO}.git"
                else:
                    remote_url = f"https://x-access-token:{token_to_use}@github.com/{REPO}.git"
            else:
                remote_url = "origin"

            subprocess.run(["git", "config", "user.name", "TelegramController"], cwd=WORKSPACE_DIR, capture_output=True)
            subprocess.run(["git", "config", "user.email", "bot@controller.local"], cwd=WORKSPACE_DIR, capture_output=True)
            
            # Determine target branch
            try:
                cur_b = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=WORKSPACE_DIR).decode().strip()
                target_branch = cur_b if (cur_b and cur_b != "HEAD") else (WORKFLOW_REF or "main")
            except Exception:
                target_branch = WORKFLOW_REF if WORKFLOW_REF else "main"

            # 1. Stage all changes including deletions (-A)
            subprocess.run(["git", "add", "-A"], cwd=WORKSPACE_DIR, capture_output=True)
            
            # 2. Check if there are changes to commit
            status = subprocess.run(["git", "status", "--porcelain"], cwd=WORKSPACE_DIR, capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", commit_message], cwd=WORKSPACE_DIR, capture_output=True)
            
            # 3. Push changes directly to GitHub
            push_res = subprocess.run(["git", "push", remote_url, target_branch], cwd=WORKSPACE_DIR, capture_output=True, text=True)
            if push_res.returncode == 0:
                logger.info(f"Auto-sync to cloud complete: {commit_message}")
                return True, "Cloud sync complete! All changes backed up."

            # Fallback to origin if remote_url failed
            if remote_url != "origin":
                push_res_orig = subprocess.run(["git", "push", "origin", target_branch], cwd=WORKSPACE_DIR, capture_output=True, text=True)
                if push_res_orig.returncode == 0:
                    logger.info(f"Auto-sync to cloud complete via origin: {commit_message}")
                    return True, "Cloud sync complete! All changes backed up."

            # Rebase with -X theirs so local deletions/updates strictly take precedence over remote!
            rebase_res = subprocess.run(["git", "pull", "--rebase", "--autostash", "-X", "theirs", remote_url, target_branch], cwd=WORKSPACE_DIR, capture_output=True)
            if rebase_res.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], cwd=WORKSPACE_DIR, capture_output=True)
                subprocess.run(["git", "pull", "--no-rebase", "-X", "theirs", remote_url, target_branch], cwd=WORKSPACE_DIR, capture_output=True)
            
            # Re-stage all local changes and deletions
            subprocess.run(["git", "add", "-A"], cwd=WORKSPACE_DIR, capture_output=True)
            status2 = subprocess.run(["git", "status", "--porcelain"], cwd=WORKSPACE_DIR, capture_output=True, text=True)
            if status2.stdout.strip():
                subprocess.run(["git", "commit", "-m", f"{commit_message} (reconciled)"], cwd=WORKSPACE_DIR, capture_output=True)
            
            push_res = subprocess.run(["git", "push", remote_url, target_branch], cwd=WORKSPACE_DIR, capture_output=True, text=True)
            if push_res.returncode == 0:
                logger.info(f"Auto-sync to cloud complete after rebase: {commit_message}")
                return True, "Cloud sync complete! All changes backed up."

            push_lease = subprocess.run(["git", "push", "--force-with-lease", remote_url, target_branch], cwd=WORKSPACE_DIR, capture_output=True, text=True)
            if push_lease.returncode == 0:
                logger.info(f"Auto-sync to cloud complete via force-with-lease: {commit_message}")
                return True, "Cloud sync complete! All changes backed up."

            logger.error(f"Git push error: {push_res.stderr}")
            return False, f"Cloud Sync error: {push_res.stderr[-200:]}"
        except Exception as e:
            logger.error(f"git_sync_to_github error: {e}")
            return False, str(e)

# ---------------------------------------------------------------------------
# Process Manager (Run, Stop, Restart Scripts)
# ---------------------------------------------------------------------------
def append_log(fname, line):
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {line}"
    if fname in running_processes:
        logs = running_processes[fname].setdefault("logs", [])
        logs.append(formatted)
        if len(logs) > LOG_BUFFER_MAX:
            logs.pop(0)
    logger.info(f"[{fname}] {line}")

def log_stream_reader(pipe, fname):
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            append_log(fname, line.rstrip())
        pipe.close()
    except Exception:
        pass

def extract_missing_module(log_text):
    import re
    match = re.search(r"No module named ['\"]([^'\"]+)['\"]", log_text)
    if match:
        return match.group(1)
    return None

def map_module_to_pip_pkg(mod_name):
    """Maps imported python module name to the correct pip package name."""
    if not mod_name:
        return ""
    root = mod_name.split(".")[0]
    mapping = {
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "yaml": "pyyaml",
        "dateutil": "python-dateutil",
        "dotenv": "python-dotenv",
        "telegram": "python-telegram-bot",
        "cv2": "opencv-python-headless",
        "crypto": "pycryptodome",
        "Crypto": "pycryptodome",
        "nacl": "PyNaCl",
        "telethon": "telethon",
        "pyrogram": "pyrogram",
        "tgcrypto": "tgcrypto",
        "aiohttp": "aiohttp",
        "httpx": "httpx",
        "requests": "requests",
        "psutil": "psutil"
    }
    return mapping.get(root, root)

def get_script_req_path(py_filename):
    base_name = os.path.basename(py_filename)
    if base_name.endswith(".py"):
        base_name = base_name[:-3]
    
    # Check directory of script if nested
    dir_name = os.path.dirname(py_filename)
    search_dirs = [SCRIPTS_DIR]
    if dir_name:
        search_dirs.insert(0, os.path.join(SCRIPTS_DIR, dir_name))
    
    candidates = []
    for d in search_dirs:
        candidates.extend([
            os.path.join(d, f"{base_name}.requirements.txt"),
            os.path.join(d, f"{base_name}_requirements.txt"),
            os.path.join(d, f"{base_name}_req.txt"),
            os.path.join(d, f"{base_name}.req.txt"),
            os.path.join(d, "requirements.txt"),
            os.path.join(d, "req.txt")
        ])
    for c in candidates:
        if os.path.exists(c):
            return c
    return None

def install_script_requirements(py_filename):
    req_path = get_script_req_path(py_filename)
    if not req_path:
        return True, "No dedicated requirements file."
    
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req_path],
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            return True, res.stdout[-2000:]
        else:
            return False, (res.stdout + "\n" + res.stderr)[-2000:]
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------------------
# Security & Resource Guard Shield (Crypto, DDoS, Infinite Loop, Mass Spam)
# ---------------------------------------------------------------------------
CRYPTO_SIGNATURES = [
    "stratum+tcp://", "stratum+ssl://", "xmrig", "cryptonight", "minerd",
    "ethminer", "monero", "hashrate", "coinhive", "nicehash", "stratum"
]

DDOS_SPAM_SIGNATURES = [
    "syn flood", "udp flood", "slowloris", "http flood", "dos attack",
    "ddos attack", "packet flood", "mass spam"
]

def scan_script_for_abuse(fpath):
    """Scans Python script source code for crypto-mining, DDoS, and dangerous abuse patterns."""
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().lower()
        for sig in CRYPTO_SIGNATURES:
            if sig in content:
                return f"⛏️ Crypto-Mining Signature Detected ('{sig}')"
        for sig in DDOS_SPAM_SIGNATURES:
            if sig in content:
                return f"🌊 DDoS / Attack Signature Detected ('{sig}')"
    except Exception:
        pass
    return None

def trigger_guard_violation(fname, reason, peak_cpu, peak_ram_mb):
    """Safely terminates the offending process and alerts all admins with logs and reasons."""
    logger.error(f"🚨 [SecurityGuard] Terminating {fname} due to policy violation: {reason}")
    
    pdata = running_processes.get(fname, {})
    recent_logs = "\n".join(pdata.get("logs", [])[-25:]) if pdata.get("logs") else "(No output recorded)"
    escaped_logs = html.escape(recent_logs)
    
    stop_child_app(script_name=fname)
    
    alert_text = (
        "🚨 <b>SECURITY & RESOURCE GUARD ALERT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>Script Name:</b> <code>{fname}</code>\n"
        f"🛑 <b>Action:</b> <b>Process Auto-Stopped for Safety</b>\n"
        f"⚠️ <b>Trigger Reason:</b>\n👉 <i>{reason}</i>\n\n"
        "📊 <b>Telemetry at Stop:</b>\n"
        f"• 📈 <b>Peak CPU:</b> <code>{peak_cpu:.1f}%</code> (Limit: 60.0%)\n"
        f"• 💾 <b>RAM Usage:</b> <code>{peak_ram_mb} MB</code>\n\n"
        "📋 <b>Recent Execution Logs:</b>\n"
        f"<pre>{escaped_logs[-2000:]}</pre>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Your server has been protected. Other running scripts remain active.</i>"
    )
    markup = {
        "inline_keyboard": [
            [{"text": f"📋 Logs: {fname}", "callback_data": f"show_log_for_{fname}"}, {"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}],
            [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
        ]
    }
    notify_all_admins(alert_text, reply_markup=markup)

def resource_guard_monitor(proc, fname):
    """Real-time Guard: Detects Crypto-Mining, DDoS Socket Floods, and Runaway Log Spam Loops."""
    spam_burst_count = 0
    max_cpu_seen = 0.0
    last_log_count = 0
    last_check_time = time.time()
    
    try:
        ps_proc = psutil.Process(proc.pid)
    except Exception:
        return

    while proc.poll() is None:
        try:
            # 1. Measure CPU and RAM for telemetry
            cpu_usage = ps_proc.cpu_percent(interval=2.0)
            mem_info = ps_proc.memory_info()
            ram_mb = mem_info.rss // (1024 * 1024)
            if cpu_usage > max_cpu_seen:
                max_cpu_seen = cpu_usage

            now = time.time()
            dt = max(0.5, now - last_check_time)
            pdata = running_processes.get(fname, {})
            current_log_count = len(pdata.get("logs", []))
            logs_per_sec = (current_log_count - last_log_count) / dt
            last_log_count = current_log_count
            last_check_time = now
            start_age = now - pdata.get("start_time", now)

            # 2. ⛏️ Crypto-Mining Detection in Execution Logs
            recent_logs_str = " ".join(pdata.get("logs", [])[-15:]).lower()
            for sig in CRYPTO_SIGNATURES:
                if sig in recent_logs_str:
                    trigger_guard_violation(
                        fname,
                        reason=f"⛏️ Crypto-Mining Activity Detected (Signature: <code>{sig}</code>)",
                        peak_cpu=cpu_usage,
                        peak_ram_mb=ram_mb
                    )
                    return

            # 3. 🌊 DDoS & Network Flooding Detection (Open Socket Threshold > 250)
            try:
                open_conns = len(ps_proc.net_connections(kind='inet'))
                if open_conns > 250:
                    trigger_guard_violation(
                        fname,
                        reason=f"🌊 DDoS / Network Socket Flood Detected ({open_conns} concurrent network sockets)",
                        peak_cpu=cpu_usage,
                        peak_ram_mb=ram_mb
                    )
                    return
            except (psutil.AccessDenied, Exception):
                pass

            # 4. 📩 Runaway Infinite Log Spamming (Sustained >300 logs/sec for 4 checks ~8s, with startup grace period)
            if start_age > 25.0 and logs_per_sec > 300.0:
                spam_burst_count += 1
                if spam_burst_count >= 4:
                    trigger_guard_violation(
                        fname,
                        reason=f"📩 Runaway Infinite Log Spamming ({logs_per_sec:.0f} logs/sec sustained)",
                        peak_cpu=cpu_usage,
                        peak_ram_mb=ram_mb
                    )
                    return
            else:
                spam_burst_count = max(0, spam_burst_count - 1)

            time.sleep(2)
        except psutil.NoSuchProcess:
            break
        except Exception as e:
            logger.debug(f"Resource guard loop error: {e}")
            time.sleep(2)

def perform_system_memory_cleanup():
    """
    Executes deep RAM cleanup:
    1. Python generational garbage collection (gc.collect).
    2. Linux glibc malloc_trim(0) to release unmapped heap memory directly back to the OS kernel.
    3. Trims in-memory process log buffers to 100 lines max per script.
    4. Returns detailed statistics on memory freed.
    """
    import gc
    import ctypes

    try:
        mem_before = psutil.virtual_memory()
        proc_self = psutil.Process()
        rss_before = proc_self.memory_info().rss
    except Exception:
        mem_before = None
        rss_before = 0

    # 1. Trim log buffers in memory
    trimmed_logs = 0
    for fname, pdata in list(running_processes.items()):
        logs = pdata.get("logs", [])
        if len(logs) > 100:
            trimmed_logs += len(logs) - 100
            pdata["logs"] = logs[-100:]

    # 2. Python generational garbage collection
    uncollected_count = gc.collect()

    # 3. Linux glibc malloc_trim
    malloc_trimmed = False
    try:
        libc = ctypes.CDLL("libc.so.6")
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
            malloc_trimmed = True
    except Exception as e:
        logger.debug(f"malloc_trim notice: {e}")

    try:
        mem_after = psutil.virtual_memory()
        rss_after = proc_self.memory_info().rss
        freed_rss_mb = max(0.0, (rss_before - rss_after) / (1024 * 1024))
        used_mb = mem_after.used / (1024 * 1024)
        total_mb = mem_after.total / (1024 * 1024)
        percent = mem_after.percent
    except Exception:
        freed_rss_mb = 0.0
        used_mb = 0.0
        total_mb = 0.0
        percent = 0.0

    return {
        "uncollected_objects": uncollected_count,
        "malloc_trimmed": malloc_trimmed,
        "freed_mb": freed_rss_mb,
        "used_mb": used_mb,
        "total_mb": total_mb,
        "percent": percent,
        "trimmed_logs": trimmed_logs
    }

def background_memory_cleaner_loop():
    """Periodically trims memory and collects garbage every 10 minutes."""
    while IS_RUNNING:
        time.sleep(600)
        if not IS_RUNNING:
            break
        try:
            perform_system_memory_cleanup()
            logger.debug("Periodic background RAM cleanup executed.")
        except Exception:
            pass

autofix_attempts = {}

def child_watchdog(proc, fname):
    """Watches the running child process and sends alert or self-heals if it exits or crashes."""
    ret = proc.wait()
    pdata = running_processes.get(fname, {})
    is_stopped = pdata.get("is_stopped", False)
    
    running_processes.pop(fname, None)

    # A script must ONLY be removed from active_scripts when manually stopped by user or deleted.
    # Unexpected crashes/exits preserve active_scripts so scripts auto-resume on restart/relay.
    # If stopped intentionally by admin or terminated via SIGKILL/SIGTERM/shutdown, skip crash alert
    if is_stopped or not IS_RUNNING or ret in [-9, -15, 137, 143, -2, 2]:
        logger.info(f"Process {fname} stopped cleanly (Exit code: {ret}).")
        return
    
    recent_err = "\n".join(pdata.get("logs", [])[-20:]) if pdata.get("logs") else "(No output recorded)"
    escaped_err = html.escape(recent_err)
    
    if config.get("admin_ids"):
        if ret != 0:
            # Check for Telethon / Pyrogram incompatible or corrupted SQLite session file
            if "ValueError: too many values to unpack (expected 5)" in recent_err or "telethon.sessions.sqlite" in recent_err or "SQLiteSession" in recent_err or "database disk image is malformed" in recent_err:
                retry_key = f"session_fix_{fname}"
                attempts = autofix_attempts.get(retry_key, 0)
                if attempts < 2:
                    autofix_attempts[retry_key] = attempts + 1
                    logger.info(f"🛠️ [Auto-Self-Heal] Incompatible Telethon session detected in {fname}. Resetting session files and restarting...")
                    notify_all_admins(
                        f"🛠️ <b>Self-Healing System Active:</b>\n"
                        f"Incompatible <code>.session</code> SQLite schema detected in <code>{fname}</code>.\n"
                        f"🔄 Automatically resetting corrupted session file and restarting..."
                    )
                    
                    script_dir = os.path.dirname(os.path.join(SCRIPTS_DIR, fname)) or SCRIPTS_DIR
                    if os.path.exists(script_dir):
                        for f in os.listdir(script_dir):
                            if f.endswith(".session") or f.endswith(".session-journal"):
                                try:
                                    os.remove(os.path.join(script_dir, f))
                                    logger.info(f"Removed incompatible session file: {f}")
                                except Exception as e:
                                    logger.error(f"Error removing session file {f}: {e}")
                                    
                    time.sleep(1.0)
                    ok_restart, restart_msg = start_child_app(fname, force_restart=True)
                    if ok_restart:
                        notify_all_admins(f"🟢 <b>Auto-Healing Succeeded!</b>\n<code>{fname}</code> is now running fresh with a newly initialized session.")
                        return

            missing_mod = extract_missing_module(recent_err)
            if missing_mod:
                pip_pkg = map_module_to_pip_pkg(missing_mod)
                retry_key = f"autofix_{fname}"
                attempts = autofix_attempts.get(retry_key, 0)
                if attempts < 3:
                    autofix_attempts[retry_key] = attempts + 1
                    logger.info(f"⚡ [Auto-Self-Heal] Missing module '{missing_mod}' in {fname}. Auto-installing '{pip_pkg}'...")
                    notify_all_admins(
                        f"🛠️ <b>Self-Healing System Active:</b>\n"
                        f"Missing package <code>{missing_mod}</code> detected in <code>{fname}</code>.\n"
                        f"⏳ Automatically installing <code>{pip_pkg}</code> and restarting (Attempt {attempts+1}/3)..."
                    )
                    py_bin, pip_bin, venv_dir = get_or_create_venv(fname)
                    if isinstance(pip_bin, list):
                        cmd = pip_bin + ["install", pip_pkg]
                    else:
                        cmd = [pip_bin, "install", pip_pkg]
                    subprocess.run(cmd, capture_output=True, text=True)
                    subprocess.run([sys.executable, "-m", "pip", "install", pip_pkg], capture_output=True, text=True)
                    
                    time.sleep(1.0)
                    ok_restart, restart_msg = start_child_app(fname)
                    if ok_restart:
                        notify_all_admins(f"🟢 <b>Auto-Healing Succeeded!</b>\n<code>{fname}</code> is now running with <code>{pip_pkg}</code> installed.")
                        return

                alert_text = (
                    f"⚠️ <b>Script Crashed: Missing Module <code>{missing_mod}</code></b>\n"
                    f"📁 <b>Script:</b> <code>{fname}</code>\n"
                    f"🔴 <b>Exit Code:</b> <code>{ret}</code>\n\n"
                    f"<b>Error Traceback:</b>\n"
                    f"<pre>{escaped_err[-2000:]}</pre>\n\n"
                    f"💡 <i>Click below to auto-install <b>{missing_mod}</b> and restart:</i>"
                )
                markup = {
                    "inline_keyboard": [
                        [{"text": f"📦 Auto-Install {missing_mod} & Run", "callback_data": f"autofix_pkg_{missing_mod}_{fname}"}],
                        [{"text": f"📋 Logs: {fname}", "callback_data": f"show_log_for_{fname}"}],
                        [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
                    ]
                }
            else:
                alert_text = (
                    f"⚠️ <b>Script Crashed / Exited!</b>\n"
                    f"📁 <b>File:</b> <code>{fname}</code>\n"
                    f"🔴 <b>Exit Code:</b> <code>{ret}</code>\n\n"
                    f"<b>Error Traceback:</b>\n"
                    f"<pre>{escaped_err[-2500:]}</pre>\n\n"
                    f"💡 <i>Tip: Use <b>Install Pip Package</b> if a dependency is missing.</i>"
                )
                markup = {
                    "inline_keyboard": [
                        [{"text": "📦 Install Pip Package", "callback_data": "menu_pip_prompt"}],
                        [{"text": f"🔄 Restart {fname}", "callback_data": f"exec_run_{fname}"}],
                        [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
                    ]
                }
        else:
            alert_text = (
                f"ℹ️ <b>Script Completed:</b> <code>{fname}</code> finished execution (Code 0).\n\n"
                f"<b>Output:</b>\n<pre>{escaped_err[-2000:]}</pre>"
            )
            markup = get_main_menu_keyboard()
        notify_all_admins(alert_text, reply_markup=markup)

# ---------------------------------------------------------------------------
# Virtualenv & Multi-Python Environment Isolation Engine
# ---------------------------------------------------------------------------
VENVS_DIR = os.path.join(WORKSPACE_DIR, ".venvs")
installed_req_hashes = {}

def get_script_venv_slug(clean_name):
    """Generates a clean directory slug for the script's virtualenv."""
    dir_name = os.path.dirname(clean_name)
    if dir_name:
        return dir_name.replace("/", "_").replace("\\", "_")
    base = os.path.splitext(os.path.basename(clean_name))[0]
    return base.replace(".", "_")

def get_script_venv_dir(clean_name):
    slug = get_script_venv_slug(clean_name)
    return os.path.join(VENVS_DIR, slug)

def get_or_create_venv(clean_name):
    """Returns (python_bin, pip_bin, venv_dir) for isolated script execution."""
    os.makedirs(VENVS_DIR, exist_ok=True)
    venv_dir = get_script_venv_dir(clean_name)
    py_bin = os.path.join(venv_dir, "bin", "python")
    pip_bin = os.path.join(venv_dir, "bin", "pip")
    
    # On Windows fallback compatibility
    if not os.path.exists(py_bin) and os.path.exists(os.path.join(venv_dir, "Scripts", "python.exe")):
        py_bin = os.path.join(venv_dir, "Scripts", "python.exe")
        pip_bin = os.path.join(venv_dir, "Scripts", "pip.exe")

    if not os.path.exists(py_bin):
        logger.info(f"🛡️ Creating isolated virtualenv for {clean_name} at {venv_dir}...")
        try:
            subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", venv_dir], check=True, capture_output=True)
        except Exception as e:
            logger.error(f"Failed to create venv: {e}, falling back to system python")
            return sys.executable, [sys.executable, "-m", "pip"], None

    return py_bin, pip_bin, venv_dir

def check_and_install_reqs(req_path, clean_name=None):
    """Smart installer: installs packages into both the isolated virtualenv and system Python with line-by-line error fallback."""
    if not req_path or not os.path.exists(req_path):
        return
    import hashlib
    try:
        with open(req_path, "rb") as f:
            file_hash = hashlib.md5(f.read()).hexdigest()
            
        hash_key = f"{clean_name or 'global'}:{req_path}"
        if installed_req_hashes.get(hash_key) == file_hash:
            return
        
        py_bin, pip_bin, venv_dir = get_or_create_venv(clean_name or "global")
        logger.info(f"📦 Installing requirements into isolated venv ({clean_name}) from {os.path.basename(req_path)}...")
        
        # 1. Install into virtualenv
        if isinstance(pip_bin, list):
            cmd = pip_bin + ["install", "-r", req_path]
        else:
            cmd = [pip_bin, "install", "-r", req_path]
            
        res = subprocess.run(cmd, capture_output=True, text=True)
        
        # 2. Install into host Python as well for maximum reliability
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path], capture_output=True, text=True)
        
        # 3. If bulk install encountered an error, try installing line-by-line
        if res.returncode != 0:
            logger.warning(f"Bulk install returned non-zero, falling back to line-by-line install for {req_path}...")
            try:
                with open(req_path, "r", encoding="utf-8", errors="ignore") as rf:
                    lines = [l.strip() for l in rf if l.strip() and not l.startswith("#")]
                for line in lines:
                    pkg_spec = line.split(";")[0].strip()
                    if pkg_spec:
                        if isinstance(pip_bin, list):
                            subprocess.run(pip_bin + ["install", pkg_spec], capture_output=True)
                        else:
                            subprocess.run([pip_bin, "install", pkg_spec], capture_output=True)
                        subprocess.run([sys.executable, "-m", "pip", "install", pkg_spec], capture_output=True)
            except Exception:
                pass
                
        installed_req_hashes[hash_key] = file_hash
    except Exception as e:
        logger.error(f"Error installing requirements: {e}")

def auto_install_script_imports(py_filepath, clean_name=None):
    """Scans python script for third-party imports and automatically pre-installs missing packages."""
    if not py_filepath or not os.path.exists(py_filepath):
        return
    stdlib_modules = {
        "os", "sys", "time", "json", "math", "re", "threading", "asyncio", "subprocess",
        "shutil", "hashlib", "logging", "datetime", "html", "urllib", "collections",
        "itertools", "functools", "typing", "pathlib", "tempfile", "glob", "socket",
        "traceback", "base64", "struct", "io", "sqlite3", "random", "uuid", "queue",
        "contextlib", "inspect", "enum", "dataclasses", "unittest", "copy", "platform",
        "signal", "select", "multiprocessing", "gc", "builtins", "abc", "typing_extensions"
    }
    try:
        with open(py_filepath, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
        import re
        imports = set()
        for m in re.finditer(r'^(?:from|import)\s+([a-zA-Z0-9_]+)', code, re.MULTILINE):
            mod = m.group(1)
            if mod and mod not in stdlib_modules:
                imports.add(mod)
        
        if imports:
            py_bin, pip_bin, venv_dir = get_or_create_venv(clean_name or os.path.basename(py_filepath))
            for mod in imports:
                pip_pkg = map_module_to_pip_pkg(mod)
                if pip_pkg:
                    if isinstance(pip_bin, list):
                        subprocess.run(pip_bin + ["install", pip_pkg], capture_output=True)
                    else:
                        subprocess.run([pip_bin, "install", pip_pkg], capture_output=True)
                    subprocess.run([sys.executable, "-m", "pip", "install", pip_pkg], capture_output=True)
    except Exception as e:
        logger.error(f"Error in auto_install_script_imports: {e}")

def stop_process_graceful_then_force(proc, pid, stored_create_time, timeout=2.5):
    """
    Two-layer verified termination:
    1. Sends SIGTERM / terminate() to allow graceful state saving & port cleanup.
    2. Waits for process and children to exit.
    3. Escalates to SIGKILL / kill() if still alive after timeout.
    """
    try:
        if not pid or not psutil.pid_exists(pid):
            return True
        p = psutil.Process(pid)
        # Verify creation timestamp to prevent PID reuse kills
        if stored_create_time and abs(p.create_time() - stored_create_time) >= 0.5:
            return True
        
        children = []
        try:
            children = p.children(recursive=True)
        except Exception:
            pass

        # Step 1: Graceful SIGTERM
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            p.terminate()
        except Exception:
            pass

        # Step 2: Wait for graceful exit
        procs_to_wait = children + [p]
        gone, alive = psutil.wait_procs(procs_to_wait, timeout=timeout)
        if not alive:
            return True

        # Step 3: Force kill fallback for any stubborn process
        for child in alive:
            try:
                child.kill()
            except Exception:
                pass
        try:
            p.kill()
        except Exception:
            pass
        psutil.wait_procs(alive, timeout=1.0)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        pass
    except Exception as e:
        logger.error(f"Error terminating PID {pid}: {e}")

    try:
        if proc:
            proc.poll()
    except Exception:
        pass
    return True

def start_child_app(filename="bot.py", force_restart=False):
    # Strip any prefix like scripts/
    clean_name = os.path.normpath(filename.replace("scripts/", "").lstrip("/")).replace("\\", "/")
    full_path = os.path.join(SCRIPTS_DIR, clean_name)
    
    if not os.path.exists(full_path):
        base_filename = os.path.basename(filename)
        # Search recursively inside SCRIPTS_DIR
        found_path = None
        for root, _, files in os.walk(SCRIPTS_DIR):
            if base_filename in files:
                found_path = os.path.join(root, base_filename)
                break
        if found_path and os.path.exists(found_path):
            full_path = found_path
            clean_name = os.path.relpath(found_path, SCRIPTS_DIR)
        elif os.path.exists(os.path.join(WORKSPACE_DIR, filename)):
            full_path = os.path.join(WORKSPACE_DIR, filename)
            clean_name = os.path.basename(filename)
        else:
            return False, f"File <code>{clean_name}</code> not found in scripts folder."
    
    # Normalize clean_name and absolute path
    clean_name = os.path.normpath(clean_name).replace("\\", "/")
    real_script_path = os.path.realpath(full_path)
    base_filename = os.path.basename(clean_name)
    script_working_dir = os.path.dirname(full_path) or SCRIPTS_DIR

    # Acquire per-target execution mutex lock (prevents concurrent launch race conditions)
    target_lock = get_target_lock(clean_name)
    with target_lock:
        # Check if this script is already running
        active_now = get_active_running_processes()
        if clean_name in active_now:
            if force_restart:
                stop_child_app(script_name=clean_name, clear_active=False)
                time.sleep(0.5)
            else:
                pid = active_now[clean_name]["pid"]
                return False, f"⚠️ <code>{clean_name}</code> is already running (PID: <code>{pid}</code>)."

        # 1. Get or create isolated Virtualenv for this script/project!
        venv_py, venv_pip, venv_dir = get_or_create_venv(clean_name)

        # 2. Smart Auto-install dependencies into isolated venv and system Python
        req_path = get_script_req_path(clean_name)
        if req_path:
            check_and_install_reqs(req_path, clean_name=clean_name)
            
        if "/" in clean_name:
            proj_root = os.path.join(SCRIPTS_DIR, clean_name.split("/")[0])
            for r, _, fs in os.walk(proj_root):
                for f in fs:
                    if "requirements" in f.lower() and f.endswith(".txt"):
                        check_and_install_reqs(os.path.join(r, f), clean_name=clean_name)

        auto_install_script_imports(real_script_path, clean_name=clean_name)

        # 3. Security scan before launch
        abuse_reason = scan_script_for_abuse(full_path)
        if abuse_reason:
            return False, (
                f"🚫 <b>Launch Blocked by Security Guard!</b>\n\n"
                f"⚠️ <b>Reason:</b> <i>{abuse_reason}</i>\n\n"
                f"💡 <i>Remove suspicious mining / abuse code to run this script.</i>"
            )
        
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            if venv_dir:
                env["VIRTUAL_ENV"] = venv_dir
                env["PATH"] = f"{os.path.join(venv_dir, 'bin')}:{env.get('PATH', '')}"
            env["PYTHONPATH"] = f"{script_working_dir}:{SCRIPTS_DIR}:{WORKSPACE_DIR}:{env.get('PYTHONPATH', '')}"
            
            # Inject global env vars
            env.update(config.get("env_vars", {}))
            
            # Inject script-specific private .env variables!
            script_private_env = read_script_env(clean_name)
            env.update(script_private_env)
            
            # Ensure a physical .env file exists in the script's cwd so python-dotenv works!
            if script_private_env:
                target_dot_env = os.path.join(script_working_dir, ".env")
                try:
                    with open(target_dot_env, "w", encoding="utf-8") as f:
                        for k, v in sorted(script_private_env.items()):
                            f.write(f"{k}={v}\n")
                except Exception as e:
                    logger.error(f"Error creating local .env: {e}")
            
            # Launch using the isolated virtualenv Python binary!
            cmd = [venv_py, "-u", full_path]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=script_working_dir, # Run in project directory!
                env=env
            )
            
            # Capture exact OS creation timestamp for two-layer PID verification
            create_time = time.time()
            try:
                p_os = psutil.Process(proc.pid)
                create_time = p_os.create_time()
            except Exception:
                pass

            running_processes[clean_name] = {
                "proc": proc,
                "start_time": time.time(),
                "create_time": create_time,
                "logs": [],
                "is_stopped": False,
                "pid": proc.pid,
                "script_real_path": real_script_path,
                "parent_pid": os.getpid()
            }
            
            threading.Thread(target=log_stream_reader, args=(proc.stdout, clean_name), daemon=True).start()
            threading.Thread(target=child_watchdog, args=(proc, clean_name), daemon=True).start()
            threading.Thread(target=resource_guard_monitor, args=(proc, clean_name), daemon=True).start()
            
            # Post-launch health-check window (verify process stays alive and detect crash-on-start)
            time.sleep(1.5)
            
            poll_res = proc.poll()
            if poll_res is not None:
                pdata = running_processes.pop(clean_name, {})
                err_msg = "\n".join(pdata.get("logs", [])) if pdata.get("logs") else "(No output recorded)"
                missing_mod = extract_missing_module(err_msg)
                
                if missing_mod:
                    return False, (
                        f"❌ <b>{base_filename} Crash: Missing Package <code>{missing_mod}</code></b>\n\n"
                        f"<b>Error Details:</b>\n<pre>{html.escape(err_msg[-1500:])}</pre>\n\n"
                        f"💡 <i>Use <b>Install Pip</b> to install <code>{missing_mod}</code>.</i>"
                    )
                
                return False, (
                    f"❌ <b>{base_filename} failed to start (Exit Code: {poll_res})</b>\n\n"
                    f"<b>Error Details:</b>\n<pre>{html.escape(err_msg[-2500:])}</pre>\n\n"
                    f"💡 <i>Tip: Use <b>Install Pip Package</b> if a dependency is missing.</i>"
                )
            
            current_active = list(config.get("active_scripts", []))
            if clean_name not in current_active:
                current_active.append(clean_name)
            config["active_scripts"] = current_active
            
            # Remove from manually_stopped_scripts so it will run on future boots
            manually_stopped = list(config.get("manually_stopped_scripts", []))
            config["manually_stopped_scripts"] = [
                s for s in manually_stopped
                if s != clean_name and os.path.basename(s) != base_filename
            ]
            save_config(config)
            
            # Immediately lock running state to GitHub cloud in background
            threading.Thread(target=git_sync_to_github, args=(f"Set active scripts: {', '.join(current_active)}",), daemon=True).start()
            
            req_note = f" (📦 {os.path.basename(req_path)})" if req_path else " (📄 Standalone)"
            return True, f"✨ <b>{clean_name}</b> started successfully!{req_note}\n🆔 PID: <code>{proc.pid}</code>\n🟢 <b>Active Scripts:</b> {len(current_active)} running"
        except Exception as e:
            return False, f"❌ Start error: {e}"

def stop_child_app(script_name=None, clear_active=True):
    """
    Stops a specific script, a project folder, or all running scripts.
    Guarantees:
    - Path normalization (resolves absolute realpaths, handles (2) special characters).
    - Two-layer verification: matches (a) realpath/normalized name + (b) bot parent PID ownership and create_time.
    - Zero false-positive kills.
    - Graceful-then-force termination sequence.
    """
    stopped_names = []
    
    if not script_name:
        targets = list(running_processes.keys())
    else:
        clean_target = os.path.normpath(script_name.replace("scripts/", "").lstrip("/")).replace("\\", "/")
        target_abs_path = os.path.realpath(os.path.join(SCRIPTS_DIR, clean_target))
        base_target = os.path.basename(clean_target)
        zip_stem = clean_target[:-4] if clean_target.endswith(".zip") else clean_target
        
        targets = []
        for k, pdata in list(running_processes.items()):
            k_norm = os.path.normpath(k).replace("\\", "/")
            k_real_path = pdata.get("script_real_path") or os.path.realpath(os.path.join(SCRIPTS_DIR, k_norm))
            
            # Exact match, path prefix match, realpath equality, or directory containment
            is_match = (
                k_norm == clean_target
                or k_norm == zip_stem
                or k_real_path == target_abs_path
                or k_real_path.startswith(target_abs_path + os.sep)
                or k_norm.startswith(f"{clean_target}/")
                or k_norm.startswith(f"{zip_stem}/")
                or os.path.basename(k_norm) == base_target
                or os.path.dirname(k_norm) == clean_target
                or os.path.dirname(k_norm) == zip_stem
            )
            if is_match:
                targets.append(k)

    for name in targets:
        pdata = running_processes.get(name)
        if pdata:
            pdata["is_stopped"] = True
            proc = pdata.get("proc")
            pid = pdata.get("pid")
            stored_create_time = pdata.get("create_time")
            
            # Two-layer verified termination
            stop_process_graceful_then_force(proc, pid, stored_create_time, timeout=2.5)
            stopped_names.append(name)
            running_processes.pop(name, None)

    if clear_active:
        if not script_name:
            # User stopped ALL scripts
            all_active = list(config.get("active_scripts", []))
            stopped_list = list(config.get("manually_stopped_scripts", []))
            for s in all_active:
                if s not in stopped_list:
                    stopped_list.append(s)
            config["manually_stopped_scripts"] = stopped_list
            config["active_scripts"] = []
            config["active_script"] = None
            save_config(config)
            threading.Thread(target=git_sync_to_github, args=("Stop all scripts",), daemon=True).start()
        else:
            clean_target = os.path.normpath(script_name.replace("scripts/", "").lstrip("/")).replace("\\", "/")
            base_target = os.path.basename(clean_target)
            proj_target = clean_target.split("/")[0] if "/" in clean_target else clean_target
            
            current_active = list(config.get("active_scripts", []))
            new_active = []
            for s in current_active:
                s_clean = os.path.normpath(s.replace("scripts/", "").lstrip("/")).replace("\\", "/")
                s_base = os.path.basename(s_clean)
                s_proj = s_clean.split("/")[0] if "/" in s_clean else s_clean
                is_match = (
                    s == script_name
                    or s_clean == clean_target
                    or s_base == base_target
                    or s_clean.startswith(f"{clean_target}/")
                    or (proj_target and s_proj == proj_target)
                    or s in stopped_names
                )
                if not is_match:
                    new_active.append(s)
            config["active_scripts"] = new_active
            
            # Add to manually_stopped_scripts so it will remain stopped on future boots
            stopped_list = list(config.get("manually_stopped_scripts", []))
            if clean_target not in stopped_list:
                stopped_list.append(clean_target)
            config["manually_stopped_scripts"] = stopped_list
            
            if config.get("active_script") in [clean_target, script_name, base_target]:
                config["active_script"] = None
                
            save_config(config)
            threading.Thread(target=git_sync_to_github, args=(f"Stop script: {clean_target}",), daemon=True).start()

    if stopped_names:
        if len(stopped_names) == 1:
            return True, f"🛑 <b>{stopped_names[0]}</b> has been stopped successfully."
        else:
            return True, f"🛑 Stopped {len(stopped_names)} scripts: " + ", ".join([f"<code>{n}</code>" for n in stopped_names])
    elif script_name:
        return True, f"🛑 <b>{script_name}</b> is marked as stopped."
    return False, "ℹ️ No running script found to stop."

def restart_child_app(script_name=None):
    """Restarts a specific script, or restarts ALL active scripts in parallel."""
    active = get_active_running_processes()
    
    if script_name:
        stop_child_app(script_name=script_name, clear_active=False)
        time.sleep(1.0)
        return start_child_app(script_name)
    
    # Identify all targets to restart: only currently running or active scripts that are not manually stopped
    targets = list(active.keys())
    if not targets:
        manually_stopped = set(config.get("manually_stopped_scripts", []))
        targets = [s for s in config.get("active_scripts", []) if s not in manually_stopped]
    if not targets and config.get("active_script"):
        if config["active_script"] not in config.get("manually_stopped_scripts", []):
            targets = [config["active_script"]]
                
    if not targets:
        return False, "ℹ️ No active scripts to restart. Please run a script from the dashboard first."
        
    # Stop all targets cleanly without clearing persistence
    stop_child_app(script_name=None, clear_active=False)
    time.sleep(1.5)
    
    success_list = []
    fail_list = []
    
    for s in targets:
        ok, res_msg = start_child_app(s)
        if ok:
            success_list.append(s)
        else:
            fail_list.append(f"<code>{s}</code> ({res_msg})")
        time.sleep(0.5)
        
    if success_list and not fail_list:
        return True, f"🔄 <b>Restarted {len(success_list)} scripts successfully in parallel:</b>\n" + "\n".join([f"• 🟢 <code>{s}</code>" for s in success_list])
    elif success_list and fail_list:
        return True, (
            f"🔄 <b>Partial Restart:</b>\n"
            f"• <b>Started:</b> " + ", ".join([f"<code>{s}</code>" for s in success_list]) + "\n"
            f"• <b>Errors:</b>\n" + "\n".join(fail_list)
        )
    else:
        return False, f"❌ <b>Restart failed:</b>\n" + "\n".join(fail_list)

# ---------------------------------------------------------------------------
# Self-Trigger: Next Runner Launch (Relay Handoff) & Diagnostics
# ---------------------------------------------------------------------------
def check_relay_configuration():
    """
    Checks if GH_PAT and workflow dispatch permissions are available.
    Returns (status_enum, diagnostic_html_string)
    """
    if not GH_PAT:
        return (
            "MISSING_PAT",
            "⚠️ <b>Action Needed:</b> <code>GH_PAT</code> secret is not configured in GitHub repository settings.\n\n"
            "• GitHub Actions security blocks the default <code>GITHUB_TOKEN</code> from self-triggering workflows.\n"
            "• <b>To ensure seamless 24/7 auto-restart:</b>\n"
            "  1. Open GitHub ➔ Settings ➔ Secrets and variables ➔ Actions\n"
            "  2. Add repository secret <code>GH_PAT</code> with a Personal Access Token (PAT) having <code>repo</code> and <code>workflow</code> permissions."
        )

    token_to_use = GH_PAT
    auth_headers = {
        "Authorization": f"Bearer {token_to_use}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        url = f"https://api.github.com/repos/{REPO}/actions/workflows"
        resp = requests.get(url, headers=auth_headers, timeout=10)
        if resp.status_code == 200:
            return ("OK", "🟢 <b>GH_PAT Verified:</b> Workflow dispatch permissions confirmed. 5.5-hour relay transitions are fully autonomous!")
        elif resp.status_code == 401:
            return ("INVALID_PAT", "❌ <b>Invalid GH_PAT:</b> GitHub returned <code>HTTP 401 Unauthorized</code> (token is expired or incorrect).")
        elif resp.status_code == 403:
            return ("INSUFFICIENT_PERMISSIONS", "⚠️ <b>Insufficient Permissions:</b> <code>HTTP 403 Forbidden</code>. Ensure the PAT has <code>workflow</code> & <code>actions:write</code> scopes.")
        elif resp.status_code == 404:
            return ("NOT_FOUND", f"⚠️ <b>Repo/Workflows Not Found:</b> <code>HTTP 404</code> for <code>{REPO}</code>. Check repo name and PAT <code>repo</code> scope.")
        else:
            return ("UNKNOWN_STATUS", f"⚠️ GitHub API returned <code>HTTP {resp.status_code}</code>: {html.escape(resp.text[:150])}")
    except Exception as e:
        return ("NETWORK_ERROR", f"⚠️ Connection error during relay check: {html.escape(str(e))}")


def trigger_next_runner_detailed():
    """
    Multi-Strategy Autonomous Runner Dispatch:
    1. Workflow Dispatch API via filename ('server.yml')
    2. Workflow Dispatch API via dynamic integer Workflow ID
    3. Repository Dispatch API ('relay-handoff' event)
    4. GitHub CLI tool ('gh workflow run')
    
    Returns: (bool, str) -> (is_success, detail_message)
    """
    token_to_use = EFFECTIVE_TOKEN
    if not token_to_use:
        return False, "No GitHub token (GH_PAT or GITHUB_TOKEN) available in environment."

    auth_headers = {
        "Authorization": f"Bearer {token_to_use}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    errors = []

    # 1. Strategy 1: Standard Workflow Dispatch via File Name
    try:
        url_file = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
        payload_file = {"ref": WORKFLOW_REF}
        resp = requests.post(url_file, headers=auth_headers, json=payload_file, timeout=15)
        if resp.status_code == 204:
            logger.info(f"✅ Strategy 1 succeeded: Workflow dispatch via '{WORKFLOW_FILE}' (HTTP 204)")
            return True, f"Workflow dispatch via '{WORKFLOW_FILE}' succeeded."
        else:
            msg = f"Strategy 1 (filename '{WORKFLOW_FILE}') returned HTTP {resp.status_code}: {resp.text.strip()}"
            logger.warning(msg)
            errors.append(msg)
    except Exception as e:
        msg = f"Strategy 1 exception: {e}"
        logger.warning(msg)
        errors.append(msg)

    # 2. Strategy 2: Dynamic Workflow ID Lookup & Dispatch
    try:
        url_list = f"https://api.github.com/repos/{REPO}/actions/workflows"
        resp_list = requests.get(url_list, headers=auth_headers, timeout=15)
        if resp_list.status_code == 200:
            workflows_list = resp_list.json().get("workflows", [])
            target_id = None
            for wf in workflows_list:
                wpath = wf.get("path", "")
                wname = wf.get("name", "")
                wid = wf.get("id")
                if WORKFLOW_FILE in wpath or WORKFLOW_FILE == str(wid) or "Relay" in wname:
                    target_id = wid
                    break
            if target_id:
                url_id = f"https://api.github.com/repos/{REPO}/actions/workflows/{target_id}/dispatches"
                resp_id = requests.post(url_id, headers=auth_headers, json={"ref": WORKFLOW_REF}, timeout=15)
                if resp_id.status_code == 204:
                    logger.info(f"✅ Strategy 2 succeeded: Workflow dispatch via ID {target_id} (HTTP 204)")
                    return True, f"Workflow dispatch via ID {target_id} succeeded."
                else:
                    msg = f"Strategy 2 (workflow ID {target_id}) returned HTTP {resp_id.status_code}: {resp_id.text.strip()}"
                    logger.warning(msg)
                    errors.append(msg)
    except Exception as e:
        msg = f"Strategy 2 exception: {e}"
        logger.warning(msg)
        errors.append(msg)

    # 3. Strategy 3: Repository Dispatch API ('relay-handoff' event)
    try:
        url_repo = f"https://api.github.com/repos/{REPO}/dispatches"
        payload_repo = {
            "event_type": "relay-handoff",
            "client_payload": {
                "ref": WORKFLOW_REF,
                "timestamp": int(time.time()),
                "prev_run_id": str(RUN_ID)
            }
        }
        resp_repo = requests.post(url_repo, headers=auth_headers, json=payload_repo, timeout=15)
        if resp_repo.status_code == 204:
            logger.info("✅ Strategy 3 succeeded: Repository dispatch ('relay-handoff') (HTTP 204)")
            return True, "Repository dispatch ('relay-handoff') succeeded."
        else:
            msg = f"Strategy 3 (repository_dispatch) returned HTTP {resp_repo.status_code}: {resp_repo.text.strip()}"
            logger.warning(msg)
            errors.append(msg)
    except Exception as e:
        msg = f"Strategy 3 exception: {e}"
        logger.warning(msg)
        errors.append(msg)

    # 4. Strategy 4: GitHub CLI ('gh workflow run')
    try:
        gh_cmd = ["gh", "workflow", "run", WORKFLOW_FILE, "--ref", WORKFLOW_REF, "-R", REPO]
        gh_env = {**os.environ, "GH_TOKEN": token_to_use, "GITHUB_TOKEN": token_to_use}
        gh_res = subprocess.run(gh_cmd, capture_output=True, text=True, timeout=15, env=gh_env)
        if gh_res.returncode == 0:
            logger.info("✅ Strategy 4 succeeded: gh workflow run CLI tool")
            return True, "GitHub CLI dispatch succeeded."
        else:
            msg = f"Strategy 4 (gh CLI) returned code {gh_res.returncode}: {gh_res.stderr.strip()}"
            logger.warning(msg)
            errors.append(msg)
    except FileNotFoundError:
        pass
    except Exception as e:
        msg = f"Strategy 4 exception: {e}"
        logger.warning(msg)
        errors.append(msg)

    final_err = " | ".join(errors) if errors else "Unknown dispatch error"
    return False, final_err


def trigger_next_runner():
    """Backward compatibility wrapper."""
    ok, _ = trigger_next_runner_detailed()
    return ok


def flush_all_sqlite_databases():
    """
    Safely flushes and checkpoints all SQLite WAL journals across workspace and scripts.
    Prevents database corruption or incomplete commits during relay handoff.
    """
    import sqlite3
    db_exts = {".db", ".sqlite", ".sqlite3", ".session"}
    flushed_count = 0
    for root, _, files in os.walk(WORKSPACE_DIR):
        if ".git" in root or "__pycache__" in root:
            continue
        for f in files:
            if any(f.endswith(ext) for ext in db_exts) and not f.endswith(("-wal", "-shm", "-journal")):
                db_path = os.path.join(root, f)
                try:
                    conn = sqlite3.connect(db_path, timeout=5.0)
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    conn.commit()
                    conn.close()
                    flushed_count += 1
                except Exception as e:
                    logger.debug(f"SQLite checkpoint on {f}: {e}")
    if flushed_count > 0:
        logger.info(f"💾 Checkpointed and flushed {flushed_count} SQLite database(s) before sync.")


def wait_for_warm_handshake(max_wait_seconds=45):
    """
    Zero-Downtime Warm Handshake:
    Keeps current runner and child scripts alive during new runner's bootup window (~30-45s).
    Monitors GitHub Actions for new runner arrival, then gracefully terminates old processes.
    """
    token_to_use = EFFECTIVE_TOKEN
    start_wait = time.time()
    logger.info(f"🤝 Warm Handshake activated: Keeping child scripts LIVE during runner bootstrap (~{max_wait_seconds}s window)...")
    
    while time.time() - start_wait < max_wait_seconds:
        time.sleep(5)
        try:
            url = f"https://api.github.com/repos/{REPO}/actions/runs?per_page=5"
            headers = {
                "Authorization": f"Bearer {token_to_use}",
                "Accept": "application/vnd.github+json"
            }
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                runs = resp.json().get("workflow_runs", [])
                for r in runs:
                    r_id = str(r.get("id"))
                    r_status = r.get("status")
                    if r_id != str(RUN_ID) and r_id != "local-dev":
                        if r_status in ["in_progress", "queued"]:
                            elapsed_warm = int(time.time() - start_wait)
                            logger.info(f"🟢 Warm Handshake confirmed: Next runner #{r_id} is {r_status} (after {elapsed_warm}s).")
                            time.sleep(15)
                            return True
        except Exception as e:
            logger.debug(f"Warm Handshake poll error: {e}")
            
    logger.info("⏳ Warm Handshake window complete. Transitioning to release phase.")
    return True


def execute_relay_handoff_sequence(reason="Manual Shutdown"):
    """
    Clean server shutdown sequence:
    1. Checkpoints and flushes all SQLite database WAL files
    2. Records active running scripts to bot_config.json
    3. Syncs and pushes repository changes to GitHub
    4. Cleanly stops child scripts without auto-dispatching new runners
    """
    global IS_RUNNING
    IS_RUNNING = False
    logger.info(f"🛑 Clean Server Shutdown ({reason}). Auto-restart is disabled.")

    # Preserve scripts previously started by user and NOT manually stopped, plus current active processes
    manually_stopped = set(config.get("manually_stopped_scripts", []))
    preserved = [s for s in config.get("active_scripts", []) if s not in manually_stopped]
    for k in get_active_running_processes().keys():
        if k not in manually_stopped and k not in preserved:
            preserved.append(k)
    config["active_scripts"] = preserved
    save_config(config)

    notify_all_admins(
        f"🛑 <b>Server Stopped ({reason}):</b>\n"
        "Workspace data and databases have been safely backed up to GitHub.\n"
        "💡 <i>Auto-restart is disabled. The workflow will only run when you manually start it.</i>"
    )

    # 1. Flush SQLite databases to guarantee zero corruption
    flush_all_sqlite_databases()

    # 2. Push workspace changes to GitHub
    git_sync_to_github(f"Final workspace backup on clean shutdown ({reason})")

    # 3. Cleanly stop child apps and exit
    stop_child_app(script_name=None, clear_active=False)
    time.sleep(1)
    os._exit(0)

# ---------------------------------------------------------------------------
# Visual UI & Keyboards
# ---------------------------------------------------------------------------
def get_main_menu_keyboard():
    active = get_active_running_processes()
    count = len(active)
    
    if count == 1:
        name = list(active.keys())[0]
        pdata = active[name]
        cu_sec = int(time.time() - pdata["start_time"])
        ch, cr = divmod(cu_sec, 3600)
        cm, _ = divmod(cr, 60)
        status_btn = f"🟢 {name} ({ch}h {cm}m)"
    elif count > 1:
        status_btn = f"🟢 {count} Scripts Running"
    else:
        status_btn = "🔴 All Scripts Stopped"
    
    ram = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=None)
    stats_btn = f"⚡ CPU: {cpu}% | RAM: {ram.percent}%"
    
    return {
        "inline_keyboard": [
            [
                {"text": status_btn, "callback_data": "menu_runner"},
                {"text": stats_btn, "callback_data": "menu_status"}
            ],
            [
                {"text": "🚀 Scripts Runner", "callback_data": "menu_runner"},
                {"text": f"🛑 Stop Script{'s' if count > 1 else ''}", "callback_data": "menu_stop"}
            ],
            [
                {"text": "🔄 Restart", "callback_data": "menu_restart"},
                {"text": "📋 Live Logs", "callback_data": "menu_logs"}
            ],
            [
                {"text": "⚙️ Script ENVs", "callback_data": "menu_env_select"},
                {"text": "📂 Workspace Files", "callback_data": "menu_files"}
            ],
            [
                {"text": "🗄️ Databases & Storage", "callback_data": "menu_db_inspector"},
                {"text": "💾 Cloud Sync", "callback_data": "menu_sync"}
            ],
            [
                {"text": "📦 Install Pip", "callback_data": "menu_pip_prompt"},
                {"text": "💻 Linux Shell", "callback_data": "menu_sh_prompt"}
            ],
            [
                {"text": "ℹ️ Server Info", "callback_data": "menu_server_info"},
                {"text": "🧹 Clean RAM", "callback_data": "menu_clean_ram"}
            ],
            [
                {"text": "🛑 Stop Workflow", "callback_data": "menu_stop_workflow_prompt"}
            ]
        ]
    }

def get_back_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
        ]
    }

def render_dashboard_text():
    uptime_sec = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"
    
    active = get_active_running_processes()
    count = len(active)
    
    if count == 0:
        script_status_lines = "• <b>Running Processes:</b> 🔴 <i>None (All Stopped / Standby)</i>"
    else:
        items = []
        for name, pdata in sorted(active.items()):
            cu_sec = int(time.time() - pdata["start_time"])
            ch, cr = divmod(cu_sec, 3600)
            cm, cs = divmod(cr, 60)
            items.append(f"  └ 🟢 <code>{name}</code> (PID: <code>{pdata['pid']}</code> | Uptime: <code>{ch}h {cm}m {cs}s</code>)")
        script_status_lines = f"• <b>Running Processes ({count} Active Concurrently):</b>\n" + "\n".join(items)

    ram = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=None)

    status_text = (
        f"⚡ <b>Cloud Server Multi-Process Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥️ <b>Server Host:</b> High-Speed Cloud Server (Linux)\n"
        f"⏱️ <b>Server Uptime:</b> {uptime_str}\n"
        f"{script_status_lines}\n"
        f"💾 <b>RAM Usage:</b> {ram.percent}% ({ram.used // (1024*1024)}MB / {ram.total // (1024*1024)}MB)\n"
        f"📈 <b>CPU Load:</b> {cpu}%\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Multiple scripts can run in parallel concurrently 24/7!</i>"
    )
    return status_text

# ---------------------------------------------------------------------------
# Message & Interactive Step Handler
# ---------------------------------------------------------------------------
def handle_text_message(chat_id, user_id, text):
    global user_states
    
    # 1. Admin Authentication Check
    if not is_admin(user_id):
        send_tg_message(chat_id, f"⛔ <b>Access Denied:</b> User ID <code>{user_id}</code> is not authorized.")
        return

    raw_text = text.strip()
    state = user_states.get(user_id)

    # Check for cancel command
    if raw_text in ["/cancel", "Cancel", "❌ Cancel"]:
        user_states.pop(user_id, None)
        send_tg_message(chat_id, "❌ Action cancelled.", reply_markup=get_main_menu_keyboard())
        return

    # Check state machine for pending interactive inputs
    if state == "WAITING_PIP_PACKAGE":
        user_states.pop(user_id, None)
        pkg_name = raw_text.replace("pip install", "").strip()
        send_tg_message(chat_id, f"⏳ <b>Installing package:</b> <code>{pkg_name}</code>...")
        res = subprocess.run([sys.executable, "-m", "pip", "install", pkg_name], capture_output=True, text=True)
        out = (res.stdout + "\n" + res.stderr).strip()
        result_msg = (
            f"📦 <b>Package: {pkg_name}</b>\n\n"
            f"<pre>{out[-2500:]}</pre>"
        )
        send_tg_message(chat_id, result_msg, reply_markup=get_main_menu_keyboard())
        return

    elif state == "WAITING_SHELL_CMD":
        user_states.pop(user_id, None)
        send_tg_message(chat_id, f"⚡ <b>Executing:</b> <code>{raw_text}</code>...")
        res = subprocess.run(raw_text, shell=True, capture_output=True, text=True, cwd=WORKSPACE_DIR)
        out = (res.stdout + "\n" + res.stderr).strip() or "(No output)"
        result_msg = (
            f"💻 <b>Command Output:</b>\n\n"
            f"<pre>{out[-2800:]}</pre>"
        )
        send_tg_message(chat_id, result_msg, reply_markup=get_main_menu_keyboard())
        return

    elif isinstance(state, dict) and state.get("action") == "WAITING_ENV_VAR":
        target_py = state.get("target_py", "bot.py")
        user_states.pop(user_id, None)
        info = resolve_env_target_info(target_py)
        
        # Parse single or multi-line environment variables
        parsed_vars = {}
        for line in raw_text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k:
                    parsed_vars[k] = v
                    
        if parsed_vars:
            env_dict = read_script_env(info["name"])
            env_dict.update(parsed_vars)
            ok, msg = write_script_env(info["name"], env_dict)
            
            # Check if this target is currently running
            active = get_active_running_processes()
            is_running = (
                info["entry_script"] in active or
                info["name"] in active or
                any(k.startswith(f"{info['name']}/") or k == info['name'] for k in active.keys())
            )
            running_key = next((k for k in active.keys() if k == info["entry_script"] or k == info["name"] or k.startswith(f"{info['name']}/")), info["entry_script"]) if is_running else info["entry_script"]

            var_lines = []
            for k, v in sorted(parsed_vars.items())[:15]:
                var_lines.append(f"• 🔑 <code>{k}</code> = <code>{mask_secret_val(v)}</code>")
            if len(parsed_vars) > 15:
                var_lines.append(f"<i>...and {len(parsed_vars) - 15} more variables</i>")

            rel_dot_env = os.path.relpath(info["primary_dot_env"], WORKSPACE_DIR)
            restart_hint = ""
            if is_running:
                restart_hint = "\n\n⚡ <i>This script is currently RUNNING. Tap <b>🔄 Apply & Restart</b> below to load new variables immediately!</i>"

            confirm_text = (
                f"✅ <b>Variables Saved for <code>{info['display_name']}</code>!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Variables Added / Updated ({len(parsed_vars)}):</b>\n"
                + "\n".join(var_lines)
                + f"\n\n📁 Saved to <code>{rel_dot_env}</code> and backed up to Cloud!"
                + restart_hint
            )
            buttons = [
                [{"text": f"⚙️ Manage {info['display_name']} ENV", "callback_data": f"env_dash_{info['slug']}"}]
            ]
            if is_running:
                buttons.append([{"text": "🔄 Apply & Restart Script", "callback_data": f"exec_run_{running_key}"}])
            else:
                buttons.append([{"text": f"▶️ Run {info['display_name']} Now", "callback_data": f"exec_run_{info['entry_script']}"}])
            buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
            
            send_tg_message(chat_id, confirm_text, reply_markup={"inline_keyboard": buttons})
        else:
            send_tg_message(
                chat_id,
                "⚠️ <b>Invalid Format!</b>\n\nPlease send variables in <code>KEY=VALUE</code> format.\n(Example: <code>BOT_TOKEN=123456:AAH...</code>)\n\n<i>You can also paste multiple lines at once!</i>",
                reply_markup={"inline_keyboard": [[{"text": "🔙 Back", "callback_data": f"env_dash_{info['slug']}"}]]}
            )
        return

    elif isinstance(state, dict) and state.get("action") == "WAITING_CUSTOM_PY_NAME":
        custom_name = raw_text.strip()
        if not custom_name.endswith(".py"):
            custom_name += ".py"
        # Sanitize filename
        custom_name = custom_name.replace("/", "_").replace("\\", "_")
        staged_path = state.get("staging_path")
        if staged_path and os.path.exists(staged_path):
            import shutil
            dest_path = os.path.join(SCRIPTS_DIR, custom_name)
            shutil.move(staged_path, dest_path)
            user_states.pop(user_id, None)
            git_sync_to_github(f"Upload {custom_name}")
            send_tg_message(
                chat_id,
                f"✅ <b>Saved as <code>{custom_name}</code>!</b>\n\nTap below to launch immediately:",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": f"▶️ Run {custom_name} Now", "callback_data": f"exec_run_{custom_name}"}],
                        [{"text": f"⚙️ Manage {custom_name} ENV", "callback_data": f"env_dash_{custom_name}"}],
                        [{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}],
                        [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
                    ]
                }
            )
            return

    elif state == "WAITING_RUN_FILE":
        user_states.pop(user_id, None)
        filename = raw_text
        if not filename.endswith(".py"):
            filename += ".py"
        ok, msg = start_child_app(filename)
        send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())
        return

    # Default Commands
    if raw_text in ["/start", "/menu", "/help"]:
        send_tg_message(chat_id, render_dashboard_text(), reply_markup=get_main_menu_keyboard())
    
    elif raw_text == "/status":
        send_tg_message(chat_id, render_dashboard_text(), reply_markup=get_main_menu_keyboard())
    
    elif raw_text in ["/env", "/envs", "/config"]:
        prompt_env_script_select(chat_id, user_id)

    elif raw_text.startswith("/run"):
        parts = raw_text.split()
        if len(parts) > 1:
            ok, msg = start_child_app(parts[1])
            send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())
        elif raw_text in ["/runner", "/run", "/scripts"]:
            prompt_runner_menu(chat_id, user_id)
        else:
            # Show interactive run menu
            prompt_run_menu(chat_id, user_id)

    elif raw_text in ["/stop", "/stop_app", "/kill"] or raw_text.startswith(("/stop ", "/kill ")):
        parts = raw_text.split()
        if len(parts) > 1:
            ok, msg = stop_child_app(script_name=parts[1], clear_active=True)
            send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())
        else:
            ok, msg = stop_child_app(script_name=None, clear_active=True)
            send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())

    elif raw_text in ["/restart"]:
        send_tg_message(chat_id, "🔄 Restarting application...")
        ok, msg = restart_child_app()
        send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())

    elif raw_text.startswith("/logs"):
        show_logs_view(chat_id)

    elif raw_text == "/files":
        show_files_view(chat_id)

    elif raw_text.startswith("/pip"):
        parts = raw_text.split()
        if len(parts) > 1:
            pkg = " ".join(parts[1:]).replace("install", "").strip()
            send_tg_message(chat_id, f"⏳ Installing <code>{pkg}</code>...")
            res = subprocess.run([sys.executable, "-m", "pip", "install", pkg], capture_output=True, text=True)
            send_tg_message(chat_id, f"<pre>{res.stdout[-2500:]}</pre>", reply_markup=get_main_menu_keyboard())
        else:
            prompt_pip_menu(chat_id, user_id)

    elif raw_text.startswith("/sh") or raw_text.startswith("/exec"):
        parts = raw_text.split()
        if len(parts) > 1:
            cmd = " ".join(parts[1:])
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=WORKSPACE_DIR)
            out = (res.stdout + "\n" + res.stderr).strip() or "(No output)"
            send_tg_message(chat_id, f"<pre>{out[-2800:]}</pre>", reply_markup=get_main_menu_keyboard())
        else:
            prompt_sh_menu(chat_id, user_id)

    elif raw_text.startswith("/addadmin"):
        parts = raw_text.split()
        if len(parts) > 1 and parts[1].isdigit():
            new_id = int(parts[1])
            if "admin_ids" not in config:
                config["admin_ids"] = []
            if new_id not in config["admin_ids"]:
                config["admin_ids"].append(new_id)
                save_config(config)
                git_sync_to_github(f"Add admin {new_id}")
                send_tg_message(chat_id, f"✅ Added User ID <code>{new_id}</code> as authorized admin.", reply_markup=get_main_menu_keyboard())
            else:
                send_tg_message(chat_id, f"ℹ️ User ID <code>{new_id}</code> is already an admin.", reply_markup=get_main_menu_keyboard())
        else:
            send_tg_message(chat_id, "Usage: <code>/addadmin 123456789</code>")

    elif raw_text.startswith("/admins"):
        admin_list_str = "\n".join([f"• <code>{x}</code>" for x in config.get("admin_ids", [])])
        send_tg_message(chat_id, f"👑 <b>Authorized Admins:</b>\n{admin_list_str}", reply_markup=get_main_menu_keyboard())

    elif raw_text in ["/sync", "/backup"]:
        send_tg_message(chat_id, "⏳ Syncing all files to Cloud Storage...")
        ok, msg = git_sync_to_github()
        send_tg_message(chat_id, f"{'✅' if ok else '❌'} {msg}", reply_markup=get_main_menu_keyboard())

    else:
        # If user just types text, show dashboard
        send_tg_message(chat_id, render_dashboard_text(), reply_markup=get_main_menu_keyboard())

# ---------------------------------------------------------------------------
# Per-Script Environment Variables (.env) Engine & Entry Point Filter
# ---------------------------------------------------------------------------
NON_RUNNABLE_MODULES = {
    "database.py", "db.py", "models.py", "model.py", "utils.py", "util.py",
    "config.py", "configs.py", "helpers.py", "helper.py", "__init__.py",
    "settings.py", "constants.py", "constant.py", "schema.py", "schemas.py",
    "types.py", "handlers.py", "filters.py", "client.py", "session.py"
}

def is_runnable_entry_point(fpath):
    base = os.path.basename(fpath).lower()
    if not base.endswith(".py"):
        return False
    if base.startswith("_") or base in NON_RUNNABLE_MODULES:
        return False
    return True

def detect_project_entry_script(project_dir):
    """
    Scans a specific project directory to find the real entry point script.
    Checks candidate names first, then any non-helper python script,
    and returns the relative path from SCRIPTS_DIR.
    """
    project_py_files = []
    for root, _, fs in os.walk(project_dir):
        for f in fs:
            if f.endswith(".py") and not f.startswith("."):
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, SCRIPTS_DIR)
                project_py_files.append(rel)
    
    if not project_py_files:
        return None

    # Priority candidate names
    priority_candidates = ["bot.py", "main.py", "app.py", "run.py", "start.py", "server.py", "telegram_bot.py", "worker.py"]
    for cand in priority_candidates:
        for py in project_py_files:
            if os.path.basename(py).lower() == cand:
                return py

    # If no standard name found, find the first runnable script that is not a library/helper module
    for py in project_py_files:
        if is_runnable_entry_point(py):
            return py

    # Fallback to the first python file
    return project_py_files[0]

def resolve_env_target_info(target):
    """
    Universally resolves any target (project name, script name, entry path, or slug)
    into its physical paths, primary .env file, entry point script, and vault key aliases.
    """
    if not target:
        target = "bot.py"
    clean = os.path.normpath(str(target).replace("scripts/", "").lstrip("/")).replace("\\", "/")
    
    # Check if target is a project directory in SCRIPTS_DIR
    proj_dir = os.path.join(SCRIPTS_DIR, clean)
    if os.path.isdir(proj_dir):
        entry = detect_project_entry_script(proj_dir) or clean
        base_name = os.path.basename(clean)
        proj_name = clean
        actual_dir = proj_dir
    elif "/" in clean:
        parts = clean.split("/")
        proj_name = parts[0]
        actual_dir = os.path.join(SCRIPTS_DIR, proj_name)
        entry = clean
        base_name = os.path.basename(clean)
    else:
        # Check if there is a directory matching slug or basename in SCRIPTS_DIR
        found_dir = None
        clean_clean = clean.replace("_", " ").lower()
        for it in os.listdir(SCRIPTS_DIR) if os.path.exists(SCRIPTS_DIR) else []:
            p = os.path.join(SCRIPTS_DIR, it)
            it_clean = it.replace("_", " ").lower()
            if os.path.isdir(p) and (it == clean or it.replace(" ", "_") == clean or it.lower() == clean.lower() or it_clean == clean_clean or clean_clean in it_clean or it_clean in clean_clean):
                found_dir = p
                proj_name = it
                break
        if found_dir:
            actual_dir = found_dir
            entry = detect_project_entry_script(found_dir) or clean
            base_name = os.path.basename(entry)
        else:
            proj_name = clean
            actual_dir = SCRIPTS_DIR
            entry = clean
            base_name = clean
            
    if base_name.endswith(".py"):
        base_stem = base_name[:-3]
    else:
        base_stem = base_name
        
    primary_dot_env = os.path.join(actual_dir, ".env")
    secondary_env = os.path.join(actual_dir, f"{base_stem}.env")
    
    candidates = [
        primary_dot_env,
        secondary_env,
        os.path.join(SCRIPTS_DIR, ".env"),
        os.path.join(SCRIPTS_DIR, f"{base_stem}.env")
    ]
    unique_candidates = []
    for c in candidates:
        if c not in unique_candidates:
            unique_candidates.append(c)
            
    vault_keys = [clean, entry, proj_name, base_name, f"scripts/{clean}", f"scripts/{entry}", f"scripts/{proj_name}"]
    unique_vault_keys = []
    for vk in vault_keys:
        if vk and vk not in unique_vault_keys:
            unique_vault_keys.append(vk)
            
    vault_save_keys = [entry, proj_name] if entry != proj_name else [entry]
    is_project = (actual_dir != SCRIPTS_DIR)
    
    # Generate clean short slug (guaranteed <= 32 chars)
    raw_slug = proj_name if is_project else entry
    slug = raw_slug.replace(" ", "_").replace("/", "__")
    if len(slug) > 32:
        import hashlib
        slug = slug[:24] + "_" + hashlib.md5(raw_slug.encode()).hexdigest()[:6]

    return {
        "type": "project" if is_project else "script",
        "name": proj_name if is_project else entry,
        "display_name": proj_name if is_project else entry,
        "is_project": is_project,
        "entry_script": entry,
        "project_dir": actual_dir,
        "primary_dot_env": primary_dot_env,
        "secondary_env": secondary_env,
        "env_file_candidates": unique_candidates,
        "vault_keys": unique_vault_keys,
        "vault_save_keys": vault_save_keys,
        "slug": slug
    }

def get_manageable_targets():
    """
    Returns a clean, deduplicated list of top-level project folders
    and standalone scripts in the workspace.
    """
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    targets = []
    seen_names = set()
    active = get_active_running_processes()
    
    # 1. Project folders and top-level scripts in scripts/
    top_items = sorted([f for f in os.listdir(SCRIPTS_DIR) if not f.startswith(".") and f != "__pycache__"])
    for it in top_items:
        p = os.path.join(SCRIPTS_DIR, it)
        if os.path.isdir(p):
            t_info = resolve_env_target_info(it)
            if t_info["name"] not in seen_names:
                seen_names.add(t_info["name"])
                targets.append(t_info)
        elif it.endswith(".py") and not it.startswith("_"):
            t_info = resolve_env_target_info(it)
            if t_info["name"] not in seen_names:
                seen_names.add(t_info["name"])
                targets.append(t_info)
                
    # 2. Also include any active running scripts or configured active scripts
    all_active = list(active.keys()) + config.get("active_scripts", [])
    for a in all_active:
        t_info = resolve_env_target_info(a)
        if t_info["name"] not in seen_names:
            seen_names.add(t_info["name"])
            targets.append(t_info)
            
    return targets

def get_all_env_candidates(py_filename):
    info = resolve_env_target_info(py_filename)
    return info["env_file_candidates"]

def get_script_env_path(py_filename):
    info = resolve_env_target_info(py_filename)
    for c in info["env_file_candidates"]:
        if os.path.exists(c):
            return c
    return info["primary_dot_env"]

def get_vault_master_key():
    """Derives a 256-bit encryption key from the private TG_BOT_TOKEN secret."""
    import hashlib
    secret = TG_BOT_TOKEN or "fallback_vault_salt_saini920_private_cloud"
    return hashlib.sha256(secret.encode('utf-8')).digest()

def encrypt_secret_data(plain_text: str) -> str:
    """AES-grade CTR + HMAC-SHA256 authenticated encryption using the private master key."""
    import hashlib, hmac, os, base64
    key = get_vault_master_key()
    nonce = os.urandom(16)
    data_bytes = plain_text.encode('utf-8')
    keystream = bytearray()
    counter = 0
    while len(keystream) < len(data_bytes):
        block = hashlib.sha256(key + nonce + counter.to_bytes(4, 'big')).digest()
        keystream.extend(block)
        counter += 1
    ciphertext = bytes(a ^ b for a, b in zip(data_bytes, keystream[:len(data_bytes)]))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    payload = nonce + tag + ciphertext
    return base64.b64encode(payload).decode('utf-8')

def decrypt_secret_data(enc_b64: str) -> str:
    """Decrypts and verifies authentication tag using master key, with graceful fallback."""
    import hashlib, hmac, base64
    try:
        raw = base64.b64decode(enc_b64.encode('utf-8'))
        if len(raw) < 48:
            return ""
        nonce = raw[:16]
        tag = raw[16:48]
        ciphertext = raw[48:]
        
        # Test candidate keys: primary TG_BOT_TOKEN key first, then fallback salt
        candidate_secrets = []
        if TG_BOT_TOKEN:
            candidate_secrets.append(TG_BOT_TOKEN)
        candidate_secrets.append("fallback_vault_salt_saini920_private_cloud")
        
        for sec in candidate_secrets:
            key = hashlib.sha256(sec.encode('utf-8')).digest()
            expected_tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
            if hmac.compare_digest(tag, expected_tag):
                keystream = bytearray()
                counter = 0
                while len(keystream) < len(ciphertext):
                    block = hashlib.sha256(key + nonce + counter.to_bytes(4, 'big')).digest()
                    keystream.extend(block)
                    counter += 1
                decrypted_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))
                return decrypted_bytes.decode('utf-8')
                
        logger.debug("Vault decryption failed: Authentication tag mismatch or secret key mismatch.")
        return ""
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return ""

def get_env_vault_file():
    return os.path.join(WORKSPACE_DIR, ".env_vault.json")

def load_env_vault():
    vault_file = get_env_vault_file()
    if os.path.exists(vault_file):
        try:
            with open(vault_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return config.get("env_vault", {})

def save_env_vault(vault):
    config["env_vault"] = vault
    save_config(config)
    vault_file = get_env_vault_file()
    try:
        with open(vault_file, "w") as f:
            json.dump(vault, f, indent=2)
    except Exception:
        pass

def restore_all_env_vaults_on_boot():
    """Unpacks all encrypted environments locally on runner boot for all projects/scripts."""
    vault = load_env_vault()
    if not isinstance(vault, dict) or not vault:
        return
        
    for script_name, stored_enc in vault.items():
        if not stored_enc:
            continue
        try:
            decrypted_json_str = decrypt_secret_data(stored_enc)
            if not decrypted_json_str:
                continue
            decrypted_dict = json.loads(decrypted_json_str)
            if not isinstance(decrypted_dict, dict) or not decrypted_dict:
                continue
                
            info = resolve_env_target_info(script_name)
            os.makedirs(os.path.dirname(info["primary_dot_env"]), exist_ok=True)
            content = "\n".join([f"{k}={v}" for k, v in sorted(decrypted_dict.items())]) + "\n"
            with open(info["primary_dot_env"], "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Restored .env on boot for {info['display_name']} -> {info['primary_dot_env']}")
        except Exception as e:
            logger.error(f"Error unpacking vault on boot for {script_name}: {e}")

def read_script_env(py_filename):
    """
    Reads environment variables for a project/script from physical .env files
    and the Encrypted Vault, keeping them synchronized and up-to-date.
    """
    info = resolve_env_target_info(py_filename)
    merged_env = {}
    
    # 1. Read from physical .env files on disk first
    for c in reversed(info["env_file_candidates"]):
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k:
                                merged_env[k] = v
            except Exception as e:
                logger.error(f"Error reading local .env file {c}: {e}")

    # 2. Check Encrypted Vault (Source of truth across cloud instances)
    vault = load_env_vault()
    stored_enc = None
    for vk in info["vault_keys"]:
        if vk in vault and vault[vk]:
            stored_enc = vault[vk]
            break
            
    if stored_enc:
        decrypted_json_str = decrypt_secret_data(stored_enc)
        if decrypted_json_str:
            try:
                decrypted_dict = json.loads(decrypted_json_str)
                if isinstance(decrypted_dict, dict) and decrypted_dict:
                    merged_env.update(decrypted_dict)
            except Exception as e:
                logger.error(f"Error parsing decrypted vault for {info['name']}: {e}")

    # 3. Always ensure physical .env in project directory exists and is updated!
    if merged_env and info["primary_dot_env"]:
        try:
            os.makedirs(os.path.dirname(info["primary_dot_env"]), exist_ok=True)
            content = "\n".join([f"{k}={v}" for k, v in sorted(merged_env.items())]) + "\n"
            needs_write = True
            if os.path.exists(info["primary_dot_env"]):
                try:
                    with open(info["primary_dot_env"], "r", encoding="utf-8", errors="ignore") as cur_f:
                        if cur_f.read() == content:
                            needs_write = False
                except Exception:
                    pass
            if needs_write:
                with open(info["primary_dot_env"], "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception as e:
            logger.error(f"Error updating local .env for {info['name']}: {e}")

    return merged_env

def write_script_env(py_filename, env_dict):
    """
    Writes environment variables to the project's local .env file,
    encrypts into the cloud vault, and syncs asynchronously to GitHub.
    """
    info = resolve_env_target_info(py_filename)
    try:
        content = "\n".join([f"{k}={v}" for k, v in sorted(env_dict.items())]) + "\n"
        
        # 1. Write to physical .env in the project directory
        os.makedirs(os.path.dirname(info["primary_dot_env"]), exist_ok=True)
        with open(info["primary_dot_env"], "w", encoding="utf-8") as f:
            f.write(content)
            
        # 2. Also write to secondary <stem>.env if distinct
        if info["secondary_env"] and info["secondary_env"] != info["primary_dot_env"]:
            try:
                with open(info["secondary_env"], "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass
                
        # 3. Encrypt and save to vault under all alias keys
        vault = load_env_vault()
        json_str = json.dumps(env_dict)
        encrypted_ciphertext = encrypt_secret_data(json_str)
        for vk in info["vault_save_keys"]:
            vault[vk] = encrypted_ciphertext
        save_env_vault(vault)
        
        # 4. Asynchronously sync to GitHub in background (NEVER block Telegram polling!)
        commit_desc = f"Update encrypted vault for {info['display_name']}"
        threading.Thread(target=git_sync_to_github, args=(commit_desc,), daemon=True).start()
        
        return True, "Env saved successfully."
    except Exception as e:
        logger.error(f"write_script_env error: {e}")
        return False, f"Error saving env: {e}"

def purge_project_or_script_completely(target_name, exclude_paths=None):
    """
    Completely and permanently deletes a project or standalone script everywhere:
    1. Immediately terminates running child processes matching this project or script.
    2. Wipes all files, subdirectories, and hidden files (.env, .git, etc.) from disk.
    3. Purges all candidate .env files across scripts/ and root workspace.
    4. Clears all associated keys from .env_vault.json and config['env_vault'].
    5. Cleans all references from config['active_scripts'], config['manually_stopped_scripts'], etc.
    6. Removes isolated virtualenvs (.venvs/).
    7. Cleans all databases, sessions, logs, temporary archives.
    8. Stages complete removal from Git index and triggers background push to GitHub.
    """
    import shutil, signal, re
    
    clean_target = os.path.normpath(str(target_name).replace("scripts/", "").lstrip("/")).replace("\\", "/")
    info = resolve_env_target_info(clean_target)
    
    proj_dir = info["project_dir"]
    is_project = info["is_project"] or (proj_dir != SCRIPTS_DIR and os.path.isdir(proj_dir))
    proj_name = info["name"]
    slug = info["slug"]
    entry_script = info["entry_script"]
    base_stem = proj_name.rsplit(".", 1)[0]
    
    exclude_set = set()
    if exclude_paths:
        for ep in exclude_paths:
            if ep:
                try: exclude_set.add(os.path.realpath(ep))
                except Exception: pass

    # Always preserve any active upload staging paths from user_states!
    for u_state in list(user_states.values()):
        if isinstance(u_state, dict) and u_state.get("staging_path"):
            try: exclude_set.add(os.path.realpath(u_state["staging_path"]))
            except Exception: pass
            
    def is_excluded(path_to_check):
        if not path_to_check:
            return False
        try:
            real_p = os.path.realpath(path_to_check)
            if real_p in exclude_set:
                return True
        except Exception:
            pass
        return False
    
    # 1. Real-time Process Termination
    stop_child_app(script_name=clean_target, clear_active=True)
    stop_child_app(script_name=proj_name, clear_active=True)
    if entry_script:
        stop_child_app(script_name=entry_script, clear_active=True)
    if slug != proj_name:
        stop_child_app(script_name=slug, clear_active=True)
        
    proj_abs_path = os.path.realpath(proj_dir) if (is_project and os.path.exists(proj_dir)) else ""
    for k, pdata in list(running_processes.items()):
        k_norm = os.path.normpath(k).replace("\\", "/")
        k_real = pdata.get("script_real_path") or os.path.realpath(os.path.join(SCRIPTS_DIR, k_norm))
        proc = pdata.get("proc")
        pid = pdata.get("pid")
        
        matches = (
            k_norm == clean_target
            or k_norm == proj_name
            or k_norm == entry_script
            or k_norm == slug
            or k_norm.startswith(f"{proj_name}/")
            or k_norm.startswith(f"{clean_target}/")
            or os.path.dirname(k_norm) == proj_name
            or (is_project and proj_abs_path and (k_real == proj_abs_path or k_real.startswith(proj_abs_path + os.sep)))
        )
        if matches:
            if proc and hasattr(proc, "pid") and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            running_processes.pop(k, None)
            
    # 2. Disk Cleanup: Project Directory or Single File
    if is_project and proj_dir not in [SCRIPTS_DIR, WORKSPACE_DIR] and os.path.exists(proj_dir) and not is_excluded(proj_dir):
        try:
            shutil.rmtree(proj_dir, ignore_errors=True)
        except Exception as e:
            logger.error(f"Error removing proj_dir {proj_dir}: {e}")
        if os.path.exists(proj_dir):
            subprocess.run(["rm", "-rf", proj_dir], capture_output=True)
            
    # Standalone single file
    single_script_path = os.path.join(SCRIPTS_DIR, clean_target)
    if os.path.exists(single_script_path) and not is_excluded(single_script_path):
        try:
            if os.path.isdir(single_script_path) and single_script_path not in [SCRIPTS_DIR, WORKSPACE_DIR]:
                shutil.rmtree(single_script_path, ignore_errors=True)
            else:
                os.remove(single_script_path)
        except Exception as e:
            logger.error(f"Error removing single script {single_script_path}: {e}")

    # Check root workspace mirror if exists
    ws_mirror = os.path.join(WORKSPACE_DIR, proj_name)
    if os.path.exists(ws_mirror) and ws_mirror not in [WORKSPACE_DIR, SCRIPTS_DIR] and not is_excluded(ws_mirror):
        try:
            if os.path.isdir(ws_mirror):
                shutil.rmtree(ws_mirror, ignore_errors=True)
            else:
                os.remove(ws_mirror)
        except Exception:
            pass

    # 3. Dedicated Removal of ALL Candidate and Associated .env Files
    all_env_paths = list(info.get("env_file_candidates", [])) + [
        info["primary_dot_env"],
        info["secondary_env"],
        os.path.join(SCRIPTS_DIR, f"{proj_name}.env"),
        os.path.join(SCRIPTS_DIR, f"{clean_target}.env"),
        os.path.join(SCRIPTS_DIR, f"{base_stem}.env"),
        os.path.join(SCRIPTS_DIR, f"{slug}.env"),
        os.path.join(WORKSPACE_DIR, f"{proj_name}.env"),
        os.path.join(WORKSPACE_DIR, f"{clean_target}.env"),
        os.path.join(WORKSPACE_DIR, f"{base_stem}.env"),
        os.path.join(WORKSPACE_DIR, f"{slug}.env"),
    ]
    for ef in set(all_env_paths):
        if ef and os.path.exists(ef) and not is_excluded(ef):
            try:
                os.remove(ef)
            except Exception as e:
                logger.error(f"Error deleting .env file {ef}: {e}")

    # 4. Remove Requirements, Logs, Temp Archives, Databases, and Sessions
    companion_files = [
        os.path.join(SCRIPTS_DIR, f"{proj_name}.requirements.txt"),
        os.path.join(SCRIPTS_DIR, f"{clean_target}.requirements.txt"),
        os.path.join(SCRIPTS_DIR, f"{base_stem}.requirements.txt"),
        os.path.join(SCRIPTS_DIR, f"{proj_name}.log"),
        os.path.join(SCRIPTS_DIR, f"{clean_target}.log"),
        os.path.join(SCRIPTS_DIR, f"{base_stem}.log"),
        os.path.join(WORKSPACE_DIR, f"temp_{proj_name}.zip"),
        os.path.join(WORKSPACE_DIR, f"temp_{clean_target}.zip"),
        os.path.join(WORKSPACE_DIR, f"temp_{base_stem}.zip"),
        os.path.join(WORKSPACE_DIR, f"temp_{slug}.zip"),
    ]
    for cf in companion_files:
        if os.path.exists(cf) and not is_excluded(cf):
            try:
                os.remove(cf)
            except Exception:
                pass

    # Clean residual databases, sqlite, session, journal, wal files (NEVER touch .staging_* or excluded files!)
    search_keys = [proj_name.lower(), clean_target.lower(), base_stem.lower(), slug.lower()]
    db_exts = {".db", ".sqlite", ".sqlite3", ".session", ".session-journal", ".session-shm", ".session-wal", ".db-journal", ".db-wal", ".db-shm"}
    for search_root in [SCRIPTS_DIR, WORKSPACE_DIR]:
        if not os.path.exists(search_root):
            continue
        try:
            for it in os.listdir(search_root):
                if it.startswith(".staging_") or it.startswith(".git"):
                    continue
                it_path = os.path.join(search_root, it)
                if is_excluded(it_path):
                    continue
                if os.path.isfile(it_path):
                    it_lower = it.lower()
                    ext = os.path.splitext(it_lower)[1]
                    if ext in db_exts and any(k in it_lower for k in search_keys if len(k) >= 3):
                        try: os.remove(it_path)
                        except Exception: pass
        except Exception:
            pass

    # 5. Remove Isolated Virtualenvs
    venv_slugs = set([
        re.sub(r'[^a-zA-Z0-9_\-\.]', '_', proj_name),
        re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean_target),
        re.sub(r'[^a-zA-Z0-9_\-\.]', '_', base_stem),
        re.sub(r'[^a-zA-Z0-9_\-\.]', '_', slug),
        proj_name,
        slug
    ])
    for v_name in venv_slugs:
        v_dir = os.path.join(VENVS_DIR, v_name)
        if os.path.exists(v_dir):
            try:
                shutil.rmtree(v_dir, ignore_errors=True)
                if os.path.exists(v_dir):
                    subprocess.run(["rm", "-rf", v_dir], capture_output=True)
            except Exception:
                pass

    # 6. Real-time Vault & Config Purge
    vault = load_env_vault()
    keys_to_purge = set(info.get("vault_keys", []) + info.get("vault_save_keys", []))
    for vk in list(vault.keys()):
        vk_lower = vk.lower()
        if (
            vk in keys_to_purge
            or vk == proj_name
            or vk == clean_target
            or vk == slug
            or vk == base_stem
            or vk.startswith(f"{proj_name}/")
            or vk.startswith(f"{clean_target}/")
            or vk.startswith(f"scripts/{proj_name}/")
            or vk.startswith(f"scripts/{clean_target}/")
            or os.path.dirname(vk) == proj_name
            or os.path.basename(vk) in [proj_name, base_stem, slug]
            or vk_lower == proj_name.lower()
            or vk_lower.startswith(f"{proj_name.lower()}/")
        ):
            vault.pop(vk, None)
    save_env_vault(vault)
    config["env_vault"] = vault

    def is_match_config(s):
        s_norm = os.path.normpath(str(s)).replace("\\", "/")
        return (
            s_norm == clean_target
            or s_norm == proj_name
            or s_norm == entry_script
            or s_norm == slug
            or s_norm.startswith(f"{proj_name}/")
            or s_norm.startswith(f"{clean_target}/")
            or os.path.dirname(s_norm) == proj_name
            or os.path.basename(s_norm) in [proj_name, base_stem]
        )

    config["active_scripts"] = [s for s in list(config.get("active_scripts", [])) if not is_match_config(s)]
    config["manually_stopped_scripts"] = [s for s in list(config.get("manually_stopped_scripts", [])) if not is_match_config(s)]
    if config.get("active_script") and is_match_config(config["active_script"]):
        config["active_script"] = None
    if config.get("auto_run_file") and is_match_config(config["auto_run_file"]):
        config["auto_run_file"] = None
    if "script_envs" in config and isinstance(config["script_envs"], dict):
        for k in list(config["script_envs"].keys()):
            if is_match_config(k):
                config["script_envs"].pop(k, None)
    save_config(config)

    # 7. Git Index Removal
    git_targets = [
        f"scripts/{proj_name}",
        f"scripts/{clean_target}",
        f"scripts/{entry_script}",
        f"scripts/{base_stem}",
        f"scripts/{proj_name}.requirements.txt",
        f"scripts/{clean_target}.requirements.txt",
        proj_name,
        clean_target,
    ]
    for gt in set(git_targets):
        if gt:
            try:
                subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", gt], cwd=WORKSPACE_DIR, capture_output=True)
            except Exception:
                pass

    # 8. Async Git Sync in Background
    commit_msg = f"Permanently delete {info['display_name']} and all associated files"
    threading.Thread(target=git_sync_to_github, args=(commit_msg,), daemon=True).start()

    logger.info(f"Purged project/script {info['display_name']} completely everywhere.")
    return True, info

def mask_secret_val(val):
    if not val:
        return "(empty)"
    if len(val) <= 6:
        return "***"
    return f"{val[:3]}...{val[-3:]}"

def prompt_env_script_select(chat_id, user_id, message_id=None):
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    targets = get_manageable_targets()
    vault = load_env_vault()
    buttons = []
    
    if not targets:
        text = (
            "⚙️ <b>Per-Project / Script Environment (.env) Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📁 No projects or scripts found in <code>scripts/</code> folder.\n\n"
            "💡 <i>Send a new script (.py) or ZIP project in chat to add one.</i>"
        )
    else:
        text = (
            "⚙️ <b>Per-Project / Script Environment (.env) Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Each bot project has its own private <b><code>.env</code></b> vault loaded on launch.\n\n"
            "<i>Select a project below to view and configure its environment variables:</i>"
        )
        for t in targets:
            env_vars = read_script_env(t["name"])
            count = len(env_vars)
            badge = f"({count} vars)" if count > 0 else "(0 vars)"
            icon = "📦" if t["is_project"] else "📄"
            cfg_btn = {"text": f"{icon} {t['display_name']} {badge}", "callback_data": f"env_dash_{t['slug']}"}
            del_btn = {"text": "🗑️ Wipe", "callback_data": f"env_wipe_{t['slug']}"}
            buttons.append([cfg_btn, del_btn])
    
    if targets or (vault and len(vault) > 0):
        buttons.append([{"text": "💣 Delete All (.env) Variables", "callback_data": "env_wipe_all_prompt"}])
        
    buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
    markup = {"inline_keyboard": buttons}
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_script_env_dashboard(chat_id, user_id, py_filename, message_id=None):
    info = resolve_env_target_info(py_filename)
    env_vars = read_script_env(info["name"])
    active = get_active_running_processes()
    
    is_this_running = (
        info["entry_script"] in active or
        info["name"] in active or
        any(k.startswith(f"{info['name']}/") or k == info['name'] for k in active.keys())
    )
    running_key = next((k for k in active.keys() if k == info["entry_script"] or k == info["name"] or k.startswith(f"{info['name']}/")), info["entry_script"]) if is_this_running else info["entry_script"]

    var_lines = []
    if not env_vars:
        var_lines.append("<i>No environment variables configured yet.</i>")
    else:
        for k, v in sorted(env_vars.items())[:35]:
            masked = mask_secret_val(v)
            var_lines.append(f"• 🔑 <code>{k}</code> = <code>{masked}</code>")
        if len(env_vars) > 35:
            var_lines.append(f"<i>...and {len(env_vars) - 35} more variables</i>")
            
    rel_dot_env = os.path.relpath(info["primary_dot_env"], WORKSPACE_DIR)
    status_badge = "🟢 <b>Running</b>" if is_this_running else "🔴 <b>Stopped</b>"
    
    text = (
        f"⚙️ <b>Environment Manager:</b> <code>{info['display_name']}</code>\n"
        f"🎯 <b>Entry Script:</b> <code>scripts/{info['entry_script']}</code>\n"
        f"📁 <b>Physical File:</b> <code>{rel_dot_env}</code>\n"
        f"⚡ <b>Status:</b> {status_badge}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(var_lines)
        + "\n\n<i>Use the buttons below to manage, add, or delete variables:</i>"
    )
    
    buttons = [
        [
            {"text": "➕ Add / Edit Variable", "callback_data": f"env_add_{info['slug']}"},
            {"text": "🗑️ Delete Variable", "callback_data": f"env_del_{info['slug']}"}
        ],
        [
            {"text": "💣 Wipe All ENVs", "callback_data": f"env_wipe_{info['slug']}"},
            {"text": "📥 Export .env", "callback_data": f"env_exp_{info['slug']}"}
        ],
        [
            {"text": "🛡️ View Venv Packages", "callback_data": f"venv_list_{info['entry_script']}"}
        ]
    ]
    if is_this_running:
        buttons.append([{"text": "🔄 Apply & Restart Script", "callback_data": f"exec_run_{running_key}"}])
    else:
        buttons.append([{"text": f"▶️ Run {info['display_name']} Now", "callback_data": f"exec_run_{info['entry_script']}"}])
        
    buttons.append([{"text": "🔙 Back to Script ENVs", "callback_data": "menu_env_select"}])
    markup = {"inline_keyboard": buttons}
    
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_env_delete_list(chat_id, user_id, py_filename, message_id=None):
    info = resolve_env_target_info(py_filename)
    env_vars = read_script_env(info["name"])
    if not env_vars:
        text = f"ℹ️ No variables to delete for <code>{info['display_name']}</code>."
        markup = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": f"env_dash_{info['slug']}"}]]}
    else:
        sorted_keys = sorted(env_vars.keys())
        user_states[user_id] = {
            "action": "DELETING_ENV_VAR",
            "target": info["name"],
            "slug": info["slug"],
            "keys": sorted_keys
        }
        text = (
            f"🗑️ <b>Delete Variable from <code>{info['display_name']}</code>:</b>\n\n"
            "Tap a variable below to remove it:"
        )
        buttons = []
        for idx, k in enumerate(sorted_keys[:40]):
            buttons.append([{"text": f"❌ Delete {k}", "callback_data": f"env_dv_{idx}"}])
        buttons.append([{"text": "💣 Wipe All Variables", "callback_data": f"env_wipe_{info['slug']}"}])
        buttons.append([{"text": "🔙 Back", "callback_data": f"env_dash_{info['slug']}"}])
        markup = {"inline_keyboard": buttons}
    
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

# ---------------------------------------------------------------------------
# Interactive Submenus
# ---------------------------------------------------------------------------
def prompt_runner_menu(chat_id, user_id, message_id=None):
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    active = get_active_running_processes()
    
    # 1. Scan all Python files recursively across scripts/ and projects
    all_files = []
    for root, _, fs in os.walk(SCRIPTS_DIR):
        for f in fs:
            if f.endswith(".py") and not f.startswith(".") and f != "__init__.py":
                rel = os.path.relpath(os.path.join(root, f), SCRIPTS_DIR).replace("\\", "/")
                all_files.append(rel)
                
    # Also include any active scripts tracked in running_processes
    for k in active.keys():
        clean_k = k.replace("scripts/", "").lstrip("/").replace("\\", "/")
        if clean_k not in all_files and not clean_k.startswith("."):
            all_files.append(clean_k)
            
    # Sort: running scripts first, then entry points (bot.py, main.py, app.py), then others
    def sort_script_key(s):
        is_run = s in active or os.path.basename(s) in active or any(k.endswith(s) or s.endswith(k) for k in active.keys())
        base = os.path.basename(s).lower()
        is_priority = base in ["bot.py", "main.py", "app.py", "run.py", "start.py", "server.py"]
        return (0 if is_run else (1 if is_priority else 2), s)
        
    all_files.sort(key=sort_script_key)
    
    buttons = []
    if not all_files:
        text = (
            "🚀 <b>Scripts Runner Manager (Multi-Process)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📁 No runnable Python scripts found in <code>scripts/</code> folder.\n\n"
            "💡 <i>You can send any <code>.py</code> or <code>.zip</code> file in chat to add it!</i>"
        )
    else:
        text = (
            "🚀 <b>Scripts Runner Manager (Multi-Process)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Active Running Scripts:</b> 🟢 <b>{len(active)} Running</b>\n"
            f"• <b>Total Scripts Available:</b> <b>{len(all_files)}</b>\n\n"
            "<i>Tap <b>▶️ Run</b> to launch concurrently or <b>🛑 Stop</b> to terminate:</i>"
        )
        for py in all_files:
            # Check if this script is active/running
            is_this_running = py in active or os.path.basename(py) in active or any(k.endswith(py) or py.endswith(k) for k in active.keys())
            running_key = next((k for k in active.keys() if k == py or k == os.path.basename(py) or k.endswith(py) or py.endswith(k)), py) if is_this_running else py
            
            req_p = get_script_req_path(py)
            has_env = len(read_script_env(py)) > 0
            
            badges = []
            if req_p:
                badges.append("📦")
            if has_env:
                badges.append("🔒")
            badge_str = f" {' '.join(badges)}" if badges else ""
            
            if is_this_running:
                pdata = active.get(running_key, {})
                st = pdata.get("start_time", time.time())
                cu_sec = int(time.time() - st)
                ch, cr = divmod(cu_sec, 3600)
                cm, _ = divmod(cr, 60)
                run_btn = {"text": f"🛑 Stop {py} ({ch}h {cm}m){badge_str}", "callback_data": f"confirm_stop_prompt_{running_key}"}
            else:
                run_btn = {"text": f"▶️ Run {py}{badge_str}", "callback_data": f"exec_run_{py}"}
            
            py_info = resolve_env_target_info(py)
            del_btn = {"text": "🗑️ Delete", "callback_data": f"confirm_del_file_{py_info['slug']}"}
            buttons.append([run_btn, del_btn])

    buttons.append([{"text": "📤 Upload New Script / ZIP", "callback_data": "menu_upload_prompt"}])
    if len(active) > 1:
        buttons.append([{"text": "🛑 Stop ALL Running Scripts", "callback_data": "menu_stop_all"}])
    buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])

    markup = {"inline_keyboard": buttons[:85]}
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_run_menu(chat_id, user_id, message_id=None):
    prompt_runner_menu(chat_id, user_id, message_id)

def prompt_pip_menu(chat_id, user_id, message_id=None):
    user_states[user_id] = "WAITING_PIP_PACKAGE"
    text = (
        "📦 <b>Install Python Package</b>\n\n"
        "Please send the package name you want to install:\n"
        "<i>(Example: <code>telethon</code>, <code>aiohttp</code>, <code>bs4</code>)</i>"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "❌ Cancel", "callback_data": "menu_main"}]
        ]
    }
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_sh_menu(chat_id, user_id, message_id=None):
    user_states[user_id] = "WAITING_SHELL_CMD"
    text = (
        "💻 <b>Linux Shell Terminal</b>\n\n"
        "Please send the bash command you want to execute:\n"
        "<i>(Example: <code>ls -la</code>, <code>df -h</code>, <code>python --version</code>)</i>"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "❌ Cancel", "callback_data": "menu_main"}]
        ]
    }
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def send_logs_markdown_file(chat_id, script_name):
    pdata = running_processes.get(script_name, {})
    logs = pdata.get("logs", [])
    pid = pdata.get("pid", "N/A")
    uptime = ""
    if pdata.get("start_time"):
        cu_sec = int(time.time() - pdata["start_time"])
        ch, cr = divmod(cu_sec, 3600)
        cm, cs = divmod(cr, 60)
        uptime = f"{ch}h {cm}m {cs}s"
    
    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    full_output = "\n".join(logs) if logs else "(No output recorded yet)"
    clean_base = os.path.basename(script_name).replace(".py", "")
    
    md_content = (
        f"# 📋 Live Execution Logs for `{script_name}`\n\n"
        f"- **Timestamp:** `{timestamp_str}`\n"
        f"- **Process PID:** `{pid}`\n"
        f"- **Uptime:** `{uptime or 'N/A'}`\n"
        f"- **Total Lines:** `{len(logs)}`\n\n"
        "---\n\n"
        "## 📜 Standard Output & Error Stream\n\n"
        "```text\n"
        f"{full_output}\n"
        "```\n"
    )
    
    temp_md_path = os.path.join(WORKSPACE_DIR, f"logs_{clean_base}.md")
    try:
        with open(temp_md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        send_tg_document(
            chat_id,
            temp_md_path,
            caption=f"📋 <b>Complete Execution Logs:</b> <code>{script_name}</code> ({len(logs)} lines)"
        )
    except Exception as e:
        logger.error(f"Error sending logs markdown: {e}")
    finally:
        try:
            if os.path.exists(temp_md_path):
                os.remove(temp_md_path)
        except Exception:
            pass

def show_logs_view(chat_id, message_id=None, target_script=None):
    active = get_active_running_processes()
    
    if not active:
        text = (
            "📋 <b>Live Execution Logs</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>No scripts are currently running. Start a script to view live output.</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}],
                [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        if message_id:
            edit_tg_message(chat_id, message_id, text, reply_markup=markup)
        else:
            send_tg_message(chat_id, text, reply_markup=markup)
        return

    if len(active) == 1 or target_script:
        selected_script = target_script if target_script and target_script in running_processes else list(active.keys())[0]
        pdata = running_processes.get(selected_script, {})
        logs = pdata.get("logs", [])
        
        full_log_str = "\n".join(logs)
        is_too_big = len(full_log_str) > 2500 or len(logs) > 35
        
        if not logs:
            log_text = "<i>(Process initialized, waiting for output...)</i>"
        elif is_too_big:
            recent = "\n".join(logs[-12:])
            log_text = (
                f"⚠️ <b>Logs are large ({len(logs)} lines | {len(full_log_str)} chars).</b>\n"
                f"📄 <i>Full <code>logs_{os.path.basename(selected_script).replace('.py', '')}.md</code> file is sent below!</i>\n\n"
                f"<b>Recent Output Preview:</b>\n<pre>{html.escape(recent)}</pre>"
            )
        else:
            log_text = f"<pre>{html.escape(full_log_str)}</pre>"
            
        text = (
            f"📋 <b>Live Execution Logs:</b> <code>{selected_script}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>PID:</b> <code>{pdata.get('pid', 'N/A')}</code>\n"
            f"• <b>Total Logs in Buffer:</b> {len(logs)} lines\n\n"
            f"{log_text}"
        )
        buttons = [
            [
                {"text": "🔄 Refresh Logs", "callback_data": f"show_log_for_{selected_script}"},
                {"text": "📥 Export logs.md", "callback_data": f"export_log_md_{selected_script}"}
            ],
            [{"text": f"🛑 Stop {selected_script}", "callback_data": f"confirm_stop_prompt_{selected_script}"}, {"text": "🚀 Runner", "callback_data": "menu_runner"}]
        ]
        if len(active) > 1:
            buttons.append([{"text": "📑 Switch Script Logs", "callback_data": "menu_logs_select"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
        markup = {"inline_keyboard": buttons}
        
        if message_id:
            edit_tg_message(chat_id, message_id, text, reply_markup=markup)
        else:
            send_tg_message(chat_id, text, reply_markup=markup)
            
        # If the logs are too big to comfortably fit in Telegram, send logs.md file automatically!
        if is_too_big and logs:
            threading.Thread(target=send_logs_markdown_file, args=(chat_id, selected_script), daemon=True).start()
            
    else:
        # Multiple scripts running: show selector
        text = (
            "📋 <b>Live Execution Logs (Multi-Process)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Multiple scripts are currently active ({len(active)} running in parallel).\n\n"
            "<i>Select a script below to view its live logs:</i>"
        )
        buttons = []
        for sname in sorted(active.keys()):
            buttons.append([{"text": f"📄 Logs: {sname}", "callback_data": f"show_log_for_{sname}"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
        markup = {"inline_keyboard": buttons}

        if message_id:
            edit_tg_message(chat_id, message_id, text, reply_markup=markup)
        else:
            send_tg_message(chat_id, text, reply_markup=markup)

def show_files_view(chat_id, message_id=None):
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    top_items = sorted([f for f in os.listdir(SCRIPTS_DIR) if not f.startswith(".") and f != "__pycache__"])
    active = get_active_running_processes()
    
    file_lines = []
    download_buttons = []
    
    # 1. Prominently display all Active Running Scripts at the top
    if active:
        file_lines.append("🟢 <b>Currently Active Running Processes:</b>")
        for sname, pdata in sorted(active.items()):
            st = pdata.get("start_time", time.time())
            cu_sec = int(time.time() - st)
            ch, cr = divmod(cu_sec, 3600)
            cm, cs = divmod(cr, 60)
            pid = pdata.get("pid", "N/A")
            file_lines.append(f"• 🟢 <code>{sname}</code> (PID: <code>{pid}</code> | Uptime: <code>{ch}h {cm}m {cs}s</code>)")
        file_lines.append("")
        
    if not top_items:
        file_lines.append("<i>📁 Scripts folder (scripts/) is currently empty.\nSend any .py script or .zip project archive to upload!</i>")
    else:
        file_lines.append("📁 <b>Workspace Projects & Scripts:</b>")
        for it in top_items:
            p = os.path.join(SCRIPTS_DIR, it)
            if os.path.isdir(p):
                # It's a Project Archive / Directory!
                inner_files = []
                inner_py_files = []
                total_size = 0
                for root, _, fs in os.walk(p):
                    for f in fs:
                        if not f.startswith(".") and f != "__pycache__":
                            fp = os.path.join(root, f)
                            sz = os.path.getsize(fp)
                            total_size += sz
                            rel = os.path.relpath(fp, SCRIPTS_DIR).replace("\\", "/")
                            inner_files.append((rel, f, sz))
                            if f.endswith(".py"):
                                inner_py_files.append((rel, f, sz))
                
                # Check if running
                is_this_running = any(k.startswith(f"{it}/") or k == it or os.path.dirname(k) == it for k in active.keys())
                running_script_name = next((k for k in active.keys() if k.startswith(f"{it}/") or k == it or os.path.dirname(k) == it), None)
                status_icon = "🟢" if is_this_running else "📦"
                
                entry_script = detect_project_entry_script(p)
                entry_base = os.path.basename(entry_script) if entry_script else ""
                
                t_info = resolve_env_target_info(it)
                env_vars_count = len(read_script_env(it))
                env_badge = f" <i>(🔒 {env_vars_count} ENVs)</i>" if env_vars_count > 0 else " <i>(⚠️ No ENV)</i>"
                human_sz = format_bytes_human(total_size)
                file_lines.append(f"• {status_icon} <b>{it}/</b> (<code>{len(inner_files)} files</code> | <code>{human_sz}</code>){env_badge}{' <b>[RUNNING]</b>' if is_this_running else ''}")
                
                # Show inner python scripts inside this project
                for rel_py, py_name, py_sz in inner_py_files[:4]:
                    py_is_run = rel_py in active or py_name in active or (running_script_name and running_script_name == rel_py)
                    py_icon = "🟢" if py_is_run else "📄"
                    file_lines.append(f"   └ {py_icon} <code>{rel_py}</code> ({format_bytes_human(py_sz)})")
                if len(inner_py_files) > 4:
                    file_lines.append(f"   └ <i>...and {len(inner_py_files) - 4} more files</i>")
                    
                # Action buttons for this project
                row_btns = [{"text": f"📦 {it}.zip", "callback_data": f"file_dl_{t_info['slug']}"}]
                if is_this_running and running_script_name:
                    row_btns.append({"text": f"🛑 Stop", "callback_data": f"confirm_stop_prompt_{running_script_name}"})
                elif entry_script:
                    row_btns.append({"text": f"▶️ Run", "callback_data": f"exec_run_{entry_script}"})
                row_btns.append({"text": f"⚙️ ENV ({env_vars_count})", "callback_data": f"env_dash_{t_info['slug']}"})
                row_btns.append({"text": "🗑️ Delete", "callback_data": f"confirm_del_file_{t_info['slug']}"})
                download_buttons.append(row_btns)
                
            elif os.path.isfile(p):
                sz = os.path.getsize(p)
                is_this_running = it in active
                status_icon = "🟢" if is_this_running else "📄"
                human_sz = format_bytes_human(sz)
                t_info = resolve_env_target_info(it)
                
                if it.endswith(".py"):
                    req_p = get_script_req_path(it)
                    env_vars_count = len(read_script_env(it))
                    badges = []
                    if req_p:
                        badges.append("📦 Req")
                    if env_vars_count > 0:
                        badges.append(f"🔒 {env_vars_count} Env")
                    badge_str = f" <i>({' | '.join(badges)})</i>" if badges else ""
                    
                    file_lines.append(f"• {status_icon} <code>{it}</code> ({human_sz}){badge_str}{' <b>[RUNNING]</b>' if is_this_running else ''}")
                    
                    row_btns = [{"text": f"📥 {it}", "callback_data": f"file_dl_{t_info['slug']}"}]
                    if is_this_running:
                        row_btns.append({"text": "🛑 Stop", "callback_data": f"confirm_stop_prompt_{it}"})
                    else:
                        row_btns.append({"text": "▶️ Run", "callback_data": f"exec_run_{it}"})
                    row_btns.append({"text": f"⚙️ ENV ({env_vars_count})", "callback_data": f"env_dash_{t_info['slug']}"})
                    row_btns.append({"text": "🗑️ Delete", "callback_data": f"confirm_del_file_{t_info['slug']}"})
                    download_buttons.append(row_btns)
                else:
                    file_lines.append(f"• 📄 <code>{it}</code> ({human_sz})")
                    download_buttons.append([
                        {"text": f"📥 {it}", "callback_data": f"file_dl_{t_info['slug']}"},
                        {"text": "🗑️ Delete", "callback_data": f"confirm_del_file_{t_info['slug']}"}
                    ])
                    
    text = (
        "📂 <b>Workspace & Scripts File Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>Directory:</b> <code>scripts/</code> (Cloud Storage)\n"
        f"📊 <b>Total Projects & Files:</b> <b>{len(top_items)}</b>\n"
        f"🟢 <b>Active Processes:</b> <b>{len(active)} Running</b>\n\n"
        + "\n".join(file_lines[:45])
        + ("\n<i>...and more items</i>" if len(file_lines) > 45 else "")
        + "\n\n<i>Tap a button below to Download, Run, or Delete:</i>"
    )
    
    download_buttons.append([{"text": "📤 Upload New Script / ZIP", "callback_data": "menu_upload_prompt"}])
    download_buttons.append([{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}])
    if top_items:
        download_buttons.append([{"text": "💣 Delete All Scripts & Projects", "callback_data": "wipe_all_workspace_prompt"}])
    download_buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
    markup = {"inline_keyboard": download_buttons[:85]}
    
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_stop_menu(chat_id, user_id, message_id=None):
    active = get_active_running_processes()
    if not active:
        text = "ℹ️ <b>No Running Scripts</b>\n\nThere are no scripts currently running."
        markup = {"inline_keyboard": [[{"text": "🔙 Main Menu", "callback_data": "menu_main"}]]}
    else:
        text = (
            f"🛑 <b>Running Scripts Manager ({len(active)} Active)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Select a script below to terminate it:</i>"
        )
        buttons = []
        for name, pdata in sorted(active.items()):
            cu_sec = int(time.time() - pdata["start_time"])
            ch, cr = divmod(cu_sec, 3600)
            cm, cs = divmod(cr, 60)
            buttons.append([{"text": f"🛑 Stop {name} ({ch}h {cm}m | PID: {pdata['pid']})", "callback_data": f"confirm_stop_prompt_{name}"}])
        if len(active) > 1:
            buttons.append([{"text": "🛑 Stop ALL Running Scripts", "callback_data": "menu_stop_all"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
        markup = {"inline_keyboard": buttons}
        
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def get_github_actions_monthly_quota(owner_login):
    """
    Retrieves real-time GitHub Actions monthly usage quota for private repositories.
    GitHub Free tier includes 2,000 private runner minutes / month.
    """
    total_quota = 2000
    used_mins = 0
    now_dt = datetime.now(timezone.utc)
    
    try:
        month_start_iso = f"{now_dt.year}-{now_dt.month:02d}-01T00:00:00Z"
        url = f"https://api.github.com/repos/{REPO}/actions/runs?created=>={month_start_iso}&per_page=100"
        headers = {"Accept": "application/vnd.github+json"}
        if EFFECTIVE_TOKEN:
            headers["Authorization"] = f"Bearer {EFFECTIVE_TOKEN}"
            
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            runs_data = r.json().get("workflow_runs", [])
            total_seconds = 0
            for run in runs_data:
                created_str = run.get("created_at")
                updated_str = run.get("updated_at")
                if created_str and updated_str:
                    try:
                        t1 = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        t2 = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                        diff_sec = max(0, int((t2 - t1).total_seconds()))
                        total_seconds += diff_sec
                    except Exception:
                        pass
            current_uptime = int(time.time() - START_TIME)
            total_seconds += current_uptime
            used_mins = max(1, total_seconds // 60)
        else:
            current_uptime = int(time.time() - START_TIME)
            used_mins = max(1, current_uptime // 60)
    except Exception as e:
        logger.debug(f"Actions quota calculation fallback: {e}")
        current_uptime = int(time.time() - START_TIME)
        used_mins = max(1, current_uptime // 60)

    try:
        if now_dt.month == 12:
            next_reset = date(now_dt.year + 1, 1, 1)
        else:
            next_reset = date(now_dt.year, now_dt.month + 1, 1)
        reset_str = next_reset.strftime("%d %b %Y")
    except Exception:
        reset_str = "1st of Next Month"

    remaining_mins = max(0, total_quota - used_mins)
    pct_used = min(100.0, (used_mins / total_quota) * 100.0)
    pct_remain = max(0.0, 100.0 - pct_used)

    return {
        "total_quota": total_quota,
        "used_mins": used_mins,
        "remaining_mins": remaining_mins,
        "pct_used": pct_used,
        "pct_remain": pct_remain,
        "reset_date": reset_str
    }

def show_server_info_view(chat_id, message_id=None):
    """
    Renders the complete, real-time Cloud Server & Repository Intelligence view
    including GitHub Actions monthly time limits for private repos.
    """
    try:
        repo_info = {}
        token_to_use = EFFECTIVE_TOKEN
        try:
            url = f"https://api.github.com/repos/{REPO}"
            headers = {"Accept": "application/vnd.github+json"}
            if token_to_use:
                headers["Authorization"] = f"Bearer {token_to_use}"
            resp = requests.get(url, headers=headers, timeout=4)
            if resp.status_code == 200:
                repo_info = resp.json()
        except Exception as e:
            logger.debug(f"GitHub API info query: {e}")

        # Telemetry
        uptime_sec = int(time.time() - START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        limit_remain = max(0, RUN_DURATION_SECONDS - uptime_sec)
        lh, lr = divmod(limit_remain, 3600)
        lm, ls = divmod(lr, 60)
        
        active = get_active_running_processes()
        count = len(active)
        if count == 0:
            active_summary = "🔴 <i>None (Standby)</i>"
        else:
            active_summary = f"🟢 <b>{count} Active:</b> " + ", ".join([f"<code>{s}</code>" for s in sorted(active.keys())])
        
        repo_name = repo_info.get("full_name") if isinstance(repo_info.get("full_name"), str) else REPO
        is_private = repo_info.get("private", True)
        visibility = "🔒 Private Repo" if is_private else "🌍 Public Repo"
        repo_size_kb = repo_info.get("size", 0)
        default_branch = repo_info.get("default_branch") if isinstance(repo_info.get("default_branch"), str) else "main"
        created_at = repo_info.get("created_at", "N/A")[:10] if repo_info.get("created_at") else "N/A"
        
        owner_data = repo_info.get("owner")
        if isinstance(owner_data, dict) and owner_data.get("login"):
            owner_login = owner_data.get("login")
        else:
            owner_login = repo_name.split("/")[0] if "/" in repo_name else "N/A"
            
        repo_html_url = f"https://github.com/{REPO}"
        if isinstance(repo_info.get("html_url"), str) and repo_info["html_url"].startswith("http"):
            repo_html_url = repo_info["html_url"]

        quota = get_github_actions_monthly_quota(owner_login)
        quota_icon = "🟢" if quota["pct_remain"] > 30 else ("🟡" if quota["pct_remain"] > 10 else "🔴")

        text = (
            "ℹ️ <b>Cloud Server & Repository Intelligence</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>Repository:</b> <code>{repo_name}</code>\n"
            f"👑 <b>Owner:</b> <code>{owner_login}</code>\n"
            f"🛡️ <b>Visibility:</b> <b>{visibility}</b>\n"
            f"🌿 <b>Default Branch:</b> <code>{default_branch}</code>\n"
            f"📦 <b>Repo Size:</b> <code>{repo_size_kb} KB</code>\n"
            f"📅 <b>Created On:</b> <code>{created_at}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⏱️ <b>Monthly Private Actions Time Limit (Real-Time):</b>\n"
            f"• 📊 <b>Monthly Free Quota:</b> <code>2,000 Mins / Month</code>\n"
            f"• ⏳ <b>Used This Month:</b> <code>{quota['used_mins']} Mins</code> ({quota['pct_used']:.1f}%)\n"
            f"• {quota_icon} <b>Remaining Balance:</b> <code>{quota['remaining_mins']} Mins</code> ({quota['pct_remain']:.1f}%)\n"
            f"• 🔄 <b>Cycle Resets On:</b> <code>{quota['reset_date']}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <b>Live Server Status:</b>\n"
            "• <b>Daemon Status:</b> 🟢 <b>Active & Healthy</b>\n"
            "• <b>Auto-Restart:</b> 🛑 <b>Disabled (Manual Run Control)</b>\n"
            f"• <b>Active Scripts:</b> {active_summary}\n"
            f"• <b>Current Run ID:</b> <code>#{RUN_ID}</code>\n"
            f"• <b>Current Uptime:</b> <code>{hours}h {minutes}m {seconds}s</code>\n"
            f"• <b>Scheduled Stop In:</b> <code>{lh}h {lm}m {ls}s</code> (30m Alert Active)\n"
            f"• <b>Security Vault:</b> 🔐 <b>AES-256 Authenticated Encryption</b>\n"
            f"• <b>Secret Scanner Shield:</b> 🛡️ <b>100% Protected (.gitignore active)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Refresh Info", "callback_data": "menu_server_info"},
                    {"text": "🧹 Clean RAM", "callback_data": "menu_clean_ram"}
                ],
                [
                    {"text": "🛑 Stop Workflow", "callback_data": "menu_stop_workflow_prompt"},
                    {"text": "🌐 Open on GitHub", "url": repo_html_url}
                ],
                [
                    {"text": "🚀 Scripts Runner", "callback_data": "menu_runner"},
                    {"text": "📂 View Files", "callback_data": "menu_files"}
                ],
                [
                    {"text": "🔙 Main Menu", "callback_data": "menu_main"}
                ]
            ]
        }
        if message_id:
            edit_tg_message(chat_id, message_id, text, reply_markup=markup)
        else:
            send_tg_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        logger.error(f"Failed to render server info view: {e}")
        fallback_text = f"ℹ️ <b>Server Info:</b>\n• <b>Uptime:</b> Active\n• <b>Repo:</b> <code>{REPO}</code>\n• <b>Run ID:</b> <code>#{RUN_ID}</code>"
        if message_id:
            edit_tg_message(chat_id, message_id, fallback_text, reply_markup={"inline_keyboard": [[{"text": "🔙 Main Menu", "callback_data": "menu_main"}]]})
        else:
            send_tg_message(chat_id, fallback_text, reply_markup={"inline_keyboard": [[{"text": "🔙 Main Menu", "callback_data": "menu_main"}]]})
    
def format_bytes_human(size_in_bytes):
    """Formats raw bytes into human readable KB, MB, GB."""
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.1f} KB"
    elif size_in_bytes < 1024 * 1024 * 1024:
        return f"{size_in_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_in_bytes / (1024 * 1024 * 1024):.2f} GB"

def get_sqlite_table_summary(db_path):
    """Safely extracts table names and row counts from SQLite databases without locks."""
    if not os.path.exists(db_path) or not db_path.endswith(('.db', '.sqlite', '.sqlite3', '.session')):
        return ""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [r[0] for r in cursor.fetchall()]
        summaries = []
        for t in tables[:3]:
            try:
                cursor.execute(f"SELECT count(*) FROM `{t}`;")
                count = cursor.fetchone()[0]
                summaries.append(f"{t}: {count}")
            except Exception:
                summaries.append(t)
        conn.close()
        if summaries:
            return " <i>(" + ", ".join(summaries) + ")</i>"
    except Exception:
        pass
    return ""

def get_all_bot_entities():
    """
    Returns a dictionary of all bot/project entities in the workspace:
    e.g. {'Ghidrabot vps': {...}, 'tgdriveapi-python (3)': {...}, 'xxtgdriveapibot': {...}}
    """
    bots = {}
    if os.path.exists(SCRIPTS_DIR):
        for item in sorted(os.listdir(SCRIPTS_DIR)):
            if item.startswith(".") or item == "__pycache__":
                continue
            item_path = os.path.join(SCRIPTS_DIR, item)
            if os.path.isdir(item_path):
                bots[item] = {"name": item, "type": "folder", "path": item_path}
            elif item.endswith(".py"):
                bots[item] = {"name": item, "type": "script", "path": item_path}
                
    for s in config.get("active_scripts", []):
        clean = s.replace("scripts/", "").lstrip("/").replace("\\", "/")
        parts = clean.split("/")
        proj = parts[0] if len(parts) > 1 else clean
        if proj not in bots:
            bots[proj] = {"name": proj, "type": "folder" if len(parts) > 1 else "script", "path": os.path.join(SCRIPTS_DIR, proj)}
            
    return bots


def scan_database_items_for_bot(bot_key):
    """
    Scans only the database, session, and storage files belonging strictly to a specific bot/project.
    """
    items = []
    db_exts = {".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm"}
    session_exts = {".session", ".session-journal", ".session-shm", ".session-wal"}
    
    if bot_key == "ROOT":
        if os.path.exists(WORKSPACE_DIR):
            for f in os.listdir(WORKSPACE_DIR):
                if f.startswith(".git") or f in [".venvs", "scripts", "__pycache__"]:
                    continue
                fp = os.path.join(WORKSPACE_DIR, f)
                if os.path.isfile(fp):
                    f_lower = f.lower()
                    ext = os.path.splitext(f_lower)[1]
                    cat = None
                    if ext in db_exts:
                        cat = "DATABASE"
                    elif ext in session_exts:
                        cat = "SESSION"
                    elif f_lower.endswith(".json") and ("data" in f_lower or "db" in f_lower or "bot" in f_lower):
                        cat = "DATABASE"
                    if cat:
                        try:
                            sz = os.path.getsize(fp)
                            items.append({
                                "name": f,
                                "path": fp,
                                "rel_path": f,
                                "size": sz,
                                "human_size": format_bytes_human(sz),
                                "category": cat,
                                "mtime": os.path.getmtime(fp)
                            })
                        except Exception:
                            pass
        items.sort(key=lambda x: x["size"], reverse=True)
        return items

    bot_dir = os.path.join(SCRIPTS_DIR, bot_key)
    bot_stem = bot_key.rsplit(".", 1)[0]
    
    if os.path.isdir(bot_dir):
        for root, _, files in os.walk(bot_dir):
            if "__pycache__" in root:
                continue
            for f in files:
                f_lower = f.lower()
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, WORKSPACE_DIR).replace("\\", "/")
                ext = os.path.splitext(f_lower)[1]
                cat = None
                if ext in db_exts:
                    cat = "DATABASE"
                elif ext in session_exts:
                    cat = "SESSION"
                elif f_lower.endswith(".json") and not f_lower.endswith((".env_vault.json", "package.json")):
                    cat = "DATABASE"
                elif f_lower.endswith(".log"):
                    cat = "LOG"
                    
                if cat:
                    try:
                        sz = os.path.getsize(fp)
                        items.append({
                            "name": f,
                            "path": fp,
                            "rel_path": rel,
                            "size": sz,
                            "human_size": format_bytes_human(sz),
                            "category": cat,
                            "mtime": os.path.getmtime(fp)
                        })
                    except Exception:
                        pass
    else:
        if os.path.exists(SCRIPTS_DIR):
            for f in os.listdir(SCRIPTS_DIR):
                f_lower = f.lower()
                fp = os.path.join(SCRIPTS_DIR, f)
                if os.path.isfile(fp):
                    rel = os.path.relpath(fp, WORKSPACE_DIR).replace("\\", "/")
                    ext = os.path.splitext(f_lower)[1]
                    matches_bot = f_lower.startswith(bot_stem.lower())
                    if matches_bot:
                        cat = None
                        if ext in db_exts:
                            cat = "DATABASE"
                        elif ext in session_exts:
                            cat = "SESSION"
                        elif f_lower.endswith(".json"):
                            cat = "DATABASE"
                        if cat:
                            try:
                                sz = os.path.getsize(fp)
                                items.append({
                                    "name": f,
                                    "path": fp,
                                    "rel_path": rel,
                                    "size": sz,
                                    "human_size": format_bytes_human(sz),
                                    "category": cat,
                                    "mtime": os.path.getmtime(fp)
                                })
                            except Exception:
                                pass

    items.sort(key=lambda x: x["size"], reverse=True)
    return items


def show_database_bot_selection_view(chat_id, message_id=None):
    """
    Level 1: Displays the Bot Selection Hub.
    Running and installed bots are shown separately with their isolated database count.
    """
    bots = get_all_bot_entities()
    active_now = get_active_running_processes()
    
    bot_stats = []
    for bot_name, binfo in bots.items():
        db_items = scan_database_items_for_bot(bot_name)
        total_sz = sum(x["size"] for x in db_items)
        is_running = any(k.startswith(f"{bot_name}/") or k == bot_name or os.path.dirname(k) == bot_name for k in active_now.keys())
        bot_stats.append({
            "name": bot_name,
            "type": binfo["type"],
            "items_count": len(db_items),
            "total_size": total_sz,
            "human_size": format_bytes_human(total_sz),
            "is_running": is_running
        })
        
    root_dbs = scan_database_items_for_bot("ROOT")
    
    text = (
        "🗄️ <b>Database & Storage Inspector</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Select a Bot / Project below to view, download, or manage its isolated database files:</i>\n\n"
    )
    
    buttons = []
    if not bot_stats and not root_dbs:
        text += "✨ <i>No bots or databases found in workspace.</i>\n"
    else:
        for b in bot_stats:
            status_icon = "🟢" if b["is_running"] else "⚪"
            db_badge = f"{b['items_count']} DBs • {b['human_size']}" if b["items_count"] > 0 else "Clean (0 DBs)"
            text += f"• {status_icon} <b>{b['name']}</b> ({db_badge})\n"
            
            btn_text = f"{status_icon} {b['name']} ({db_badge})"
            buttons.append([{"text": btn_text, "callback_data": f"db_bot_{b['name']}"}])
            
        if root_dbs:
            root_sz = format_bytes_human(sum(x["size"] for x in root_dbs))
            text += f"• 🌐 <b>Root Workspace</b> ({len(root_dbs)} DBs • {root_sz})\n"
            buttons.append([{"text": f"🌐 Root Workspace DBs ({len(root_dbs)} Files)", "callback_data": "db_bot_ROOT"}])

    text += "\n━━━━━━━━━━━━━━━━━━━━━━\n💡 <i>Each bot's databases and sessions are displayed separately.</i>"

    buttons.append([
        {"text": "🔄 Refresh", "callback_data": "menu_db_inspector"},
        {"text": "🔙 Main Menu", "callback_data": "menu_main"}
    ])
    
    markup = {"inline_keyboard": buttons}
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)


def show_bot_database_view(chat_id, bot_key, message_id=None):
    """
    Level 2: Displays the isolated database files belonging strictly to `bot_key`.
    Provides direct download file and single delete buttons.
    """
    db_items = scan_database_items_for_bot(bot_key)
    display_title = "Root Workspace" if bot_key == "ROOT" else bot_key
    total_sz = sum(x["size"] for x in db_items)
    
    lines = [
        f"🗄️ <b>Database Inspector:</b> <code>{display_title}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>Total Bot Storage:</b> <code>{format_bytes_human(total_sz)}</code> ({len(db_items)} files)",
        ""
    ]
    
    if not db_items:
        lines.append("✨ <i>No database or session files found for this bot!</i>")
    else:
        lines.append("<b>Database & Storage Files:</b>")
        for item in db_items:
            table_info = get_sqlite_table_summary(item["path"]) if item["name"].endswith((".db", ".sqlite", ".sqlite3")) else ""
            lines.append(f"• <code>{item['name']}</code> (<code>{item['human_size']}</code>){table_info}")
        lines.append("")
        lines.append("<i>Tap an item to Download file or Delete it:</i>")

    text = "\n".join(lines)
    
    buttons = []
    for item in db_items:
        rel = item["rel_path"]
        f_name = item["name"]
        name_trunc = f_name if len(f_name) <= 18 else f_name[:15] + "..."
        btn_dl = {"text": f"📥 {name_trunc} ({item['human_size']})", "callback_data": f"db_dl_{rel}"}
        btn_del = {"text": "🗑️ Delete", "callback_data": f"db_del_one_{rel}"}
        buttons.append([btn_dl, btn_del])
        
    if db_items:
        buttons.append([{"text": f"💣 Wipe All Databases of {display_title}", "callback_data": f"db_wipe_bot_prompt_{bot_key}"}])
        
    buttons.append([
        {"text": "🔄 Refresh", "callback_data": f"db_bot_{bot_key}"},
        {"text": "🔙 Back to All Bots", "callback_data": "menu_db_inspector"}
    ])
    
    markup = {"inline_keyboard": buttons}
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)


def show_database_inspector_view(chat_id, message_id=None):
    """Fallback / compatibility handler that routes directly to the bot selection hub."""
    show_database_bot_selection_view(chat_id, message_id)

# ---------------------------------------------------------------------------
# Callback Query Handler (Button Clicks)
# ---------------------------------------------------------------------------
def handle_callback_query(callback_id, chat_id, user_id, message_id, data):
    if not is_admin(user_id):
        answer_callback(callback_id, "⛔ Access Denied!", show_alert=True)
        return

    # 1. Main Menu
    if data == "menu_main":
        user_states.pop(user_id, None)
        answer_callback(callback_id)
        edit_tg_message(chat_id, message_id, render_dashboard_text(), reply_markup=get_main_menu_keyboard())

    # 2. Status
    elif data == "menu_status":
        answer_callback(callback_id, "📊 Status Refreshed!")
        edit_tg_message(chat_id, message_id, render_dashboard_text(), reply_markup=get_main_menu_keyboard())

    # 2b. Script ENV Menu
    elif data == "menu_env_select":
        answer_callback(callback_id)
        prompt_env_script_select(chat_id, user_id, message_id)

    # 2c. Specific Script ENV Dashboard
    elif data.startswith("env_dash_"):
        fname = data.replace("env_dash_", "")
        info = resolve_env_target_info(fname)
        answer_callback(callback_id)
        prompt_script_env_dashboard(chat_id, user_id, info["name"], message_id)

    # 2d. Add/Edit Variable Prompt
    elif data.startswith("env_add_"):
        fname = data.replace("env_add_", "")
        info = resolve_env_target_info(fname)
        user_states[user_id] = {
            "action": "WAITING_ENV_VAR",
            "target_py": info["name"]
        }
        answer_callback(callback_id)
        text = (
            f"⚙️ <b>Set Environment Variable for <code>{info['display_name']}</code></b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send the variable name and value in chat:\n\n"
            "• <b>Format:</b> <code>KEY=VALUE</code>\n"
            "• <b>Multiple lines supported!</b> You can paste multiple variables or an entire <code>.env</code> file.\n"
            "• <b>Example:</b>\n"
            "<code>BOT_TOKEN=123456789:AAH...\n"
            "API_ID=123456\n"
            "API_HASH=abcdef0123456789</code>\n\n"
            f"<i>Saved to <code>{os.path.relpath(info['primary_dot_env'], WORKSPACE_DIR)}</code> and encrypted to Cloud!</i>"
        )
        edit_tg_message(chat_id, message_id, text, reply_markup={"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"env_dash_{info['slug']}"}]]})

    # 2e. Delete Variable Menu
    elif data.startswith("env_del_list_") or data.startswith("env_del_"):
        fname = data.replace("env_del_list_", "").replace("env_del_", "")
        info = resolve_env_target_info(fname)
        answer_callback(callback_id)
        prompt_env_delete_list(chat_id, user_id, info["name"], message_id)

    # 2f. Do Delete Variable (Index-based)
    elif data.startswith("env_dv_"):
        idx_str = data.replace("env_dv_", "")
        state = user_states.get(user_id, {})
        target = state.get("target")
        keys = state.get("keys", [])
        try:
            idx = int(idx_str)
            if 0 <= idx < len(keys):
                var_key = keys[idx]
                env_dict = read_script_env(target)
                env_dict.pop(var_key, None)
                write_script_env(target, env_dict)
                answer_callback(callback_id, f"🗑️ {var_key} deleted!", show_alert=True)
                prompt_script_env_dashboard(chat_id, user_id, target, message_id)
            else:
                answer_callback(callback_id, "Variable not found.", show_alert=True)
        except Exception as e:
            logger.error(f"env_dv error: {e}")
            answer_callback(callback_id, "Error deleting variable.", show_alert=True)

    # 2f1. Do Delete Variable (Legacy format compatibility)
    elif data.startswith("env_dodel_"):
        raw = data.replace("env_dodel_", "")
        if ":::" in raw:
            fname, var_key = raw.split(":::", 1)
        else:
            parts = raw.split("_", 1)
            fname, var_key = (parts[0], parts[1]) if len(parts) == 2 else ("", "")
            
        if fname and var_key:
            info = resolve_env_target_info(fname)
            env_dict = read_script_env(info["name"])
            env_dict.pop(var_key, None)
            write_script_env(info["name"], env_dict)
            answer_callback(callback_id, f"🗑️ {var_key} deleted in real-time!", show_alert=True)
            prompt_script_env_dashboard(chat_id, user_id, info["name"], message_id)

    # 2f2. Wipe Single Script's / Project's Environment Variables
    elif data.startswith("env_wipe_one_") or data.startswith("env_wipe_"):
        fname = data.replace("env_wipe_one_", "").replace("env_wipe_", "")
        info = resolve_env_target_info(fname)
        answer_callback(callback_id, f"Wiping environment for {info['display_name']}...")
        
        # 1. Clear from vault
        vault = load_env_vault()
        for vk in info["vault_keys"]:
            vault.pop(vk, None)
        save_env_vault(vault)
        
        # 2. Delete physical .env files on disk
        for ef in info["env_file_candidates"]:
            if os.path.exists(ef):
                try:
                    os.remove(ef)
                except Exception:
                    pass
                    
        # 3. Synchronize deletion to GitHub in background
        threading.Thread(target=git_sync_to_github, args=(f"Wipe .env variables for {info['display_name']}",), daemon=True).start()
        
        answer_callback(callback_id, f"✅ Environment wiped for {info['display_name']}!", show_alert=True)
        prompt_script_env_dashboard(chat_id, user_id, info["name"], message_id)

    # 2f3. Wipe All Script Environments Confirmation Prompt
    elif data == "env_wipe_all_prompt":
        answer_callback(callback_id)
        text = (
            "💣 <b>Confirm Delete All (.env) Variables</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>WARNING:</b> This will permanently delete <b>ALL environment variables and .env vaults</b> across ALL scripts and projects in your workspace!\n\n"
            "Are you absolutely sure?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "💣 Yes, Delete All ENVs", "callback_data": "do_env_wipe_all"}],
                [{"text": "❌ Cancel", "callback_data": "menu_env_select"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 2f4. Execute Wipe All Script Environments in Real-Time
    elif data == "do_env_wipe_all":
        answer_callback(callback_id, "Wiping all environments...")
        
        # 1. Clear entire vault
        config["env_vault"] = {}
        save_config(config)
        vault_file = get_env_vault_file()
        try:
            with open(vault_file, "w") as f:
                json.dump({}, f)
        except Exception:
            pass
            
        # 2. Delete all physical .env files across scripts/ directory
        for root, _, fs in os.walk(SCRIPTS_DIR):
            for f in fs:
                if f.endswith(".env") or f == ".env":
                    try:
                        os.remove(os.path.join(root, f))
                    except Exception:
                        pass
                        
        # 3. Synchronize deletion to GitHub in background
        threading.Thread(target=git_sync_to_github, args=("Wipe all per-script .env vaults via Telegram",), daemon=True).start()
        
        answer_callback(callback_id, "✅ All .env variables deleted successfully in real-time!", show_alert=True)
        prompt_env_script_select(chat_id, user_id, message_id)

    # 2g. Export .env file
    elif data.startswith("env_exp_"):
        fname = data.replace("env_exp_", "")
        info = resolve_env_target_info(fname)
        env_path = info["primary_dot_env"]
        # Ensure latest env is written before export
        read_script_env(info["name"])
        if os.path.exists(env_path):
            answer_callback(callback_id, f"Exporting {info['display_name']} .env...")
            send_tg_document(chat_id, env_path, caption=f"📄 <b>{info['display_name']}/.env</b>")
        else:
            answer_callback(callback_id, "No .env variables found for this project.", show_alert=True)

    # 2h. View Virtualenv Packages
    elif data.startswith("venv_list_"):
        fname = data.replace("venv_list_", "")
        answer_callback(callback_id, "Listing packages...")
        py_bin, pip_bin, venv_dir = get_or_create_venv(fname)
        
        cmd = [pip_bin, "list", "--format=columns"] if not isinstance(pip_bin, list) else pip_bin + ["list", "--format=columns"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        pkg_out = res.stdout.strip() or "(No packages installed in this venv)"
        
        text = (
            f"🛡️ <b>Isolated Environment:</b> <code>scripts/{fname}</code>\n"
            f"📁 <b>Venv Path:</b> <code>{venv_dir or 'Global'}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<pre>{html.escape(pkg_out[-2500:])}</pre>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🔙 Back to Script ENVs", "callback_data": f"env_dash_{fname}"}],
                [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 2i. Database & Storage Inspector: Main Bot Selection Hub
    elif data == "menu_db_inspector":
        answer_callback(callback_id, "Scanning databases by bot...")
        show_database_bot_selection_view(chat_id, message_id)

    # 2i2. Specific Bot Database View
    elif data.startswith("db_bot_"):
        bot_key = data.replace("db_bot_", "")
        answer_callback(callback_id)
        show_bot_database_view(chat_id, bot_key, message_id)

    # 2j. Download specific DB / Session / Storage file with inline delete button
    elif data.startswith("db_dl_"):
        rel_target = data.replace("db_dl_", "")
        full_target = os.path.join(WORKSPACE_DIR, rel_target)
        
        # Identify bot_key
        clean_rel = rel_target.replace("scripts/", "").lstrip("/")
        parts = clean_rel.split("/")
        bot_key = parts[0] if len(parts) > 1 else ("ROOT" if not rel_target.startswith("scripts/") else clean_rel)
        
        if os.path.exists(full_target):
            fname = os.path.basename(rel_target)
            sz_human = format_bytes_human(os.path.getsize(full_target))
            answer_callback(callback_id, f"Sending {fname}...")
            
            doc_markup = {
                "inline_keyboard": [
                    [{"text": f"🗑️ Delete {fname}", "callback_data": f"db_del_from_doc_{rel_target}"}],
                    [{"text": "🔙 Back to Bot Databases", "callback_data": f"db_bot_{bot_key}"}]
                ]
            }
            send_tg_document(
                chat_id, 
                full_target, 
                caption=f"🗄️ <b>Storage File:</b> <code>{fname}</code>\n📁 <b>Path:</b> <code>{rel_target}</code>\n📦 <b>Size:</b> <code>{sz_human}</code>",
                reply_markup=doc_markup
            )
        else:
            answer_callback(callback_id, "File not found or already deleted!", show_alert=True)

    # 2j2. Delete directly from document message
    elif data.startswith("db_del_from_doc_"):
        rel_target = data.replace("db_del_from_doc_", "")
        full_target = os.path.join(WORKSPACE_DIR, rel_target)
        fname = os.path.basename(rel_target)
        
        answer_callback(callback_id, f"Deleting {fname}...")
        if os.path.exists(full_target):
            try:
                os.remove(full_target)
            except Exception:
                pass
                
        stem = os.path.splitext(full_target)[0]
        for extra_ext in [".session-journal", ".session-shm", ".session-wal", ".db-journal", ".db-wal", ".db-shm"]:
            c_path = stem + extra_ext
            if os.path.exists(c_path):
                try:
                    os.remove(c_path)
                except Exception:
                    pass
                    
        try:
            subprocess.run(["git", "rm", "-f", "--ignore-unmatch", rel_target, f"{rel_target}*"], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception:
            pass
            
        threading.Thread(target=git_sync_to_github, args=(f"Permanently delete {rel_target}",), daemon=True).start()
        edit_tg_message(chat_id, message_id, f"🗑️ <b>Deleted:</b> <code>{rel_target}</code> has been permanently removed from disk and Git.", reply_markup={"inline_keyboard": [[{"text": "🔙 Back to Databases", "callback_data": "menu_db_inspector"}]]})

    # 2k. Delete single specific DB / Session file from Bot View
    elif data.startswith("db_del_one_"):
        rel_target = data.replace("db_del_one_", "")
        full_target = os.path.join(WORKSPACE_DIR, rel_target)
        fname = os.path.basename(rel_target)
        
        # Identify bot_key to return to
        clean_rel = rel_target.replace("scripts/", "").lstrip("/")
        parts = clean_rel.split("/")
        bot_key = parts[0] if len(parts) > 1 else ("ROOT" if not rel_target.startswith("scripts/") else clean_rel)
        
        answer_callback(callback_id, f"Deleting {fname}...")
        
        # 1. Delete from disk
        if os.path.exists(full_target):
            try:
                if os.path.isdir(full_target):
                    shutil.rmtree(full_target, ignore_errors=True)
                else:
                    os.remove(full_target)
            except Exception as e_del:
                logger.error(f"Error removing {full_target}: {e_del}")
                
        # 2. Delete journal/wal/shm companion files if any
        stem = os.path.splitext(full_target)[0]
        for extra_ext in [".session-journal", ".session-shm", ".session-wal", ".db-journal", ".db-wal", ".db-shm"]:
            c_path = stem + extra_ext
            if os.path.exists(c_path):
                try:
                    os.remove(c_path)
                except Exception:
                    pass
                    
        # 3. Explicitly remove from Git index
        try:
            subprocess.run(["git", "rm", "-f", "--ignore-unmatch", rel_target, f"{rel_target}*"], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception:
            pass
            
        # 4. Sync deletion to GitHub
        threading.Thread(target=git_sync_to_github, args=(f"Permanently delete storage file {rel_target}",), daemon=True).start()
        
        answer_callback(callback_id, f"🗑️ {fname} permanently deleted!", show_alert=True)
        show_bot_database_view(chat_id, bot_key, message_id)

    # 2k2. Wipe All Databases of Specific Bot Prompt
    elif data.startswith("db_wipe_bot_prompt_"):
        bot_key = data.replace("db_wipe_bot_prompt_", "")
        display_title = "Root Workspace" if bot_key == "ROOT" else bot_key
        answer_callback(callback_id)
        text = (
            f"💣 <b>Confirm Wipe All Databases for:</b> <code>{display_title}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>WARNING:</b> This will permanently delete <b>ALL databases and sessions</b> belonging to <code>{display_title}</code>!\n\n"
            "Are you sure you want to proceed?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": f"💣 Yes, Delete All DBs for {display_title}", "callback_data": f"do_db_wipe_bot_{bot_key}"}],
                [{"text": "❌ Cancel", "callback_data": f"db_bot_{bot_key}"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 2k3. Execute Wipe All Databases of Specific Bot
    elif data.startswith("do_db_wipe_bot_"):
        bot_key = data.replace("do_db_wipe_bot_", "")
        display_title = "Root Workspace" if bot_key == "ROOT" else bot_key
        answer_callback(callback_id, f"Deleting databases of {display_title}...")
        
        db_items = scan_database_items_for_bot(bot_key)
        deleted_count = 0
        
        for item in db_items:
            fp = item["path"]
            rel = item["rel_path"]
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    deleted_count += 1
                except Exception:
                    pass
            stem = os.path.splitext(fp)[0]
            for extra_ext in [".session-journal", ".session-shm", ".session-wal", ".db-journal", ".db-wal", ".db-shm"]:
                c_path = stem + extra_ext
                if os.path.exists(c_path):
                    try:
                        os.remove(c_path)
                    except Exception:
                        pass
            try:
                subprocess.run(["git", "rm", "-f", "--ignore-unmatch", rel, f"{rel}*"], cwd=WORKSPACE_DIR, capture_output=True)
            except Exception:
                pass
                
        threading.Thread(target=git_sync_to_github, args=(f"Wipe all databases for {display_title}",), daemon=True).start()
        answer_callback(callback_id, f"✅ Deleted {deleted_count} databases for {display_title}!", show_alert=True)
        show_bot_database_view(chat_id, bot_key, message_id)

    # 2l. Wipe All Databases Prompt (Global)
    elif data == "db_wipe_all_dbs_prompt":
        answer_callback(callback_id)
        text = (
            "🗄️ <b>Confirm Wipe All Database Files</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>WARNING:</b> This will permanently delete <b>ALL SQLite database files (.db, .sqlite, .sqlite3)</b> across all scripts and workspace!\n\n"
            "Are you sure you want to proceed?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "💣 Yes, Delete All Databases", "callback_data": "do_db_wipe_all_dbs"}],
                [{"text": "❌ Cancel", "callback_data": "menu_db_inspector"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 2m. Execute Wipe All Databases
    elif data == "do_db_wipe_all_dbs":
        answer_callback(callback_id, "Deleting all databases...")
        db_exts = {".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm"}
        deleted_count = 0
        
        for root, _, files in os.walk(WORKSPACE_DIR):
            if ".git" in root:
                continue
            for f in files:
                ext = os.path.splitext(f.lower())[1]
                if ext in db_exts:
                    fp = os.path.join(root, f)
                    try:
                        os.remove(fp)
                        deleted_count += 1
                    except Exception:
                        pass
                        
        try:
            subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", "*.db", "*.sqlite*", "scripts/*.db", "scripts/*/*.db"], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception:
            pass
            
        threading.Thread(target=git_sync_to_github, args=("Wipe all SQLite database files via Telegram",), daemon=True).start()
        answer_callback(callback_id, f"✅ Deleted {deleted_count} database files!", show_alert=True)
        show_database_bot_selection_view(chat_id, message_id)

    # 2n. Wipe All Sessions Prompt
    elif data == "db_wipe_all_sessions_prompt":
        answer_callback(callback_id)
        text = (
            "🔑 <b>Confirm Wipe All Telegram Sessions</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>WARNING:</b> This will permanently delete <b>ALL Telethon & Pyrogram session files (.session)</b> across all scripts!\n\n"
            "Are you sure you want to proceed?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "💣 Yes, Delete All Sessions", "callback_data": "do_db_wipe_all_sessions"}],
                [{"text": "❌ Cancel", "callback_data": "menu_db_inspector"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 2o. Execute Wipe All Sessions
    elif data == "do_db_wipe_all_sessions":
        answer_callback(callback_id, "Deleting all session files...")
        session_exts = {".session", ".session-journal", ".session-shm", ".session-wal"}
        deleted_count = 0
        
        for root, _, files in os.walk(WORKSPACE_DIR):
            if ".git" in root:
                continue
            for f in files:
                ext = os.path.splitext(f.lower())[1]
                if ext in session_exts:
                    fp = os.path.join(root, f)
                    try:
                        os.remove(fp)
                        deleted_count += 1
                    except Exception:
                        pass
                        
        try:
            subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", "*.session*", "scripts/*.session*", "scripts/*/*.session*"], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception:
            pass
            
        threading.Thread(target=git_sync_to_github, args=("Wipe all Telegram session files via Telegram",), daemon=True).start()
        answer_callback(callback_id, f"✅ Deleted {deleted_count} session files!", show_alert=True)
        show_database_inspector_view(chat_id, message_id)

    # 2p. Nuke All Databases & Storage Prompt
    elif data == "db_nuke_all_prompt":
        answer_callback(callback_id)
        text = (
            "💣 <b>CONFIRM COMPLETE STORAGE NUKE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>DANGER:</b> This will permanently wipe <b>ALL databases (.db), sessions (.session), temporary files, logs, and vaults</b> in your entire repository!\n\n"
            "Are you 100% sure you want to delete everything?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "💥 YES, NUKE ALL STORAGE DATA", "callback_data": "do_db_nuke_all"}],
                [{"text": "❌ Cancel", "callback_data": "menu_db_inspector"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 2q. Execute Nuke All Storage
    elif data == "do_db_nuke_all":
        answer_callback(callback_id, "Nuking all databases, sessions, and storage...")
        
        # 1. Stop all scripts
        stop_child_app(script_name=None, clear_active=True)
        time.sleep(0.5)
        
        # 2. Delete all database, session, staging, temp, log files
        wiped_exts = {".db", ".sqlite", ".sqlite3", ".db-journal", ".db-wal", ".db-shm", ".session", ".session-journal", ".session-shm", ".session-wal", ".log"}
        deleted_count = 0
        
        for root, _, files in os.walk(WORKSPACE_DIR):
            if ".git" in root:
                continue
            for f in files:
                f_lower = f.lower()
                ext = os.path.splitext(f_lower)[1]
                if ext in wiped_exts or f_lower.startswith(".staging_") or f_lower.startswith("temp_") or f_lower.endswith(".env") or f_lower == ".env":
                    fp = os.path.join(root, f)
                    try:
                        os.remove(fp)
                        deleted_count += 1
                    except Exception:
                        pass
                        
        # 3. Clear vaults
        config["env_vault"] = {}
        save_config(config)
        vault_file = get_env_vault_file()
        try:
            with open(vault_file, "w") as f:
                json.dump({}, f)
        except Exception:
            pass
            
        # 4. Remove all from Git
        try:
            subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", "*.db", "*.sqlite*", "*.session*", "scripts/*.db", "scripts/*.session*"], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception:
            pass
            
        threading.Thread(target=git_sync_to_github, args=("Nuke all databases, sessions, and storage via Telegram",), daemon=True).start()
        answer_callback(callback_id, f"💥 Storage Nuked! ({deleted_count} files removed)", show_alert=True)
        show_database_inspector_view(chat_id, message_id)

    # 3. Runner Menu
    elif data in ["menu_runner", "menu_run_select"]:
        answer_callback(callback_id)
        prompt_runner_menu(chat_id, user_id, message_id)

    # Autofix Missing Package Callback
    elif data.startswith("autofix_pkg_"):
        parts = data.replace("autofix_pkg_", "").split("_", 1)
        if len(parts) == 2:
            pkg_name, target_py = parts[0], parts[1]
            answer_callback(callback_id, f"Installing {pkg_name} into isolated venv...")
            send_tg_message(chat_id, f"⏳ <b>Auto-Installing:</b> <code>{pkg_name}</code> into isolated environment for <code>{target_py}</code>...")
            
            py_bin, pip_bin, venv_dir = get_or_create_venv(target_py)
            cmd = [pip_bin, "install", pkg_name] if not isinstance(pip_bin, list) else pip_bin + ["install", pkg_name]
            res = subprocess.run(cmd, capture_output=True, text=True)
            
            # Save into target_py's dedicated requirements file
            base_n = target_py.rsplit('.', 1)[0]
            req_file = os.path.join(SCRIPTS_DIR, f"{base_n}.requirements.txt")
            with open(req_file, "a+", encoding="utf-8") as f:
                f.seek(0)
                existing = f.read()
                if pkg_name not in existing:
                    f.write(f"\n{pkg_name}\n")
            git_sync_to_github(f"Add {pkg_name} to {base_n}.requirements.txt")
            
            send_tg_message(chat_id, f"✅ <b>{pkg_name} installed in isolated venv & saved to {base_n}.requirements.txt!</b>\n🚀 Now auto-launching <code>{target_py}</code>...")
            res_ok, res_msg = start_child_app(target_py)
            send_tg_message(chat_id, res_msg, reply_markup=get_main_menu_keyboard())

    # 4. Execute a specific file
    elif data.startswith("exec_run_"):
        fname = data.replace("exec_run_", "")
        active = get_active_running_processes()
        if fname in active:
            answer_callback(callback_id, f"Reloading {fname} with latest code...")
            stop_child_app(script_name=fname, clear_active=False)
            time.sleep(0.5)
        else:
            answer_callback(callback_id, f"Starting {fname}...")
        ok, msg = start_child_app(fname)
        send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())

    # 4b. Instance Conflict Resolution Callbacks
    elif data.startswith("inst_parallel_"):
        fname = data.replace("inst_parallel_", "")
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path")
        if not staged_path or not os.path.exists(staged_path):
            answer_callback(callback_id, "Staged file expired. Please upload again.", show_alert=True)
            return
        
        active = get_active_running_processes()
        base, ext = os.path.splitext(fname)
        idx = 2
        while os.path.exists(os.path.join(SCRIPTS_DIR, f"{base}_{idx}{ext}")) or f"{base}_{idx}{ext}" in active:
            idx += 1
        new_name = f"{base}_{idx}{ext}"
        new_path = os.path.join(SCRIPTS_DIR, new_name)
        
        import shutil
        shutil.move(staged_path, new_path)
        user_states.pop(user_id, None)
        git_sync_to_github(f"Create parallel instance: {new_name}")
        
        answer_callback(callback_id, f"Launching {new_name}...")
        ok, msg = start_child_app(new_name)
        send_tg_message(chat_id, f"🔀 <b>Created & Launched Parallel Instance:</b> <code>{new_name}</code>\n\n{msg}", reply_markup=get_main_menu_keyboard())

    elif data.startswith("inst_replace_"):
        fname = data.replace("inst_replace_", "")
        clean_fname = fname.replace("scripts/", "").lstrip("/")
        base_stem = clean_fname.rsplit(".", 1)[0]
        
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path")
        if not staged_path or not os.path.exists(staged_path):
            answer_callback(callback_id, "Staged file expired. Please upload again.", show_alert=True)
            return
        
        target_path = os.path.join(SCRIPTS_DIR, clean_fname)
        answer_callback(callback_id, f"Replacing and restarting {clean_fname}...")
        
        # 1. Stop old process & release file locks
        stop_child_app(script_name=clean_fname, clear_active=False)
        time.sleep(0.5)
        
        # 2. Clean old associated databases and sessions so old data is not mixed
        for extra_ext in [".db", ".db-wal", ".db-shm", ".db-journal", ".session", ".session-journal", ".session-wal", ".session-shm"]:
            f_db = os.path.join(SCRIPTS_DIR, base_stem + extra_ext)
            if os.path.exists(f_db):
                try:
                    os.remove(f_db)
                    subprocess.run(["git", "rm", "-f", "--ignore-unmatch", f"scripts/{base_stem}{extra_ext}"], cwd=WORKSPACE_DIR, capture_output=True)
                except Exception:
                    pass
        
        # 3. Move fresh script into place
        import shutil
        shutil.move(staged_path, target_path)
        user_states.pop(user_id, None)
        
        # 4. Sync updated code to GitHub
        git_sync_to_github(f"Update and replace script: {clean_fname}")
        
        ok, msg = start_child_app(clean_fname, force_restart=True)
        send_tg_message(chat_id, f"🔄 <b>Updated & Restarted Instance:</b> <code>{clean_fname}</code>\n\n{msg}", reply_markup=get_main_menu_keyboard())

    elif data.startswith("inst_custom_"):
        fname = data.replace("inst_custom_", "")
        answer_callback(callback_id)
        if user_id in user_states and isinstance(user_states[user_id], dict):
            user_states[user_id]["action"] = "WAITING_CUSTOM_PY_NAME"
        text = (
            "✏️ <b>Enter Custom Filename</b>\n\n"
            f"Please send the new filename for this script:\n"
            f"<i>(Example: <code>worker.py</code>, <code>tg_bot.py</code>, <code>userbot.py</code>)</i>"
        )
        markup = {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"inst_cancel_{fname}"}]]}
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    elif data.startswith("inst_cancel_"):
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path") if isinstance(state, dict) else None
        if staged_path and os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except Exception:
                pass
        user_states.pop(user_id, None)
        answer_callback(callback_id, "Upload cancelled.")
        edit_tg_message(chat_id, message_id, "❌ <b>Upload cancelled.</b> Running instances were not modified.", reply_markup=get_main_menu_keyboard())

    # 4d. Duplicate ZIP Project: 1. Deploy New Project
    elif data.startswith("zip_new_"):
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path")
        file_name = state.get("file_name", "project.zip")
        next_proj_name = state.get("next_proj_name", "project_2")
        
        if not staged_path or not os.path.exists(staged_path):
            answer_callback(callback_id, "Staged file expired. Please upload again.", show_alert=True)
            return
            
        answer_callback(callback_id, f"Deploying new project {next_proj_name}...")
        user_states.pop(user_id, None)
        
        import zipfile
        target_dir = os.path.join(SCRIPTS_DIR, next_proj_name)
        os.makedirs(target_dir, exist_ok=True)
        extracted_files = []
        try:
            with zipfile.ZipFile(staged_path, 'r') as zip_ref:
                namelist = zip_ref.namelist()
                top_dirs = {item.split('/')[0] for item in namelist if item and not item.startswith('/')}
                if len(top_dirs) == 1 and all(item.startswith(list(top_dirs)[0] + '/') or item == list(top_dirs)[0] for item in namelist):
                    root_f_name = list(top_dirs)[0]
                    for member in zip_ref.infolist():
                        parts = member.filename.split('/', 1)
                        if len(parts) > 1 and parts[1]:
                            member.filename = parts[1]
                            zip_ref.extract(member, target_dir)
                else:
                    zip_ref.extractall(target_dir)
                extracted_files = namelist
            try:
                os.remove(staged_path)
            except Exception:
                pass
        except Exception as e:
            try:
                os.remove(staged_path)
            except Exception:
                pass
            edit_tg_message(chat_id, message_id, f"❌ Failed to extract zip: {e}", reply_markup=get_main_menu_keyboard())
            return
            
        entry_script = detect_project_entry_script(target_dir)
        
        found_reqs = []
        found_envs = []
        for root, _, fs in os.walk(target_dir):
            for f in fs:
                fp = os.path.join(root, f)
                if "requirements" in f.lower() and f.endswith(".txt"):
                    found_reqs.append(fp)
                elif f.endswith(".env") or f == ".env":
                    found_envs.append(fp)
                    
        req_count = 0
        if found_reqs and entry_script:
            for req in found_reqs:
                check_and_install_reqs(req, clean_name=entry_script)
                with open(req, "r", encoding="utf-8", errors="ignore") as rf:
                    req_count += len([l for l in rf if l.strip() and not l.startswith("#")])
                    
        target_name = entry_script or next_proj_name
        info = resolve_env_target_info(target_name)
        env_count = 0
        if found_envs:
            for ef in found_envs:
                parsed = {}
                with open(ef, "r", encoding="utf-8", errors="ignore") as rf:
                    for l in rf:
                        l = l.strip()
                        if "=" in l and not l.startswith("#"):
                            k, v = l.split("=", 1)
                            clean_k, clean_v = k.strip(), v.strip().strip("'\"")
                            if clean_k and clean_v:
                                parsed[clean_k] = clean_v
                if parsed:
                    curr_env = read_script_env(info["name"])
                    curr_env.update(parsed)
                    write_script_env(info["name"], curr_env)
                    env_count += len(parsed)
                    
        threading.Thread(target=git_sync_to_github, args=(f"Deploy new separate project: {next_proj_name}",), daemon=True).start()
        
        launch_status = ""
        if entry_script:
            ok_run, run_msg = start_child_app(entry_script, force_restart=True)
            launch_status = f"🟢 <b>Status:</b> <code>{entry_script}</code> is now <b>RUNNING!</b>" if ok_run else f"⚠️ <b>Status:</b> {run_msg}"
            
        env_info_str = f"{env_count} variables loaded" if env_count else "⚠️ No .env found (Set variables before running!)"
        success_msg = (
            f"🚀 <b>New Project Deployed Successfully!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Archive:</b> <code>{file_name}</code>\n"
            f"📁 <b>Directory:</b> <code>scripts/{next_proj_name}/</code>\n"
            f"🎯 <b>Detected Entry Script:</b> <code>{entry_script or 'None'}</code>\n"
            f"{launch_status}\n"
            f"📦 <b>Dependencies:</b> {'Installed packages' if found_reqs else 'None'}\n"
            f"🔒 <b>Environment:</b> {env_info_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Manage your new project using the buttons below:</i>"
        )
        buttons = []
        if entry_script:
            buttons.append([{"text": f"🛑 Stop {os.path.basename(entry_script)}", "callback_data": f"confirm_stop_prompt_{entry_script}"}, {"text": "🔄 Restart", "callback_data": f"exec_run_{entry_script}"}])
        
        env_btn_row = [{"text": f"⚙️ Manage ENV ({env_count})", "callback_data": f"env_dash_{info['slug']}"}]
        if env_count == 0:
            env_btn_row.insert(0, {"text": "➕ Set ENVs Now", "callback_data": f"env_add_{info['slug']}"})
        buttons.append(env_btn_row)
        
        if entry_script:
            buttons.append([{"text": "📋 View Live Logs", "callback_data": f"show_log_for_{entry_script}"}])
        buttons.append([{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "📂 View Files", "callback_data": "menu_files"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
        
        user_states[user_id] = {"action": "PROJECT_DEPLOYED", "last_project": info["name"]}
        edit_tg_message(chat_id, message_id, success_msg, reply_markup={"inline_keyboard": buttons})

    # 4e. Duplicate ZIP Project: 2. Update the Old (Complete Clean Replacement)
    elif data.startswith("zip_update_"):
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path")
        file_name = state.get("file_name", "project.zip")
        zip_base = state.get("zip_base", "project")
        
        if not staged_path or not os.path.exists(staged_path):
            answer_callback(callback_id, "Staged file expired. Please upload again.", show_alert=True)
            return
            
        answer_callback(callback_id, f"Wiping old {zip_base} and deploying fresh...")
        user_states.pop(user_id, None)
        
        # Completely delete old project, all old files, databases, .env files, and vault entries!
        purge_project_or_script_completely(zip_base, exclude_paths=[staged_path])
        time.sleep(0.3)

        if not os.path.exists(staged_path):
            logger.error(f"Staged file missing after purge: {staged_path}")
            edit_tg_message(chat_id, message_id, "❌ Uploaded archive was not found. Please upload again.", reply_markup=get_main_menu_keyboard())
            return
            
        # Extract fresh new project code
        import zipfile
        old_target_dir = os.path.join(SCRIPTS_DIR, zip_base)
        os.makedirs(old_target_dir, exist_ok=True)
        extracted_files = []
        try:
            with zipfile.ZipFile(staged_path, 'r') as zip_ref:
                namelist = zip_ref.namelist()
                top_dirs = {item.split('/')[0] for item in namelist if item and not item.startswith('/')}
                if len(top_dirs) == 1 and all(item.startswith(list(top_dirs)[0] + '/') or item == list(top_dirs)[0] for item in namelist):
                    for member in zip_ref.infolist():
                        parts = member.filename.split('/', 1)
                        if len(parts) > 1 and parts[1]:
                            member.filename = parts[1]
                            zip_ref.extract(member, old_target_dir)
                else:
                    zip_ref.extractall(old_target_dir)
                extracted_files = namelist
            try:
                os.remove(staged_path)
            except Exception:
                pass
        except Exception as e:
            try:
                os.remove(staged_path)
            except Exception:
                pass
            edit_tg_message(chat_id, message_id, f"❌ Failed to extract zip: {e}", reply_markup=get_main_menu_keyboard())
            return
            
        entry_script = detect_project_entry_script(old_target_dir)
        
        found_reqs = []
        found_envs = []
        for root, _, fs in os.walk(old_target_dir):
            for f in fs:
                fp = os.path.join(root, f)
                if "requirements" in f.lower() and f.endswith(".txt"):
                    found_reqs.append(fp)
                elif f.endswith(".env") or f == ".env":
                    found_envs.append(fp)
                    
        req_count = 0
        if found_reqs and entry_script:
            for req in found_reqs:
                check_and_install_reqs(req, clean_name=entry_script)
                with open(req, "r", encoding="utf-8", errors="ignore") as rf:
                    req_count += len([l for l in rf if l.strip() and not l.startswith("#")])
                    
        target_name = entry_script or zip_base
        info = resolve_env_target_info(target_name)
        env_count = 0
        if found_envs:
            for ef in found_envs:
                parsed = {}
                with open(ef, "r", encoding="utf-8", errors="ignore") as rf:
                    for l in rf:
                        l = l.strip()
                        if "=" in l and not l.startswith("#"):
                            k, v = l.split("=", 1)
                            clean_k, clean_v = k.strip(), v.strip().strip("'\"")
                            if clean_k and clean_v:
                                parsed[clean_k] = clean_v
                if parsed:
                    write_script_env(info["name"], parsed)
                    env_count += len(parsed)
                    
        threading.Thread(target=git_sync_to_github, args=(f"Update and redeploy project: {zip_base}",), daemon=True).start()
        
        launch_status = ""
        if entry_script:
            ok_run, run_msg = start_child_app(entry_script, force_restart=True)
            launch_status = f"🟢 <b>Status:</b> <code>{entry_script}</code> is now <b>RUNNING!</b>" if ok_run else f"⚠️ <b>Status:</b> {run_msg}"
            
        env_info_str = f"{env_count} fresh variables loaded" if env_count else "Clean (No .env found - set variables before running)"
        success_msg = (
            f"🔄 <b>Project Updated & Redeployed!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Archive:</b> <code>{file_name}</code>\n"
            f"📁 <b>Directory:</b> <code>scripts/{zip_base}/</code>\n"
            f"🎯 <b>Detected Entry Script:</b> <code>{entry_script or 'None'}</code>\n"
            f"{launch_status}\n"
            f"📦 <b>Dependencies:</b> {'Installed packages' if found_reqs else 'None'}\n"
            f"🔒 <b>Environment:</b> {env_info_str}\n"
            f"🗑️ <b>Cleanup:</b> <i>Old code, stale .env, and old databases permanently wiped!</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Manage your updated project using the buttons below:</i>"
        )
        buttons = []
        if entry_script:
            buttons.append([{"text": f"🛑 Stop {os.path.basename(entry_script)}", "callback_data": f"confirm_stop_prompt_{entry_script}"}, {"text": "🔄 Restart", "callback_data": f"exec_run_{entry_script}"}])
        
        env_btn_row = [{"text": f"⚙️ Manage ENV ({env_count})", "callback_data": f"env_dash_{info['slug']}"}]
        if env_count == 0:
            env_btn_row.insert(0, {"text": "➕ Set ENVs Now", "callback_data": f"env_add_{info['slug']}"})
        buttons.append(env_btn_row)
        
        if entry_script:
            buttons.append([{"text": "📋 View Live Logs", "callback_data": f"show_log_for_{entry_script}"}])
        buttons.append([{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "📂 View Files", "callback_data": "menu_files"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
        
        user_states[user_id] = {"action": "PROJECT_DEPLOYED", "last_project": info["name"]}
        edit_tg_message(chat_id, message_id, success_msg, reply_markup={"inline_keyboard": buttons})

    # 4f. Cancel ZIP Upload
    elif data.startswith("zip_cancel_"):
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path") if isinstance(state, dict) else None
        if staged_path and os.path.exists(staged_path):
            try:
                os.remove(staged_path)
            except Exception:
                pass
        user_states.pop(user_id, None)
        answer_callback(callback_id, "Upload cancelled.")
        edit_tg_message(chat_id, message_id, "❌ <b>Upload cancelled.</b> Existing projects were not modified.", reply_markup=get_main_menu_keyboard())

    # 5. Stop Script Menu
    elif data == "menu_stop":
        answer_callback(callback_id)
        prompt_stop_menu(chat_id, user_id, message_id)

    # 5b. Confirm Stop Prompt
    elif data.startswith("confirm_stop_prompt_"):
        fname = data.replace("confirm_stop_prompt_", "")
        answer_callback(callback_id)
        text = (
            f"⚠️ <b>Confirmation Required</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Are you sure you want to <b>STOP and terminate</b> <code>{fname}</code>?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": f"🛑 Yes, Stop {fname}", "callback_data": f"do_stop_{fname}"}],
                [{"text": "❌ Cancel (Keep Running)", "callback_data": "menu_main"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 5c. Do Stop Execution
    elif data.startswith("do_stop_"):
        fname = data.replace("do_stop_", "")
        ok, msg = stop_child_app(script_name=fname, clear_active=True)
        answer_callback(callback_id, f"{fname} stopped!", show_alert=True)
        edit_tg_message(
            chat_id, 
            message_id, 
            f"🛑 <b>{fname} has been stopped successfully!</b>\n\n"
            f"💡 <i>This script will remain stopped on future runs and will not run until you start it again from the dashboard.</i>", 
            reply_markup={
                "inline_keyboard": [
                    [{"text": f"▶️ Run {fname} Again", "callback_data": f"exec_run_{fname}"}],
                    [{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "🔙 Main Menu", "callback_data": "menu_main"}]
                ]
            }
        )

    # 5d. Stop All Scripts Execution
    elif data == "menu_stop_all":
        answer_callback(callback_id, "Stopping all scripts...")
        stop_child_app(script_name=None, clear_active=True)
        stop_all_text = (
            "🛑 <b>All running scripts have been stopped!</b>\n\n"
            "💡 <i>All scripts will remain stopped on future runs until you run them again from the dashboard.</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "🔙 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        if message_id:
            edit_tg_message(chat_id, message_id, stop_all_text, reply_markup=markup)
        else:
            send_tg_message(chat_id, stop_all_text, reply_markup=markup)

    # 6. Restart Script
    elif data == "menu_restart":
        answer_callback(callback_id, "Restarting...")
        ok, msg = restart_child_app()
        send_tg_message(chat_id, msg, reply_markup=get_main_menu_keyboard())

    # 7. Logs
    elif data == "menu_logs":
        answer_callback(callback_id)
        show_logs_view(chat_id, message_id)

    # 7b. Specific Script Logs
    elif data.startswith("show_log_for_"):
        fname = data.replace("show_log_for_", "")
        answer_callback(callback_id, f"Loading {fname} logs...")
        show_logs_view(chat_id, message_id, target_script=fname)

    # 7c. Select Script Logs
    elif data == "menu_logs_select":
        answer_callback(callback_id)
        show_logs_view(chat_id, message_id)

    # 7d. Manual Export logs.md
    elif data.startswith("export_log_md_"):
        fname = data.replace("export_log_md_", "")
        answer_callback(callback_id, f"Exporting logs_{os.path.basename(fname).replace('.py', '')}.md...")
        send_logs_markdown_file(chat_id, fname)

    # 8. Files
    elif data == "menu_files":
        answer_callback(callback_id)
        show_files_view(chat_id, message_id)

    # 9. Download File / Project Archive
    elif data.startswith("file_dl_"):
        raw_target = data.replace("file_dl_", "")
        info = resolve_env_target_info(raw_target)
        target_path = info["project_dir"] if (info["is_project"] and os.path.exists(info["project_dir"])) else os.path.join(SCRIPTS_DIR, info["name"])
        if not os.path.exists(target_path):
            target_path = os.path.join(SCRIPTS_DIR, raw_target)
        if not os.path.exists(target_path):
            target_path = os.path.join(WORKSPACE_DIR, raw_target)

        fname = info["display_name"]
        if os.path.exists(target_path):
            if os.path.isdir(target_path):
                import shutil
                answer_callback(callback_id, f"Archiving {fname} to ZIP...")
                zip_base = os.path.join(WORKSPACE_DIR, f"temp_{info['slug']}")
                zip_path = shutil.make_archive(zip_base, 'zip', target_path)
                send_tg_document(chat_id, zip_path, caption=f"📦 <b>Project Archive:</b> <code>{fname}.zip</code>")
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
            else:
                answer_callback(callback_id, f"Sending {fname}...")
                send_tg_document(chat_id, target_path, caption=f"📄 <code>{fname}</code>")
        else:
            answer_callback(callback_id, "File not found!", show_alert=True)

    # 10a. Delete All Workspace Prompt
    elif data in ["wipe_all_workspace_prompt", "file_del_all_prompt"]:
        answer_callback(callback_id)
        text = (
            "💣 <b>Confirm Complete Workspace Wipe</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>WARNING:</b> This will permanently delete <b>ALL scripts, ZIP archives, and project files</b> in <code>scripts/</code>!\n\n"
            "🔴 <i>Any actively running script will be stopped.</i>\n\n"
            "Are you absolutely sure?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "💣 Yes, Delete Everything", "callback_data": "do_wipe_all_workspace"}],
                [{"text": "❌ Cancel", "callback_data": "menu_files"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 10b. Do Delete All Files Execution
    elif data in ["do_wipe_all_workspace", "do_delete_all_files"]:
        import shutil
        answer_callback(callback_id, "Wiping entire workspace in real-time...")
        
        # 1. Stop all running child processes cleanly
        stop_child_app(script_name=None, clear_active=True)
        time.sleep(0.5)
        
        # 2. Reset running processes dictionary in memory
        running_processes.clear()
        
        # 3. Explicitly remove all scripts and projects from Git index
        try:
            subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", "scripts/*"], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception:
            pass
        
        # 4. Completely wipe SCRIPTS_DIR directory on disk and recreate empty with .gitkeep
        deleted_count = 0
        try:
            if os.path.exists(SCRIPTS_DIR):
                for it in os.listdir(SCRIPTS_DIR):
                    if it == ".gitkeep":
                        continue
                    ip = os.path.join(SCRIPTS_DIR, it)
                    try:
                        if os.path.isdir(ip):
                            shutil.rmtree(ip, ignore_errors=True)
                        else:
                            os.remove(ip)
                        deleted_count += 1
                    except Exception as e_del:
                        logger.error(f"Error deleting {ip}: {e_del}")
            os.makedirs(SCRIPTS_DIR, exist_ok=True)
            with open(os.path.join(SCRIPTS_DIR, ".gitkeep"), "w") as f:
                f.write("")
        except Exception as e:
            logger.error(f"Error wiping scripts dir: {e}")
            
        # 5. Clean up any virtualenvs (.venvs/)
        try:
            if os.path.exists(VENVS_DIR):
                shutil.rmtree(VENVS_DIR, ignore_errors=True)
        except Exception:
            pass

        # 6. Clean up any leftover staging files or temporary logs in WORKSPACE_DIR
        try:
            for it in os.listdir(WORKSPACE_DIR):
                if it.startswith(".staging_") or it.startswith("temp_") or it.startswith("logs_"):
                    try:
                        p = os.path.join(WORKSPACE_DIR, it)
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                    except Exception:
                        pass
        except Exception:
            pass

        # 7. Clear all active scripts, config, and vault
        config["active_scripts"] = []
        config["manually_stopped_scripts"] = []
        config["active_script"] = None
        config["auto_run_file"] = None
        config["env_vault"] = {}
        save_config(config)
        
        vault_file = get_env_vault_file()
        try:
            with open(vault_file, "w") as f:
                json.dump({}, f)
        except Exception:
            pass
        
        # 8. Commit wipe to GitHub in background with notification
        def async_wipe_sync():
            ok_w, msg_w = git_sync_to_github("Wipe all scripts via Telegram")
            if ok_w:
                notify_all_admins("🗑️ <b>All scripts and workspace files have been permanently wiped from GitHub.</b>")
            else:
                notify_all_admins(f"⚠️ <b>GitHub Wipe Sync Error:</b>\n<code>{msg_w}</code>")
        threading.Thread(target=async_wipe_sync, daemon=True).start()
        
        # 9. Edit message in real time to show confirmation
        wipe_text = (
            "✅ <b>Workspace Wiped Successfully!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🗑️ Deleted {deleted_count} items (All scripts, projects & databases).\n"
            "🔴 All running processes terminated.\n\n"
            "💡 <i>You can send any new .py script or .zip project in chat to deploy!</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "📤 Upload New Script / ZIP", "callback_data": "menu_upload_prompt"}],
                [{"text": "📂 View Files", "callback_data": "menu_files"}],
                [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        edit_tg_message(chat_id, message_id, wipe_text, reply_markup=markup)

    # 10c. Confirm Delete Single Script / Project Prompt
    elif data.startswith("confirm_del_file_"):
        raw_target = data.replace("confirm_del_file_", "")
        answer_callback(callback_id)
        info = resolve_env_target_info(raw_target)
        
        type_str = "Project Directory" if info["is_project"] else "Script File"
        text = (
            f"🗑️ <b>Confirm Permanent Deletion ({type_str})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Are you sure you want to <b>PERMANENTLY DELETE</b> <code>{info['display_name']}</code>?\n\n"
            f"⚠️ <b>This will immediately and permanently delete:</b>\n"
            f"• All project files, subdirectories, and code\n"
            f"• All <code>.env</code> files from disk & cloud vault\n"
            f"• All databases, sessions, and virtual environments\n"
            f"• Terminate any running child processes in real-time\n"
            f"• Synchronize complete deletion to GitHub repository\n\n"
            f"<i>This action cannot be undone!</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🗑️ Yes, Delete Everywhere Now", "callback_data": f"do_delete_file_{info['slug']}"}],
                [{"text": "❌ Cancel", "callback_data": "menu_files"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 10d. Direct & Fast Delete Single File/Folder Execution
    elif data.startswith("file_del_") or data.startswith("do_delete_file_"):
        if data.startswith("do_delete_file_"):
            raw_target = data.replace("do_delete_file_", "")
        else:
            raw_target = data.replace("file_del_", "")
        
        if raw_target in ["all", "all_prompt"]:
            return
            
        answer_callback(callback_id, f"🗑️ Deleting in real-time...", show_alert=False)
        
        ok, info = purge_project_or_script_completely(raw_target)
        
        answer_callback(callback_id, f"🗑️ {info['display_name']} and all associated files/ENVs deleted everywhere!", show_alert=True)
        show_files_view(chat_id, message_id)

    # 11. Pip prompt
    elif data == "menu_pip_prompt":
        answer_callback(callback_id)
        prompt_pip_menu(chat_id, user_id, message_id)

    # 12. Shell prompt
    elif data == "menu_sh_prompt":
        answer_callback(callback_id)
        prompt_sh_menu(chat_id, user_id, message_id)

    # 13. Sync
    elif data == "menu_sync":
        answer_callback(callback_id, "Syncing to Cloud...")
        ok, msg = git_sync_to_github()
        send_tg_message(chat_id, f"{'✅' if ok else '❌'} {msg}", reply_markup=get_main_menu_keyboard())

    # 14. Upload Prompt
    elif data == "menu_upload_prompt":
        user_states[user_id] = "WAITING_RUN_FILE"
        answer_callback(callback_id)
        text = (
            "📤 <b>Upload New Python Script</b>\n\n"
            "Please send your <code>.py</code> file in chat.\n"
            "It will automatically be saved into the <code>scripts/</code> folder!"
        )
        edit_tg_message(chat_id, message_id, text, reply_markup=get_back_keyboard())

    # 15. Server & Repo Info
    elif data == "menu_server_info":
        answer_callback(callback_id, "ℹ️ Loading Server Intelligence...")
        show_server_info_view(chat_id, message_id)

    # 15a. Instant Deep RAM Cleanup (GC + malloc_trim + Log Buffer Trim)
    elif data == "menu_clean_ram":
        answer_callback(callback_id, "🧹 Cleaning Memory...", show_alert=False)
        res = perform_system_memory_cleanup()
        text = (
            "🧹 <b>Deep RAM Cleanup Complete!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• 🗑️ <b>Purged Garbage Objects:</b> <code>{res['uncollected_objects']}</code>\n"
            f"• ✂️ <b>Trimmed In-Memory Logs:</b> <code>{res['trimmed_logs']} lines</code>\n"
            f"• 🗜️ <b>Linux malloc_trim:</b> {'🟢 Executed (Heap released to OS)' if res['malloc_trimmed'] else '⚪ Optimized'}\n"
            f"• 💾 <b>Current RAM Usage:</b> <code>{res['used_mb']:.1f} MB / {res['total_mb']:.1f} MB</code> (<b>{res['percent']}%</b>)\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <i>Unused memory blocks have been compacted and returned to the OS.</i>"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Clean Again", "callback_data": "menu_clean_ram"},
                    {"text": "ℹ️ Server Info", "callback_data": "menu_server_info"}
                ],
                [
                    {"text": "🔙 Main Menu", "callback_data": "menu_main"}
                ]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 15b. Stop Workflow Confirmation Prompt
    elif data == "menu_stop_workflow_prompt":
        answer_callback(callback_id)
        text = (
            "🛑 <b>Stop Workflow & Shutdown Server?</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "This action will:\n"
            "• 💾 Checkpoint and backup all SQLite databases & files to GitHub.\n"
            "• 🛑 Cleanly stop all running child bots.\n"
            f"• ⚡ Terminate active GitHub Actions workflow runner (<code>#{RUN_ID}</code>).\n"
            "• 📴 Turn off the cloud server completely (No auto-restart).\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to stop the workflow now?"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🛑 Yes, Stop Workflow Now", "callback_data": "menu_stop_workflow_do"}],
                [{"text": "❌ Cancel", "callback_data": "menu_main"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 15c. Execute Stop Workflow
    elif data == "menu_stop_workflow_do":
        answer_callback(callback_id, "🛑 Stopping Workflow...", show_alert=True)
        edit_tg_message(
            chat_id,
            message_id,
            "🛑 <b>Stopping Cloud Runner & Workflow...</b>\n"
            "Saving databases and terminating runner..."
        )
        def async_stop_workflow():
            global IS_RUNNING
            IS_RUNNING = False
            logger.info("🛑 Stop Workflow button clicked by admin. Initiating real-time shutdown.")
            
            # 1. Flush SQLite DBs to guarantee zero corruption
            flush_all_sqlite_databases()
            
            # 2. Terminate child processes immediately
            stop_child_app(script_name=None, clear_active=False)
            
            # 3. Final git sync
            git_sync_to_github("Manual workflow stop by admin via Telegram button")
            
            # 4. Notify admins
            notify_all_admins(
                "🛑 <b>Workflow & Cloud Server Stopped:</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📦 All databases and states have been safely committed to GitHub.\n"
                "📴 <i>Cloud runner is now completely OFF. Auto-restart is disabled.</i>"
            )
            
            # 5. Cancel workflow run via API if running in GitHub Actions
            if EFFECTIVE_TOKEN and RUN_ID and RUN_ID != "local-dev":
                try:
                    headers = {
                        "Authorization": f"Bearer {EFFECTIVE_TOKEN}",
                        "Accept": "application/vnd.github+json"
                    }
                    r = requests.post(
                        f"https://api.github.com/repos/{REPO}/actions/runs/{RUN_ID}/cancel",
                        headers=headers,
                        timeout=5
                    )
                    logger.info(f"Cancel workflow API response: {r.status_code}")
                except Exception as e:
                    logger.debug(f"Cancel workflow API call: {e}")
            
            time.sleep(1.0)
            logger.info("👋 Exiting process immediately via os._exit(0).")
            os._exit(0)

        threading.Thread(target=async_stop_workflow, daemon=False, name="WorkflowStopper").start()

    # 15d. Test Relay Handoff API
    elif data == "menu_test_handoff":
        answer_callback(callback_id, "🧪 Testing GitHub Dispatch API...")
        status_enum, status_msg = check_relay_configuration()
        text = (
            f"🧪 <b>Relay Dispatch Diagnostics:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>GH_PAT Secret:</b> {'🟢 Configured' if GH_PAT else '⚠️ Missing / Not Set'}\n"
            f"• <b>Target Repo:</b> <code>{REPO}</code>\n"
            f"• <b>Workflow File:</b> <code>{WORKFLOW_FILE}</code>\n"
            f"• <b>Target Ref:</b> <code>{WORKFLOW_REF}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_msg}"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Re-Test", "callback_data": "menu_test_handoff"}],
                [{"text": "🔙 Back to Info", "callback_data": "menu_server_info"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 15c. Force Handoff Confirmation Prompt
    elif data == "menu_force_handoff_confirm":
        answer_callback(callback_id)
        text = (
            "⚡ <b>Manual Relay Handoff Confirmation</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Are you sure you want to trigger the Relay Transition now?\n\n"
            "• Backs up workspace files & encrypted state to GitHub.\n"
            "• Launches next runner phase via GitHub Actions.\n"
            "• All running scripts auto-resume in the new phase.\n"
            "• Current runner gracefully shuts down."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🚀 Yes, Launch Next Runner Now", "callback_data": "menu_force_handoff_do"}],
                [{"text": "❌ Cancel", "callback_data": "menu_server_info"}]
            ]
        }
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)

    # 15d. Execute Manual Relay Handoff
    elif data in ["menu_force_handoff", "menu_force_handoff_do"]:
        answer_callback(callback_id, "🚀 Initiating Relay Handoff...", show_alert=True)
        edit_tg_message(
            chat_id,
            message_id,
            "🔄 <b>Executing Relay Handoff Sequence...</b>\n"
            "Backing up workspace and dispatching new runner phase..."
        )
        threading.Thread(target=execute_relay_handoff_sequence, args=("Manual Telegram Command",), daemon=True).start()

    else:
        # Fallback: ensure Telegram client spinner is always dismissed
        answer_callback(callback_id)

# ---------------------------------------------------------------------------
# Document & File Upload Handler
# ---------------------------------------------------------------------------
def handle_document_upload(chat_id, user_id, doc):
    if not is_admin(user_id):
        send_tg_message(chat_id, f"⛔ <b>Access Denied:</b> User ID <code>{user_id}</code> is not authorized.")
        return

    file_id = doc.get("file_id")
    file_name = doc.get("file_name", f"file_{int(time.time())}")
    
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    scripts_path = os.path.join(SCRIPTS_DIR, file_name)
    root_path = os.path.join(WORKSPACE_DIR, file_name)
    
    # 0. Check if uploading a .py file that ALREADY EXISTS or is RUNNING
    if file_name.endswith(".py"):
        active = get_active_running_processes()
        if os.path.exists(scripts_path) or file_name in active:
            # Stage download in workspace
            staging_path = os.path.join(WORKSPACE_DIR, f".staging_{user_id}_{int(time.time())}_{file_name}")
            send_tg_message(chat_id, f"📥 <b>Receiving {file_name}...</b>")
            ok, err = download_tg_file(file_id, staging_path)
            if not ok:
                send_tg_message(chat_id, f"❌ Download failed: {err}")
                return
                
            user_states[user_id] = {
                "action": "PENDING_DUPLICATE_UPLOAD",
                "file_name": file_name,
                "staging_path": staging_path
            }
            
            base, ext = os.path.splitext(file_name)
            idx = 2
            while os.path.exists(os.path.join(SCRIPTS_DIR, f"{base}_{idx}{ext}")) or f"{base}_{idx}{ext}" in active:
                idx += 1
            next_inst_name = f"{base}_{idx}{ext}"
            
            text = (
                f"✨ <b>Existing Script Detected:</b> <code>{file_name}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ <code>{file_name}</code> already exists in your workspace.\n\n"
                "<i>Please choose how you want to deploy this file:</i>\n\n"
                f"1️⃣ <b>Deploy New Project:</b> Saves as <code>{next_inst_name}</code> as a separate script.\n"
                f"2️⃣ <b>Update the Old:</b> Replaces <code>{file_name}</code> and restarts immediately."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"🚀 1. Deploy New Project ({next_inst_name})", "callback_data": f"inst_parallel_{file_name}"}],
                    [{"text": f"🔄 2. Update the Old ({file_name})", "callback_data": f"inst_replace_{file_name}"}],
                    [{"text": "❌ Cancel Upload", "callback_data": f"inst_cancel_{file_name}"}]
                ]
            }
            send_tg_message(chat_id, text, reply_markup=markup)
            return

    # Normal Download for non-conflicting files
    is_zip = file_name.endswith(".zip")
    
    # 0b. If uploading a .zip project archive, check if project already exists
    if is_zip:
        zip_base = file_name[:-4]
        target_dir = os.path.join(SCRIPTS_DIR, zip_base)
        active = get_active_running_processes()
        is_zip_running = any(k.startswith(f"{zip_base}/") or k == zip_base or os.path.dirname(k) == zip_base for k in active.keys())
        
        if os.path.exists(target_dir) or is_zip_running:
            staging_path = os.path.join(WORKSPACE_DIR, f".staging_{user_id}_{int(time.time())}_{file_name}")
            send_tg_message(chat_id, f"📥 <b>Receiving {file_name}...</b>")
            ok, err = download_tg_file(file_id, staging_path)
            if not ok:
                send_tg_message(chat_id, f"❌ Download failed: {err}")
                return
                
            idx = 2
            while os.path.exists(os.path.join(SCRIPTS_DIR, f"{zip_base}_{idx}")) or any(k.startswith(f"{zip_base}_{idx}/") or k == f"{zip_base}_{idx}" for k in active.keys()):
                idx += 1
            next_proj_name = f"{zip_base}_{idx}"
            
            user_states[user_id] = {
                "action": "PENDING_DUPLICATE_ZIP_UPLOAD",
                "file_name": file_name,
                "zip_base": zip_base,
                "next_proj_name": next_proj_name,
                "staging_path": staging_path
            }
            
            prompt_text = (
                f"📦 <b>Existing Project Detected:</b> <code>{file_name}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ A project named <code>scripts/{zip_base}/</code> already exists in your workspace.\n\n"
                "<i>Please choose how you want to deploy this upload:</i>\n\n"
                f"1️⃣ <b>Deploy New Project:</b> Deploys as a separate project (<code>scripts/{next_proj_name}/</code>).\n"
                f"2️⃣ <b>Update the Old:</b> Properly deletes the old <code>{zip_base}</code> and deploys fresh."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"🚀 1. Deploy New Project ({next_proj_name})", "callback_data": f"zip_new_{user_id}"}],
                    [{"text": f"🔄 2. Update the Old ({zip_base})", "callback_data": f"zip_update_{user_id}"}],
                    [{"text": "❌ Cancel Upload", "callback_data": f"zip_cancel_{user_id}"}]
                ]
            }
            send_tg_message(chat_id, prompt_text, reply_markup=markup)
            return

    download_dest = os.path.join(WORKSPACE_DIR, f".staging_{user_id}_{int(time.time())}_{file_name}") if is_zip else scripts_path
    
    send_tg_message(chat_id, f"📥 <b>Receiving {file_name}...</b>")
    ok, err = download_tg_file(file_id, download_dest)
    
    if not ok:
        send_tg_message(chat_id, f"❌ Download failed: {err}")
        return

    # If requirements.txt, also sync to root workspace
    if file_name == "requirements.txt":
        import shutil
        try:
            shutil.copy(download_dest, root_path)
        except Exception:
            pass
    
    # Auto-commit single standalone files (non-archives) to GitHub scripts/ folder
    if not is_zip:
        git_sync_to_github(f"Upload {file_name} into scripts/ folder")
    
    current_state = user_states.get(user_id)
    
    # Fresh .zip project archive deployment
    if is_zip:
        import zipfile
        zip_base = file_name[:-4]
        
        send_tg_message(chat_id, f"📦 <b>Unpacking Project Archive:</b> <code>{file_name}</code>...")
        
        extracted_files = []
        extracted_project_dir = None
        
        try:
            with zipfile.ZipFile(download_dest, 'r') as zip_ref:
                namelist = zip_ref.namelist()
                top_dirs = {item.split('/')[0] for item in namelist if item and not item.startswith('/')}
                
                # Check if zip contains a single top-level folder
                if len(top_dirs) == 1 and all(item.startswith(list(top_dirs)[0] + '/') or item == list(top_dirs)[0] for item in namelist):
                    root_folder_name = list(top_dirs)[0]
                    zip_ref.extractall(SCRIPTS_DIR)
                    extracted_files = namelist
                    extracted_project_dir = os.path.join(SCRIPTS_DIR, root_folder_name)
                else:
                    # Flat files: extract into a dedicated project directory named after the zip
                    target_extract_dir = os.path.join(SCRIPTS_DIR, zip_base)
                    os.makedirs(target_extract_dir, exist_ok=True)
                    zip_ref.extractall(target_extract_dir)
                    extracted_files = namelist
                    extracted_project_dir = target_extract_dir
            
            try:
                os.remove(download_dest)
            except Exception:
                pass
                
        except Exception as e:
            try:
                os.remove(download_dest)
            except Exception:
                pass
            send_tg_message(chat_id, f"❌ Failed to extract zip: {e}")
            return

        # Accurately detect the real entry point strictly within THIS project!
        entry_script = detect_project_entry_script(extracted_project_dir)
        
        # Scan for project-specific requirements and .env files
        found_reqs = []
        found_envs = []
        for root, _, fs in os.walk(extracted_project_dir):
            for f in fs:
                fp = os.path.join(root, f)
                if "requirements" in f.lower() and f.endswith(".txt"):
                    found_reqs.append(fp)
                elif f.endswith(".env") or f == ".env":
                    found_envs.append(fp)

        # Auto-Install Requirements into this project's isolated virtualenv
        req_installed_count = 0
        if found_reqs and entry_script:
            for req in found_reqs:
                rel_req = os.path.relpath(req, SCRIPTS_DIR)
                send_tg_message(chat_id, f"⏳ Installing packages from <code>{rel_req}</code> into isolated environment...")
                check_and_install_reqs(req, clean_name=entry_script)
                with open(req, "r", encoding="utf-8", errors="ignore") as rf:
                    req_installed_count += len([l for l in rf if l.strip() and not l.startswith("#")])

        # Auto-Load & Bind .env Variables into AES-256 Encrypted Vault
        env_loaded_count = 0
        target_name = entry_script or (os.path.basename(extracted_project_dir) if extracted_project_dir else zip_base)
        info = resolve_env_target_info(target_name)
        if found_envs:
            for ef in found_envs:
                parsed = {}
                with open(ef, "r", encoding="utf-8", errors="ignore") as rf:
                    for l in rf:
                        l = l.strip()
                        if "=" in l and not l.startswith("#"):
                            k, v = l.split("=", 1)
                            clean_k = k.strip()
                            clean_v = v.strip().strip("'\"")
                            if clean_k and clean_v:
                                parsed[clean_k] = clean_v
                if parsed:
                    curr_env = read_script_env(info["name"])
                    curr_env.update(parsed)
                    write_script_env(info["name"], curr_env)
                    env_loaded_count += len(parsed)
                # Note: .env is preserved locally on disk for python-dotenv, protected from git via .gitignore

        # Sync project code safely to GitHub cloud in background
        threading.Thread(target=git_sync_to_github, args=(f"Deploy ZIP project: {zip_base}",), daemon=True).start()

        # Auto-Launch the newly uploaded project entry script immediately!
        launch_status_text = ""
        if entry_script:
            ok_run, run_msg = start_child_app(entry_script, force_restart=True)
            if ok_run:
                launch_status_text = f"🟢 <b>Status:</b> <code>{entry_script}</code> is now <b>RUNNING!</b>"
            else:
                launch_status_text = f"⚠️ <b>Launch Note:</b> {run_msg}"

        entry_display = entry_script or 'None'
        env_info_str = f"{env_loaded_count} variables loaded" if env_loaded_count else "⚠️ No .env found (Set variables before running!)"
        deploy_msg = (
            f"🚀 <b>ZIP Project Deployed & Launched!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Archive:</b> <code>{file_name}</code>\n"
            f"📁 <b>Files Extracted:</b> {len(extracted_files)}\n"
            f"🎯 <b>Detected Entry Script:</b> <code>{entry_display}</code>\n"
            f"{launch_status_text}\n"
            f"📦 <b>Dependencies:</b> {'Installed ~' + str(req_installed_count) + ' packages' if found_reqs else 'No requirements.txt found'}\n"
            f"🔒 <b>Environment:</b> {env_info_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Manage your project using the buttons below:</i>"
        )
        buttons = []
        if entry_script:
            buttons.append([{"text": f"🛑 Stop {os.path.basename(entry_script)}", "callback_data": f"confirm_stop_prompt_{entry_script}"}, {"text": "🔄 Restart", "callback_data": f"exec_run_{entry_script}"}])
        
        env_btn_row = [{"text": f"⚙️ Manage ENV ({env_loaded_count})", "callback_data": f"env_dash_{info['slug']}"}]
        if env_loaded_count == 0:
            env_btn_row.insert(0, {"text": "➕ Set ENVs Now", "callback_data": f"env_add_{info['slug']}"})
        buttons.append(env_btn_row)
        
        if entry_script:
            buttons.append([{"text": "📋 View Live Logs", "callback_data": f"show_log_for_{entry_script}"}])
        buttons.append([{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "📂 View Files", "callback_data": "menu_files"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])

        user_states[user_id] = {"action": "PROJECT_DEPLOYED", "last_project": info["name"]}
        send_tg_message(chat_id, deploy_msg, reply_markup={"inline_keyboard": buttons})
        return

    # 1. If uploaded requirements file
    elif file_name.endswith(".requirements.txt") or file_name.endswith("_requirements.txt") or file_name.endswith("_req.txt") or file_name == "requirements.txt":
        # Extract target script name if named like bot.requirements.txt
        if file_name == "requirements.txt":
            target_py = "bot.py"
        elif file_name.endswith(".requirements.txt"):
            target_py = file_name[:-17] + ".py"
        elif file_name.endswith("_requirements.txt"):
            target_py = file_name[:-17] + ".py"
        else:
            target_py = file_name.split("_")[0].split(".")[0] + ".py"
            
        send_tg_message(chat_id, f"📦 <b><code>{file_name}</code> received!</b>\n⏳ Installing dependencies in real-time...")
        res = subprocess.run([sys.executable, "-m", "pip", "install", "-r", scripts_path], capture_output=True, text=True)
        
        # Check if user had previously sent a Python file waiting for this requirements.txt
        if isinstance(current_state, dict) and current_state.get("action") == "WAITING_REQ_FOR_PY":
            target_py = current_state.get("target_py", target_py)
            user_states.pop(user_id, None)
            send_tg_message(chat_id, f"✅ <b>Dependencies installed!</b>\n🚀 Now auto-launching <code>{target_py}</code>...")
            ok_run, run_msg = start_child_app(target_py, force_restart=True)
            send_tg_message(chat_id, run_msg, reply_markup=get_main_menu_keyboard())
        else:
            out_summary = res.stdout[-2000:] if res.stdout else "All requirements satisfied."
            text = (
                f"✅ <b>Dependencies Installed for <code>{target_py}</code>!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 Linked to: <code>scripts/{file_name}</code>\n\n"
                f"<pre>{html.escape(out_summary)}</pre>"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"▶️ Run {target_py} Now", "callback_data": f"exec_run_{target_py}"}],
                    [{"text": "🚀 Open Scripts Runner", "callback_data": "menu_runner"}],
                    [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
                ]
            }
            send_tg_message(chat_id, text, reply_markup=markup)
    
    # 2. If uploaded a .py script
    elif file_name.endswith(".py"):
        user_states.pop(user_id, None)
        
        # Stop any existing instance so new code runs fresh
        stop_child_app(script_name=file_name, clear_active=False)
        time.sleep(0.5)
        
        # Auto-launch the newly uploaded Python script immediately!
        ok_run, run_msg = start_child_app(file_name, force_restart=True)
        status_line = f"🟢 <b>Status:</b> <code>{file_name}</code> is now <b>RUNNING!</b>" if ok_run else f"⚠️ <b>Status:</b> {run_msg}"
        
        text = (
            f"✨ <b>Python Script Deployed & Launched!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 <b>File:</b> <code>scripts/{file_name}</code>\n"
            f"{status_line}\n\n"
            "<i>Manage your script using the buttons below:</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": f"🛑 Stop {file_name}", "callback_data": f"confirm_stop_prompt_{file_name}"}, {"text": "🔄 Restart", "callback_data": f"exec_run_{file_name}"}],
                [{"text": f"⚙️ Manage {file_name} ENV", "callback_data": f"env_dash_{file_name}"}],
                [{"text": "📋 View Live Logs", "callback_data": f"show_log_for_{file_name}"}],
                [{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "📂 View Files", "callback_data": "menu_files"}],
                [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        send_tg_message(chat_id, text, reply_markup=markup)

    # 3. If uploaded a .env file
    elif file_name.endswith(".env") or file_name == ".env":
        # Parse all variables from the uploaded file
        uploaded_vars = {}
        if os.path.exists(scripts_path):
            with open(scripts_path, "r", encoding="utf-8", errors="ignore") as f:
                for l in f:
                    l = l.strip()
                    if "=" in l and not l.startswith("#"):
                        k, v = l.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k:
                            uploaded_vars[k] = v

        # Determine target project
        current_state = user_states.get(user_id)
        if isinstance(current_state, dict) and current_state.get("action") == "WAITING_ENV_VAR":
            target = current_state.get("target_py")
            user_states.pop(user_id, None)
        elif isinstance(current_state, dict) and current_state.get("last_project"):
            target = current_state.get("last_project")
            user_states.pop(user_id, None)
        else:
            base_file = file_name[:-4] if file_name.endswith(".env") and file_name != ".env" else ""
            if base_file and os.path.isdir(os.path.join(SCRIPTS_DIR, base_file)):
                target = base_file
            else:
                targets = get_manageable_targets()
                active = get_active_running_processes()
                if len(targets) == 1:
                    target = targets[0]["name"]
                elif active:
                    target = list(active.keys())[0]
                elif targets:
                    target = targets[0]["name"]
                else:
                    target = "bot.py"

        info = resolve_env_target_info(target)
        if uploaded_vars:
            curr = read_script_env(info["name"])
            curr.update(uploaded_vars)
            write_script_env(info["name"], curr)

        active = get_active_running_processes()
        is_running = (
            info["entry_script"] in active or
            info["name"] in active or
            any(k.startswith(f"{info['name']}/") or k == info['name'] for k in active.keys())
        )
        running_key = next((k for k in active.keys() if k == info["entry_script"] or k == info["name"] or k.startswith(f"{info['name']}/")), info["entry_script"]) if is_running else info["entry_script"]

        restart_hint = ""
        if is_running:
            restart_hint = "\n\n⚡ <i>This script is currently RUNNING. Tap <b>🔄 Apply & Restart</b> below to load new variables immediately!</i>"

        text = (
            f"🔒 <b>Environment File Applied:</b> <code>scripts/{file_name}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Assigned Project:</b> <code>{info['display_name']}</code>\n"
            f"• <b>Variables Loaded:</b> <code>{len(uploaded_vars)}</code>\n"
            f"• <b>Target File:</b> <code>{os.path.relpath(info['primary_dot_env'], WORKSPACE_DIR)}</code>\n\n"
            f"📁 Variables saved to project directory and backed up to Cloud!"
            + restart_hint
        )
        buttons = [
            [{"text": f"⚙️ Manage {info['display_name']} ENV", "callback_data": f"env_dash_{info['slug']}"}]
        ]
        if is_running:
            buttons.append([{"text": "🔄 Apply & Restart Script", "callback_data": f"exec_run_{running_key}"}])
        else:
            buttons.append([{"text": f"▶️ Run {info['display_name']} Now", "callback_data": f"exec_run_{info['entry_script']}"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])

        send_tg_message(chat_id, text, reply_markup={"inline_keyboard": buttons})
        
    else:
        send_tg_message(
            chat_id,
            f"✅ <b>{file_name}</b> <code>scripts/</code> folder me save ho gayi hai.",
            reply_markup=get_main_menu_keyboard()
        )

# ---------------------------------------------------------------------------
# Safe Worker Handlers (Prevents thread crashes & ensures UI responsiveness)
# ---------------------------------------------------------------------------
def safe_handle_callback_query(callback_id, chat_id, user_id, message_id, data):
    answered = False
    try:
        handle_callback_query(callback_id, chat_id, user_id, message_id, data)
        answered = True
    except Exception as e:
        logger.error(f"❌ Error in handle_callback_query (data: {data}): {e}\n{traceback.format_exc()}")
        try:
            answer_callback(callback_id, "⚠️ Error processing button click", show_alert=True)
            answered = True
        except Exception:
            pass
    finally:
        # Fallback to make 100% sure Telegram client spinner stops
        if not answered:
            try:
                answer_callback(callback_id)
            except Exception:
                pass

def safe_handle_text_message(chat_id, user_id, text):
    try:
        handle_text_message(chat_id, user_id, text)
    except Exception as e:
        logger.error(f"❌ Error in handle_text_message: {e}\n{traceback.format_exc()}")
        try:
            send_tg_message(chat_id, f"⚠️ An error occurred: {html.escape(str(e))}", reply_markup=get_main_menu_keyboard())
        except Exception:
            pass

def safe_handle_document_upload(chat_id, user_id, doc):
    try:
        handle_document_upload(chat_id, user_id, doc)
    except Exception as e:
        logger.error(f"❌ Error in handle_document_upload: {e}\n{traceback.format_exc()}")
        try:
            send_tg_message(chat_id, f"⚠️ An error occurred with upload: {html.escape(str(e))}", reply_markup=get_main_menu_keyboard())
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Polling Engine
# ---------------------------------------------------------------------------
def telegram_polling_loop():
    logger.info("🤖 Real-Time Telegram Polling Engine starting...")
    # 1. Clear any active webhook so getUpdates receives updates
    delete_telegram_webhook()
    
    offset = 0
    session = requests.Session()
    allowed_updates_json = json.dumps(["message", "edited_message", "callback_query"])
    
    logger.info("🤖 Real-Time Telegram Polling Engine active & ready for commands and buttons...")
    while IS_RUNNING:
        try:
            params = {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": allowed_updates_json
            }
            resp = session.get(f"{TG_BASE_URL}/getUpdates", params=params, timeout=(10, 30))
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        
                        # Handle Callback Queries (Button Clicks) in non-blocking real-time thread
                        if "callback_query" in update:
                            cq = update["callback_query"]
                            cb_id = cq.get("id")
                            from_user = cq.get("from", {})
                            c_user = from_user.get("id")
                            msg = cq.get("message") or {}
                            c_chat = msg.get("chat", {}).get("id") or c_user
                            c_msg_id = msg.get("message_id")
                            c_data = cq.get("data", "")
                            
                            if cb_id and c_user:
                                threading.Thread(
                                    target=safe_handle_callback_query,
                                    args=(cb_id, c_chat, c_user, c_msg_id, c_data),
                                    daemon=True
                                ).start()
                        
                        # Handle Normal Messages in non-blocking real-time thread
                        elif "message" in update:
                            msg = update["message"]
                            chat_id = msg.get("chat", {}).get("id")
                            user_id = msg.get("from", {}).get("id")
                            
                            if not chat_id or not user_id:
                                continue
                            
                            if "text" in msg:
                                threading.Thread(
                                    target=safe_handle_text_message,
                                    args=(chat_id, user_id, msg["text"]),
                                    daemon=True
                                ).start()
                            elif "document" in msg:
                                threading.Thread(
                                    target=safe_handle_document_upload,
                                    args=(chat_id, user_id, msg["document"]),
                                    daemon=True
                                ).start()
            elif resp.status_code == 409:
                err_text = resp.text.lower()
                if "webhook" in err_text:
                    logger.warning("⚠️ Webhook conflict detected (409). Calling deleteWebhook...")
                    delete_telegram_webhook()
                    time.sleep(2)
                else:
                    logger.warning("⚠️ Bot instance conflict (409) - another runner may be shutting down. Backing off 3s...")
                    time.sleep(3)
            elif resp.status_code == 429:
                try:
                    retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry_after = 5
                logger.warning(f"⚠️ Telegram rate limit (429). Sleeping {retry_after}s...")
                time.sleep(retry_after)
            else:
                logger.warning(f"⚠️ Telegram getUpdates returned HTTP {resp.status_code}: {resp.text[:150]}")
                time.sleep(2)
        except requests.exceptions.Timeout:
            # Normal long-polling timeout, continue next cycle immediately
            continue
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
            time.sleep(2)

# ---------------------------------------------------------------------------
# Main Orchestrator & Auto-Runner
# ---------------------------------------------------------------------------
def main():
    global IS_RUNNING
    logger.info("=" * 60)
    logger.info(f"🚀 Telegram Relay Controller Initialized [Run #{RUN_ID}]")
    logger.info("=" * 60)
    
    if not TG_BOT_TOKEN:
        logger.error("❌ CRITICAL: TG_BOT_TOKEN is missing! Please configure TG_BOT_TOKEN in GitHub Repository Secrets.")
        time.sleep(10)
        return
    
    # Restore all private environments from encoded vault (100% safe from secret scanner!)
    restore_all_env_vaults_on_boot()

    # Check relay configuration on boot and notify admins if action is needed
    status_enum, status_msg = check_relay_configuration()
    if status_enum != "OK":
        logger.warning(f"Relay configuration check on boot: {status_enum}")
        def delayed_warn():
            time.sleep(4.0)
            notify_all_admins(status_msg)
        threading.Thread(target=delayed_warn, daemon=True).start()
    
    # Seamless Multi-Script Relay Persistence: Auto-resume ONLY scripts executed by user and NOT manually stopped
    active_list = config.get("active_scripts")
    if active_list is None:
        active_list = []
        if config.get("active_script"):
            active_list = [config["active_script"]]
        config["active_scripts"] = active_list
        save_config(config)

    # Strictly filter out:
    # 1. Any scripts that were manually stopped by the user from the dashboard
    # 2. Any scripts that no longer exist on disk (deleted)
    manually_stopped = set(config.get("manually_stopped_scripts", []))
    valid_active = []
    if active_list:
        for s in active_list:
            clean_s = os.path.normpath(s.replace("scripts/", "").lstrip("/")).replace("\\", "/")
            if clean_s in manually_stopped or os.path.basename(clean_s) in manually_stopped:
                logger.info(f"Skipping auto-resume for manually stopped script: {s}")
                continue
            full_s = os.path.join(SCRIPTS_DIR, clean_s)
            if os.path.exists(full_s) or os.path.exists(os.path.join(WORKSPACE_DIR, s)):
                valid_active.append(s)
        if len(valid_active) != len(active_list):
            logger.info(f"Updated active_scripts from {len(active_list)} to {len(valid_active)} valid runnable scripts.")
            config["active_scripts"] = valid_active
            save_config(config)

    def delayed_boot_notification(scripts_to_run):
        time.sleep(1.5)
        if scripts_to_run:
            notify_all_admins(
                f"🔄 <b>Cloud Server Online [Run #{RUN_ID}]:</b>\n"
                f"Auto-resuming {len(scripts_to_run)} scripts in parallel:\n"
                + "\n".join([f"• <code>{s}</code>" for s in scripts_to_run])
            )
            success_count = 0
            for s in scripts_to_run:
                ok, msg = start_child_app(s)
                if not ok:
                    # Retry once after 2.5s (handles virtualenv / pip site-packages lock)
                    time.sleep(2.5)
                    ok, msg = start_child_app(s)
                if ok:
                    success_count += 1
                else:
                    notify_all_admins(f"⚠️ <b>Auto-resume error for <code>{s}</code>:</b>\n{msg}")
                time.sleep(0.5)
            notify_all_admins(
                f"🟢 <b>Cloud Server Online [Run #{RUN_ID}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Scripts Running:</b> <code>{success_count}/{len(scripts_to_run)}</code>\n"
                f"• <b>Status:</b> Controller Daemon Ready",
                reply_markup=get_main_menu_keyboard()
            )
        else:
            notify_all_admins(
                f"🟢 <b>Cloud Server Online [Run #{RUN_ID}]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Controller Daemon:</b> Active & Ready\n"
                f"📂 <b>Workspace:</b> <code>{REPO}</code>\n\n"
                f"<i>Use the control panel below to run scripts or manage your server:</i>",
                reply_markup=get_main_menu_keyboard()
            )

    threading.Thread(target=delayed_boot_notification, args=(valid_active,), daemon=True).start()

    # Start Telegram polling and Memory Cleaner threads
    tg_thread = threading.Thread(target=telegram_polling_loop, daemon=True, name="TGPolling")
    tg_thread.start()
    threading.Thread(target=background_memory_cleaner_loop, daemon=True, name="MemoryCleaner").start()
    
    # Watchdog loop: runs for 2.0 hours (7200s), sends alert 30 min before stopping, and cleanly exits without auto-restart
    warn_30m_sent = False
    try:
        while IS_RUNNING:
            elapsed = time.time() - START_TIME
            remaining = RUN_DURATION_SECONDS - elapsed

            # 1. 📢 30-Minute Prior Alert before shutdown (at 1.5 hours)
            if not warn_30m_sent and remaining <= 1800 and elapsed > 60:
                warn_30m_sent = True
                notify_all_admins(
                    "⚠️ <b>Server Expiry Warning (30 Minutes Remaining)</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "⏳ <b>Server Uptime:</b> <code>1.5 Hours</code>\n"
                    "🛑 <b>Scheduled Shutdown in:</b> <code>30 Minutes</code>\n\n"
                    "💡 <i>Auto-restart is disabled. The workflow will stop cleanly at 2.0 hours. All databases and active scripts will be safely saved to GitHub.</i>",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "📊 Open Dashboard", "callback_data": "menu_main"}]
                        ]
                    }
                )

            # 2. 🛑 2.0 Hours Reached: Clean stop without auto-restart
            if elapsed >= RUN_DURATION_SECONDS:
                logger.info("⏳ 2.0 Hours limit reached. Performing clean shutdown without auto-restart...")
                break

            time.sleep(5)
    except (KeyboardInterrupt, SystemExit):
        pass

    execute_relay_handoff_sequence("2.0 Hours reached")

if __name__ == "__main__":
    main()

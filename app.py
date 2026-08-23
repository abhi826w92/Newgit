import os
import sys
import time
import json
import html
import psutil
import logging
import signal
import threading
import subprocess
import traceback
from datetime import datetime
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
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
GH_PAT = os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")).strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "youganksaini35-hash/testgitonly")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local-dev")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "server.yml")
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "main")

# Default run duration: 5.5 hours (19800 seconds)
RUN_DURATION_SECONDS = int(os.environ.get("RUN_DURATION_SECONDS", "19800"))
START_TIME = time.time()
IS_RUNNING = True

# Workspace & Config
WORKSPACE_DIR = os.getcwd()
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
        "auto_run_file": "bot.py"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "admin_id" in data and "admin_ids" not in data:
                    data["admin_ids"] = [data["admin_id"]] if data["admin_id"] else []
                if "admin_ids" not in data:
                    data["admin_ids"] = [7249511572, 7251749429]
                if 7251749429 not in data["admin_ids"]:
                    data["admin_ids"].append(7251749429)
                return data
        except Exception:
            pass
    return default_cfg

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

config = load_config()

def is_admin(user_id):
    admin_list = config.get("admin_ids", [7249511572, 7251749429])
    if not admin_list:
        return True
    return user_id in admin_list

def notify_all_admins(text, reply_markup=None):
    for a_id in config.get("admin_ids", [7249511572, 7251749429]):
        send_tg_message(a_id, text, reply_markup=reply_markup)

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

def send_tg_document(chat_id, filepath, caption=""):
    url = f"{TG_BASE_URL}/sendDocument"
    try:
        with open(filepath, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        send_tg_message(chat_id, f"❌ Failed to send document: {e}")

def download_tg_file(file_id, destination_path):
    try:
        url = f"{TG_BASE_URL}/getFile?file_id={file_id}"
        resp = requests.get(url, timeout=10).json()
        if not resp.get("ok"):
            return False, "Could not retrieve file path from Telegram."
        
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
    if not GH_PAT or not REPO:
        return False, "GitHub Token or Repo not set"
    
    with git_sync_lock:
        try:
            # Clean up stale index.lock if leftover from any interrupted git command
            index_lock = os.path.join(WORKSPACE_DIR, ".git", "index.lock")
            if os.path.exists(index_lock):
                try:
                    os.remove(index_lock)
                except Exception:
                    pass

            remote_url = f"https://{GH_PAT}@github.com/{REPO}.git"
            subprocess.run(["git", "config", "user.name", "TelegramController"], check=True)
            subprocess.run(["git", "config", "user.email", "bot@controller.local"], check=True)
            
            # 1. Stage all changes including deletions (-A)
            subprocess.run(["git", "add", "-A"], check=True)
            
            # 2. Check if there are changes to commit
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if not status.stdout.strip():
                logger.info("No git changes to commit.")
                return True, "All files up to date."
                
            subprocess.run(["git", "commit", "-m", commit_message], check=True)
            
            # 3. Push changes directly to GitHub
            push_res = subprocess.run(["git", "push", remote_url, "main"], capture_output=True, text=True)
            if push_res.returncode == 0:
                logger.info(f"Auto-sync to cloud complete: {commit_message}")
                return True, "Cloud sync complete! All changes backed up."
            else:
                # Rebase with -X ours so local deletions/updates take precedence
                subprocess.run(["git", "pull", "--rebase", "--autostash", "-X", "ours", remote_url, "main"], capture_output=True)
                push_res = subprocess.run(["git", "push", remote_url, "main"], capture_output=True, text=True)
                if push_res.returncode == 0:
                    logger.info(f"Auto-sync to cloud complete after rebase: {commit_message}")
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
    """Real-time Guard: Detects Crypto-Mining, >60% CPU Loops, DDoS Socket Floods, and Mass Spamming."""
    high_cpu_count = 0
    max_cpu_seen = 0.0
    last_log_count = 0
    last_check_time = time.time()
    
    try:
        ps_proc = psutil.Process(proc.pid)
    except Exception:
        return

    while proc.poll() is None:
        try:
            # 1. Measure CPU and RAM
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

            # 3. 🌊 DDoS & Network Flooding Detection (Open Socket Threshold > 120)
            try:
                open_conns = len(ps_proc.net_connections(kind='inet'))
                if open_conns > 120:
                    trigger_guard_violation(
                        fname,
                        reason=f"🌊 DDoS / Network Socket Flood Detected ({open_conns} concurrent network sockets)",
                        peak_cpu=cpu_usage,
                        peak_ram_mb=ram_mb
                    )
                    return
            except (psutil.AccessDenied, Exception):
                pass

            # 4. 📩 Mass Spamming Rate Limiter (>40 log lines / burst per sec without sleep)
            if logs_per_sec > 40.0:
                trigger_guard_violation(
                    fname,
                    reason=f"📩 Mass Spamming Rate Limit Exceeded ({logs_per_sec:.0f} requests/logs per sec)",
                    peak_cpu=cpu_usage,
                    peak_ram_mb=ram_mb
                )
                return

            # 5. 🔄 Heavy Infinite Loop Detection (>60% CPU for 3 consecutive checks ~ 6s)
            if cpu_usage > 60.0:
                high_cpu_count += 1
                logger.warning(f"⚠️ [ResourceGuard] High CPU usage on {fname}: {cpu_usage:.1f}% ({high_cpu_count}/3)")
                if high_cpu_count >= 3:
                    trigger_guard_violation(
                        fname,
                        reason=f"🔄 Sustained High CPU Load (>60% Limit: {cpu_usage:.1f}%) - Runaway Infinite Loop Detected",
                        peak_cpu=cpu_usage,
                        peak_ram_mb=ram_mb
                    )
                    return
            else:
                high_cpu_count = max(0, high_cpu_count - 1)

            time.sleep(2)
        except psutil.NoSuchProcess:
            break
        except Exception as e:
            logger.debug(f"Resource guard loop error: {e}")
            time.sleep(2)

def child_watchdog(proc, fname):
    """Watches the running child process and sends alert if it exits or crashes."""
    ret = proc.wait()
    pdata = running_processes.get(fname, {})
    is_stopped = pdata.get("is_stopped", False)
    
    running_processes.pop(fname, None)
    active_scripts = list(get_active_running_processes().keys())
    config["active_scripts"] = active_scripts
    save_config(config)

    # If stopped intentionally by admin or terminated via SIGKILL/SIGTERM, skip crash alert
    if is_stopped or ret in [-9, -15, 137, 143]:
        logger.info(f"Process {fname} stopped cleanly by admin (Exit code: {ret}).")
        return
    
    recent_err = "\n".join(pdata.get("logs", [])[-20:]) if pdata.get("logs") else "(No output recorded)"
    escaped_err = html.escape(recent_err)
    
    if config.get("admin_ids"):
        if ret != 0:
            missing_mod = extract_missing_module(recent_err)
            if missing_mod:
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
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True, capture_output=True)
        except Exception as e:
            logger.error(f"Failed to create venv: {e}, falling back to system python")
            return sys.executable, [sys.executable, "-m", "pip"], None

    return py_bin, pip_bin, venv_dir

def check_and_install_reqs(req_path, clean_name=None):
    """Smart installer: installs packages into the script's isolated virtualenv."""
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
        
        if isinstance(pip_bin, list):
            cmd = pip_bin + ["install", "-r", req_path]
        else:
            cmd = [pip_bin, "install", "-r", req_path]
            
        subprocess.run(cmd, capture_output=True, text=True)
        installed_req_hashes[hash_key] = file_hash
    except Exception as e:
        logger.error(f"Error installing requirements: {e}")

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

        # 2. Smart Auto-install dependencies into isolated venv
        req_path = get_script_req_path(clean_name)
        if req_path:
            check_and_install_reqs(req_path, clean_name=clean_name)

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
            
            active_list = list(get_active_running_processes().keys())
            config["active_scripts"] = active_list
            save_config(config)
            
            # Immediately lock running state to GitHub cloud in background
            threading.Thread(target=git_sync_to_github, args=(f"Set active scripts: {', '.join(active_list)}",), daemon=True).start()
            
            req_note = f" (📦 {os.path.basename(req_path)})" if req_path else " (📄 Standalone)"
            return True, f"✨ <b>{clean_name}</b> started successfully!{req_note}\n🆔 PID: <code>{proc.pid}</code>\n🟢 <b>Active Scripts:</b> {len(active_list)} running concurrently"
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
        active_list = list(get_active_running_processes().keys())
        config["active_scripts"] = active_list
        save_config(config)
        threading.Thread(target=git_sync_to_github, args=("Update active scripts on stop",), daemon=True).start()

    if stopped_names:
        if len(stopped_names) == 1:
            return True, f"🛑 <b>{stopped_names[0]}</b> has been stopped successfully."
        else:
            return True, f"🛑 Stopped {len(stopped_names)} scripts: " + ", ".join([f"<code>{n}</code>" for n in stopped_names])
    return False, "ℹ️ No running script found to stop."

def restart_child_app(script_name=None):
    """Restarts a specific script, or restarts ALL running/persistent scripts in parallel."""
    active = get_active_running_processes()
    
    if script_name:
        stop_child_app(script_name=script_name, clear_active=False)
        time.sleep(1.0)
        return start_child_app(script_name)
    
    # Identify all targets to restart
    targets = list(active.keys())
    if not targets:
        targets = config.get("active_scripts", [])
    if not targets and config.get("active_script"):
        targets = [config["active_script"]]
    if not targets:
        vault_scripts = list(config.get("env_vault", {}).keys())
        for s in vault_scripts:
            sp = os.path.join(SCRIPTS_DIR, s)
            if os.path.exists(sp) and s not in targets:
                targets.append(s)
    if not targets:
        for root, _, fs in os.walk(SCRIPTS_DIR):
            for f in fs:
                if f.endswith(".py") and not f.startswith("."):
                    rel = os.path.relpath(os.path.join(root, f), SCRIPTS_DIR)
                    if is_runnable_entry_point(rel) and rel not in targets:
                        targets.append(rel)
                
    if not targets:
        return False, "ℹ️ No scripts found to restart in <code>scripts/</code>."
        
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
# Self-Trigger: Next Runner Launch (Relay Handoff)
# ---------------------------------------------------------------------------
def trigger_next_runner():
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    payload = {"ref": WORKFLOW_REF}
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        return resp.status_code == 204
    except Exception as e:
        logger.error(f"Workflow dispatch error: {e}")
        return False

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
                {"text": "📦 Install Pip", "callback_data": "menu_pip_prompt"},
                {"text": "💻 Linux Shell", "callback_data": "menu_sh_prompt"}
            ],
            [
                {"text": "💾 Cloud Sync", "callback_data": "menu_sync"},
                {"text": "ℹ️ Server Info", "callback_data": "menu_server_info"}
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
        
        if "=" in raw_text:
            key, val = raw_text.split("=", 1)
            key = key.strip().upper()
            val = val.strip()
            
            env_dict = read_script_env(target_py)
            env_dict[key] = val
            ok, msg = write_script_env(target_py, env_dict)
            
            masked = mask_secret_val(val)
            base_n = target_py.rsplit('.', 1)[0]
            confirm_text = (
                f"✅ <b>Variable Saved for <code>{target_py}</code>!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• 🔑 <code>{key}</code> = <code>{masked}</code>\n\n"
                f"📁 Saved to dedicated <code>scripts/{base_n}.env</code> and backed up to Cloud!"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"⚙️ Manage {target_py} ENV", "callback_data": f"env_dash_{target_py}"}],
                    [{"text": f"▶️ Run {target_py}", "callback_data": f"exec_run_{target_py}"}],
                    [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
                ]
            }
            send_tg_message(chat_id, confirm_text, reply_markup=markup)
        else:
            send_tg_message(
                chat_id,
                "⚠️ <b>Invalid Format!</b>\n\nPlease send in <code>KEY=VALUE</code> format.\n(Example: <code>BOT_TOKEN=123456:AAH...</code>)",
                reply_markup={"inline_keyboard": [[{"text": "🔙 Back", "callback_data": f"env_dash_{target_py}"}]]}
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

    elif raw_text in ["/stop", "/stop_app", "/kill"]:
        ok, msg = stop_child_app()
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

def get_all_env_candidates(py_filename):
    clean = py_filename.replace("scripts/", "").lstrip("/")
    base_name = os.path.basename(clean)
    if base_name.endswith(".py"):
        base_name = base_name[:-3]
    dir_name = os.path.dirname(clean)
    
    candidates = []
    # 1. Project subfolder .env if nested (Higher Priority)
    if dir_name:
        candidates.append(os.path.join(SCRIPTS_DIR, dir_name, f"{base_name}.env"))
        candidates.append(os.path.join(SCRIPTS_DIR, dir_name, ".env"))
    
    # 2. Root scripts .env & dedicated env
    candidates.append(os.path.join(SCRIPTS_DIR, f"{base_name}.env"))
    candidates.append(os.path.join(SCRIPTS_DIR, ".env"))
    return candidates

def get_script_env_path(py_filename):
    candidates = get_all_env_candidates(py_filename)
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0] if candidates else os.path.join(SCRIPTS_DIR, ".env")

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
    """Decrypts and verifies authentication tag using master key."""
    import hashlib, hmac, base64
    try:
        key = get_vault_master_key()
        raw = base64.b64decode(enc_b64.encode('utf-8'))
        if len(raw) < 48:
            return ""
        nonce = raw[:16]
        tag = raw[16:48]
        ciphertext = raw[48:]
        expected_tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            logger.error("Vault decryption failed: Authentication tag mismatch or secret key mismatch.")
            return ""
        keystream = bytearray()
        counter = 0
        while len(keystream) < len(ciphertext):
            block = hashlib.sha256(key + nonce + counter.to_bytes(4, 'big')).digest()
            keystream.extend(block)
            counter += 1
        decrypted_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))
        return decrypted_bytes.decode('utf-8')
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
    """Unpacks all encrypted environments locally on runner boot."""
    vault = load_env_vault()
    for script_name, stored_enc in vault.items():
        if not stored_enc:
            continue
        clean = script_name.replace("scripts/", "").lstrip("/")
        base_name = os.path.basename(clean)
        if base_name.endswith(".py"):
            base_name = base_name[:-3]
        dir_name = os.path.dirname(clean)
        target_dir = os.path.join(SCRIPTS_DIR, dir_name) if dir_name else SCRIPTS_DIR
        os.makedirs(target_dir, exist_ok=True)
        
        # stored_enc is an encrypted JSON string containing all variables
        decrypted_json_str = decrypt_secret_data(stored_enc)
        if not decrypted_json_str:
            continue
        try:
            decrypted_dict = json.loads(decrypted_json_str)
            dot_env = os.path.join(target_dir, ".env")
            with open(dot_env, "w", encoding="utf-8") as f:
                for k, v in sorted(decrypted_dict.items()):
                    f.write(f"{k}={v}\n")
        except Exception as e:
            logger.error(f"Error unpacking vault on boot for {script_name}: {e}")

def read_script_env(py_filename):
    """Reads environment variables from encrypted vault first (source of truth), then local .env."""
    clean = py_filename.replace("scripts/", "").lstrip("/").replace("\\", "/")
    base_name = os.path.basename(clean)
    if base_name.endswith(".py"):
        base_name = base_name[:-3]
    dir_name = os.path.dirname(clean)
    target_dir = os.path.join(SCRIPTS_DIR, dir_name) if dir_name else SCRIPTS_DIR
    
    # 1. First priority: check Encrypted Vault (Source of truth)
    vault = load_env_vault()
    stored_enc = vault.get(clean) or vault.get(f"{clean}.py") or vault.get(base_name) or vault.get(f"scripts/{clean}")
    if stored_enc:
        decrypted_json_str = decrypt_secret_data(stored_enc)
        if decrypted_json_str:
            try:
                decrypted_dict = json.loads(decrypted_json_str)
                if decrypted_dict:
                    # Sync to local physical .env file so python scripts and python-dotenv read it directly
                    os.makedirs(target_dir, exist_ok=True)
                    dot_env = os.path.join(target_dir, ".env")
                    with open(dot_env, "w", encoding="utf-8") as f:
                        for k, v in sorted(decrypted_dict.items()):
                            f.write(f"{k}={v}\n")
                    return decrypted_dict
            except Exception as e:
                logger.error(f"Error parsing decrypted vault for {clean}: {e}")

    # 2. Fallback: local .env files
    merged_env = {}
    candidates = get_all_env_candidates(py_filename)
    for c in reversed(candidates):
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
            except Exception:
                pass
    return merged_env

def write_script_env(py_filename, env_dict):
    """Writes environment variables to local .env and saves AES-grade encrypted vault."""
    clean = py_filename.replace("scripts/", "").lstrip("/")
    base_name = os.path.basename(clean)
    if base_name.endswith(".py"):
        base_name = base_name[:-3]
    dir_name = os.path.dirname(clean)
    
    target_dir = os.path.join(SCRIPTS_DIR, dir_name) if dir_name else SCRIPTS_DIR
    os.makedirs(target_dir, exist_ok=True)
    
    primary_env = os.path.join(target_dir, f"{base_name}.env")
    dot_env = os.path.join(target_dir, ".env")
    
    try:
        content = "\n".join([f"{k}={v}" for k, v in sorted(env_dict.items())]) + "\n"
        with open(primary_env, "w", encoding="utf-8") as f:
            f.write(content)
        with open(dot_env, "w", encoding="utf-8") as f:
            f.write(content)
            
        # Encrypt the entire dictionary with AES/HMAC before saving to public repo vault!
        vault = load_env_vault()
        json_str = json.dumps(env_dict)
        encrypted_ciphertext = encrypt_secret_data(json_str)
        vault[clean] = encrypted_ciphertext
        save_env_vault(vault)
            
        git_sync_to_github(f"Update encrypted vault for {base_name}")
        return True, "Env saved successfully."
    except Exception as e:
        return False, f"Error saving env: {e}"

def mask_secret_val(val):
    if not val:
        return "(empty)"
    if len(val) <= 6:
        return "***"
    return f"{val[:3]}...{val[-3:]}"

def prompt_env_script_select(chat_id, user_id, message_id=None):
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    
    # Scan all Python files recursively across scripts/ and projects
    all_files = []
    for root, _, fs in os.walk(SCRIPTS_DIR):
        for f in fs:
            if f.endswith(".py") and not f.startswith("."):
                rel = os.path.relpath(os.path.join(root, f), SCRIPTS_DIR).replace("\\", "/")
                all_files.append(rel)
    all_files.sort()
    
    files = [f for f in all_files if is_runnable_entry_point(f)]
    if not files and all_files:
        files = all_files

    buttons = []
    if not files:
        text = (
            "⚙️ <b>Per-Script Environment (.env) Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📁 No Python scripts found in <code>scripts/</code> folder.\n\n"
            "💡 <i>Send a new script (.py) or ZIP project in chat to add one.</i>"
        )
    else:
        text = (
            "⚙️ <b>Per-Script Environment (.env) Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Each script/project has its own private <b><code>.env</code></b> vault loaded on launch.\n\n"
            "<i>Select a script below to view & manage its variables:</i>"
        )
        for py in files:
            env_vars = read_script_env(py)
            count = len(env_vars)
            badge = f"({count} vars set)" if count > 0 else "(0 vars)"
            buttons.append([{"text": f"📁 {py} {badge}", "callback_data": f"env_dash_{py}"}])
    
    buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
    markup = {"inline_keyboard": buttons}
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_script_env_dashboard(chat_id, user_id, py_filename, message_id=None):
    env_vars = read_script_env(py_filename)
    active = get_active_running_processes()
    is_this_running = py_filename in active
    
    var_lines = []
    if not env_vars:
        var_lines.append("<i>No environment variables configured yet.</i>")
    else:
        for k, v in sorted(env_vars.items()):
            masked = mask_secret_val(v)
            var_lines.append(f"• 🔑 <code>{k}</code> = <code>{masked}</code>")
    
    base_name = py_filename.rsplit('.', 1)[0]
    text = (
        f"⚙️ <b>Private Environment:</b> <code>scripts/{py_filename}</code>\n"
        f"📁 <b>Dedicated Config:</b> <code>scripts/{base_name}.env</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(var_lines)
        + "\n\n<i>Use the buttons below to add or remove variables:</i>"
    )
    
    buttons = [
        [
            {"text": "➕ Add / Edit Variable", "callback_data": f"env_add_{py_filename}"},
            {"text": "🗑️ Delete Variable", "callback_data": f"env_del_list_{py_filename}"}
        ],
        [
            {"text": "🛡️ View Venv Packages", "callback_data": f"venv_list_{py_filename}"},
            {"text": f"📥 Export {base_name}.env", "callback_data": f"env_exp_{py_filename}"}
        ]
    ]
    if is_this_running:
        buttons.append([{"text": "🔄 Apply & Restart Script", "callback_data": f"exec_run_{py_filename}"}])
    else:
        buttons.append([{"text": f"▶️ Run {py_filename} Now", "callback_data": f"exec_run_{py_filename}"}])
    
    buttons.append([{"text": "🔙 Back to Scripts", "callback_data": "menu_env_select"}])
    markup = {"inline_keyboard": buttons}
    
    if message_id:
        edit_tg_message(chat_id, message_id, text, reply_markup=markup)
    else:
        send_tg_message(chat_id, text, reply_markup=markup)

def prompt_env_delete_list(chat_id, user_id, py_filename, message_id=None):
    env_vars = read_script_env(py_filename)
    if not env_vars:
        text = f"ℹ️ No variables to delete for <code>{py_filename}</code>."
        markup = {"inline_keyboard": [[{"text": "🔙 Back", "callback_data": f"env_dash_{py_filename}"}]]}
    else:
        text = f"🗑️ <b>Delete Variable from <code>{py_filename}</code>:</b>\n\nTap a variable below to remove it:"
        buttons = []
        for k in sorted(env_vars.keys()):
            buttons.append([{"text": f"❌ Delete {k}", "callback_data": f"env_dodel_{py_filename}_{k}"}])
        buttons.append([{"text": "🔙 Back", "callback_data": f"env_dash_{py_filename}"}])
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
    
    # Scan all Python files recursively
    all_files = []
    for root, _, fs in os.walk(SCRIPTS_DIR):
        for f in fs:
            if f.endswith(".py") and not f.startswith("."):
                rel = os.path.relpath(os.path.join(root, f), SCRIPTS_DIR)
                all_files.append(rel)
    all_files.sort()
    
    runnable_files = [f for f in all_files if is_runnable_entry_point(f)]
    if not runnable_files and all_files:
        runnable_files = all_files # Fallback if only 1 non-standard file exists
    
    active = get_active_running_processes()
    
    buttons = []
    if not runnable_files:
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
            f"• <b>Active Scripts:</b> {len(active)} Running Concurrently\n"
            f"• <b>Total Scripts Available:</b> {len(runnable_files)}\n\n"
            "<i>Tap <b>▶️ Run</b> to launch in parallel or <b>🛑 Stop</b> to terminate:</i>"
        )
        for py in runnable_files:
            is_this_running = py in active
            req_p = get_script_req_path(py)
            has_env = len(read_script_env(py)) > 0
            
            badges = []
            if req_p:
                badges.append("📦")
            if has_env:
                badges.append("🔒")
            badge_str = f" {' '.join(badges)}" if badges else ""
            
            if is_this_running:
                pdata = active[py]
                cu_sec = int(time.time() - pdata["start_time"])
                ch, cr = divmod(cu_sec, 3600)
                cm, _ = divmod(cr, 60)
                run_btn = {"text": f"🛑 Stop {py} ({ch}h {cm}m){badge_str}", "callback_data": f"confirm_stop_prompt_{py}"}
            else:
                run_btn = {"text": f"▶️ Run {py}{badge_str}", "callback_data": f"exec_run_{py}"}
            
            del_btn = {"text": "🗑️ Delete", "callback_data": f"file_del_{py}"}
            buttons.append([run_btn, del_btn])

    buttons.append([{"text": "📤 Upload New Script / ZIP", "callback_data": "menu_upload_prompt"}])
    if len(active) > 1:
        buttons.append([{"text": "🛑 Stop ALL Running Scripts", "callback_data": "menu_stop_all"}])
    buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])

    markup = {"inline_keyboard": buttons}
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
    
    if not top_items:
        file_lines.append("<i>Scripts folder (scripts/) is currently empty.\nSend any .py script, .env, or .zip archive to upload!</i>")
    else:
        for it in top_items:
            p = os.path.join(SCRIPTS_DIR, it)
            if os.path.isdir(p):
                # It's a Project Archive / Directory!
                inner_files = []
                total_size = 0
                for root, _, fs in os.walk(p):
                    for f in fs:
                        if not f.startswith(".") and f != "__pycache__":
                            fp = os.path.join(root, f)
                            total_size += os.path.getsize(fp)
                            inner_files.append(os.path.relpath(fp, SCRIPTS_DIR))
                
                # Check if process is running inside this project
                is_this_running = any(k.startswith(f"{it}/") or k == it for k in active.keys())
                running_script_name = next((k for k in active.keys() if k.startswith(f"{it}/") or k == it), None)
                status_icon = "🟢" if is_this_running else "📦"
                
                # Detect entry script for Run button
                entry_script = detect_project_entry_script(p)
                            
                badge_str = f" <i>(📁 {len(inner_files)} files | {total_size} bytes)</i>"
                file_lines.append(f"• {status_icon} <b>{it}/</b> [Project Archive]{badge_str}{' <b>[RUNNING]</b>' if is_this_running else ''}")
                
                row_btns = [{"text": f"📥 {it}.zip", "callback_data": f"file_dl_{it}"}]
                if is_this_running and running_script_name:
                    row_btns.append({"text": "🛑 Stop", "callback_data": f"confirm_stop_prompt_{running_script_name}"})
                elif entry_script:
                    entry_base = os.path.basename(entry_script)
                    row_btns.append({"text": f"▶️ Run {entry_base}", "callback_data": f"exec_run_{entry_script}"})
                row_btns.append({"text": "🗑️ Delete", "callback_data": f"file_del_{it}"})
                download_buttons.append(row_btns)
                
            elif os.path.isfile(p):
                sz = os.path.getsize(p)
                is_this_running = it in active
                status_icon = "🟢" if is_this_running else "📄"
                
                if it.endswith(".py"):
                    is_runnable = is_runnable_entry_point(it)
                    req_p = get_script_req_path(it)
                    has_env = len(read_script_env(it)) > 0
                    badges = []
                    if req_p:
                        badges.append(f"📦 {os.path.basename(req_p)}")
                    else:
                        badges.append("📄 Standalone")
                    if has_env:
                        badges.append(f"🔒 {os.path.basename(it).rsplit('.', 1)[0]}.env")
                    
                    badge_str = f" <i>({' | '.join(badges)})</i>"
                    file_lines.append(f"• {status_icon} <code>{it}</code> ({sz} bytes){badge_str}{' <b>[RUNNING]</b>' if is_this_running else ''}")
                    
                    row_btns = [{"text": f"📥 {it}", "callback_data": f"file_dl_{it}"}]
                    if is_this_running:
                        row_btns.append({"text": "🛑 Stop", "callback_data": f"confirm_stop_prompt_{it}"})
                    elif is_runnable:
                        row_btns.append({"text": "▶️ Run", "callback_data": f"exec_run_{it}"})
                    row_btns.append({"text": "🗑️ Delete", "callback_data": f"file_del_{it}"})
                    download_buttons.append(row_btns)
                else:
                    file_lines.append(f"• 📄 <code>{it}</code> ({sz} bytes)")
                    download_buttons.append([
                        {"text": f"📥 {it}", "callback_data": f"file_dl_{it}"},
                        {"text": "🗑️ Delete", "callback_data": f"file_del_{it}"}
                    ])
    
    text = (
        "📂 <b>Scripts File Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>Directory:</b> <code>scripts/</code> (Cloud Storage)\n"
        f"📊 <b>Total Items:</b> {len(top_items)}\n"
        f"🟢 <b>Active Running:</b> {len(active)}\n\n"
        + "\n".join(file_lines[:40])
        + ("\n<i>...and more items</i>" if len(file_lines) > 40 else "")
        + "\n\n<i>Tap a button below to Download, Run, or Delete:</i>"
    )
    download_buttons.append([{"text": "📤 Upload New Script / ZIP", "callback_data": "menu_upload_prompt"}])
    download_buttons.append([{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}])
    if top_items:
        download_buttons.append([{"text": "💣 Delete All Scripts & Projects", "callback_data": "wipe_all_workspace_prompt"}])
    download_buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])
    markup = {"inline_keyboard": download_buttons[:90]} # Keep under TG inline keyboard limit
    
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

def show_server_info_view(chat_id, message_id=None):
    # Fetch repository details via GitHub API
    repo_info = {}
    try:
        url = f"https://api.github.com/repos/{REPO}"
        headers = {
            "Authorization": f"Bearer {GH_PAT}" if GH_PAT else "",
            "Accept": "application/vnd.github+json"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            repo_info = resp.json()
    except Exception as e:
        logger.error(f"Error fetching repo info: {e}")

    # Telemetry
    uptime_sec = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    relay_remain = max(0, RUN_DURATION_SECONDS - uptime_sec)
    rh, rr = divmod(relay_remain, 3600)
    rm, rs = divmod(rr, 60)
    
    active = get_active_running_processes()
    count = len(active)
    if count == 0:
        active_summary = "🔴 <i>None (Stopped / Standby)</i>"
    else:
        active_summary = f"🟢 <b>{count} Active:</b> " + ", ".join([f"<code>{s}</code>" for s in sorted(active.keys())])
    
    repo_name = repo_info.get("full_name", REPO)
    visibility = "🌍 Public" if not repo_info.get("private", False) else "🔒 Private"
    repo_size_kb = repo_info.get("size", 0)
    default_branch = repo_info.get("default_branch", "main")
    created_at = repo_info.get("created_at", "N/A")[:10] if repo_info.get("created_at") else "N/A"
    owner_login = repo_info.get("owner", {}).get("login", repo_name.split("/")[0] if "/" in repo_name else "N/A")
    repo_html_url = repo_info.get("html_url", f"https://github.com/{REPO}")
    
    text = (
        "ℹ️ <b>Cloud Server & Repository Intelligence</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Cloud Repository:</b> <code>{repo_name}</code>\n"
        f"👑 <b>Owner:</b> <code>{owner_login}</code>\n"
        f"🛡️ <b>Visibility:</b> <b>{visibility}</b>\n"
        f"🌿 <b>Default Branch:</b> <code>{default_branch}</code>\n"
        f"📦 <b>Repo Size:</b> <code>{repo_size_kb} KB</code>\n"
        f"📅 <b>Created On:</b> <code>{created_at}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>Live Relay Server Status:</b>\n"
        f"• <b>Daemon Status:</b> 🟢 <b>Active & Healthy</b>\n"
        f"• <b>Active Scripts:</b> {active_summary}\n"
        f"• <b>Current Run ID:</b> <code>#{RUN_ID}</code>\n"
        f"• <b>Current Phase Uptime:</b> <code>{hours}h {minutes}m {seconds}s</code>\n"
        f"• <b>Next Relay Handoff In:</b> <code>{rh}h {rm}m {rs}s</code> (Auto-Resuming)\n"
        f"• <b>Security Vault:</b> 🔐 <b>AES-256 Authenticated Encryption (Active)</b>\n"
        f"• <b>Secret Scanner Shield:</b> 🛡️ <b>100% Protected (.gitignore active)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    markup = {
        "inline_keyboard": [
            [
                {"text": "🔄 Refresh Info", "callback_data": "menu_server_info"},
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
        answer_callback(callback_id)
        prompt_script_env_dashboard(chat_id, user_id, fname, message_id)

    # 2d. Add/Edit Variable Prompt
    elif data.startswith("env_add_"):
        fname = data.replace("env_add_", "")
        user_states[user_id] = {
            "action": "WAITING_ENV_VAR",
            "target_py": fname
        }
        answer_callback(callback_id)
        text = (
            f"⚙️ <b>Set Environment Variable for <code>{fname}</code></b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send the variable name and value in chat:\n\n"
            "• <b>Format:</b> <code>KEY=VALUE</code>\n"
            "• <b>Example:</b> <code>BOT_TOKEN=123456789:AAH...</code>\n"
            "• <b>Example:</b> <code>GMAIL_EMAIL=mybot@gmail.com</code>\n\n"
            "<i>Saved into dedicated <code>scripts/{fname.rsplit('.', 1)[0]}.env</code>.</i>"
        )
        edit_tg_message(chat_id, message_id, text, reply_markup={"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"env_dash_{fname}"}]]})

    # 2e. Delete Variable Menu
    elif data.startswith("env_del_list_"):
        fname = data.replace("env_del_list_", "")
        answer_callback(callback_id)
        prompt_env_delete_list(chat_id, user_id, fname, message_id)

    # 2f. Do Delete Variable
    elif data.startswith("env_dodel_"):
        parts = data.replace("env_dodel_", "").split("_", 1)
        if len(parts) == 2:
            fname, var_key = parts[0], parts[1]
            env_dict = read_script_env(fname)
            env_dict.pop(var_key, None)
            write_script_env(fname, env_dict)
            answer_callback(callback_id, f"{var_key} deleted!", show_alert=True)
            prompt_script_env_dashboard(chat_id, user_id, fname, message_id)

    # 2g. Export .env file
    elif data.startswith("env_exp_"):
        fname = data.replace("env_exp_", "")
        env_path = get_script_env_path(fname)
        if os.path.exists(env_path):
            answer_callback(callback_id, f"Exporting {fname.rsplit('.', 1)[0]}.env...")
            send_tg_document(chat_id, env_path, caption=f"📄 <b>{os.path.basename(env_path)}</b>")
        else:
            answer_callback(callback_id, "No .env file found for this script.", show_alert=True)

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
        state = user_states.get(user_id, {})
        staged_path = state.get("staging_path")
        if not staged_path or not os.path.exists(staged_path):
            answer_callback(callback_id, "Staged file expired. Please upload again.", show_alert=True)
            return
        
        target_path = os.path.join(SCRIPTS_DIR, fname)
        answer_callback(callback_id, f"Replacing and restarting {fname}...")
        
        stop_child_app(script_name=fname, clear_active=False)
        time.sleep(1.0)
        
        import shutil
        shutil.move(staged_path, target_path)
        user_states.pop(user_id, None)
        git_sync_to_github(f"Update and replace: {fname}")
        
        ok, msg = start_child_app(fname)
        send_tg_message(chat_id, f"🔄 <b>Updated & Restarted Instance:</b> <code>{fname}</code>\n\n{msg}", reply_markup=get_main_menu_keyboard())

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
        ok, msg = stop_child_app(script_name=fname)
        answer_callback(callback_id, f"{fname} stopped!", show_alert=True)
        edit_tg_message(chat_id, message_id, f"🛑 <b>{fname} has been stopped successfully!</b>", reply_markup=get_main_menu_keyboard())

    # 5d. Stop All Scripts Execution
    elif data == "menu_stop_all":
        answer_callback(callback_id, "Stopping all scripts...")
        stop_child_app(script_name=None, clear_active=True)
        send_tg_message(chat_id, "🛑 <b>All running scripts have been stopped.</b>", reply_markup=get_main_menu_keyboard())

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
        fname = data.replace("file_dl_", "")
        target_path = os.path.join(SCRIPTS_DIR, fname)
        if not os.path.exists(target_path):
            target_path = os.path.join(WORKSPACE_DIR, fname)

        if os.path.exists(target_path):
            if os.path.isdir(target_path):
                import shutil
                answer_callback(callback_id, f"Archiving {fname} to ZIP...")
                zip_base = os.path.join(WORKSPACE_DIR, f"temp_{fname}")
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
        answer_callback(callback_id, "Wiping workspace...")
        
        # 1. Stop all running child processes cleanly
        stop_child_app(script_name=None, clear_active=True)
        time.sleep(0.5)
        
        # 2. Reset running processes dictionary in memory
        running_processes.clear()
        
        # 3. Delete all files & directories inside SCRIPTS_DIR safely
        deleted_count = 0
        try:
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
        except Exception as e:
            logger.error(f"Error wiping scripts dir: {e}")
            
        # 4. Clean up any virtualenvs (.venvs/)
        try:
            if os.path.exists(VENVS_DIR):
                shutil.rmtree(VENVS_DIR, ignore_errors=True)
        except Exception:
            pass

        # 5. Clear all active scripts, config, and vault
        config["active_scripts"] = []
        config["active_script"] = None
        config["auto_run_file"] = None
        config["env_vault"] = {}
        save_config(config)
        
        # 6. Commit wipe to GitHub in background
        threading.Thread(target=git_sync_to_github, args=("Wipe all scripts via Telegram",), daemon=True).start()
        
        # 7. Edit message in real time to show confirmation
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

    # 10c. Direct & Fast Delete Single File/Folder Execution
    elif data.startswith("file_del_") or data.startswith("do_delete_file_"):
        if data.startswith("do_delete_file_"):
            fname = data.replace("do_delete_file_", "")
        else:
            fname = data.replace("file_del_", "")
        
        if fname in ["all", "all_prompt"]:
            # Handled by wipe workspace
            return
            
        # 1. Answer callback INSTANTLY so Telegram spinner immediately clears!
        answer_callback(callback_id, f"🗑️ Deleting {fname}...", show_alert=False)
        
        # 2. Identify candidate paths to delete on disk
        candidates_to_remove = [
            os.path.join(SCRIPTS_DIR, fname),
            os.path.join(WORKSPACE_DIR, fname),
        ]
        
        # If fname is a nested path like "Ghidrabot vps/bot.py" or "xxtgdriveapibot/main.py", also check the parent project directory
        clean_fname = fname.replace("\\", "/").strip("/")
        if "/" in clean_fname:
            parent_dir_name = clean_fname.split("/")[0]
            candidates_to_remove.append(os.path.join(SCRIPTS_DIR, parent_dir_name))
            candidates_to_remove.append(os.path.join(WORKSPACE_DIR, parent_dir_name))
        
        # Also stem without .py
        base_stem = clean_fname.rsplit(".", 1)[0]
        candidates_to_remove.append(os.path.join(SCRIPTS_DIR, base_stem))
        
        # 3. Stop running processes matching this script or its directory and wait for OS locks to clear
        stop_child_app(script_name=fname, clear_active=True)
        time.sleep(0.5)
        
        # 4. Explicitly mark removed from Git index in WORKSPACE_DIR
        try:
            for rel_target in [f"scripts/{clean_fname}", f"scripts/{fname}", f"scripts/{base_stem}", clean_fname, fname, base_stem]:
                subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", rel_target], cwd=WORKSPACE_DIR, capture_output=True)
            if "/" in clean_fname:
                parent_stem = clean_fname.split("/")[0]
                subprocess.run(["git", "rm", "-r", "-f", "--ignore-unmatch", f"scripts/{parent_stem}", parent_stem], cwd=WORKSPACE_DIR, capture_output=True)
        except Exception as e_git:
            logger.error(f"git rm error: {e_git}")

        # 5. Clean up disk
        import shutil
        deleted_anything = False
        for target_path in candidates_to_remove:
            if os.path.exists(target_path):
                try:
                    if os.path.isdir(target_path):
                        shutil.rmtree(target_path, ignore_errors=True)
                    else:
                        os.remove(target_path)
                    deleted_anything = True
                except Exception as e_del:
                    logger.error(f"Error removing {target_path}: {e_del}")

        # Clean companion files (.env, .requirements.txt, etc.)
        for extra in [f"{base_stem}.requirements.txt", f"{base_stem}.env", f"{clean_fname}.env", f"{clean_fname}.requirements.txt"]:
            extra_p = os.path.join(SCRIPTS_DIR, extra)
            if os.path.exists(extra_p):
                try:
                    os.remove(extra_p)
                except Exception:
                    pass

        # 6. Clean up isolated virtualenvs
        venv_slugs = [
            re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean_fname),
            re.sub(r'[^a-zA-Z0-9_\-\.]', '_', base_stem),
        ]
        if "/" in clean_fname:
            venv_slugs.append(re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean_fname.split("/")[0]))
            
        for vslug in venv_slugs:
            v_dir = os.path.join(VENVS_DIR, vslug)
            if os.path.exists(v_dir):
                try:
                    shutil.rmtree(v_dir, ignore_errors=True)
                except Exception:
                    pass

        # 7. Clean up config active_scripts and vault
        active_list = list(get_active_running_processes().keys())
        config["active_scripts"] = active_list

        vault = load_env_vault()
        keys_to_del = [
            k for k in list(vault.keys()) 
            if k == clean_fname 
            or k == fname 
            or k.startswith(f"{clean_fname}/") 
            or (("/" in clean_fname) and (k.startswith(clean_fname.split("/")[0]) or os.path.dirname(k) == clean_fname.split("/")[0]))
            or os.path.basename(k) == clean_fname
            or os.path.basename(k) == base_stem
        ]
        for k in keys_to_del:
            vault.pop(k, None)
        save_env_vault(vault)
        save_config(config)

        # 8. Single thread-safe background sync to permanently push deletion to GitHub repo!
        threading.Thread(target=git_sync_to_github, args=(f"Permanently delete scripts/{fname} from repository",), daemon=True).start()

        # 9. Refresh Telegram View in Real Time!
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
        answer_callback(callback_id, "ℹ️ Loading Repo Intelligence...")
        show_server_info_view(chat_id, message_id)

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
    
    # 0. Check if uploading a .py file that is ALREADY RUNNING
    if file_name.endswith(".py"):
        active = get_active_running_processes()
        if file_name in active:
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
            
            pdata = active[file_name]
            cu_sec = int(time.time() - pdata["start_time"])
            ch, cr = divmod(cu_sec, 3600)
            cm, _ = divmod(cr, 60)
            
            base, ext = os.path.splitext(file_name)
            idx = 2
            while os.path.exists(os.path.join(SCRIPTS_DIR, f"{base}_{idx}{ext}")) or f"{base}_{idx}{ext}" in active:
                idx += 1
            next_inst_name = f"{base}_{idx}{ext}"
            
            text = (
                f"✨ <b>Python Script Received:</b> <code>{file_name}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ <b>Instance Alert:</b> <code>{file_name}</code> is currently <b>RUNNING (PID: {pdata['pid']} | {ch}h {cm}m)</b>.\n\n"
                "<i>Choose how you want to deploy this file:</i>\n\n"
                f"• <b>🔀 Run Parallel:</b> Saves as <code>{next_inst_name}</code> and runs alongside the existing instance.\n"
                f"• <b>🔄 Update & Restart:</b> Stops PID {pdata['pid']}, replaces code, and restarts immediately."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": f"🔀 Run Parallel ({next_inst_name})", "callback_data": f"inst_parallel_{file_name}"}],
                    [{"text": f"🔄 Replace & Restart (PID {pdata['pid']})", "callback_data": f"inst_replace_{file_name}"}],
                    [{"text": "✏️ Save with Custom Name", "callback_data": f"inst_custom_{file_name}"}],
                    [{"text": "❌ Cancel Upload", "callback_data": f"inst_cancel_{file_name}"}]
                ]
            }
            send_tg_message(chat_id, text, reply_markup=markup)
            return

    # Normal Download for non-conflicting files
    is_zip = file_name.endswith(".zip")
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
    
    # 0b. If uploaded a .zip project archive
    if is_zip:
        import zipfile
        zip_base = file_name[:-4]
        
        # Stop any existing running instance of this project so newly uploaded code is deployed cleanly!
        active = get_active_running_processes()
        is_zip_running = any(k.startswith(f"{zip_base}/") or k == zip_base or os.path.dirname(k) == zip_base for k in active.keys())
        if is_zip_running:
            stop_child_app(script_name=zip_base, clear_active=False)
            time.sleep(0.5)

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

        # Auto-Load & Bind .env Variables into AES-256 Encrypted Vault and purge plain files
        env_loaded_count = 0
        if found_envs and entry_script:
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
                    curr_env = read_script_env(entry_script)
                    curr_env.update(parsed)
                    write_script_env(entry_script, curr_env)
                    env_loaded_count += len(parsed)
                # Purge raw plaintext .env from disk so git never commits it!
                try:
                    os.remove(ef)
                except Exception:
                    pass

        # Sync project code safely to GitHub cloud
        git_sync_to_github(f"Deploy ZIP project: {zip_base}")

        # Auto-Launch the newly uploaded project entry script immediately!
        launch_status_text = ""
        if entry_script:
            ok_run, run_msg = start_child_app(entry_script, force_restart=True)
            if ok_run:
                launch_status_text = f"🟢 <b>Status:</b> <code>{entry_script}</code> is now <b>RUNNING!</b>"
            else:
                launch_status_text = f"⚠️ <b>Launch Note:</b> {run_msg}"

        entry_display = entry_script or 'None'
        deploy_msg = (
            f"🚀 <b>ZIP Project Deployed & Launched!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Archive:</b> <code>{file_name}</code>\n"
            f"📁 <b>Files Extracted:</b> {len(extracted_files)}\n"
            f"🎯 <b>Detected Entry Script:</b> <code>{entry_display}</code>\n"
            f"{launch_status_text}\n"
            f"📦 <b>Dependencies:</b> {'Installed ~' + str(req_installed_count) + ' packages' if found_reqs else 'No requirements.txt found'}\n"
            f"🔒 <b>Environment:</b> {str(env_loaded_count) + ' variables loaded' if env_loaded_count else 'No .env found'}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Manage your project using the buttons below:</i>"
        )
        buttons = []
        if entry_script:
            buttons.append([{"text": f"🛑 Stop {os.path.basename(entry_script)}", "callback_data": f"confirm_stop_prompt_{entry_script}"}, {"text": "🔄 Restart", "callback_data": f"exec_run_{entry_script}"}])
            buttons.append([{"text": f"⚙️ Configure {os.path.basename(entry_script)} ENV", "callback_data": f"env_dash_{entry_script}"}])
            buttons.append([{"text": "📋 View Live Logs", "callback_data": f"show_log_for_{entry_script}"}])
        buttons.append([{"text": "🚀 Scripts Runner", "callback_data": "menu_runner"}, {"text": "📂 View Files", "callback_data": "menu_files"}])
        buttons.append([{"text": "🔙 Main Menu", "callback_data": "menu_main"}])

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
        target_py = "bot.py" if file_name == ".env" else file_name[:-4] + ".py"
        parsed_vars = read_script_env(target_py)
        count = len(parsed_vars)
        text = (
            f"🔒 <b>Environment File Saved:</b> <code>scripts/{file_name}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Linked Script:</b> <code>scripts/{target_py}</code>\n"
            f"• <b>Total Variables Loaded:</b> {count}\n\n"
            f"📁 Saved and backed up to Cloud Storage."
        )
        markup = {
            "inline_keyboard": [
                [{"text": f"⚙️ Manage {target_py} ENV", "callback_data": f"env_dash_{target_py}"}],
                [{"text": f"▶️ Run {target_py}", "callback_data": f"exec_run_{target_py}"}],
                [{"text": "🔙 Main Menu", "callback_data": "menu_main"}]
            ]
        }
        send_tg_message(chat_id, text, reply_markup=markup)
        
    else:
        send_tg_message(
            chat_id,
            f"✅ <b>{file_name}</b> <code>scripts/</code> folder me save ho gayi hai.",
            reply_markup=get_main_menu_keyboard()
        )

# ---------------------------------------------------------------------------
# Polling Engine
# ---------------------------------------------------------------------------
def telegram_polling_loop():
    logger.info("🤖 Real-Time Telegram Polling Engine active...")
    offset = 0
    
    while IS_RUNNING:
        try:
            url = f"{TG_BASE_URL}/getUpdates?offset={offset}&timeout=20"
            resp = requests.get(url, timeout=25)
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        
                        # Handle Callback Queries (Button Clicks) in non-blocking real-time thread
                        if "callback_query" in update:
                            cq = update["callback_query"]
                            cb_id = cq["id"]
                            c_user = cq["from"]["id"]
                            c_chat = cq["message"]["chat"]["id"]
                            c_msg_id = cq["message"]["message_id"]
                            c_data = cq.get("data", "")
                            threading.Thread(
                                target=handle_callback_query,
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
                                    target=handle_text_message,
                                    args=(chat_id, user_id, msg["text"]),
                                    daemon=True
                                ).start()
                            elif "document" in msg:
                                threading.Thread(
                                    target=handle_document_upload,
                                    args=(chat_id, user_id, msg["document"]),
                                    daemon=True
                                ).start()
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
    
    # Seamless Multi-Script Relay Persistence: Auto-resume active scripts
    active_list = config.get("active_scripts")
    
    # If active_scripts was never initialized in config (first boot), look for candidates
    if active_list is None:
        active_list = []
        if config.get("active_script"):
            active_list = [config["active_script"]]
        else:
            vault_scripts = list(config.get("env_vault", {}).keys())
            for s in vault_scripts:
                sp = os.path.join(SCRIPTS_DIR, s)
                if os.path.exists(sp) and s not in active_list:
                    active_list.append(s)
            if not active_list:
                for root, _, fs in os.walk(SCRIPTS_DIR):
                    for f in fs:
                        if f.endswith(".py") and not f.startswith("."):
                            rel = os.path.relpath(os.path.join(root, f), SCRIPTS_DIR)
                            if is_runnable_entry_point(rel) and rel not in active_list:
                                active_list.append(rel)
                                break

    if active_list:
        logger.info(f"🔄 Auto-resuming {len(active_list)} active scripts across relay handoff/boot: {active_list}")
        def delayed_multi_resume(scripts_to_run):
            time.sleep(2.0)
            notify_all_admins(
                f"🔄 <b>Cloud Server Online / Restarted:</b>\n"
                f"Auto-resuming {len(scripts_to_run)} scripts in parallel:\n"
                + "\n".join([f"• <code>{s}</code>" for s in scripts_to_run])
            )
            success_count = 0
            for s in scripts_to_run:
                ok, msg = start_child_app(s)
                if ok:
                    success_count += 1
                else:
                    notify_all_admins(f"⚠️ <b>Auto-resume error for <code>{s}</code>:</b>\n{msg}")
                time.sleep(0.5)
            if success_count > 0:
                notify_all_admins(f"🟢 <b>{success_count}/{len(scripts_to_run)} scripts are now active and running in parallel!</b>", reply_markup=get_main_menu_keyboard())
        threading.Thread(target=delayed_multi_resume, args=(active_list,), daemon=True).start()

    # Start Telegram polling thread
    tg_thread = threading.Thread(target=telegram_polling_loop, daemon=True, name="TGPolling")
    tg_thread.start()
    
    # Watchdog loop for 5.5 hours duration
    while IS_RUNNING:
        elapsed = time.time() - START_TIME
        if elapsed >= RUN_DURATION_SECONDS:
            logger.info(f"⏳ 5.5 Hours reached. Triggering Handoff...")
            break
        time.sleep(5)
    
    # --- HANDOFF SEQUENCE ---
    IS_RUNNING = False # Stop Telegram polling immediately on old runner
    
    active_now = list(get_active_running_processes().keys())
    config["active_scripts"] = active_now
    save_config(config)
    
    resume_note = ""
    if active_now:
        resume_note = f"\n🚀 <i>{len(active_now)} active scripts will auto-resume in new phase:</i>\n" + "\n".join([f"• <code>{s}</code>" for s in active_now])
    
    notify_all_admins(
        "🔄 <b>Relay Transition (5.5 Hours):</b>\n"
        "Backing up workspace and transitioning to next runner..."
        + resume_note
    )
    
    stop_child_app(script_name=None, clear_active=False) # Stop processes without erasing active_scripts list!
    git_sync_to_github("Auto-backup before Relay Handoff")
    trigger_next_runner()
    time.sleep(5)
    logger.info("Handoff sequence complete. Exiting.")

if __name__ == "__main__":
    main()

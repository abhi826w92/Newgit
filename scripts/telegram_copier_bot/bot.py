import os
import re
import json
import asyncio
import time
import hashlib
import sqlite3
import urllib.parse
import hmac
import base64
import secrets
import pyaes
from datetime import datetime, timezone, timedelta

# Python 3.12+ compatibility fix for Pyrogram import
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import Client
from pyrogram.raw import functions
from pyrogram.errors import FloodWait, SessionPasswordNeeded

# Load .env
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k] = v

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN in .env file")
bot = AsyncTeleBot(BOT_TOKEN)

# ==================== DATABASE ENCRYPTION ENGINE (AES-256 + HMAC) ====================

DB_KEY_ENV = os.environ.get("DB_ENCRYPTION_KEY")
if not DB_KEY_ENV:
    DB_KEY_ENV = secrets.token_hex(32)
    os.environ["DB_ENCRYPTION_KEY"] = DB_KEY_ENV
    try:
        with open(".env", "a") as f:
            f.write(f"\nDB_ENCRYPTION_KEY={DB_KEY_ENV}\n")
        print("[SECURITY] Generated new AES-256 DB_ENCRYPTION_KEY and saved to .env")
    except Exception as e:
        print(f"[SECURITY] Warning: Could not write key to .env: {e}")

DB_CIPHER_KEY = hashlib.pbkdf2_hmac(
    "sha256",
    DB_KEY_ENV.encode("utf-8"),
    b"telegram_copier_db_salt_v1",
    100000,
    32
)

def encrypt_db_payload(plain_dict):
    raw_bytes = json.dumps(plain_dict).encode("utf-8")
    iv = os.urandom(16)
    counter = pyaes.Counter(initial_value=int.from_bytes(iv, byteorder="big"))
    aes = pyaes.AESModeOfOperationCTR(DB_CIPHER_KEY, counter=counter)
    ciphertext = aes.encrypt(raw_bytes)
    tag = hmac.new(DB_CIPHER_KEY, iv + ciphertext, hashlib.sha256).digest()

    return {
        "enc": True,
        "v": 1,
        "iv": base64.b64encode(iv).decode("utf-8"),
        "data": base64.b64encode(ciphertext).decode("utf-8"),
        "hmac": base64.b64encode(tag).decode("utf-8")
    }

def decrypt_db_payload(packed):
    iv = base64.b64decode(packed["iv"])
    ciphertext = base64.b64decode(packed["data"])
    expected_tag = base64.b64decode(packed["hmac"])

    tag = hmac.new(DB_CIPHER_KEY, iv + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Database HMAC verification failed! File corrupted or encryption key mismatch.")

    counter = pyaes.Counter(initial_value=int.from_bytes(iv, byteorder="big"))
    aes = pyaes.AESModeOfOperationCTR(DB_CIPHER_KEY, counter=counter)
    decrypted_bytes = aes.decrypt(ciphertext)
    return json.loads(decrypted_bytes.decode("utf-8"))

def load_db():
    if not os.path.exists("db.json"):
        return {}
    try:
        with open("db.json", "r") as f:
            raw = json.load(f)

        if isinstance(raw, dict) and raw.get("enc") is True:
            return decrypt_db_payload(raw)

        # Legacy plain-text JSON detected -> automatically migrate to AES-256 encrypted storage!
        print("[SECURITY] Plain-text db.json detected. Migrating to AES-256 encrypted storage...")
        save_db(raw)
        return raw
    except Exception as e:
        print(f"[SECURITY] Error reading db.json: {e}")
        return {}

def save_db(data):
    try:
        encrypted_dict = encrypt_db_payload(data)
        with open("db.json", "w") as f:
            json.dump(encrypted_dict, f, indent=2)
    except Exception as e:
        print(f"[SECURITY] Error encrypting and saving db.json: {e}")

DB = load_db()

# State and session storage
user_states = {}            # user_id -> str
user_auth_data = {}         # user_id -> dict
user_clients = {}           # user_id -> Pyrogram Client
active_tasks = {}           # user_id -> bool
user_dialog_cache = {}      # user_id -> list of dicts: {"id", "title", "raw_title", "username", "type"}
user_dialog_last_fetch = {} # user_id -> timestamp
user_active_query = {}      # user_id -> active search query string or None
user_task_info = {}         # user_id -> dict for live progress status

CONTENT_TYPE_LABELS = {
    "all": "All Content 📁",
    "video": "Videos Only 🎬",
    "photo": "Photos Only 🖼️",
    "document": "Documents/Files 📄",
    "text": "Text Only 📝"
}

CONTENT_TYPE_CYCLE = ["all", "video", "photo", "document", "text"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEDUP_DB_PATH = os.path.join(BASE_DIR, "dedup.db")
STATS_DB_PATH = os.path.join(BASE_DIR, "stats.db")

# ==================== DEDUPLICATION ENGINE (Feature 3) ====================

user_dedup_cache = {}  # user_id -> set(file_hash)

def init_dedup_db():
    conn = sqlite3.connect(DEDUP_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS copied_hashes (
            user_id INTEGER,
            src_chat_id INTEGER,
            file_hash TEXT,
            copied_at REAL,
            PRIMARY KEY (user_id, file_hash)
        )
    """)
    conn.commit()
    conn.close()

def load_user_dedup_cache(user_id):
    if user_id in user_dedup_cache:
        return user_dedup_cache[user_id]
    hashes = set()
    try:
        conn = sqlite3.connect(DEDUP_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT file_hash FROM copied_hashes WHERE user_id = ?", (user_id,))
        for row in cur.fetchall():
            hashes.add(row[0])
        conn.close()
    except Exception as e:
        print(f"[DEDUP] Error loading cache for {user_id}: {e}")
    user_dedup_cache[user_id] = hashes
    return hashes

def compute_quick_hash(msg):
    """
    Returns unique deterministic hash for message:
    - Media uses Telegram's file_unique_id (or fallback file_id hash)
    - Text uses MD5 hash of message text
    """
    if not msg:
        return None
    media = (
        getattr(msg, "photo", None)
        or getattr(msg, "video", None)
        or getattr(msg, "document", None)
        or getattr(msg, "audio", None)
        or getattr(msg, "voice", None)
        or getattr(msg, "animation", None)
        or getattr(msg, "sticker", None)
    )
    if media:
        uid = getattr(media, "file_unique_id", None)
        if uid:
            return f"media_{uid}"
        fid = getattr(media, "file_id", None)
        if fid:
            return f"media_{hashlib.md5(fid.encode('utf-8')).hexdigest()}"
    if getattr(msg, "text", None):
        t = msg.text.strip()
        if t:
            return f"text_{hashlib.md5(t.encode('utf-8')).hexdigest()}"
    return None

def is_hash_copied(user_id, file_hash):
    if not file_hash:
        return False
    cache = load_user_dedup_cache(user_id)
    return file_hash in cache

def record_copied_hashes_batch(user_id, src_chat_id, hash_list):
    if not hash_list:
        return
    valid = [h for h in hash_list if h]
    if not valid:
        return
    cache = load_user_dedup_cache(user_id)
    for h in valid:
        cache.add(h)
    now = time.time()
    try:
        s_id = int(src_chat_id) if str(src_chat_id).lstrip('-').isdigit() else 0
    except Exception:
        s_id = 0
    try:
        conn = sqlite3.connect(DEDUP_DB_PATH)
        cur = conn.cursor()
        cur.executemany(
            "INSERT OR IGNORE INTO copied_hashes (user_id, src_chat_id, file_hash, copied_at) VALUES (?, ?, ?, ?)",
            [(user_id, s_id, h, now) for h in valid]
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DEDUP] Error saving hashes: {e}")

def get_user_dedup_count(user_id):
    cache = load_user_dedup_cache(user_id)
    return len(cache)

def clear_user_dedup_cache(user_id):
    user_dedup_cache[user_id] = set()
    try:
        conn = sqlite3.connect(DEDUP_DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM copied_hashes WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DEDUP] Error clearing cache for {user_id}: {e}")

# ==================== LIVE ANALYTICS (Feature 4) ====================

def init_stats_db():
    conn = sqlite3.connect(STATS_DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS job_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            src_title TEXT,
            dst_title TEXT,
            total INTEGER,
            copied INTEGER,
            skipped_dup INTEGER,
            failed INTEGER,
            duration_sec REAL,
            avg_speed REAL,
            created_at REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            user_id INTEGER,
            date_str TEXT,
            messages_copied INTEGER,
            jobs_completed INTEGER,
            PRIMARY KEY (user_id, date_str)
        )
    """)
    conn.commit()
    conn.close()

def record_job_stats(user_id, src_title, dst_title, total, copied, skipped_dup, failed, duration_sec):
    try:
        speed = round(copied / max(duration_sec, 0.1), 1)
        now = time.time()
        date_str = datetime.now().strftime("%Y-%m-%d")

        conn = sqlite3.connect(STATS_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO job_stats (user_id, src_title, dst_title, total, copied, skipped_dup, failed, duration_sec, avg_speed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, src_title, dst_title, total, copied, skipped_dup, failed, round(duration_sec, 2), speed, now))

        cur.execute("""
            INSERT INTO daily_stats (user_id, date_str, messages_copied, jobs_completed)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, date_str) DO UPDATE SET
                messages_copied = messages_copied + excluded.messages_copied,
                jobs_completed = jobs_completed + 1
        """, (user_id, date_str, copied))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[STATS] Error recording job stats: {e}")

def get_user_analytics_summary(user_id):
    try:
        conn = sqlite3.connect(STATS_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*), 
                COALESCE(SUM(copied), 0), 
                COALESCE(SUM(total), 0), 
                COALESCE(AVG(avg_speed), 0),
                COALESCE(SUM(duration_sec), 0)
            FROM job_stats WHERE user_id = ?
        """, (user_id,))
        row = cur.fetchone()
        total_jobs = row[0] if row else 0
        total_copied = row[1] if row else 0
        total_total = row[2] if row else 0
        avg_speed = row[3] if row else 0.0
        total_duration = row[4] if row else 0.0

        today_str = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT messages_copied, jobs_completed FROM daily_stats WHERE user_id = ? AND date_str = ?", (user_id, today_str))
        d_row = cur.fetchone()
        today_copied = d_row[0] if d_row else 0
        today_jobs = d_row[1] if d_row else 0

        conn.close()
        success_rate = round((total_copied / max(total_total, 1)) * 100, 1) if total_total > 0 else 100.0
        return {
            "total_jobs": total_jobs,
            "total_copied": total_copied,
            "avg_speed": round(avg_speed, 1),
            "total_duration": total_duration,
            "success_rate": success_rate,
            "today_copied": today_copied,
            "today_jobs": today_jobs
        }
    except Exception as e:
        print(f"[STATS] Error fetching analytics summary: {e}")
        return {
            "total_jobs": 0, "total_copied": 0, "avg_speed": 0.0,
            "total_duration": 0.0, "success_rate": 100.0, "today_copied": 0, "today_jobs": 0
        }

def get_user_7day_stats(user_id):
    result = []
    today = datetime.now().date()
    try:
        conn = sqlite3.connect(STATS_DB_PATH)
        cur = conn.cursor()
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            display_d = d.strftime("%m/%d")
            cur.execute("SELECT messages_copied FROM daily_stats WHERE user_id = ? AND date_str = ?", (user_id, d_str))
            row = cur.fetchone()
            count = row[0] if row else 0
            result.append((display_d, count))
        conn.close()
    except Exception as e:
        print(f"[STATS] Error fetching 7-day stats: {e}")
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            result.append((d.strftime("%m/%d"), 0))
    return result

def get_user_recent_jobs(user_id, limit=5):
    try:
        conn = sqlite3.connect(STATS_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT src_title, dst_title, total, copied, skipped_dup, failed, duration_sec, avg_speed, created_at
            FROM job_stats
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[STATS] Error fetching recent jobs: {e}")
        return []

def clear_user_stats(user_id):
    try:
        conn = sqlite3.connect(STATS_DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM job_stats WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM daily_stats WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[STATS] Error clearing stats: {e}")

# ==================== PROXY ENGINE (Feature 5) ====================

def parse_proxy_string(text):
    text = text.strip()
    if not (text.startswith("socks5://") or text.startswith("socks4://") or text.startswith("http://") or text.startswith("https://")):
        parts = text.split(":")
        if len(parts) == 2:
            host, port = parts
            if port.strip().isdigit():
                return {
                    "scheme": "socks5",
                    "hostname": host.strip(),
                    "port": int(port.strip()),
                    "username": None,
                    "password": None
                }
        elif len(parts) == 4:
            host, port, user, pwd = parts
            if port.strip().isdigit():
                return {
                    "scheme": "socks5",
                    "hostname": host.strip(),
                    "port": int(port.strip()),
                    "username": user.strip(),
                    "password": pwd.strip()
                }
        return None

    try:
        p = urllib.parse.urlparse(text)
        scheme = p.scheme.lower()
        if scheme not in ["socks5", "socks4", "http", "https"]:
            return None
        hostname = p.hostname
        port = p.port
        if not hostname or not port:
            return None
        return {
            "scheme": scheme,
            "hostname": hostname,
            "port": int(port),
            "username": p.username,
            "password": p.password
        }
    except Exception:
        return None

async def test_proxy_ping(proxy_dict, timeout=4.0):
    start = time.time()
    try:
        hostname = proxy_dict["hostname"]
        port = int(proxy_dict["port"])
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hostname, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        latency = int((time.time() - start) * 1000)
        return max(1, latency)
    except Exception:
        return None

def get_user_proxies(user_id):
    uid = str(user_id)
    if uid not in DB:
        DB[uid] = {}
    if "proxies" not in DB[uid]:
        DB[uid]["proxies"] = []
    return DB[uid]["proxies"]

def get_user_active_proxy(user_id):
    uid = str(user_id)
    if uid not in DB:
        return None
    active_id = DB[uid].get("active_proxy_id")
    if not active_id:
        return None
    for px in DB[uid].get("proxies", []):
        if px["id"] == active_id:
            return px
    return None

def build_pyrogram_proxy(px):
    if not px:
        return None
    arg = {
        "scheme": px["scheme"],
        "hostname": px["hostname"],
        "port": int(px["port"])
    }
    if px.get("username"):
        arg["username"] = px["username"]
    if px.get("password"):
        arg["password"] = px["password"]
    return arg

async def switch_user_proxy(user_id, proxy_id_or_none):
    uid = str(user_id)
    if uid not in DB:
        return False, "User not found in database"

    DB[uid]["active_proxy_id"] = proxy_id_or_none
    save_db(DB)

    data = DB[uid]
    active_px = get_user_active_proxy(user_id)
    proxy_arg = build_pyrogram_proxy(active_px)

    old_client = user_clients.get(user_id)
    if old_client:
        try:
            if old_client.is_connected:
                await old_client.disconnect()
        except Exception:
            pass

    new_client = Client(
        f"user_{uid}",
        session_string=data["session"],
        api_id=data["api_id"],
        api_hash=data["api_hash"],
        proxy=proxy_arg,
        in_memory=True,
        sleep_threshold=60
    )
    try:
        await new_client.connect()
        try:
            new_client.me = await new_client.get_me()
        except Exception:
            pass
        user_clients[user_id] = new_client
        target_name = active_px["label"] if active_px else "Direct (No Proxy)"
        print(f"[PROXY] User {user_id} switched to {target_name}")
        return True, f"Connected via {target_name}"
    except Exception as e:
        print(f"[PROXY] Connection failed for user {user_id} with proxy: {e}. Falling back to Direct.")
        try:
            fallback = Client(
                f"user_{uid}",
                session_string=data["session"],
                api_id=data["api_id"],
                api_hash=data["api_hash"],
                proxy=None,
                in_memory=True,
                sleep_threshold=60
            )
            await fallback.connect()
            try:
                fallback.me = await fallback.get_me()
            except Exception:
                pass
            user_clients[user_id] = fallback
            DB[uid]["active_proxy_id"] = None
            save_db(DB)
            return False, f"Proxy connection failed ({e}). Reverted to Direct connection."
        except Exception as ex2:
            return False, f"Reconnection error: {ex2}"

# Initialize SQLite tables on startup
init_dedup_db()
init_stats_db()


async def init_saved_clients():
    for uid_str, data in DB.items():
        try:
            uid = int(uid_str)
            active_px = get_user_active_proxy(uid)
            proxy_arg = build_pyrogram_proxy(active_px)

            c = Client(
                f"user_{uid}",
                session_string=data["session"],
                api_id=data["api_id"],
                api_hash=data["api_hash"],
                proxy=proxy_arg,
                in_memory=True,
                sleep_threshold=60
            )
            try:
                await c.connect()
                user_clients[uid] = c
                px_name = active_px['label'] if active_px else 'Direct'
                print(f"Loaded session for user {uid} (Proxy: {px_name})")
                try:
                    c.me = await c.get_me()
                except Exception:
                    pass
            except Exception as pe:
                if proxy_arg:
                    print(f"Proxy failed for user {uid} ({pe}), attempting Direct fallback...")
                    fallback = Client(
                        f"user_{uid}",
                        session_string=data["session"],
                        api_id=data["api_id"],
                        api_hash=data["api_hash"],
                        proxy=None,
                        in_memory=True,
                        sleep_threshold=60
                    )
                    await fallback.connect()
                    user_clients[uid] = fallback
                    try:
                        fallback.me = await fallback.get_me()
                    except Exception:
                        pass
                    DB[uid_str]["active_proxy_id"] = None
                    save_db(DB)
                    print(f"Fallback to Direct successful for user {uid}")
                else:
                    raise pe
        except Exception as e:
            print(f"Failed to load session for {uid_str}: {e}")

# ==================== PARSERS & HELPERS ====================

def parse_input(text):
    text = text.strip()
    if 't.me/+' in text or 't.me/joinchat/' in text:
        return 'INVITE_LINK', text
    m = re.search(r't\.me/c/(\d+)', text)
    if m:
        return 'CHAT_ID', int('-100' + m.group(1))
    m = re.search(r't\.me/([a-zA-Z0-9_]+)', text)
    if m and m.group(1) not in ('c', 'joinchat'):
        return 'USERNAME', m.group(1)
    if text.startswith('@'):
        return 'USERNAME', text[1:]
    if re.match(r'^-?\d+$', text):
        return 'CHAT_ID', int(text)
    return 'SEARCH_QUERY', text

def parse_id_range(text):
    text = text.strip()
    m = re.match(r'^(\d+)\s*(?:->|-|to)\s*(\d+)$', text, re.IGNORECASE)
    if m:
        min_id, max_id = int(m.group(1)), int(m.group(2))
        if min_id > max_id:
            min_id, max_id = max_id, min_id
        return min_id, max_id
    if re.match(r'^\d+$', text):
        mid = int(text)
        return mid, mid
    return None

def parse_date_range(text):
    text = text.strip()
    parts = re.split(r'\s*(?:->|to)\s*', text, flags=re.IGNORECASE)
    try:
        if len(parts) == 2:
            d1 = datetime.strptime(parts[0].strip(), '%Y-%m-%d')
            d2 = datetime.strptime(parts[1].strip(), '%Y-%m-%d')
            if d1 > d2:
                d1, d2 = d2, d1
            d2 = d2.replace(hour=23, minute=59, second=59)
            return d1, d2
        elif len(parts) == 1:
            d1 = datetime.strptime(parts[0].strip(), '%Y-%m-%d')
            d2 = datetime.now()
            return d1, d2
    except Exception:
        return None
    return None

def apply_signature(caption, signature_template, src_title):
    if not signature_template:
        return caption or ""
    sig = signature_template.replace("{channel}", src_title).strip()
    if not sig:
        return caption or ""

    if caption:
        full = f"{caption}\n\n{sig}"
    else:
        full = sig

    if len(full) > 1024:
        avail = 1024 - len(sig) - 5
        full = f"{(caption or '')[:avail]}...\n\n{sig}"
    return full

async def resolve_chat(u_client, target_type, target_val):
    try:
        if target_type == 'INVITE_LINK':
            try:
                chat = await u_client.join_chat(target_val)
                title = chat.title or "Private Channel"
                return chat.id, title
            except Exception:
                chat = await u_client.get_chat(target_val)
                title = chat.title or "Private Channel"
                return chat.id, title
        else:
            chat = await u_client.get_chat(target_val)
            title = chat.title or chat.first_name or "Unknown Chat"
            return chat.id, title
    except Exception as e:
        return None, str(e)

def get_user_range_config(user_id):
    if user_id not in user_auth_data:
        user_auth_data[user_id] = {}
    if "range_config" not in user_auth_data[user_id]:
        user_auth_data[user_id]["range_config"] = {
            "type": "all",
            "label": "All messages (Full History)"
        }
    return user_auth_data[user_id]["range_config"]

def set_user_range_config(user_id, rtype, val=None, label=None, extra=None):
    if user_id not in user_auth_data:
        user_auth_data[user_id] = {}
    cfg = {"type": rtype, "label": label or rtype}
    if val is not None:
        cfg["value"] = val
    if extra:
        cfg.update(extra)
    user_auth_data[user_id]["range_config"] = cfg

def get_user_copy_settings(user_id):
    if user_id not in user_auth_data:
        user_auth_data[user_id] = {}
    if "copy_settings" not in user_auth_data[user_id]:
        user_auth_data[user_id]["copy_settings"] = {
            "content_type": "all",
            "skip_service": True,
            "skip_forwarded": False,
            "skip_duplicates": False,
            "signature": None
        }
    if "skip_duplicates" not in user_auth_data[user_id]["copy_settings"]:
        user_auth_data[user_id]["copy_settings"]["skip_duplicates"] = False
    return user_auth_data[user_id]["copy_settings"]

# Cache user dialogs for fast pagination & search
async def refresh_user_dialogs(user_id):
    if user_id not in user_clients:
        return []
    u_client = user_clients[user_id]
    dialogs = []
    dialogs.append({
        "id": "me",
        "title": "Saved Messages 🔖",
        "raw_title": "Saved Messages",
        "username": "",
        "type": "personal"
    })
    try:
        me = getattr(u_client, "me", None)
        if not me:
            try:
                me = await u_client.get_me()
                u_client.me = me
            except Exception:
                me = None
        my_id = me.id if me else None

        async for d in u_client.get_dialogs(limit=250):
            if my_id and d.chat.id == my_id:
                continue
            title = d.chat.title or d.chat.first_name or "Unknown Chat"
            chat_type = str(d.chat.type).split('.')[-1].lower()
            icon = "📢" if "channel" in chat_type else "👥" if "group" in chat_type else "👤"
            username = getattr(d.chat, "username", "") or ""
            dialogs.append({
                "id": d.chat.id,
                "title": f"{icon} {title[:25]}",
                "raw_title": title,
                "username": username,
                "type": chat_type
            })
    except Exception as e:
        print(f"Dialog fetch error for {user_id}: {e}")
    user_dialog_cache[user_id] = dialogs
    user_dialog_last_fetch[user_id] = time.time()
    return dialogs

# Build paginated dialog keyboard with search & refresh support
def build_dialog_keyboard(user_id, mode, page=0, query=None):
    all_dialogs = user_dialog_cache.get(user_id, [])
    if query:
        q = query.lower().strip()
        dialogs = [
            d for d in all_dialogs
            if q in d.get("raw_title", "").lower()
            or q in d.get("title", "").lower()
            or q in d.get("username", "").lower()
        ]
    else:
        dialogs = all_dialogs

    ITEMS_PER_PAGE = 6
    total_pages = max(1, (len(dialogs) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = dialogs[start:end]

    kb = InlineKeyboardMarkup()
    for item in page_items:
        kb.add(InlineKeyboardButton(item["title"], callback_data=f"sel_{mode}_{item['id']}"))

    if query and len(dialogs) == 0:
        kb.add(InlineKeyboardButton("⚠️ No chats matched this search", callback_data="noop"))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"p_{mode}_{page - 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton("⏹️", callback_data="noop"))

    nav_buttons.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"p_{mode}_{page + 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton("⏹️", callback_data="noop"))

    kb.row(*nav_buttons)

    if query:
        kb.add(InlineKeyboardButton("❌ Clear Search / Show All", callback_data=f"clear_search_{mode}"))
    else:
        kb.row(
            InlineKeyboardButton("🔄 Refresh Chats", callback_data=f"refresh_{mode}"),
            InlineKeyboardButton("🔖 Saved Messages", callback_data=f"sel_{mode}_me")
        )

    if mode == "dst":
        kb.add(InlineKeyboardButton("🔙 Back to Source Selection", callback_data="flow_pick_src"))
    kb.add(InlineKeyboardButton("🏠 Back to Main Menu", callback_data="nav_home"))
    return kb, page, total_pages, len(dialogs)

# ==================== SCREENS & DASHBOARD ====================

async def send_dashboard(chat_id, user_id, message_id=None):
    is_logged_in = user_id in user_clients
    kb = InlineKeyboardMarkup()

    if is_logged_in:
        u_client = user_clients[user_id]
        me = getattr(u_client, "me", None)
        if not me:
            try:
                me = await u_client.get_me()
                u_client.me = me
            except Exception:
                me = None

        if me:
            name = me.first_name or "Telegram User"
            username = f"@{me.username}" if me.username else "No username"
            phone = me.phone_number or "Hidden"
        else:
            name, username, phone = "Logged In", "", ""

        is_running = active_tasks.get(user_id, False)
        status_text = "🟢 Active Copying..." if is_running else "⚪ Idle / Ready"

        active_px = get_user_active_proxy(user_id)
        if active_px:
            ping_str = f" ({active_px.get('ping_ms', -1)}ms)" if active_px.get("ping_ms", -1) > 0 else ""
            proxy_display = f"🟢 {active_px['label']}{ping_str}"
        else:
            proxy_display = "⚪ Direct (No Proxy)"

        text = (
            "🤖 <b>Telegram Channel Copier — Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Account:</b> {name} ({username})\n"
            f"📱 <b>Phone:</b> +{phone}\n"
            f"📊 <b>Status:</b> {status_text}\n"
            f"🌐 <b>Proxy:</b> {proxy_display}\n"
            f"⚡ <b>Engine:</b> 100× MTProto Batching / Parallel (Active)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose an option below to start:"
        )

        kb.add(InlineKeyboardButton("🚀 Start New Copy", callback_data="flow_pick_src"))
        if is_running:
            kb.add(InlineKeyboardButton("📊 View Active Task Progress", callback_data="view_task"))
        kb.row(
            InlineKeyboardButton("📊 My Analytics", callback_data="nav_analytics"),
            InlineKeyboardButton("🌐 Proxy Settings", callback_data="nav_proxy")
        )
        kb.row(
            InlineKeyboardButton("❓ Help & Guide", callback_data="nav_help"),
            InlineKeyboardButton("🚪 Logout", callback_data="nav_logout")
        )
    else:
        text = (
            "🤖 <b>Telegram Channel Copier v2.0</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <b>Ultra-Fast 100× MTProto Batching Engine</b>\n"
            "🛡️ <b>Built-in Anti-Ban Safeguards</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👋 Welcome! Please log in with your Telegram account to start copying channels and groups."
        )
        kb.add(InlineKeyboardButton("🔐 Login with Telegram", callback_data="start_login"))
        kb.add(InlineKeyboardButton("❓ Help & Instructions", callback_data="nav_help"))

    if message_id:
        try:
            await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

# Step 3: Range Selection Screen
async def show_range_selection(chat_id, user_id, message_id=None):
    user_states[user_id] = "PICK_RANGE"
    data = user_auth_data.get(user_id, {})
    src_title = data.get("src_title", "Selected Source")
    dst_title = data.get("dst_title", "Selected Destination")
    current_range = get_user_range_config(user_id).get("label", "All messages")

    text = (
        "📊 <b>Step 3/5: Select Message Range</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Source:</b> {src_title}\n"
        f"🔸 <b>Destination:</b> {dst_title}\n\n"
        f"📌 <b>Current Selection:</b> <code>{current_range}</code>\n\n"
        "Choose how many messages to copy:"
    )

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📥 Last 100", callback_data="range_last_100"),
        InlineKeyboardButton("📥 Last 500", callback_data="range_last_500")
    )
    kb.row(
        InlineKeyboardButton("📥 Last 1,000", callback_data="range_last_1000"),
        InlineKeyboardButton("📚 All Messages", callback_data="range_all")
    )
    kb.row(
        InlineKeyboardButton("🔢 Custom Message ID", callback_data="range_custom_id"),
        InlineKeyboardButton("📅 Date Range", callback_data="range_custom_date")
    )
    kb.row(
        InlineKeyboardButton("⬅️ Back to Destination", callback_data="flow_pick_dst"),
        InlineKeyboardButton("❌ Cancel", callback_data="nav_home")
    )

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

# Step 4: Advanced Copy Settings & Filters Screen
async def show_copy_settings(chat_id, user_id, message_id=None):
    user_states[user_id] = "CONFIG_SETTINGS"
    settings = get_user_copy_settings(user_id)
    range_cfg = get_user_range_config(user_id)
    ctype = settings.get("content_type", "all")
    ctype_label = CONTENT_TYPE_LABELS.get(ctype, "All Content")
    skip_fwd = settings.get("skip_forwarded", False)
    fwd_icon = "✅ YES (Skip)" if skip_fwd else "⬜ NO (Include)"
    skip_dup = settings.get("skip_duplicates", False)
    dup_icon = "✅ YES (Skip)" if skip_dup else "⬜ NO (Copy All)"
    dedup_count = get_user_dedup_count(user_id)
    sig = settings.get("signature")
    sig_display = f"<code>{sig[:30]}...</code>" if sig else "None (Off)"

    text = (
        "⚙️ <b>Step 4/5: Advanced Filters & Options</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Range:</b> {range_cfg.get('label')}\n"
        f"📄 <b>Content Filter:</b> <b>{ctype_label}</b>\n"
        f"📦 <b>Skip Forwarded:</b> {fwd_icon}\n"
        f"⚡ <b>Skip Duplicates:</b> {dup_icon} (<code>{dedup_count}</code> indexed)\n"
        f"✍️ <b>Signature:</b> {sig_display}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap buttons below to toggle options, or tap <b>Continue</b>:"
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(f"📄 Content Filter: {ctype_label}", callback_data="toggle_content_type"))
    kb.row(
        InlineKeyboardButton(f"📦 Skip Fwd: {fwd_icon}", callback_data="toggle_skip_fwd"),
        InlineKeyboardButton(f"⚡ Skip Dup: {dup_icon}", callback_data="toggle_skip_dup")
    )
    if sig:
        kb.row(
            InlineKeyboardButton("✍️ Edit Signature", callback_data="btn_set_sig"),
            InlineKeyboardButton("🗑️ Remove", callback_data="btn_clear_sig")
        )
    else:
        kb.add(InlineKeyboardButton("✍️ Add Custom Signature", callback_data="btn_set_sig"))

    kb.add(InlineKeyboardButton("✅ Continue to Review ➔", callback_data="flow_confirm"))
    kb.row(
        InlineKeyboardButton("⬅️ Back to Range", callback_data="flow_pick_range"),
        InlineKeyboardButton("🔄 Reset Defaults", callback_data="reset_settings")
    )

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

# Step 5: Final Review & Confirmation Screen
async def show_confirmation(chat_id, user_id, message_id=None):
    user_states[user_id] = "CONFIRM"
    data = user_auth_data.get(user_id, {})
    src_title = data.get("src_title", "Unknown")
    dst_title = data.get("dst_title", "Unknown")
    src_id = data.get("source", "Unknown")
    dst_id = data.get("dest", "Unknown")
    range_cfg = get_user_range_config(user_id)
    settings = get_user_copy_settings(user_id)

    ctype = settings.get("content_type", "all")
    ctype_label = CONTENT_TYPE_LABELS.get(ctype, "All Content")
    skip_fwd = "✅ Yes" if settings.get("skip_forwarded") else "⬜ No"
    skip_dup = "✅ Yes (dedup.db)" if settings.get("skip_duplicates") else "⬜ No"
    sig = settings.get("signature")
    sig_summary = "✅ Active" if sig else "None (Off)"

    text = (
        "📋 <b>Step 5/5: Final Review & Confirmation</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>FROM (Source):</b> {src_title}\n"
        f"   └ <code>{src_id}</code>\n\n"
        f"🔸 <b>TO (Destination):</b> {dst_title}\n"
        f"   └ <code>{dst_id}</code>\n\n"
        f"📊 <b>Range:</b> {range_cfg.get('label')}\n"
        f"📄 <b>Content Filter:</b> {ctype_label}\n"
        f"📦 <b>Skip Forwarded:</b> {skip_fwd}\n"
        f"⚡ <b>Skip Duplicates:</b> {skip_dup}\n"
        f"✍️ <b>Custom Signature:</b> {sig_summary}\n"
        "⚡ <b>Engine:</b> 100× MTProto Batching / Parallel Bypass (6x)\n"
        "🛡️ <b>Anti-Ban:</b> Tuned Delays & Auto-Breather Active\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Ready to begin? Click <b>Confirm & Start Copying</b> below:"
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("▶️ Confirm & Start Copying", callback_data="confirm_copy"))
    kb.row(
        InlineKeyboardButton("📊 Change Range", callback_data="flow_pick_range"),
        InlineKeyboardButton("⚙️ Change Settings", callback_data="flow_settings")
    )
    kb.add(InlineKeyboardButton("❌ Cancel Job", callback_data="nav_home"))

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


# ==================== ANALYTICS & PROXY SCREENS ====================

async def show_analytics(chat_id, user_id, message_id=None):
    summary = get_user_analytics_summary(user_id)
    dedup_count = get_user_dedup_count(user_id)

    mins = int(summary["total_duration"] // 60)
    secs = int(summary["total_duration"] % 60)
    duration_fmt = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

    text = (
        "📊 <b>Live Analytics Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>User ID:</b> <code>{user_id}</code>\n\n"
        "📈 <b>All-Time Performance:</b>\n"
        f"• Messages Copied: <b>{summary['total_copied']:,}</b>\n"
        f"• Total Jobs Run: <b>{summary['total_jobs']}</b>\n"
        f"• Success Rate: <b>{summary['success_rate']}%</b>\n"
        f"• Average Speed: <b>{summary['avg_speed']} msg/s</b>\n"
        f"• Total Duration: <b>{duration_fmt}</b>\n\n"
        "📅 <b>Today's Activity:</b>\n"
        f"• Messages Copied: <b>{summary['today_copied']:,}</b>\n"
        f"• Jobs Completed: <b>{summary['today_jobs']}</b>\n\n"
        "🛡️ <b>Deduplication Engine:</b>\n"
        f"• Unique Hashes Indexed: <b>{dedup_count:,}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📈 7-Day Chart", callback_data="analytics_chart"),
        InlineKeyboardButton("🕒 Recent 5 Jobs", callback_data="analytics_recent")
    )
    if dedup_count > 0:
        kb.add(InlineKeyboardButton("🧹 Clear Dedup Cache", callback_data="dedup_clear_prompt"))
    kb.add(InlineKeyboardButton("🏠 Back to Dashboard", callback_data="nav_home"))

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

async def show_analytics_chart(chat_id, user_id, message_id=None):
    stats = get_user_7day_stats(user_id)
    total_7d = sum(c for _, c in stats)
    max_val = max([c for _, c in stats] + [1])

    lines = []
    for d_str, count in stats:
        bar_len = int((count / max_val) * 10) if max_val > 0 else 0
        bar = "▰" * bar_len + "▱" * (10 - bar_len)
        lines.append(f"<code>{d_str}</code> {bar} <b>{count:,}</b>")

    chart_body = "\n".join(lines)
    text = (
        "📈 <b>7-Day Activity Chart</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Total (Last 7 Days):</b> <b>{total_7d:,} messages</b>\n\n"
        f"{chart_body}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Proportional bars indicate daily volume.</i>"
    )

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("⬅️ Back to Analytics", callback_data="nav_analytics"),
        InlineKeyboardButton("🏠 Home", callback_data="nav_home")
    )

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

async def show_analytics_recent(chat_id, user_id, message_id=None):
    jobs = get_user_recent_jobs(user_id, 5)
    if not jobs:
        recent_text = "<i>No copy jobs recorded yet.</i>"
    else:
        items = []
        for i, job in enumerate(jobs, 1):
            src_t, dst_t, total_cnt, copied_cnt, dup_cnt, fail_cnt, dur, spd, created = job
            pct = round((copied_cnt / max(total_cnt, 1)) * 100, 1) if total_cnt > 0 else 100
            dt_str = datetime.fromtimestamp(created).strftime("%m/%d %H:%M")
            items.append(
                f"<b>{i}. {src_t[:15]} ➔ {dst_t[:15]}</b>\n"
                f"   └ Copied: <b>{copied_cnt}/{total_cnt}</b> ({pct}%) | ⚡ {spd} msg/s\n"
                f"   └ Dups: {dup_cnt} | Time: {dur:.1f}s | {dt_str}"
            )
        recent_text = "\n\n".join(items)

    text = (
        "🕒 <b>Recent Copy Jobs (Last 5)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{recent_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup()
    if jobs:
        kb.add(InlineKeyboardButton("🗑️ Clear Job History", callback_data="analytics_clear_prompt"))
    kb.row(
        InlineKeyboardButton("⬅️ Back to Analytics", callback_data="nav_analytics"),
        InlineKeyboardButton("🏠 Home", callback_data="nav_home")
    )

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

async def show_proxy_menu(chat_id, user_id, message_id=None):
    proxies = get_user_proxies(user_id)
    active_px = get_user_active_proxy(user_id)
    active_id = DB.get(str(user_id), {}).get("active_proxy_id")

    if active_px:
        ping_str = f" ({active_px.get('ping_ms', -1)}ms)" if active_px.get("ping_ms", -1) > 0 else ""
        current_status = f"🟢 <b>{active_px['label']}</b>{ping_str}"
    else:
        current_status = "⚪ <b>Direct Connection</b> (No Proxy)"

    text = (
        "🌐 <b>Proxy Configuration & Hot-Swap</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ <b>Current Mode:</b> {current_status}\n"
        f"📦 <b>Saved Proxies:</b> {len(proxies)}/5\n\n"
        "Use proxies to bypass Telegram geo-restrictions, prevent IP bans, and stabilize downloads."
    )

    kb = InlineKeyboardMarkup()
    if proxies:
        for px in proxies:
            pid = px["id"]
            is_act = (pid == active_id)
            act_mark = "✅ " if is_act else "⚡ "
            ping_val = px.get("ping_ms", -1)
            ping_txt = f"{ping_val}ms" if ping_val > 0 else "N/A"

            btn_label = f"{act_mark}{px['label'][:20]}"
            kb.row(
                InlineKeyboardButton(btn_label, callback_data=f"proxy_use_{pid}"),
                InlineKeyboardButton(f"📶 {ping_txt}", callback_data=f"proxy_ping_{pid}"),
                InlineKeyboardButton("🗑️", callback_data=f"proxy_del_{pid}")
            )
        if active_id:
            kb.add(InlineKeyboardButton("🔌 Switch to Direct Connection (No Proxy)", callback_data="proxy_use_direct"))
    else:
        text += "\n\n<i>No proxies added yet. Tap 'Add New Proxy' below to add one.</i>"

    if len(proxies) < 5:
        kb.add(InlineKeyboardButton("➕ Add New Proxy", callback_data="proxy_add"))
    kb.add(InlineKeyboardButton("🏠 Back to Dashboard", callback_data="nav_home"))

    if message_id:
        try:
            return await bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

# ==================== BOT HANDLERS ====================

@bot.message_handler(commands=['start'])
async def start_cmd(message):
    user_id = message.from_user.id
    user_states[user_id] = None
    user_active_query[user_id] = None
    await send_dashboard(message.chat.id, user_id)

@bot.message_handler(commands=['help'])
async def help_cmd(message):
    await show_help(message.chat.id)

@bot.message_handler(commands=['logout'])
async def logout_cmd(message):
    await perform_logout(message.chat.id, message.from_user.id)

async def show_help(chat_id, message_id=None):
    help_text = (
        "🤖 <b>Channel Copier Bot — User Guide</b>\n\n"
        "This bot copies messages from any channel/group to another with <b>100× MTProto Batching</b>.\n\n"
        "<b>📌 New Features Added:</b>\n"
        "1. <b>📊 Message Range Selector:</b> Copy Last 100, 500, 1000, Custom ID, or Date ranges without scanning the entire channel.\n"
        "2. <b>📄 Content Type Filters:</b> Copy Videos only, Photos only, Documents only, or Text only.\n"
        "3. <b>✍️ Custom Signature:</b> Append your channel link/tag to all copied messages automatically (use <code>{channel}</code>).\n"
        "4. <b>⚡ Parallel Bypass:</b> Bypasses protected/restricted channels using 6 concurrent workers.\n"
        "5. <b>🔄 Refresh Button:</b> Instantly fetch newly joined channels.\n\n"
        "👨‍💻 <b>Developer:</b> @R3v_x"
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🏠 Back to Dashboard", callback_data="nav_home"))

    if message_id:
        try:
            await bot.edit_message_text(help_text, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, help_text, reply_markup=kb, parse_mode="HTML")

async def perform_logout(chat_id, user_id, message_id=None):
    if user_id in user_clients:
        try:
            await user_clients[user_id].log_out()
        except Exception:
            pass
        del user_clients[user_id]

    if str(user_id) in DB:
        del DB[str(user_id)]
        save_db(DB)

    user_states[user_id] = None
    user_auth_data[user_id] = {}
    user_dialog_cache[user_id] = []
    user_active_query[user_id] = None
    active_tasks[user_id] = False

    msg = "🚪 <b>Logged Out Successfully.</b>\nYour session and credentials have been cleared."
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔐 Login Again", callback_data="start_login"))

    if message_id:
        try:
            await bot.edit_message_text(msg, chat_id, message_id, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, msg, reply_markup=kb, parse_mode="HTML")

# ==================== CALLBACK NAVIGATION ====================

@bot.callback_query_handler(func=lambda call: call.data == "noop")
async def noop_cb(call):
    await bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "nav_home")
async def nav_home_cb(call):
    user_id = call.from_user.id
    user_states[user_id] = None
    user_active_query[user_id] = None
    await send_dashboard(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "nav_help")
async def nav_help_cb(call):
    await show_help(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "nav_logout")
async def nav_logout_cb(call):
    await perform_logout(call.message.chat.id, call.from_user.id, call.message.message_id)

# Refresh Chats callback
@bot.callback_query_handler(func=lambda call: call.data in ("refresh_src", "refresh_dst"))
async def refresh_chats_cb(call):
    user_id = call.from_user.id
    mode = "src" if "src" in call.data else "dst"
    await bot.answer_callback_query(call.id, "🔄 Fetching latest chats from Telegram...")
    await refresh_user_dialogs(user_id)
    query = user_active_query.get(user_id)
    kb, page, total, count = build_dialog_keyboard(user_id, mode, page=0, query=query)
    step_title = "SOURCE (Copy FROM)" if mode == "src" else "DESTINATION (Copy TO)"
    text = (
        f"📍 <b>Select {step_title}</b> (✅ Chats Refreshed: {count} available)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap a chat below, <b>OR</b> simply type/paste in chat:\n"
        "• Channel Link: <code>https://t.me/...</code>\n"
        "• Username: <code>@channel_name</code>\n"
        "• Chat ID: <code>-100...</code>\n"
        "• Or <b>forward any message</b> from the channel here!"
    )
    try:
        await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

# Clear active search
@bot.callback_query_handler(func=lambda call: call.data in ("clear_search_src", "clear_search_dst"))
async def clear_search_cb(call):
    user_id = call.from_user.id
    mode = "src" if "src" in call.data else "dst"
    user_active_query[user_id] = None
    await bot.answer_callback_query(call.id, "Search cleared.")
    kb, page, total, count = build_dialog_keyboard(user_id, mode, page=0)
    step_title = "SOURCE (Copy FROM)" if mode == "src" else "DESTINATION (Copy TO)"
    text = (
        f"📍 <b>Select {step_title}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap a chat below, <b>OR</b> simply type/paste in chat:\n"
        "• Channel Link: <code>https://t.me/...</code>\n"
        "• Username: <code>@channel_name</code>\n"
        "• Chat ID: <code>-100...</code>\n"
        "• Or <b>forward any message</b> from the channel here!"
    )
    try:
        await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

# Step 1: Start Source Selection Flow
@bot.callback_query_handler(func=lambda call: call.data == "flow_pick_src")
async def flow_pick_src_cb(call):
    user_id = call.from_user.id
    if user_id not in user_clients:
        return await bot.answer_callback_query(call.id, "Please login first!", show_alert=True)

    user_states[user_id] = "PICK_SRC"
    user_active_query[user_id] = None
    if user_id not in user_auth_data:
        user_auth_data[user_id] = {}

    await bot.answer_callback_query(call.id)

    now = time.time()
    cache_age = now - user_dialog_last_fetch.get(user_id, 0)
    if user_dialog_cache.get(user_id) and cache_age < 30:
        kb, page, total, count = build_dialog_keyboard(user_id, "src", page=0)
        text = (
            "📍 <b>Step 1/5: Select SOURCE Chat (Copy FROM)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Tap a chat below, <b>OR</b> simply type/paste in chat:\n"
            "• Search by Name: <code>movies</code>, <code>tech</code>, etc.\n"
            "• Channel Link: <code>https://t.me/...</code>\n"
            "• Username: <code>@channel_name</code>\n"
            "• Or <b>forward any message</b> from the source channel here!"
        )
        try:
            return await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            return await bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

    temp_kb = InlineKeyboardMarkup()
    temp_kb.add(InlineKeyboardButton("Saved Messages 🔖", callback_data="sel_src_me"))
    temp_kb.add(InlineKeyboardButton("🏠 Back to Main Menu", callback_data="nav_home"))
    loading_text = (
        "📍 <b>Step 1/5: Select SOURCE Chat</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <i>Loading latest chats...</i>\n\n"
        "💡 <b>You can already type or paste:</b>\n"
        "• Search name: <code>movies</code>, <code>news</code>, etc.\n"
        "• Channel Link: <code>https://t.me/...</code>\n"
        "• Username: <code>@channel_name</code>\n"
        "• Or <b>forward any message</b> from the channel!"
    )
    try:
        await bot.edit_message_text(loading_text, call.message.chat.id, call.message.message_id, reply_markup=temp_kb, parse_mode="HTML")
    except Exception:
        pass

    await refresh_user_dialogs(user_id)
    kb, page, total, count = build_dialog_keyboard(user_id, "src", page=0)
    text = (
        "📍 <b>Step 1/5: Select SOURCE Chat (Copy FROM)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap a chat below, <b>OR</b> simply type/paste in chat:\n"
        "• Search by Name: <code>movies</code>, <code>tech</code>, etc.\n"
        "• Channel Link: <code>https://t.me/...</code>\n"
        "• Username: <code>@channel_name</code>\n"
        "• Or <b>forward any message</b> from the source channel here!"
    )
    try:
        await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

# Step 2: Start Destination Selection Flow
@bot.callback_query_handler(func=lambda call: call.data == "flow_pick_dst")
async def flow_pick_dst_cb(call):
    user_id = call.from_user.id
    if user_id not in user_clients:
        return await bot.answer_callback_query(call.id, "Please login first!", show_alert=True)

    user_states[user_id] = "PICK_DST"
    user_active_query[user_id] = None
    src_title = user_auth_data.get(user_id, {}).get("src_title", "Selected Source")

    await bot.answer_callback_query(call.id)

    now = time.time()
    cache_age = now - user_dialog_last_fetch.get(user_id, 0)
    if not user_dialog_cache.get(user_id) or cache_age > 30:
        await refresh_user_dialogs(user_id)

    kb, page, total, count = build_dialog_keyboard(user_id, "dst", page=0)
    text = (
        "📍 <b>Step 2/5: Select DESTINATION Chat (Copy TO)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Selected Source: <b>{src_title}</b>\n\n"
        "Tap a chat below, <b>OR</b> simply type/paste in chat:\n"
        "• Search by Name: <code>movies</code>, <code>backup</code>, etc.\n"
        "• Channel Link: <code>https://t.me/...</code>\n"
        "• Username: <code>@channel_name</code>\n"
        "• Or <b>forward any message</b> from the destination channel here!"
    )
    try:
        await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

# Pagination handler for dialog buttons
@bot.callback_query_handler(func=lambda call: call.data.startswith("p_"))
async def pagination_cb(call):
    user_id = call.from_user.id
    parts = call.data.split("_")
    mode = parts[1]
    page = int(parts[2])
    query = user_active_query.get(user_id)

    kb, page, total, count = build_dialog_keyboard(user_id, mode, page=page, query=query)
    try:
        await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        pass
    await bot.answer_callback_query(call.id)

# Dialog selection: Source chosen via button
@bot.callback_query_handler(func=lambda call: call.data.startswith("sel_src_"))
async def sel_src_cb(call):
    user_id = call.from_user.id
    raw_id = call.data.replace("sel_src_", "")
    target = "me" if raw_id == "me" else int(raw_id)

    u_client = user_clients[user_id]
    chat_id, title = await resolve_chat(u_client, 'CHAT_ID', target)
    if not chat_id:
        return await bot.answer_callback_query(call.id, f"Error: {title}", show_alert=True)

    if user_id not in user_auth_data:
        user_auth_data[user_id] = {}
    user_auth_data[user_id]["source"] = chat_id
    user_auth_data[user_id]["src_title"] = title
    user_active_query[user_id] = None

    await bot.answer_callback_query(call.id, f"Source: {title[:20]}")
    await flow_pick_dst_cb(call)

# Dialog selection: Destination chosen via button
@bot.callback_query_handler(func=lambda call: call.data.startswith("sel_dst_"))
async def sel_dst_cb(call):
    user_id = call.from_user.id
    raw_id = call.data.replace("sel_dst_", "")
    target = "me" if raw_id == "me" else int(raw_id)

    u_client = user_clients[user_id]
    chat_id, title = await resolve_chat(u_client, 'CHAT_ID', target)
    if not chat_id:
        return await bot.answer_callback_query(call.id, f"Error: {title}", show_alert=True)

    user_auth_data[user_id]["dest"] = chat_id
    user_auth_data[user_id]["dst_title"] = title
    user_active_query[user_id] = None

    await bot.answer_callback_query(call.id, f"Destination: {title[:20]}")
    await show_range_selection(call.message.chat.id, user_id, call.message.message_id)

# ==================== RANGE SELECTION CALLBACKS ====================

@bot.callback_query_handler(func=lambda call: call.data == "flow_pick_range")
async def flow_pick_range_cb(call):
    user_id = call.from_user.id
    await bot.answer_callback_query(call.id)
    await show_range_selection(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "range_last_100")
async def range_last_100_cb(call):
    user_id = call.from_user.id
    set_user_range_config(user_id, "last_n", val=100, label="Last 100 messages")
    await bot.answer_callback_query(call.id, "Range set: Last 100 messages")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "range_last_500")
async def range_last_500_cb(call):
    user_id = call.from_user.id
    set_user_range_config(user_id, "last_n", val=500, label="Last 500 messages")
    await bot.answer_callback_query(call.id, "Range set: Last 500 messages")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "range_last_1000")
async def range_last_1000_cb(call):
    user_id = call.from_user.id
    set_user_range_config(user_id, "last_n", val=1000, label="Last 1,000 messages")
    await bot.answer_callback_query(call.id, "Range set: Last 1,000 messages")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "range_all")
async def range_all_cb(call):
    user_id = call.from_user.id
    set_user_range_config(user_id, "all", label="All messages (Full History)")
    await bot.answer_callback_query(call.id, "Range set: All messages")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "range_custom_id")
async def range_custom_id_cb(call):
    user_id = call.from_user.id
    user_states[user_id] = "WAIT_CUSTOM_ID"
    await bot.answer_callback_query(call.id)

    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Range Options", callback_data="flow_pick_range"))
    text = (
        "🔢 <b>Custom Message ID Range</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Send the message ID range in one of these formats:\n\n"
        "• Range: <code>100 -> 500</code> (or <code>100-500</code>)\n"
        "• Single Message ID: <code>250</code>\n\n"
        "Send your message ID input below:"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "range_custom_date")
async def range_custom_date_cb(call):
    user_id = call.from_user.id
    user_states[user_id] = "WAIT_CUSTOM_DATE"
    await bot.answer_callback_query(call.id)

    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Range Options", callback_data="flow_pick_range"))
    text = (
        "📅 <b>Custom Date Range</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Send date range in this format (YYYY-MM-DD):\n\n"
        "• Range: <code>2024-01-01 -> 2024-06-30</code>\n"
        "• From Date to Now: <code>2024-06-01</code>\n\n"
        "Send your date range below:"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

# ==================== ADVANCED SETTINGS & FILTERS CALLBACKS ====================

@bot.callback_query_handler(func=lambda call: call.data == "flow_settings")
async def flow_settings_cb(call):
    user_id = call.from_user.id
    await bot.answer_callback_query(call.id)
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_content_type")
async def toggle_content_type_cb(call):
    user_id = call.from_user.id
    settings = get_user_copy_settings(user_id)
    current = settings.get("content_type", "all")
    idx = CONTENT_TYPE_CYCLE.index(current) if current in CONTENT_TYPE_CYCLE else 0
    next_type = CONTENT_TYPE_CYCLE[(idx + 1) % len(CONTENT_TYPE_CYCLE)]
    settings["content_type"] = next_type
    await bot.answer_callback_query(call.id, f"Filter: {CONTENT_TYPE_LABELS.get(next_type)}")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_skip_fwd")
async def toggle_skip_fwd_cb(call):
    user_id = call.from_user.id
    settings = get_user_copy_settings(user_id)
    settings["skip_forwarded"] = not settings.get("skip_forwarded", False)
    status = "ON (Skip forwarded)" if settings["skip_forwarded"] else "OFF (Include forwarded)"
    await bot.answer_callback_query(call.id, f"Skip Forwarded: {status}")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "btn_set_sig")
async def btn_set_sig_cb(call):
    user_id = call.from_user.id
    user_states[user_id] = "WAIT_SIGNATURE"
    await bot.answer_callback_query(call.id)

    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Settings", callback_data="flow_settings"))
    text = (
        "✍️ <b>Add Custom Signature</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "This text will be appended to every copied message's caption.\n\n"
        "💡 Use <code>{channel}</code> to automatically insert the source channel's name!\n\n"
        "<b>Example:</b>\n"
        "<code>📥 From: {channel}\n"
        "🔔 Join: @MyChannel</code>\n\n"
        "Send your signature text below in chat:"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "btn_clear_sig")
async def btn_clear_sig_cb(call):
    user_id = call.from_user.id
    settings = get_user_copy_settings(user_id)
    settings["signature"] = None
    await bot.answer_callback_query(call.id, "Signature removed.")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "reset_settings")
async def reset_settings_cb(call):
    user_id = call.from_user.id
    user_auth_data[user_id]["copy_settings"] = {
        "content_type": "all",
        "skip_service": True,
        "skip_forwarded": False,
        "signature": None
    }
    await bot.answer_callback_query(call.id, "Settings reset to defaults.")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "flow_confirm")
async def flow_confirm_cb(call):
    user_id = call.from_user.id
    await bot.answer_callback_query(call.id)
    await show_confirmation(call.message.chat.id, user_id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "toggle_skip_dup")
async def toggle_skip_dup_cb(call):
    user_id = call.from_user.id
    settings = get_user_copy_settings(user_id)
    settings["skip_duplicates"] = not settings.get("skip_duplicates", False)
    status = "ON (Skip duplicates)" if settings["skip_duplicates"] else "OFF (Copy all)"
    await bot.answer_callback_query(call.id, f"Skip Duplicates: {status}")
    await show_copy_settings(call.message.chat.id, user_id, call.message.message_id)

# ==================== ANALYTICS CALLBACKS ====================

@bot.callback_query_handler(func=lambda call: call.data == "nav_analytics")
async def nav_analytics_cb(call):
    await bot.answer_callback_query(call.id)
    await show_analytics(call.message.chat.id, call.from_user.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "analytics_chart")
async def analytics_chart_cb(call):
    await bot.answer_callback_query(call.id)
    await show_analytics_chart(call.message.chat.id, call.from_user.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "analytics_recent")
async def analytics_recent_cb(call):
    await bot.answer_callback_query(call.id)
    await show_analytics_recent(call.message.chat.id, call.from_user.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "analytics_clear_prompt")
async def analytics_clear_prompt_cb(call):
    await bot.answer_callback_query(call.id)
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("⚠️ Yes, Delete All History", callback_data="analytics_clear_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="nav_analytics")
    )
    text = (
        "⚠️ <b>Clear Analytics History?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "This will permanently delete all your past job records and daily statistics.\n"
        "Are you sure?"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "analytics_clear_confirm")
async def analytics_clear_confirm_cb(call):
    clear_user_stats(call.from_user.id)
    await bot.answer_callback_query(call.id, "✅ History cleared successfully!", show_alert=True)
    await show_analytics(call.message.chat.id, call.from_user.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "dedup_clear_prompt")
async def dedup_clear_prompt_cb(call):
    await bot.answer_callback_query(call.id)
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("⚠️ Yes, Clear Dedup Cache", callback_data="dedup_clear_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="nav_analytics")
    )
    text = (
        "🧹 <b>Clear Deduplication Cache?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "This will remove all indexed message hashes.\n"
        "Previously copied messages will no longer be recognized as duplicates.\n"
        "Are you sure?"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "dedup_clear_confirm")
async def dedup_clear_confirm_cb(call):
    clear_user_dedup_cache(call.from_user.id)
    await bot.answer_callback_query(call.id, "✅ Dedup cache cleared!", show_alert=True)
    await show_analytics(call.message.chat.id, call.from_user.id, call.message.message_id)

# ==================== PROXY CALLBACKS ====================

@bot.callback_query_handler(func=lambda call: call.data == "nav_proxy")
async def nav_proxy_cb(call):
    user_states[call.from_user.id] = None
    await bot.answer_callback_query(call.id)
    await show_proxy_menu(call.message.chat.id, call.from_user.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "proxy_add")
async def proxy_add_cb(call):
    user_id = call.from_user.id
    user_states[user_id] = "WAIT_PROXY"
    await bot.answer_callback_query(call.id)

    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Proxy Menu", callback_data="nav_proxy"))
    text = (
        "🌐 <b>Add New Proxy</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Send your proxy below in one of these formats:\n\n"
        "• <code>socks5://1.2.3.4:1080</code>\n"
        "• <code>socks5://user:pass@1.2.3.4:1080</code>\n"
        "• <code>http://1.2.3.4:8080</code>\n"
        "• <code>1.2.3.4:1080:user:pass</code>\n"
        "• <code>1.2.3.4:1080</code>\n\n"
        "💡 <i>Bot will automatically test connection latency before saving.</i>"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("proxy_use_"))
async def proxy_use_cb(call):
    user_id = call.from_user.id
    pid = call.data.replace("proxy_use_", "")
    await bot.answer_callback_query(call.id, "🔄 Connecting to proxy...")
    target_id = None if pid == "direct" else pid
    ok, msg = await switch_user_proxy(user_id, target_id)
    if ok:
        await bot.answer_callback_query(call.id, f"✅ {msg}")
    else:
        await bot.answer_callback_query(call.id, f"⚠️ {msg}", show_alert=True)
    await show_proxy_menu(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("proxy_ping_"))
async def proxy_ping_cb(call):
    user_id = call.from_user.id
    pid = call.data.replace("proxy_ping_", "")
    await bot.answer_callback_query(call.id, "📶 Testing latency...")

    proxies = get_user_proxies(user_id)
    px = next((p for p in proxies if p["id"] == pid), None)
    if px:
        latency = await test_proxy_ping(px)
        px["ping_ms"] = latency if latency is not None else -1
        save_db(DB)
        if latency:
            await bot.answer_callback_query(call.id, f"📶 Ping: {latency}ms (Online)")
        else:
            await bot.answer_callback_query(call.id, "❌ Ping failed: Offline / Unreachable", show_alert=True)
    await show_proxy_menu(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("proxy_del_"))
async def proxy_del_cb(call):
    user_id = call.from_user.id
    pid = call.data.replace("proxy_del_", "")
    proxies = get_user_proxies(user_id)
    proxies = [p for p in proxies if p["id"] != pid]
    DB[str(user_id)]["proxies"] = proxies

    # If deleted proxy was active, switch to direct
    if DB.get(str(user_id), {}).get("active_proxy_id") == pid:
        await switch_user_proxy(user_id, None)
    else:
        save_db(DB)

    await bot.answer_callback_query(call.id, "🗑️ Proxy deleted.")
    await show_proxy_menu(call.message.chat.id, user_id, call.message.message_id)

# Confirm Copy Job
@bot.callback_query_handler(func=lambda call: call.data == "confirm_copy")
async def confirm_copy_cb(call):
    user_id = call.from_user.id
    data = user_auth_data.get(user_id, {})
    src = data.get("source")
    dst = data.get("dest")

    if not src or not dst:
        return await bot.answer_callback_query(call.id, "Session expired. Please select again.", show_alert=True)

    user_states[user_id] = None
    user_active_query[user_id] = None
    active_tasks[user_id] = True

    await bot.answer_callback_query(call.id, "Starting Copy Job!")
    await bot.edit_message_text(
        "🚀 <b>Initializing Copy Engine...</b>\nScanning messages matching your selected range and filters...",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )
    asyncio.create_task(copy_messages(user_id, call.message.chat.id, src, dst))

# Stop Copying Handler
@bot.callback_query_handler(func=lambda call: call.data == "stop_copy")
async def stop_copy_cb(call):
    user_id = call.from_user.id
    active_tasks[user_id] = False
    await bot.answer_callback_query(call.id, "🛑 Stopping copy process...", show_alert=True)
    try:
        await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

# View Active Task Progress
@bot.callback_query_handler(func=lambda call: call.data == "view_task")
async def view_task_cb(call):
    user_id = call.from_user.id
    task = user_task_info.get(user_id)
    if not task or not active_tasks.get(user_id, False):
        return await bot.answer_callback_query(call.id, "No active copy task running.", show_alert=True)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Stop Copying 🛑", callback_data="stop_copy"))
    kb.add(InlineKeyboardButton("🏠 Back to Dashboard", callback_data="nav_home"))

    text = (
        "⚡ <b>Active Copy Job</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>From:</b> {task.get('src_title')}\n"
        f"🔸 <b>To:</b> {task.get('dst_title')}\n"
        f"📊 <b>Progress:</b> {task.get('count', 0)} / {task.get('total', 0)} messages\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

# ==================== LOGIN WIZARD ====================

@bot.callback_query_handler(func=lambda call: call.data == "start_login")
async def start_login_cb(call):
    user_id = call.from_user.id
    user_states[user_id] = "WAIT_API_ID"
    user_auth_data[user_id] = {}

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Cancel Login", callback_data="nav_home"))

    text = (
        "🔐 <b>Telegram Login [Step 1 of 4]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Please send your <b>API ID</b> (obtained from my.telegram.org):\n\n"
        "<i>(Send numbers only)</i>"
    )
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

# ==================== TEXT & FORWARD MESSAGE ROUTER ====================

@bot.message_handler(func=lambda m: True)
async def handle_text_and_forwards(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    chat_id = message.chat.id
    cancel_kb = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel", callback_data="nav_home"))

    # Forwarded message handler during selection
    forward_chat = getattr(message, "forward_from_chat", None)
    if forward_chat and state in ("PICK_SRC", "PICK_DST"):
        target = forward_chat.id
        title = forward_chat.title or "Forwarded Channel"
        if state == "PICK_SRC":
            user_auth_data[user_id]["source"] = target
            user_auth_data[user_id]["src_title"] = title
            user_states[user_id] = "PICK_DST"
            user_active_query[user_id] = None
            await bot.send_message(chat_id, f"✅ <b>Source Selected:</b> {title}\nNow select or paste DESTINATION chat:")
            return await flow_pick_dst_text(chat_id, user_id)
        else:
            user_auth_data[user_id]["dest"] = target
            user_auth_data[user_id]["dst_title"] = title
            user_active_query[user_id] = None
            await bot.send_message(chat_id, f"✅ <b>Destination Selected:</b> {title}")
            return await show_range_selection(chat_id, user_id)

    if not state:
        return await send_dashboard(chat_id, user_id)

    text = message.text.strip() if message.text else ""

    # ==================== RANGE CUSTOM INPUTS ====================
    if state == "WAIT_CUSTOM_ID":
        parsed = parse_id_range(text)
        if parsed:
            min_id, max_id = parsed
            set_user_range_config(user_id, "id", label=f"IDs {min_id} to {max_id}", extra={"min": min_id, "max": max_id})
            await bot.send_message(chat_id, f"✅ <b>Message ID Range set:</b> IDs {min_id} to {max_id}", parse_mode="HTML")
            return await show_copy_settings(chat_id, user_id)
        else:
            return await bot.send_message(
                chat_id,
                "⚠️ <b>Invalid format!</b>\nPlease send range like <code>100 -> 500</code> (or single ID like <code>250</code>):",
                reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Range Options", callback_data="flow_pick_range")),
                parse_mode="HTML"
            )

    elif state == "WAIT_CUSTOM_DATE":
        parsed = parse_date_range(text)
        if parsed:
            d1, d2 = parsed
            set_user_range_config(user_id, "date", label=f"{d1.strftime('%Y-%m-%d')} to {d2.strftime('%Y-%m-%d')}", extra={"from": d1, "to": d2})
            await bot.send_message(chat_id, f"✅ <b>Date Range set:</b> {d1.strftime('%Y-%m-%d')} to {d2.strftime('%Y-%m-%d')}", parse_mode="HTML")
            return await show_copy_settings(chat_id, user_id)
        else:
            return await bot.send_message(
                chat_id,
                "⚠️ <b>Invalid date format!</b>\nPlease send like <code>2024-01-01 -> 2024-06-30</code> (YYYY-MM-DD):",
                reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Range Options", callback_data="flow_pick_range")),
                parse_mode="HTML"
            )

    elif state == "WAIT_SIGNATURE":
        settings = get_user_copy_settings(user_id)
        settings["signature"] = text
        await bot.send_message(chat_id, f"✅ <b>Custom Signature saved!</b>\n\nPreview:\n<i>{text}</i>", parse_mode="HTML")
        return await show_copy_settings(chat_id, user_id)

    elif state == "WAIT_PROXY":
        parsed = parse_proxy_string(text)
        if not parsed:
            return await bot.send_message(
                chat_id,
                "⚠️ <b>Invalid proxy format!</b>\n\n"
                "Please send in one of these formats:\n"
                "• <code>socks5://1.2.3.4:1080</code>\n"
                "• <code>socks5://user:pass@1.2.3.4:1080</code>\n"
                "• <code>http://1.2.3.4:8080</code>\n"
                "• <code>1.2.3.4:1080:user:pass</code>\n"
                "• <code>1.2.3.4:1080</code>",
                reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back to Proxy Menu", callback_data="nav_proxy")),
                parse_mode="HTML"
            )

        test_msg = await bot.send_message(chat_id, f"⏳ <b>Testing latency to {parsed['hostname']}:{parsed['port']}...</b>", parse_mode="HTML")
        latency = await test_proxy_ping(parsed)

        proxies = get_user_proxies(user_id)
        pid = f"px_{int(time.time() * 1000)}"
        label = f"{parsed['scheme'].upper()} {parsed['hostname']}:{parsed['port']}"
        proxy_item = {
            "id": pid,
            "scheme": parsed["scheme"],
            "hostname": parsed["hostname"],
            "port": parsed["port"],
            "username": parsed.get("username"),
            "password": parsed.get("password"),
            "label": label,
            "ping_ms": latency if latency is not None else -1
        }
        proxies.append(proxy_item)
        DB[str(user_id)]["proxies"] = proxies
        save_db(DB)
        user_states[user_id] = None

        try:
            await bot.delete_message(chat_id, test_msg.message_id)
        except Exception:
            pass

        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("⚡ Activate This Proxy", callback_data=f"proxy_use_{pid}"),
            InlineKeyboardButton("🌐 Proxy Menu", callback_data="nav_proxy")
        )
        ping_report = f"🟢 Ping: <b>{latency}ms</b> (Online)" if latency else "⚠️ Ping: <b>Offline / Unreachable</b>"
        await bot.send_message(
            chat_id,
            f"✅ <b>Proxy Added Successfully!</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>Host:</b> {parsed['hostname']}:{parsed['port']}\n"
            f"🛡️ <b>Type:</b> {parsed['scheme'].upper()}\n"
            f"📶 <b>Status:</b> {ping_report}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return

    # ==================== CHANNEL SELECTION: SEARCH OR DIRECT TARGET ====================
    elif state in ("PICK_SRC", "PICK_DST"):
        target_type, target_val = parse_input(text)
        mode = "src" if state == "PICK_SRC" else "dst"

        if target_type == 'SEARCH_QUERY':
            now = time.time()
            if now - user_dialog_last_fetch.get(user_id, 0) > 20 or not user_dialog_cache.get(user_id):
                await refresh_user_dialogs(user_id)

            user_active_query[user_id] = target_val
            kb, page, total, match_count = build_dialog_keyboard(user_id, mode, page=0, query=target_val)
            step_name = "SOURCE" if mode == "src" else "DESTINATION"

            if match_count > 0:
                msg_text = (
                    f"🔍 <b>Found {match_count} chat(s) matching '<i>{target_val}</i>':</b>\n"
                    f"Tap your {step_name} chat from the results below:"
                )
            else:
                msg_text = (
                    f"❌ <b>No chat found matching '<code>{target_val}</code>' in your joined chats.</b>\n\n"
                    "💡 <b>Tips:</b>\n"
                    "• If you just joined this channel, tap <b>🔄 Refresh Chats</b> below.\n"
                    "• Or send the exact channel link: <code>https://t.me/...</code>\n"
                    "• Or send its username: <code>@channel_username</code>\n"
                    "• Or forward any message from that channel here!"
                )
            return await bot.send_message(chat_id, msg_text, reply_markup=kb, parse_mode="HTML")

        else:
            u_client = user_clients.get(user_id)
            resolved_id, title = await resolve_chat(u_client, target_type, target_val)

            if resolved_id:
                if mode == "src":
                    user_auth_data[user_id]["source"] = resolved_id
                    user_auth_data[user_id]["src_title"] = title
                    user_states[user_id] = "PICK_DST"
                    user_active_query[user_id] = None
                    await bot.send_message(chat_id, f"✅ <b>Source Selected:</b> {title} (<code>{resolved_id}</code>)\nNow choose or paste DESTINATION chat:")
                    return await flow_pick_dst_text(chat_id, user_id)
                else:
                    user_auth_data[user_id]["dest"] = resolved_id
                    user_auth_data[user_id]["dst_title"] = title
                    user_active_query[user_id] = None
                    await bot.send_message(chat_id, f"✅ <b>Destination Selected:</b> {title} (<code>{resolved_id}</code>)")
                    return await show_range_selection(chat_id, user_id)
            else:
                return await bot.send_message(
                    chat_id,
                    f"❌ <b>Could not access channel:</b> {title}\n\n"
                    "Please verify your Telegram account has access to this channel, or forward any message from it directly to this bot.",
                    parse_mode="HTML"
                )

    # ==================== LOGIN FLOW STEPS ====================
    elif state == "WAIT_API_ID":
        try:
            user_auth_data[user_id]["api_id"] = int(text)
            user_states[user_id] = "WAIT_API_HASH"
            await bot.send_message(chat_id, "🔐 <b>Telegram Login [Step 2 of 4]</b>\nNow send your <b>API HASH</b>:", reply_markup=cancel_kb, parse_mode="HTML")
        except ValueError:
            await bot.send_message(chat_id, "⚠️ API ID must be numeric numbers. Please try again:", reply_markup=cancel_kb)

    elif state == "WAIT_API_HASH":
        user_auth_data[user_id]["api_hash"] = text
        user_states[user_id] = "WAIT_PHONE"
        await bot.send_message(chat_id, "📱 <b>Telegram Login [Step 3 of 4]</b>\nSend your phone number with country code (e.g., <code>+919876543210</code>):", reply_markup=cancel_kb, parse_mode="HTML")

    elif state == "WAIT_PHONE":
        phone = text
        user_auth_data[user_id]["phone"] = phone
        api_id = user_auth_data[user_id]["api_id"]
        api_hash = user_auth_data[user_id]["api_hash"]

        u_client = Client(f"user_{user_id}", api_id=api_id, api_hash=api_hash, in_memory=True, sleep_threshold=60)
        await u_client.connect()

        try:
            sent_code = await u_client.send_code(phone)
            user_auth_data[user_id]["phone_code_hash"] = sent_code.phone_code_hash
            user_clients[user_id] = u_client
            user_states[user_id] = "WAIT_OTP"
            await bot.send_message(
                chat_id,
                "💬 <b>Telegram Login [Step 4 of 4]</b>\n"
                "OTP code has been sent to your Telegram app!\n\n"
                "⚠️ <b>IMPORTANT:</b> Send OTP with spaces between numbers (e.g. <code>1 2 3 4 5</code>) so Telegram doesn't cancel it.",
                reply_markup=cancel_kb,
                parse_mode="HTML"
            )
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Error sending OTP: {e}\nPlease send your phone number again:", reply_markup=cancel_kb)

    elif state == "WAIT_OTP":
        otp = text.replace(" ", "")
        u_client = user_clients[user_id]
        phone = user_auth_data[user_id]["phone"]
        phone_code_hash = user_auth_data[user_id]["phone_code_hash"]

        try:
            await u_client.sign_in(phone, phone_code_hash, otp)
            await save_user_session(user_id, u_client)
            user_states[user_id] = None
            try:
                u_client.me = await u_client.get_me()
            except Exception:
                pass
            await refresh_user_dialogs(user_id)
            await bot.send_message(chat_id, "🎉 <b>Login Successful!</b> Welcome to Channel Copier.")
            await send_dashboard(chat_id, user_id)
        except SessionPasswordNeeded:
            user_states[user_id] = "WAIT_PASSWORD"
            await bot.send_message(chat_id, "🔐 <b>Two-Step Verification:</b>\nPlease enter your 2FA Cloud Password:", reply_markup=cancel_kb, parse_mode="HTML")
        except Exception as e:
            user_states[user_id] = "WAIT_PHONE"
            await bot.send_message(chat_id, f"❌ Error: {e}\n\nOTP expired or invalid. Send your phone number again to request a fresh OTP:", reply_markup=cancel_kb)

    elif state == "WAIT_PASSWORD":
        u_client = user_clients[user_id]
        try:
            await u_client.check_password(text)
            await save_user_session(user_id, u_client)
            user_states[user_id] = None
            try:
                u_client.me = await u_client.get_me()
            except Exception:
                pass
            await refresh_user_dialogs(user_id)
            await bot.send_message(chat_id, "🎉 <b>Login Successful!</b> Welcome to Channel Copier.")
            await send_dashboard(chat_id, user_id)
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Wrong password: {e}\nPlease try again:", reply_markup=cancel_kb)

async def flow_pick_dst_text(chat_id, user_id):
    kb, page, total, count = build_dialog_keyboard(user_id, "dst", page=0)
    text = (
        "📍 <b>Step 2/5: Select DESTINATION Chat</b>\n"
        "Tap a chat below, or type search name, paste link/ID, or forward a message:"
    )
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

async def save_user_session(user_id, u_client):
    session = await u_client.export_session_string()
    uid = str(user_id)
    if uid not in DB:
        DB[uid] = {}
    DB[uid]["session"] = session
    DB[uid]["api_id"] = user_auth_data[user_id]["api_id"]
    DB[uid]["api_hash"] = user_auth_data[user_id]["api_hash"]
    save_db(DB)

# ==================== SINGLE MESSAGE & BYPASS ENGINE ====================

async def copy_single_message(u_client, dst, src, msg_id, signature=None, src_title="", is_protected=False):
    # 1. If NOT protected AND NO signature, attempt fast server-side copy
    if not is_protected and not signature:
        try:
            await u_client.copy_message(dst, src, msg_id)
            return True
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
            try:
                await u_client.copy_message(dst, src, msg_id)
                return True
            except Exception:
                pass
        except Exception:
            pass

    # 2. Content Protection Bypass & Custom Signature application
    try:
        msg = await u_client.get_messages(src, msg_id)
        if not msg or msg.empty or msg.service:
            return False

        # Text message
        if msg.text:
            text_to_send = apply_signature(msg.text, signature, src_title)
            try:
                await u_client.send_message(
                    chat_id=dst,
                    text=text_to_send,
                    entities=msg.entities if not signature else None,
                    disable_web_page_preview=not msg.web_page
                )
                return True
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
                await u_client.send_message(
                    chat_id=dst,
                    text=text_to_send,
                    disable_web_page_preview=not msg.web_page
                )
                return True

        # Media message (Photos, Videos, Documents, Audio, Voice, Stickers)
        if msg.media:
            caption = apply_signature(msg.caption, signature, src_title)

            # If not protected and no download needed, try copy_message with custom caption
            if signature and not is_protected:
                try:
                    await u_client.copy_message(dst, src, msg_id, caption=caption)
                    return True
                except Exception:
                    pass

            # Download & Re-upload (Bypass protected content)
            try:
                temp_file = await u_client.download_media(msg)
            except Exception as de:
                print(f"❌ [DOWNLOAD FAIL] msg {msg_id}: {type(de).__name__}: {de}")
                return False

            if not temp_file or not os.path.exists(temp_file):
                print(f"⚠️ [DOWNLOAD FAIL] File not found on disk for msg {msg_id}")
                return False

            # Check 0KB download failure
            try:
                fsize = os.path.getsize(temp_file)
            except Exception:
                fsize = 0

            if fsize == 0:
                print(f"⚠️ [DOWNLOAD 0KB] Empty 0KB file for msg {msg_id}, skipping")
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
                return False

            fsize_mb = fsize / (1024 * 1024)
            print(f"📥 [DOWNLOAD OK] Msg {msg_id} ({fsize_mb:.2f} MB), uploading...")

            try:
                if msg.photo:
                    await u_client.send_photo(dst, temp_file, caption=caption)
                elif msg.video:
                    await u_client.send_video(dst, temp_file, caption=caption)
                elif msg.document:
                    await u_client.send_document(dst, temp_file, caption=caption)
                elif msg.audio:
                    await u_client.send_audio(dst, temp_file, caption=caption)
                elif msg.voice:
                    await u_client.send_voice(dst, temp_file, caption=caption)
                elif msg.animation:
                    await u_client.send_animation(dst, temp_file, caption=caption)
                elif msg.sticker:
                    await u_client.send_sticker(dst, temp_file)
                else:
                    await u_client.send_document(dst, temp_file, caption=caption)
                print(f"📤 [UPLOAD OK] Msg {msg_id} completed successfully")
                return True
            except FloodWait as fw:
                print(f"⏳ [UPLOAD FLOODWAIT] Msg {msg_id} resting {fw.value + 1}s...")
                await asyncio.sleep(fw.value + 1)
                try:
                    if msg.document or msg.video:
                        await u_client.send_document(dst, temp_file, caption=caption)
                    elif msg.photo:
                        await u_client.send_photo(dst, temp_file, caption=caption)
                    print(f"📤 [UPLOAD RETRY OK] Msg {msg_id} completed")
                    return True
                except Exception as ex2:
                    print(f"❌ [UPLOAD RETRY FAIL] msg {msg_id}: {ex2}")
                    return False
            except Exception as ue:
                print(f"❌ [UPLOAD FAIL] msg {msg_id}: {type(ue).__name__}: {ue}")
                return False
            finally:
                try:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception:
                    pass
    except Exception as e:
        print(f"Bypass error for message {msg_id}: {type(e).__name__}: {e}")
        return False

    return False

# ==================== 100x BATCH & PARALLEL COPY ENGINE ====================

async def copy_messages(user_id, chat_id, src, dst):
    u_client = user_clients[user_id]
    count = 0
    messages = []
    msg_hash_map = {}
    seen_hashes = set()
    skipped_dup = 0
    start_time = time.time()

    src_title = user_auth_data.get(user_id, {}).get("src_title", "Source")
    dst_title = user_auth_data.get(user_id, {}).get("dst_title", "Destination")
    range_cfg = get_user_range_config(user_id)
    settings = get_user_copy_settings(user_id)

    rtype = range_cfg.get("type", "all")
    ctype = settings.get("content_type", "all")
    skip_fwd = settings.get("skip_forwarded", False)
    skip_dup = settings.get("skip_duplicates", False)
    signature = settings.get("signature")

    stop_kb = InlineKeyboardMarkup()
    stop_kb.add(InlineKeyboardButton("Stop Copying 🛑", callback_data="stop_copy"))

    prog_msg = await bot.send_message(
        chat_id,
        f"⏳ <b>Scanning message history...</b>\nFrom: <b>{src_title}</b>\nRange: <code>{range_cfg.get('label')}</code>\nFilter: <b>{CONTENT_TYPE_LABELS.get(ctype)}</b>",
        reply_markup=stop_kb,
        parse_mode="HTML"
    )

    # Check upfront if Source channel has Content Protection enabled
    is_protected = False
    try:
        src_chat = await u_client.get_chat(src)
        is_protected = bool(getattr(src_chat, "has_protected_content", False))
        if is_protected:
            print(f"[INFO] Source channel '{src_title}' ({src}) has Content Protection ON.")
            await bot.send_message(
                chat_id,
                "⚠️ <b>Source channel has Content Protection ON.</b>\n"
                "Telegram server-side batch forward block karta hai. Bot <b>High-Speed Parallel Bypass Engine (6x concurrent)</b> use karega.",
                parse_mode="HTML"
            )
    except Exception as e:
        print(f"[WARN] Error checking chat protection: {e}")

    try:
        fetch_count = 0
        last_fetch_edit = 0
        kwargs = {}
        if rtype == "last_n":
            kwargs["limit"] = range_cfg.get("value", 500)

        async for msg in u_client.get_chat_history(src, **kwargs):
            if not active_tasks.get(user_id, True):
                try:
                    await bot.edit_message_text("🛑 <b>Cancelled while fetching history.</b>", chat_id, prog_msg.message_id, parse_mode="HTML")
                except Exception:
                    pass
                return

            # Check ID range condition
            if rtype == "id":
                min_id = range_cfg.get("min", 1)
                max_id = range_cfg.get("max", 999999999)
                if msg.id > max_id:
                    continue
                if msg.id < min_id:
                    break  # Stop scanning since history goes backwards!

            # Check Date range condition
            if rtype == "date":
                d_from = range_cfg.get("from")
                d_to = range_cfg.get("to")
                msg_dt = msg.date.replace(tzinfo=None) if msg.date else None
                if msg_dt:
                    if msg_dt > d_to:
                        continue
                    if msg_dt < d_from:
                        break  # Stop scanning older than start date!

            # Filter out service and empty messages
            if msg.service or msg.empty:
                continue

            # Content type filter upfront
            if ctype == "video" and not msg.video:
                continue
            if ctype == "photo" and not msg.photo:
                continue
            if ctype == "document" and not msg.document:
                continue
            if ctype == "text" and not msg.text:
                continue

            # Skip forwarded posts filter
            if skip_fwd and (msg.forward_from_chat or msg.forward_from or msg.forward_sender_name):
                continue

            # Deduplication Engine Check (Feature 3)
            h = compute_quick_hash(msg)
            if skip_dup and h:
                if is_hash_copied(user_id, h) or (h in seen_hashes):
                    skipped_dup += 1
                    continue
                seen_hashes.add(h)

            if h:
                msg_hash_map[msg.id] = h

            messages.append(msg.id)
            fetch_count += 1
            now = time.time()
            if now - last_fetch_edit >= 3.0:
                last_fetch_edit = now
                try:
                    dup_info = f"\n⏭️ Duplicates skipped: <b>{skipped_dup}</b>" if skipped_dup > 0 else ""
                    await bot.edit_message_text(
                        f"⏳ <b>Scanning messages ({range_cfg.get('label')})...</b>\nFound <b>{fetch_count}</b> matching messages so far...{dup_info}\nScanning at MTProto speed...",
                        chat_id,
                        prog_msg.message_id,
                        reply_markup=stop_kb,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error reading source: {e}")
        return

    messages.reverse()
    total = len(messages)
    if total == 0:
        dup_note = f" (⏭️ {skipped_dup} duplicate messages skipped)" if skipped_dup > 0 else ""
        return await bot.send_message(chat_id, f"⚠️ Source chat has no copyable messages matching your filter (<b>{CONTENT_TYPE_LABELS.get(ctype)}</b>) and range!{dup_note}", parse_mode="HTML")

    # Resolve peers once
    try:
        from_peer = await u_client.resolve_peer(src)
    except Exception:
        try:
            await u_client.get_chat(src)
            from_peer = await u_client.resolve_peer(src)
        except Exception as e:
            return await bot.send_message(chat_id, f"❌ Error resolving source chat: {e}")

    try:
        to_peer = await u_client.resolve_peer(dst)
    except Exception:
        try:
            await u_client.get_chat(dst)
            to_peer = await u_client.resolve_peer(dst)
        except Exception as e:
            return await bot.send_message(chat_id, f"❌ Error resolving destination chat: {e}")

    # Track active task info for dashboard
    user_task_info[user_id] = {
        "src_title": src_title,
        "dst_title": dst_title,
        "count": 0,
        "total": total
    }

    mode_label = "Parallel Bypass (6x)" if (is_protected or signature) else "100× MTProto Batching"

    BATCH_SIZE = 25
    SAFE_DELAY = 0.7  # 25 msgs / 0.7s = ~35 msg/s (MTProto safe rate)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    job_state = {
        "count": 0,
        "batch_num": 1,
        "total_batches": total_batches,
        "current_batch_size": min(BATCH_SIZE, total),
        "status_note": "🚀 Starting copy...",
        "mode_label": mode_label
    }

    copy_done_event = asyncio.Event()
    last_rendered_text = ""
    last_edit_time = 0

    def format_time(secs):
        m, s = divmod(int(secs), 60)
        return f"{m:02d}:{s:02d}"

    def build_progress_card():
        cur = job_state["count"]
        tot = total
        percent = (cur / tot) * 100 if tot > 0 else 0
        filled = min(10, int(percent / 10))
        bar = "▰" * filled + "▱" * (10 - filled)

        now = time.time()
        elapsed = max(0.1, now - start_time)
        speed = round(cur / elapsed, 1)
        remaining = max(0, tot - cur)

        if speed > 0 and remaining > 0:
            eta_secs = int(remaining / speed)
            eta_m, eta_s = divmod(eta_secs, 60)
            eta_str = f"{eta_m}m {eta_s}s" if eta_m > 0 else f"{eta_s}s"
        elif remaining == 0:
            eta_str = "Done"
        else:
            eta_str = "--"

        b_num = job_state["batch_num"]
        b_tot = job_state["total_batches"]
        b_size = job_state["current_batch_size"]

        dup_line = f"\n⏭️ <b>Duplicates Skipped:</b> {skipped_dup}" if skipped_dup > 0 else ""
        note = job_state.get("status_note", "")
        note_line = f"\n<i>{note}</i>" if note else ""

        text = (
            f"⚡ <b>{job_state['mode_label']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>From:</b> {src_title}\n"
            f"🔸 <b>To:</b> {dst_title}\n"
            f"📄 <b>Filter:</b> {CONTENT_TYPE_LABELS.get(ctype)}\n\n"
            f"{bar} <b>{percent:.2f}%</b>\n"
            f"Copied: <b>{cur}/{tot}</b> messages{dup_line}\n"
            f"🚀 <b>Batch:</b> {b_num}/{b_tot} ({b_size} msgs)\n\n"
            f"⏱️ <b>Elapsed:</b> {format_time(elapsed)}   ⚡ <b>Speed:</b> {speed} msg/s\n"
            f"⏳ <b>ETA:</b> ~{eta_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            f"{note_line}"
        )
        return text

    async def do_ui_render(force=False):
        nonlocal last_rendered_text, last_edit_time
        now = time.time()
        if not force and (now - last_edit_time < 1.8):
            return
        text = build_progress_card()
        if text != last_rendered_text or force:
            last_rendered_text = text
            last_edit_time = now
            try:
                await bot.edit_message_text(
                    text,
                    chat_id,
                    prog_msg.message_id,
                    reply_markup=stop_kb,
                    parse_mode="HTML"
                )
            except Exception:
                pass

    # Background Heartbeat Worker — refreshes UI every 2 seconds independently
    async def heartbeat_worker():
        while not copy_done_event.is_set():
            if not active_tasks.get(user_id, True):
                break
            user_task_info[user_id]["count"] = job_state["count"]
            await do_ui_render()
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break

    heartbeat_task = asyncio.create_task(heartbeat_worker())
    stopped_by_user = False

    try:
        await do_ui_render(force=True)

        for i in range(0, total, BATCH_SIZE):
            if not active_tasks.get(user_id, True):
                stopped_by_user = True
                break

            batch = messages[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            job_state["batch_num"] = batch_num
            job_state["current_batch_size"] = len(batch)
            job_state["status_note"] = f"🚀 Batch {batch_num}/{total_batches} in progress..."

            if count > 0 and count % 2000 == 0:
                job_state["status_note"] = "🛡️ Anti-ban safety: 5s breather..."
                await do_ui_render()
                await asyncio.sleep(5)
                if not active_tasks.get(user_id, True):
                    stopped_by_user = True
                    break

            batch_success = False

            # If NOT protected AND NO custom signature, use ultra-fast ForwardMessages batching
            if not is_protected and not signature:
                try:
                    await u_client.invoke(
                        functions.messages.ForwardMessages(
                            to_peer=to_peer,
                            from_peer=from_peer,
                            id=batch,
                            drop_author=True,
                            random_id=[u_client.rnd_id() for _ in batch]
                        )
                    )
                    count += len(batch)
                    job_state["count"] = count
                    batch_success = True

                    batch_hashes = [msg_hash_map[m] for m in batch if m in msg_hash_map]
                    if batch_hashes:
                        record_copied_hashes_batch(user_id, src, batch_hashes)

                    print(f"[BATCH OK] Forwarded {len(batch)} messages ({count}/{total})")
                    job_state["status_note"] = f"✅ Batch {batch_num}/{total_batches} completed"
                    await do_ui_render()
                    await asyncio.sleep(SAFE_DELAY)
                except FloodWait as e:
                    wait_time = e.value + 1
                    print(f"[FLOODWAIT] FloodWait {wait_time}s on batch forward")
                    job_state["status_note"] = f"⏳ FloodWait: resting {wait_time}s safely..."
                    await do_ui_render(force=True)
                    await asyncio.sleep(wait_time)
                    try:
                        await u_client.invoke(
                            functions.messages.ForwardMessages(
                                to_peer=to_peer,
                                from_peer=from_peer,
                                id=batch,
                                drop_author=True,
                                random_id=[u_client.rnd_id() for _ in batch]
                            )
                        )
                        count += len(batch)
                        job_state["count"] = count
                        batch_success = True
                        batch_hashes = [msg_hash_map[m] for m in batch if m in msg_hash_map]
                        if batch_hashes:
                            record_copied_hashes_batch(user_id, src, batch_hashes)
                        job_state["status_note"] = f"✅ Batch {batch_num}/{total_batches} completed after rest"
                        await do_ui_render()
                        await asyncio.sleep(SAFE_DELAY)
                    except Exception as ex:
                        print(f"[BATCH RETRY FAILED] {type(ex).__name__}: {ex}")
                        batch_success = False
                except Exception as e:
                    print(f"[BATCH FAILED] {type(e).__name__}: {e}. Switching to parallel fallback.")
                    batch_success = False

            # If protected, or custom signature, or batch failed: run high-speed parallel workers
            if not batch_success:
                is_protected = True  # Mark protected for subsequent batches to avoid wasted ForwardMessages
                job_state["mode_label"] = "Parallel Bypass (4x)"
                job_state["status_note"] = "Downloading & re-uploading media..."
                sem = asyncio.Semaphore(4)  # 4x concurrency avoids FloodWait & disk choking

                async def _copy_one(mid):
                    nonlocal count
                    async with sem:
                        if not active_tasks.get(user_id, True):
                            return 0
                        try:
                            ok = await copy_single_message(
                                u_client, dst, src, mid,
                                signature=signature, src_title=src_title,
                                is_protected=is_protected
                            )
                            if ok:
                                count += 1
                                job_state["count"] = count
                                mid_h = msg_hash_map.get(mid)
                                if mid_h:
                                    record_copied_hashes_batch(user_id, src, [mid_h])
                                job_state["status_note"] = f"✅ Copied msg {mid} ({count}/{total})"
                                # Real-time UI render on each completed file!
                                await do_ui_render()
                                return 1
                            else:
                                print(f"⚠️ [BYPASS SKIPPED/FAILED] msg {mid}")
                                return 0
                        except Exception as ex:
                            print(f"❌ [PARALLEL COPY FAIL] msg {mid}: {type(ex).__name__}: {ex}")
                            return 0

                await asyncio.gather(*[_copy_one(m) for m in batch], return_exceptions=False)
                await do_ui_render()

    finally:
        copy_done_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    active_tasks[user_id] = False
    duration_sec = time.time() - start_time
    failed_count = max(0, total - count)
    speed = round(count / max(duration_sec, 0.1), 1)

    # Save job analytics to stats.db (Feature 4)
    record_job_stats(user_id, src_title, dst_title, total, count, skipped_dup, failed_count, duration_sec)

    if stopped_by_user:
        try:
            await bot.edit_message_text(
                f"🛑 <b>Copying manually stopped!</b>\nSuccessfully copied {count}/{total} messages.",
                chat_id,
                prog_msg.message_id,
                reply_markup=None,
                parse_mode="HTML"
            )
        except Exception:
            pass
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🏠 Back to Dashboard", callback_data="nav_home"))
        await bot.send_message(chat_id, f"🛑 <b>Copying manually stopped!</b>\nSuccessfully copied {count}/{total} messages.", reply_markup=kb, parse_mode="HTML")
        return

    done_kb = InlineKeyboardMarkup()
    done_kb.row(
        InlineKeyboardButton("📊 View Analytics", callback_data="nav_analytics"),
        InlineKeyboardButton("🏠 Dashboard", callback_data="nav_home")
    )
    try:
        await bot.edit_message_text(
            f"✅ <b>Copy Completed!</b>\n▰▰▰▰▰▰▰▰▰▰ 100.00%\nSuccessfully copied <b>{count}/{total}</b> messages.",
            chat_id,
            prog_msg.message_id,
            reply_markup=None,
            parse_mode="HTML"
        )
    except Exception:
        pass

    summary_text = (
        "🎉 <b>Job Finished Successfully!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>From:</b> {src_title}\n"
        f"🔸 <b>To:</b> {dst_title}\n"
        f"📄 <b>Filter:</b> {CONTENT_TYPE_LABELS.get(ctype)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Total Scanned:</b> {total}\n"
        f"✅ <b>Successfully Copied:</b> {count}\n"
        f"⏭️ <b>Duplicates Skipped:</b> {skipped_dup}\n"
        f"❌ <b>Failed / Incomplete:</b> {failed_count}\n"
        f"⏱️ <b>Duration:</b> {duration_sec:.1f}s\n"
        f"⚡ <b>Average Speed:</b> {speed} msg/s\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await bot.send_message(chat_id, summary_text, reply_markup=done_kb, parse_mode="HTML")

# ==================== MAIN STARTUP ====================

async def main():
    await init_saved_clients()
    print("Bot is polling via HTTP...")
    await bot.polling(non_stop=True)

if __name__ == "__main__":
    asyncio.run(main())

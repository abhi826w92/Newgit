#!/usr/bin/env python3
import os
import io
import re
import sys
import time
import json
import shutil
import sqlite3
import zipfile
import asyncio
import argparse
from concurrent.futures import ThreadPoolExecutor

try:
    from telethon import TelegramClient
    from telethon.tl.types import DocumentAttributeFilename
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False

import urllib.request
import urllib.parse
import urllib.error

# ----------------- TURBO CONFIGURATION -----------------
API_ID = int(os.getenv("API_ID", "29116029"))
API_HASH = os.getenv("API_HASH", "867fafeeabc20a75163ef2ddbd877f70")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8486999738:AAEXkcxrILtF2AH2YfPesT1vwUAhPKiRVYs")
CHAT_ID = int(os.getenv("CHAT_ID", "-1003887776900"))

MAX_THREADS = int(os.getenv("THREADS", "128"))        # 128 Parallel Download Threads
CRAWL_THREADS = int(os.getenv("CRAWL_THREADS", "32"))  # 32 Parallel Page Crawl Threads
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500")) # 500 font zip files per Master Archive (~80MB)

ARCHIVE_DIR = "dafont_archive"
BUNDLES_DIR = "telegram_bundles"
DB_PATH = "fonts_index.db"
PROGRESS_FILE = "progress.json"
SESSION_NAME = "dafont_mtproto_bot"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# ----------------- CHANNEL CLOUD STATE SYNCHRONIZATION -----------------

def fetch_channel_cloud_state():
    """Fetch the latest sync state directly from the Telegram Channel's Pinned Message."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={CHAT_ID}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pinned = data.get("result", {}).get("pinned_message", {})
        text = pinned.get("text", "")
        m = re.search(r'DAFONT_STATE:({.*?})', text)
        if m:
            state = json.loads(m.group(1))
            state["pinned_msg_id"] = pinned.get("message_id")
            return state
    except Exception as e:
        print(f"[!] Note: Cloud state lookup: {e}")
    return None

def update_channel_cloud_state(state):
    """Update or pin the current live state on the Telegram Channel."""
    completed = state.get("completed_letters", [])
    last_part = state.get("last_part_idx", 0)
    curr = state.get("current_letter", "a")
    
    state_payload = json.dumps({
        "current_letter": curr,
        "last_part_idx": last_part,
        "completed_letters": completed
    })
    
    comp_str = ", ".join([c.upper() for c in completed]) if completed else "None"
    text = (
        f"📊 **DaFont Cloud Sync Progress**\n\n"
        f"🔤 Active Section: **{curr.upper()}**\n"
        f"📦 Last Uploaded Part: **#{last_part}**\n"
        f"✅ Completed Sections: **{comp_str}** ({len(completed)}/27)\n\n"
        f"`DAFONT_STATE:{state_payload}`"
    )
    
    pinned_id = state.get("pinned_msg_id")
    if pinned_id:
        edit_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = json.dumps({
            "chat_id": CHAT_ID,
            "message_id": pinned_id,
            "text": text,
            "parse_mode": "Markdown"
        }).encode("utf-8")
        req = urllib.request.Request(edit_url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10):
                return
        except Exception:
            pass

    # Send new state message and pin it
    send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }).encode("utf-8")
    req = urllib.request.Request(send_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            new_msg_id = res.get("result", {}).get("message_id")
            if new_msg_id:
                state["pinned_msg_id"] = new_msg_id
                pin_url = f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
                pin_payload = json.dumps({
                    "chat_id": CHAT_ID,
                    "message_id": new_msg_id,
                    "disable_notification": True
                }).encode("utf-8")
                pin_req = urllib.request.Request(pin_url, data=pin_payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(pin_req, timeout=10):
                    pass
    except Exception:
        pass

# ----------------- LOCAL STATE MANAGEMENT -----------------

def load_progress():
    state = {"completed_letters": [], "last_part_idx": 0, "current_letter": "a"}
    # 1. Try to fetch cloud state from Telegram Channel first!
    cloud = fetch_channel_cloud_state()
    if cloud:
        state.update(cloud)
        print(f"[📡 CHANNEL CLOUD SYNC] Auto-Detected state from Telegram Channel:")
        print(f"    - Completed Sections: {', '.join([c.upper() for c in state.get('completed_letters', [])])}")
        print(f"    - Last Uploaded Part: #{state.get('last_part_idx', 0)}")
        print(f"    - Next Active Section: {state.get('current_letter', 'a').upper()}")
        return state

    # 2. Fallback to local progress.json
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                state.update(saved)
        except Exception:
            pass
    return state

def save_progress(state):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass
    # Sync to Telegram Channel cloud state
    update_channel_cloud_state(state)

# ----------------- DATABASE -----------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA synchronous = OFF;")
    cur.execute("PRAGMA journal_mode = WAL;")
    cur.execute('''
        CREATE TABLE IF NOT EXISTS fonts (
            slug TEXT PRIMARY KEY,
            source TEXT,
            status TEXT DEFAULT 'pending',
            part_id INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# ----------------- TELEGRAM ASYNC MESSAGING & UPLOADING -----------------

async def tg_send_message(client, text):
    print(f"\n[Telegram] {text}")
    if client and client.is_connected():
        try:
            await client.send_message(CHAT_ID, text)
            return True
        except Exception as e:
            print(f"[!] MTProto send message error: {e}")
    return False

async def tg_send_document(client, file_path, caption=""):
    file_name = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(f"[*] [MTProto Fast Upload] Sending '{file_name}' ({file_size_mb:.2f} MB) to Channel {CHAT_ID}...")

    last_pct = 0
    def upload_callback(current, total):
        nonlocal last_pct
        pct = int(current * 100 / total)
        if pct >= last_pct + 20 or pct == 100:
            last_pct = pct
            print(f"  [MTProto Upload] {pct}% ({current / (1024*1024):.1f} / {total / (1024*1024):.1f} MB)")

    for attempt in range(1, 4):
        try:
            await client.send_file(
                CHAT_ID,
                file_path,
                caption=caption,
                progress_callback=upload_callback,
                attributes=[DocumentAttributeFilename(file_name)],
                force_document=True
            )
            print(f"[✓] MTProto Successfully Uploaded '{file_name}' to Telegram!")
            return True
        except Exception as e:
            print(f"[!] Upload attempt {attempt} failed: {e}")
            await asyncio.sleep(3)
    return False

# ----------------- TURBO PARALLEL CRAWLER -----------------

def crawl_alphabet_page(letter, page):
    url = f"https://www.dafont.com/alpha.php?lettre={letter}&page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('iso-8859-1', errors='replace')
        links = re.findall(r'href="//dl\.dafont\.com/dl/\?f=([a-z0-9_\-]+)"', html)
        return page, links
    except Exception:
        return page, []

async def crawl_single_letter_turbo(loop, executor, letter):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT slug FROM fonts WHERE source = ?", (f"alpha_{letter}",))
    seen = set([r[0] for r in cur.fetchall()])
    
    if len(seen) > 0:
        print(f"[✓] Section '{letter.upper()}' already has {len(seen)} indexed fonts in database.")
        conn.close()
        return len(seen)

    print(f"\n[*] Turbo Crawling Section '{letter.upper()}' with {CRAWL_THREADS} parallel threads...")
    current_page = 1
    chunk_size = 40
    new_total = 0

    while True:
        pages_to_fetch = list(range(current_page, current_page + chunk_size))
        tasks = [loop.run_in_executor(executor, crawl_alphabet_page, letter, p) for p in pages_to_fetch]
        page_results = await asyncio.gather(*tasks)

        page_results.sort(key=lambda x: x[0])
        chunk_had_new = False

        for page, links in page_results:
            new_links = [s for s in links if s not in seen and len(links) >= 15]
            if new_links:
                chunk_had_new = True
                for s in new_links:
                    seen.add(s)
                cur.executemany("INSERT OR IGNORE INTO fonts (slug, source) VALUES (?, ?)", [(s, f"alpha_{letter}") for s in new_links])
                conn.commit()
                new_total += len(new_links)

        print(f"  [Section {letter.upper()}] Scanned pages {current_page}–{current_page + chunk_size - 1} | Total indexed: {len(seen)}")

        if not chunk_had_new:
            break
        current_page += chunk_size

    conn.close()
    print(f"[✓] Section '{letter.upper()}' Crawl Complete! Indexed {len(seen)} unique fonts.")
    return len(seen)

# ----------------- PARALLEL RAW ZIP DOWNLOADER -----------------

def download_zip_worker(slug, out_dir):
    url = f"https://dl.dafont.com/dl/?f={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest_zip_file = os.path.join(out_dir, f"{slug}.zip")
    
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
                if not data.startswith(b"PK"):
                    return slug, False
                with open(dest_zip_file, "wb") as f:
                    f.write(data)
                return slug, True
        except Exception:
            time.sleep(0.1)
    return slug, False

async def upload_and_clean_batch(client, batch_slugs, part_idx, letter_name, state):
    os.makedirs(BUNDLES_DIR, exist_ok=True)
    bundle_name = f"dafont_archive_part_{part_idx:04d}.zip"
    bundle_path = os.path.join(BUNDLES_DIR, bundle_name)

    existing_zips = []
    for slug in batch_slugs:
        zpath = os.path.join(ARCHIVE_DIR, f"{slug}.zip")
        if os.path.exists(zpath) and os.path.getsize(zpath) > 22:
            existing_zips.append((zpath, f"{slug}.zip"))

    if not existing_zips:
        return False

    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_STORED) as master_zip:
        for zpath, zname in existing_zips:
            master_zip.write(zpath, zname)

    size_mb = os.path.getsize(bundle_path) / (1024 * 1024)
    caption = f"⚡ **DaFont Master Archive** [Part #{part_idx}]\n🔤 Section: **{letter_name.upper()}** | 📦 Contains **{len(existing_zips)} Font .ZIP Files** ({size_mb:.2f} MB)"
    
    uploaded = await tg_send_document(client, bundle_path, caption)
    
    if uploaded:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.executemany("UPDATE fonts SET status = 'uploaded', part_id = ? WHERE slug = ?", [(part_idx, s) for s in batch_slugs])
        conn.commit()
        conn.close()

        # Update JSON state file & Channel Cloud State
        state["last_part_idx"] = part_idx
        save_progress(state)

        # Delete local batch immediately to free up VPS disk space!
        for slug in batch_slugs:
            zpath = os.path.join(ARCHIVE_DIR, f"{slug}.zip")
            if os.path.exists(zpath):
                try:
                    os.remove(zpath)
                except Exception:
                    pass
        if os.path.exists(bundle_path):
            try:
                os.remove(bundle_path)
            except Exception:
                pass
            
        print(f"[✓] Part #{part_idx} ({len(existing_zips)} font ZIPs) uploaded to Telegram & wiped from VPS.")
        return True
    return False

async def process_section_fonts_turbo(client, loop, executor, letter, state, threads=MAX_THREADS, batch_size=DEFAULT_BATCH_SIZE):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    
    while True:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM fonts WHERE source = ? AND status = 'uploaded'", (f"alpha_{letter}",))
        done_in_section = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM fonts WHERE source = ?", (f"alpha_{letter}",))
        total_in_section = cur.fetchone()[0]
        
        cur.execute("SELECT slug FROM fonts WHERE source = ? AND status != 'uploaded' LIMIT ?", (f"alpha_{letter}", batch_size))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            if letter not in state["completed_letters"]:
                state["completed_letters"].append(letter)
                save_progress(state)
            print(f"[✓] Section '{letter.upper()}' 100% COMPLETE! All {total_in_section} font ZIPs uploaded.")
            break

        batch_slugs = [r[0] for r in rows]
        part_idx = int(state.get("last_part_idx", 0)) + 1

        print(f"\n[*] Section '{letter.upper()}': Resuming Batch [Fonts {done_in_section + 1}–{done_in_section + len(batch_slugs)} of {total_in_section}]")
        print(f"[*] Downloading {len(batch_slugs)} font .ZIP files with {threads} threads (Part #{part_idx})...")
        start_time = time.time()

        tasks = [loop.run_in_executor(executor, download_zip_worker, s, ARCHIVE_DIR) for s in batch_slugs]
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time
        speed = len(batch_slugs) / elapsed if elapsed > 0 else 0
        print(f"  [Section {letter.upper()} | Part #{part_idx}] {len(batch_slugs)} font ZIPs downloaded in {elapsed:.1f}s ({speed:.1f} fonts/sec)...")

        # Package individual font zip files into master bundle & upload to Telegram
        await upload_and_clean_batch(client, batch_slugs, part_idx, letter, state)
        await asyncio.sleep(1)

# ----------------- MASTER PIPELINE -----------------

async def async_main(args):
    init_db()
    state = load_progress()

    # Handle manual start flags if provided
    if args.start_letter:
        start_char = args.start_letter.lower()
        print(f"[Manual Override] Starting from Letter '{start_char.upper()}'")
        letters_all = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['ot_1']
        if start_char in letters_all:
            idx = letters_all.index(start_char)
            state["completed_letters"] = [l for l in state["completed_letters"] if letters_all.index(l) < idx] if letters_all else []
            state["current_letter"] = start_char
            save_progress(state)

    if args.start_part:
        state["last_part_idx"] = args.start_part - 1
        save_progress(state)

    print("[*] Connecting to Telegram Data Centers via MTProto Binary Protocol...")
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"[✓] MTProto Connected! Bot: @{me.username} ({me.id}) - 2GB Upload Limit Active.")

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=args.threads)

    letters = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['ot_1']
    if args.section:
        letters = [args.section.lower()]

    batch_sz = args.batch_size
    last_part = int(state.get("last_part_idx", 0))
    completed_letters = state.get("completed_letters", [])
    active_letter = state.get("current_letter", "a")

    print("=" * 65)
    print(" 🚀 DAFONT MTPROTO CLOUD-SYNC RESUMABLE EXPORTER")
    print(f"[*] Target Chat: {CHAT_ID}")
    print(f"[*] Download Threads: {args.threads} | Crawl Threads: {CRAWL_THREADS}")
    print(f"[*] Batch Size: {batch_sz} font .ZIP files per Master Archive")
    print(f"[*] Completed Sections: {', '.join([c.upper() for c in completed_letters]) if completed_letters else 'None'}")
    print(f"[*] Active Resumed Section: {active_letter.upper()} | Next Part: #{last_part + 1}")
    print("=" * 65)

    if last_part > 0 or completed_letters:
        await tg_send_message(client, f"🔄 **VPS Cloud-Sync Auto-Resume Active!**\nCompleted: **{len(completed_letters)}/27 Sections** ({', '.join([c.upper() for c in completed_letters])})\nResuming download stream on Section **{active_letter.upper()}** (Part **#{last_part + 1}**)...")
    else:
        await tg_send_message(client, "⚡ **DaFont MTProto Cloud-Sync Exporter Started!**\nDirect Data Center binary streams active. Packaging font .ZIP archives...")

    # Process each letter sequentially in native async pipeline
    for letter in letters:
        if letter in completed_letters:
            continue

        state["current_letter"] = letter
        save_progress(state)

        print(f"\n==========================================================")
        print(f"  ▶ PROCESSING SECTION '{letter.upper()}' (128-Thread Download ➔ MTProto Upload)")
        print(f"==========================================================")
        
        # 1. Turbo Crawl
        await crawl_single_letter_turbo(loop, executor, letter)

        # 2. Turbo Download & MTProto Direct Upload
        await process_section_fonts_turbo(client, loop, executor, letter, state, args.threads, batch_sz)

    print("\n" + "=" * 65)
    print(" 🎉 ALL SELECTED SECTIONS FULLY EXPORTED & SENT VIA MTPROTO!")
    print("=" * 65)
    await tg_send_message(client, "🎉 **ALL SELECTED DAFONT SECTIONS FULLY EXPORTED & SENT TO TELEGRAM!**\nAll VPS storage wiped 100% clean.")
    await client.disconnect()

def main():
    parser = argparse.ArgumentParser(description="DaFont MTProto Cloud-Sync Mass Exporter")
    parser.add_argument("-l", "--start-letter", type=str, default="", help="Start crawling from specific letter (e.g. H)")
    parser.add_argument("-s", "--section", type=str, default="", help="Download only one specific section (e.g. H)")
    parser.add_argument("-p", "--start-part", type=int, default=0, help="Starting part number (e.g. 10)")
    parser.add_argument("-b", "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Fonts per ZIP chunk (default: 500)")
    parser.add_argument("-t", "--threads", type=int, default=MAX_THREADS, help="Download threads (default: 128)")
    args = parser.parse_args()

    asyncio.run(async_main(args))

if __name__ == "__main__":
    main()

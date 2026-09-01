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

# ----------------- MTPROTO CONFIGURATION -----------------
API_ID = int(os.getenv("API_ID", "29116029"))
API_HASH = os.getenv("API_HASH", "867fafeeabc20a75163ef2ddbd877f70")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8486999738:AAEXkcxrILtF2AH2YfPesT1vwUAhPKiRVYs")
CHAT_ID = int(os.getenv("CHAT_ID", "-1003887776900"))

MAX_THREADS = int(os.getenv("THREADS", "128"))        # 128 Parallel Download Threads
CRAWL_THREADS = int(os.getenv("CRAWL_THREADS", "32"))  # 32 Parallel Page Crawl Threads
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "1500"))      # 1,500 font zip files per Master Bundle

ARCHIVE_DIR = "dafont_archive"
BUNDLES_DIR = "telegram_bundles"
DB_PATH = "fonts_index.db"
SESSION_NAME = "dafont_mtproto_bot"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

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
    cur.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_meta(key, default=""):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_meta(key, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def is_letter_completed(letter):
    val = get_meta(f"letter_{letter}_done", "false")
    return val == "true"

def mark_letter_completed(letter):
    set_meta(f"letter_{letter}_done", "true")

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
    print(f"[*] [MTProto 2GB Stream] Uploading '{file_name}' ({file_size_mb:.2f} MB) to Channel {CHAT_ID}...")

    last_pct = 0
    def upload_callback(current, total):
        nonlocal last_pct
        pct = int(current * 100 / total)
        if pct >= last_pct + 25 or pct == 100:
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
    if is_letter_completed(letter):
        print(f"[✓] Section '{letter}' is ALREADY completed. Skipping crawl.")
        return 0

    print(f"\n[*] Turbo Crawling Section '{letter}' with {CRAWL_THREADS} parallel threads...")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute("SELECT slug FROM fonts WHERE source = ?", (f"alpha_{letter}",))
    seen = set([r[0] for r in cur.fetchall()])
    
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

        print(f"  [Section {letter}] Scanned pages {current_page}–{current_page + chunk_size - 1} | Total fonts: {len(seen)}")

        if not chunk_had_new:
            break
        current_page += chunk_size

    conn.close()
    print(f"[✓] Section '{letter}' Turbo Crawl Complete! Indexed {len(seen)} unique fonts (+{new_total} new).")
    return new_total

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

async def upload_and_clean_batch(client, batch_slugs, part_idx):
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

    # Package individual font .zip archives inside the Master Bundle
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_STORED) as master_zip:
        for zpath, zname in existing_zips:
            master_zip.write(zpath, zname)

    size_mb = os.path.getsize(bundle_path) / (1024 * 1024)
    caption = f"⚡ **DaFont Fonts Master Archive** [Part #{part_idx}]\n📦 Contains **{len(existing_zips)} individual font .ZIP packages** ({size_mb:.2f} MB)"
    
    uploaded = await tg_send_document(client, bundle_path, caption)
    
    if uploaded:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.executemany("UPDATE fonts SET status = 'uploaded', part_id = ? WHERE slug = ?", [(part_idx, s) for s in batch_slugs])
        conn.commit()
        conn.close()

        # Delete local batch immediately to free up VPS disk space!
        for slug in batch_slugs:
            zpath = os.path.join(ARCHIVE_DIR, f"{slug}.zip")
            if os.path.exists(zpath):
                os.remove(zpath)
        if os.path.exists(bundle_path):
            os.remove(bundle_path)
            
        print(f"[✓] Part #{part_idx} ({len(existing_zips)} font zip files) uploaded to Telegram & wiped from VPS.")
        set_meta("last_part_idx", part_idx)
        return True
    return False

async def process_section_fonts_turbo(client, loop, executor, letter, threads=MAX_THREADS, batch_size=BATCH_SIZE):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    
    while True:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT slug FROM fonts WHERE source = ? AND status != 'uploaded' LIMIT ?", (f"alpha_{letter}", batch_size))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            mark_letter_completed(letter)
            print(f"[✓] All font ZIPs in Section '{letter}' successfully downloaded, uploaded to Telegram, and wiped!")
            break

        batch_slugs = [r[0] for r in rows]
        part_idx = int(get_meta("last_part_idx", "0")) + 1

        print(f"\n[*] Section '{letter}': Downloading {len(batch_slugs)} font .ZIP files with {threads} THREADS (Part #{part_idx})...")
        start_time = time.time()

        tasks = [loop.run_in_executor(executor, download_zip_worker, s, ARCHIVE_DIR) for s in batch_slugs]
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time
        speed = len(batch_slugs) / elapsed if elapsed > 0 else 0
        print(f"  [Section {letter} | Part #{part_idx}] {len(batch_slugs)} font ZIPs downloaded in {elapsed:.1f}s ({speed:.1f} fonts/sec)...")

        # Package individual font zip files into master bundle & upload to Telegram
        await upload_and_clean_batch(client, batch_slugs, part_idx)
        await asyncio.sleep(1)

# ----------------- MASTER PIPELINE -----------------

async def async_main():
    init_db()

    print("[*] Connecting to Telegram Data Centers via MTProto Binary Protocol...")
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"[✓] MTProto Connected! Bot: @{me.username} ({me.id}) - 2GB Upload Limit Active.")

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=MAX_THREADS)

    letters = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['ot_1']
    
    print("=" * 65)
    print(" 🚀 DAFONT MTPROTO 2GB RESUMABLE TURBO EXPORTER (128 THREADS)")
    print(f"[*] Target Chat: {CHAT_ID}")
    print(f"[*] Download Threads: {MAX_THREADS} | Crawl Threads: {CRAWL_THREADS}")
    print(f"[*] Batch Size: {BATCH_SIZE} font .ZIP files per Master Archive")
    print("=" * 65)

    completed_letters = [l for l in letters if is_letter_completed(l)]
    last_part = int(get_meta("last_part_idx", "0"))

    if completed_letters:
        print(f"\n[🔄 RESUME DETECTED] Completed Sections: {', '.join(completed_letters)}")
        print(f"[🔄 RESUME DETECTED] Last Uploaded Part: #{last_part}")
        await tg_send_message(client, f"🔄 **VPS MTProto Auto-Resume Active!**\nCompleted: **{len(completed_letters)}/27 Sections** (Part #{last_part})\nResuming download stream on next pending section...")
    else:
        await tg_send_message(client, "⚡ **DaFont MTProto 2GB Turbo Exporter Started!**\nDirect Data Center binary streams active. Packaging font .ZIP archives...")

    # Process each letter sequentially in native async pipeline
    for letter in letters:
        if is_letter_completed(letter):
            continue

        print(f"\n==========================================================")
        print(f"  ▶ STARTING SECTION '{letter.upper()}' (Turbo Crawl ➔ 128-Thread Download ➔ MTProto Upload)")
        print(f"==========================================================")
        
        # 1. Turbo Crawl
        await crawl_single_letter_turbo(loop, executor, letter)

        # 2. Turbo Download (128 parallel threads) & MTProto Direct Upload
        await process_section_fonts_turbo(client, loop, executor, letter, MAX_THREADS, BATCH_SIZE)

    print("\n" + "=" * 65)
    print(" 🎉 ALL 27 DAFONT SECTIONS FULLY EXPORTED & SENT VIA MTPROTO!")
    print("=" * 65)
    await tg_send_message(client, "🎉 **ALL 27 DAFONT SECTIONS FULLY EXPORTED & SENT TO TELEGRAM!**\nAll VPS storage wiped 100% clean.")
    await client.disconnect()

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()

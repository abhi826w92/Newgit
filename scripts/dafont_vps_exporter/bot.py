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
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------- CONFIGURATION -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8486999738:AAEXkcxrILtF2AH2YfPesT1vwUAhPKiRVYs")
CHAT_ID = os.getenv("CHAT_ID", "-1003887776900")
MAX_THREADS = int(os.getenv("THREADS", "64"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500"))  # Upload chunk every 500 fonts (~40MB)

ARCHIVE_DIR = "dafont_archive"
BUNDLES_DIR = "telegram_bundles"
DB_PATH = "fonts_index.db"
CHUNK_SIZE_MB = 45  # Telegram Bot API limit is 50MB; 45MB is safe

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# ----------------- TELEGRAM API -----------------

def tg_send_message(text):
    print(f"\n[Telegram] {text}")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='replace')
        print(f"[!] Telegram API Error: {err_body}")
        return None
    except Exception as e:
        print(f"[!] Telegram send message error: {e}")
        return None

def tg_send_document(file_path, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    file_name = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(f"[*] Uploading '{file_name}' ({file_size_mb:.2f} MB) to Telegram...")

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body = []
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{CHAT_ID}\r\n".encode("utf-8"))
    if caption:
        body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode("utf-8"))
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{file_name}\"\r\nContent-Type: application/zip\r\n\r\n".encode("utf-8"))
    body.append(file_bytes)
    body.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(url, data=b"".join(body))
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                if res.get("ok"):
                    print(f"[✓] Uploaded '{file_name}' successfully!")
                    return True
                else:
                    print(f"[!] Telegram API error: {res}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='replace')
            print(f"[!] Upload HTTP Error: {err_body}")
            time.sleep(2)
        except Exception as e:
            print(f"[!] Upload attempt {attempt + 1} failed: {e}")
            time.sleep(2)
    return False

# ----------------- DATABASE WITH STATE RESUMPTION -----------------

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
            font_files INTEGER DEFAULT 0,
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

# ----------------- SMART HIGH SPEED CRAWLER -----------------

def crawl_alphabet_page(letter, page):
    url = f"https://www.dafont.com/alpha.php?lettre={letter}&page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('iso-8859-1', errors='replace')
        links = re.findall(r'href="//dl\.dafont\.com/dl/\?f=([a-z0-9_\-]+)"', html)
        return links
    except Exception:
        return []

def crawl_all_alphabets_resumable():
    init_db()
    if get_meta("crawl_complete") == "true":
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM fonts")
        total = cur.fetchone()[0]
        conn.close()
        print(f"[✓] Index already saved in database ({total} fonts). Skipping crawl to resume downloads immediately.")
        return total

    letters = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['ot_1']
    print(f"\n[*] Crawling full DaFont Alphabet across {len(letters)} sections...")
    tg_send_message(f"🔍 *Starting / Resuming DaFont Alphabet Crawl across {len(letters)} sections...*")
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total_indexed = 0
    seen_global = set()

    cur.execute("SELECT slug FROM fonts")
    for row in cur.fetchall():
        seen_global.add(row[0])

    for letter in letters:
        page = 1
        consecutive_duplicates = 0
        letter_count = 0
        
        while consecutive_duplicates < 2:
            links = crawl_alphabet_page(letter, page)
            new_links = [s for s in links if s not in seen_global]
            
            if not new_links or len(links) < 15:
                consecutive_duplicates += 1
            else:
                consecutive_duplicates = 0
                for s in new_links:
                    seen_global.add(s)
                
                cur.executemany("INSERT OR IGNORE INTO fonts (slug, source) VALUES (?, ?)", [(s, f"alpha_{letter}") for s in new_links])
                conn.commit()
                letter_count += len(new_links)
                total_indexed += len(new_links)
                if page % 10 == 0:
                    print(f"  [Letter {letter}] Page {page} | +{len(new_links)} new fonts | Total indexed: {len(seen_global)}")
            
            page += 1
            time.sleep(0.04)
            
        print(f"[✓] Section '{letter}' finished: +{letter_count} fonts indexed.")

    set_meta("crawl_complete", "true")
    cur.execute("SELECT count(*) FROM fonts")
    final_count = cur.fetchone()[0]
    conn.close()

    print(f"\n[✓] Master Alphabet Crawl Complete! Total Fonts Indexed: {final_count}")
    tg_send_message(f"✅ *Indexing Complete!*\nTotal Unique Fonts: *{final_count}*\nStarting continuous download & live Telegram upload...")
    return final_count

# ----------------- PARALLEL STREAM DOWNLOADER -----------------

def download_worker(slug, out_dir):
    url = f"https://dl.dafont.com/dl/?f={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    font_folder = os.path.join(out_dir, slug)
    
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                if not data.startswith(b"PK"):
                    return slug, False, 0
                
                os.makedirs(font_folder, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    font_files = [f for f in z.namelist() if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2'))]
                    for f in font_files:
                        z.extract(f, font_folder)
                    return slug, True, len(font_files)
        except Exception:
            time.sleep(0.15)
    return slug, False, 0

# ----------------- CONTINUOUS PIPELINE: DOWNLOAD -> ZIP -> UPLOAD -> CLEANUP -----------------

def upload_and_clean_batch(batch_slugs, part_idx):
    os.makedirs(BUNDLES_DIR, exist_ok=True)
    bundle_name = f"dafont_archive_part_{part_idx:04d}.zip"
    bundle_path = os.path.join(BUNDLES_DIR, bundle_name)

    font_files = []
    for slug in batch_slugs:
        slug_dir = os.path.join(ARCHIVE_DIR, slug)
        if os.path.exists(slug_dir):
            for root, dirs, files in os.walk(slug_dir):
                for f in files:
                    if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
                        fpath = os.path.join(root, f)
                        relpath = os.path.relpath(fpath, ARCHIVE_DIR)
                        font_files.append((fpath, relpath))

    if not font_files:
        return False

    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as z:
        for fpath, relpath in font_files:
            z.write(fpath, relpath)

    size_mb = os.path.getsize(bundle_path) / (1024 * 1024)
    caption = f"📦 *DaFont Fonts Bundle* [Part #{part_idx}]\nContains *{len(font_files)} font binaries* ({size_mb:.2f} MB)"
    
    uploaded = tg_send_document(bundle_path, caption)
    
    if uploaded:
        # Mark as uploaded in DB
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.executemany("UPDATE fonts SET status = 'uploaded', part_id = ? WHERE slug = ?", [(part_idx, s) for s in batch_slugs])
        conn.commit()
        conn.close()

        # Delete local batch immediately to free up VPS disk space!
        for slug in batch_slugs:
            slug_dir = os.path.join(ARCHIVE_DIR, slug)
            if os.path.exists(slug_dir):
                shutil.rmtree(slug_dir, ignore_errors=True)
        if os.path.exists(bundle_path):
            os.remove(bundle_path)
            
        print(f"[✓] Part #{part_idx} ({len(font_files)} fonts) safely uploaded & local files wiped.")
        set_meta("last_part_idx", part_idx)
        return True
    return False

def run_continuous_pipeline(threads=MAX_THREADS, batch_size=BATCH_SIZE):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM fonts WHERE status = 'uploaded'")
    already_uploaded = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM fonts WHERE status != 'uploaded'")
    remaining = cur.fetchone()[0]
    conn.close()

    part_idx = int(get_meta("last_part_idx", "0")) + 1

    if already_uploaded > 0:
        print(f"\n[🔄 RESUME DETECTED] {already_uploaded} fonts were already uploaded to Telegram!")
        print(f"[🔄 RESUMING] Next Part: #{part_idx} | Remaining fonts: {remaining}")
        tg_send_message(f"🔄 *VPS Auto-Resumed!*\nAlready Uploaded: *{already_uploaded} fonts*\nResuming download at Part *#{part_idx}* ({remaining} remaining)...")

    while True:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT slug FROM fonts WHERE status != 'uploaded' LIMIT ?", (batch_size,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            print("\n🎉 ALL FONTS HAVE BEEN DOWNLOADED AND UPLOADED TO TELEGRAM!")
            tg_send_message("🎉 *ALL DAFONT ARCHIVES FULLY EXPORTED & UPLOADED TO TELEGRAM!*\nProcess complete. Freeing remaining VPS storage...")
            break

        batch_slugs = [r[0] for r in rows]
        print(f"\n[*] Processing Batch #{part_idx} ({len(batch_slugs)} fonts with {threads} threads)...")

        with ThreadPoolExecutor(max_workers=threads) as executor:
            future_to_slug = {executor.submit(download_worker, s, ARCHIVE_DIR): s for s in batch_slugs}
            done_count = 0
            for future in as_completed(future_to_slug):
                slug, ok, font_count = future.result()
                done_count += 1
                if done_count % 100 == 0 or done_count == len(batch_slugs):
                    print(f"  [Batch #{part_idx}] {done_count}/{len(batch_slugs)} fonts downloaded in RAM...")

        # Package and upload this batch immediately!
        upload_and_clean_batch(batch_slugs, part_idx)
        part_idx += 1
        time.sleep(1)

    # Final cleanup
    for d in [ARCHIVE_DIR, BUNDLES_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
    print("[✓] ALL LOCAL DATA WIPED. VPS 100% CLEAN.")

# ----------------- MAIN PIPELINE -----------------

def main():
    print("=" * 60)
    print(" 🚀 DAFONT RESUMABLE VPS EXPORTER & LIVE TELEGRAM UPLOADER")
    print(f"[*] Target Chat: {CHAT_ID}")
    print(f"[*] Worker Threads: {MAX_THREADS}")
    print(f"[*] Micro-Batch Size: {BATCH_SIZE} fonts per Telegram Zip")
    print("=" * 60)

    # Step 1: Resumable Alphabet Indexing
    crawl_all_alphabets_resumable()

    # Step 2: Continuous Stream: Download -> Zip -> Upload to Telegram -> Delete locally
    run_continuous_pipeline(MAX_THREADS, BATCH_SIZE)

if __name__ == "__main__":
    main()

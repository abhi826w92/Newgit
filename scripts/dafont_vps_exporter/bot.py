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

# ----------------- TURBO CONFIGURATION -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8486999738:AAEXkcxrILtF2AH2YfPesT1vwUAhPKiRVYs")
CHAT_ID = os.getenv("CHAT_ID", "-1003887776900")
MAX_THREADS = int(os.getenv("THREADS", "128"))       # 128 Ultra-fast Parallel Download Threads
CRAWL_THREADS = int(os.getenv("CRAWL_THREADS", "32")) # 32 Parallel Page Crawl Threads
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "500"))      # 500 fonts per Telegram ZIP part (~40MB)

ARCHIVE_DIR = "dafont_archive"
BUNDLES_DIR = "telegram_bundles"
DB_PATH = "fonts_index.db"
CHUNK_SIZE_MB = 45  # Telegram Bot API max safe limit

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

def is_letter_completed(letter):
    val = get_meta(f"letter_{letter}_done", "false")
    return val == "true"

def mark_letter_completed(letter):
    set_meta(f"letter_{letter}_done", "true")

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

def crawl_single_letter_turbo(letter):
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
        with ThreadPoolExecutor(max_workers=CRAWL_THREADS) as executor:
            page_results = list(executor.map(lambda p: crawl_alphabet_page(letter, p), pages_to_fetch))

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

# ----------------- TURBO PARALLEL DOWNLOADER (128 THREADS) -----------------

def download_worker(slug, out_dir):
    url = f"https://dl.dafont.com/dl/?f={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    font_folder = os.path.join(out_dir, slug)
    
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
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
            time.sleep(0.1)
    return slug, False, 0

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
    caption = f"📦 *DaFont Fonts Bundle* [Part #{part_idx}]\nContains *{len(font_files)} font files* ({size_mb:.2f} MB)"
    
    uploaded = tg_send_document(bundle_path, caption)
    
    if uploaded:
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
            
        print(f"[✓] Part #{part_idx} ({len(font_files)} fonts) safely sent to Telegram & deleted from VPS.")
        set_meta("last_part_idx", part_idx)
        return True
    return False

def process_section_fonts_turbo(letter, threads=MAX_THREADS, batch_size=BATCH_SIZE):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    
    while True:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT slug FROM fonts WHERE source = ? AND status != 'uploaded' LIMIT ?", (f"alpha_{letter}", batch_size))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            mark_letter_completed(letter)
            print(f"[✓] All fonts in Section '{letter}' successfully downloaded, uploaded to Telegram, and wiped!")
            break

        batch_slugs = [r[0] for r in rows]
        part_idx = int(get_meta("last_part_idx", "0")) + 1

        print(f"\n[*] Section '{letter}': Downloading {len(batch_slugs)} fonts with {threads} PARALLEL THREADS (Part #{part_idx})...")
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=threads) as executor:
            future_to_slug = {executor.submit(download_worker, s, ARCHIVE_DIR): s for s in batch_slugs}
            done_count = 0
            for future in as_completed(future_to_slug):
                slug, ok, font_count = future.result()
                done_count += 1
                if done_count % 100 == 0 or done_count == len(batch_slugs):
                    elapsed = time.time() - start_time
                    speed = done_count / elapsed if elapsed > 0 else 0
                    print(f"  [Section {letter} | Part #{part_idx}] {done_count}/{len(batch_slugs)} downloaded ({speed:.1f} fonts/sec)...")

        # Package and upload to Telegram immediately!
        upload_and_clean_batch(batch_slugs, part_idx)
        time.sleep(0.5)

# ----------------- MASTER PIPELINE -----------------

def main():
    init_db()
    letters = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['ot_1']
    
    print("=" * 65)
    print(" 🚀 DAFONT TURBO RESUMABLE STREAM EXPORTER (128 THREADS)")
    print(f"[*] Target Chat: {CHAT_ID}")
    print(f"[*] Download Threads: {MAX_THREADS} | Crawl Threads: {CRAWL_THREADS}")
    print(f"[*] Batch Size: {BATCH_SIZE} fonts per Telegram ZIP")
    print("=" * 65)

    completed_letters = [l for l in letters if is_letter_completed(l)]
    last_part = int(get_meta("last_part_idx", "0"))

    if completed_letters:
        print(f"\n[🔄 RESUME DETECTED] Completed Sections: {', '.join(completed_letters)}")
        print(f"[🔄 RESUME DETECTED] Last Uploaded Part: #{last_part}")
        tg_send_message(f"🔄 *VPS Auto-Resume Active!*\nCompleted Sections: *{len(completed_letters)}/27* (Part #{last_part})\nResuming download stream on next pending section...")
    else:
        tg_send_message("🚀 *DaFont TURBO Live Exporter Started (128 Threads)!*\nProcessing letters with immediate parallel Telegram delivery...")

    # Process each letter: TURBO CRAWL (seconds) -> TURBO DOWNLOAD (128 threads) -> TELEGRAM UPLOAD -> WIPE
    for letter in letters:
        if is_letter_completed(letter):
            continue

        print(f"\n==========================================================")
        print(f"  ▶ STARTING SECTION '{letter.upper()}' (Turbo Crawl ➔ 128-Thread Download ➔ Telegram)")
        print(f"==========================================================")
        
        # 1. Turbo Crawl (parallel 32 threads, finishes in ~10-15 seconds!)
        crawl_single_letter_turbo(letter)

        # 2. Turbo Download (128 parallel threads) & immediate upload to Telegram
        process_section_fonts_turbo(letter, MAX_THREADS, BATCH_SIZE)

    print("\n" + "=" * 65)
    print(" 🎉 ALL 27 DAFONT SECTIONS FULLY EXPORTED & SENT TO TELEGRAM!")
    print("=" * 65)
    tg_send_message("🎉 *ALL 27 DAFONT SECTIONS FULLY EXPORTED & SENT TO TELEGRAM!*\nAll VPS storage wiped 100% clean.")

if __name__ == "__main__":
    main()

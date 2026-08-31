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
DOWNLOAD_LIMIT = int(os.getenv("LIMIT", "0"))  # 0 = All fonts

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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# ----------------- HIGH SPEED CRAWLER -----------------

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

def crawl_all_alphabets():
    init_db()
    letters = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['ot_1']
    print(f"\n[*] Crawling full DaFont Alphabet across {len(letters)} sections...")
    tg_send_message(f"🔍 *Starting DaFont Alphabet Crawl across {len(letters)} sections...*")
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total_indexed = 0

    for letter in letters:
        page = 1
        consecutive_empty = 0
        letter_count = 0
        
        while consecutive_empty < 2:
            links = crawl_alphabet_page(letter, page)
            if not links:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
                cur.executemany("INSERT OR IGNORE INTO fonts (slug, source) VALUES (?, ?)", [(s, f"alpha_{letter}") for s in links])
                conn.commit()
                letter_count += len(links)
                total_indexed += len(links)
                if page % 10 == 0:
                    print(f"  [Letter {letter}] Page {page} | +{len(links)} fonts | Total indexed: {total_indexed}")
            page += 1
            time.sleep(0.05)
            
        print(f"[✓] Letter '{letter}' finished: {letter_count} fonts indexed.")

    conn.close()
    print(f"\n[✓] Alphabet Crawl Finished! Total Fonts Indexed: {total_indexed}")
    tg_send_message(f"✅ *Indexing Complete!*\nTotal Fonts Found: *{total_indexed}*\nStarting mass parallel download with {MAX_THREADS} worker threads...")
    return total_indexed

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

def run_mass_downloader(limit, threads):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = "SELECT slug FROM fonts WHERE status = 'pending'"
    if limit > 0:
        query += f" LIMIT {limit}"
    cur.execute(query)
    rows = cur.fetchall()
    conn.close()

    slugs = [r[0] for r in rows]
    total = len(slugs)
    if total == 0:
        print("[*] No pending fonts found to download.")
        return

    print(f"\n==========================================================")
    print(f"[*] Starting Parallel Font Downloader: {total} fonts ({threads} threads)")
    print(f"==========================================================")

    start_time = time.time()
    successful = 0
    extracted_total = 0

    update_conn = sqlite3.connect(DB_PATH)
    update_cur = update_conn.cursor()

    with ThreadPoolExecutor(max_workers=threads) as executor:
        future_to_slug = {executor.submit(download_worker, s, ARCHIVE_DIR): s for s in slugs}
        count = 0
        for future in as_completed(future_to_slug):
            slug, ok, font_count = future.result()
            count += 1
            status = 'downloaded' if ok else 'failed'
            update_cur.execute("UPDATE fonts SET status = ?, font_files = ?, updated_at = CURRENT_TIMESTAMP WHERE slug = ?", (status, font_count, slug))
            if ok:
                successful += 1
                extracted_total += font_count

            if count % 100 == 0 or count == total:
                update_conn.commit()
                elapsed = time.time() - start_time
                rate = count / elapsed if elapsed > 0 else 0
                pct = (count * 100.0 / total)
                print(f"  [Progress] {count}/{total} ({pct:.1f}%) | {successful} downloaded | {extracted_total} .TTF/.OTF files | {rate:.1f} fonts/sec")

    update_conn.commit()
    update_conn.close()

    elapsed = time.time() - start_time
    print(f"\n[✓] Mass Download Complete in {elapsed:.1f}s ({successful}/{total} successful, {extracted_total} font files)")

# ----------------- BUNDLER & CLEANUP -----------------

def bundle_fonts_into_chunks(source_dir=ARCHIVE_DIR, bundles_dir=BUNDLES_DIR, max_mb=CHUNK_SIZE_MB):
    os.makedirs(bundles_dir, exist_ok=True)
    max_bytes = max_mb * 1024 * 1024
    
    font_files = []
    for root, dirs, files in os.walk(source_dir):
        for f in files:
            if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
                fpath = os.path.join(root, f)
                relpath = os.path.relpath(fpath, source_dir)
                font_files.append((fpath, relpath, os.path.getsize(fpath)))

    print(f"\n[*] Found {len(font_files)} font binaries to bundle into zip chunks...")
    if not font_files:
        return []

    created_zips = []
    idx = 1
    curr_path = os.path.join(bundles_dir, f"dafont_archive_part{idx}.zip")
    curr_zip = zipfile.ZipFile(curr_path, "w", zipfile.ZIP_DEFLATED)
    curr_size = 0

    for fpath, relpath, fsize in font_files:
        if curr_size + fsize > max_bytes and curr_size > 0:
            curr_zip.close()
            created_zips.append(curr_path)
            size_mb = os.path.getsize(curr_path) / (1024 * 1024)
            print(f"  [+] Created bundle: {curr_path} ({size_mb:.2f} MB)")
            idx += 1
            curr_path = os.path.join(bundles_dir, f"dafont_archive_part{idx}.zip")
            curr_zip = zipfile.ZipFile(curr_path, "w", zipfile.ZIP_DEFLATED)
            curr_size = 0

        curr_zip.write(fpath, relpath)
        curr_size += fsize

    curr_zip.close()
    created_zips.append(curr_path)
    size_mb = os.path.getsize(curr_path) / (1024 * 1024)
    print(f"  [+] Created final bundle: {curr_path} ({size_mb:.2f} MB)")
    return created_zips

def run_full_cleanup():
    print("\n==========================================")
    print("[*] Running Automatic Server Storage Wipe...")
    print("==========================================")
    for d in [ARCHIVE_DIR, BUNDLES_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
            print(f"[✓] Deleted {d}")
    for f in [DB_PATH, f"{DB_PATH}-wal", f"{DB_PATH}-shm", "fast_font_downloader"]:
        if os.path.exists(f):
            try:
                os.remove(f)
                print(f"[✓] Deleted {f}")
            except Exception:
                pass
    print("[✓] ALL DATA WIPED. 100% Free Disk Space Restored.")

# ----------------- MAIN PIPELINE -----------------

def main():
    limit_str = "ALL FONTS" if DOWNLOAD_LIMIT == 0 else str(DOWNLOAD_LIMIT)
    print("=" * 60)
    print(" 🚀 DAFONT COMPLETE VPS EXPORTER & TELEGRAM BOT UPLOADER")
    print(f"[*] Target Chat: {CHAT_ID}")
    print(f"[*] Worker Threads: {MAX_THREADS}")
    print(f"[*] Limit: {limit_str}")
    print("=" * 60)

    tg_send_message("🚀 *DaFont Mass Exporter Started on VPS!*\nBeginning Alphabet indexing and parallel extraction...")

    # Step 1: Crawl alphabet (A-Z, #)
    crawl_all_alphabets()

    # Step 2: Parallel Mass Download
    run_mass_downloader(DOWNLOAD_LIMIT, MAX_THREADS)

    # Step 3: Bundle into Zip chunks
    bundles = bundle_fonts_into_chunks()
    if not bundles:
        tg_send_message("⚠️ *Warning*: No fonts were extracted to upload.")
        run_full_cleanup()
        return

    # Step 4: Upload to Telegram
    tg_send_message(f"📦 *Font Packaging Complete!*\nUploading *{len(bundles)} ZIP parts* to Telegram...")
    total_uploaded = 0
    for idx, bpath in enumerate(bundles, 1):
        caption = f"📦 DaFont Archive [Part {idx}/{len(bundles)}]\nExtracted TrueType & OpenType Fonts"
        if tg_send_document(bpath, caption):
            total_uploaded += 1
        time.sleep(1)

    tg_send_message(f"🎉 *ALL {total_uploaded}/{len(bundles)} FONT BUNDLES SENT SUCCESSFULLY!*\nWiping server storage now...")

    # Step 5: Automatic Server Wipe
    run_full_cleanup()
    print("\n[✓] Entire pipeline finished successfully!")

if __name__ == "__main__":
    main()

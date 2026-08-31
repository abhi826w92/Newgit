#!/usr/bin/env python3
import os
import sys
import time
import zipfile
import urllib.request
import urllib.parse
import json

BOT_TOKEN = "8486999738:AAEXkcxrILtF2AH2YfPesT1vwUAhPKiRVYs"
DEFAULT_CHAT_ID = "-1003887776900"
CHUNK_SIZE_MB = 45  # Telegram Bot API max is 50MB; 45MB is safe
ARCHIVE_DIR = "dafont_archive"
OUTPUT_BUNDLES_DIR = "telegram_bundles"

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"[!] Error sending message: {e}")
        return None

def send_document(chat_id, file_path, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    
    file_name = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(f"[*] Uploading '{file_name}' ({file_size_mb:.2f} MB) to Telegram Chat {chat_id}...")

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body = []
    # chat_id field
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"))
    # caption field
    if caption:
        body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode("utf-8"))
    # document field
    body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{file_name}\"\r\nContent-Type: application/zip\r\n\r\n".encode("utf-8"))
    body.append(file_bytes)
    body.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    full_payload = b"".join(body)
    req = urllib.request.Request(url, data=full_payload)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                res = json.loads(resp.read().decode('utf-8'))
                if res.get("ok"):
                    print(f"[✓] Successfully sent '{file_name}' to Telegram!")
                    return True
                else:
                    print(f"[!] Telegram API error: {res}")
        except Exception as e:
            print(f"[!] Upload attempt {attempt + 1} failed: {e}")
            time.sleep(2)
    return False

def bundle_fonts_into_chunks(source_dir=ARCHIVE_DIR, bundles_dir=OUTPUT_BUNDLES_DIR, max_mb=CHUNK_SIZE_MB):
    os.makedirs(bundles_dir, exist_ok=True)
    max_bytes = max_mb * 1024 * 1024
    
    font_files = []
    for root, dirs, files in os.walk(source_dir):
        for f in files:
            if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
                fpath = os.path.join(root, f)
                relpath = os.path.relpath(fpath, source_dir)
                font_files.append((fpath, relpath, os.path.getsize(fpath)))

    print(f"[*] Found {len(font_files)} font binaries to bundle.")
    if not font_files:
        return []

    created_zips = []
    current_zip_idx = 1
    current_zip_path = os.path.join(bundles_dir, f"dafont_archive_part{current_zip_idx}.zip")
    current_zip = zipfile.ZipFile(current_zip_path, "w", zipfile.ZIP_DEFLATED)
    current_size = 0

    for fpath, relpath, fsize in font_files:
        if current_size + fsize > max_bytes and current_size > 0:
            current_zip.close()
            created_zips.append(current_zip_path)
            print(f"  [+] Created bundle: {current_zip_path} ({os.path.getsize(current_zip_path) / (1024*1024):.2f} MB)")
            
            current_zip_idx += 1
            current_zip_path = os.path.join(bundles_dir, f"dafont_archive_part{current_zip_idx}.zip")
            current_zip = zipfile.ZipFile(current_zip_path, "w", zipfile.ZIP_DEFLATED)
            current_size = 0

        current_zip.write(fpath, relpath)
        current_size += fsize

    current_zip.close()
    created_zips.append(current_zip_path)
    print(f"  [+] Created final bundle: {current_zip_path} ({os.path.getsize(current_zip_path) / (1024*1024):.2f} MB)")

    return created_zips

def main():
    chat_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHAT_ID
    source_dir = sys.argv[2] if len(sys.argv) > 2 else ARCHIVE_DIR
    
    print(f"=== Starting Telegram Uploader ===")
    print(f"[*] Target Chat / Channel ID: {chat_id}")
    print(f"[*] Fonts Source Directory: {source_dir}")

    send_message(chat_id, "🚀 *DaFont Mass Export Complete!*\nPreparing and packaging font archives for Telegram delivery...")

    bundles = bundle_fonts_into_chunks(source_dir)
    if not bundles:
        send_message(chat_id, "⚠️ *Error*: No font files found in export directory.")
        sys.exit(1)

    total_bundles = len(bundles)
    success_count = 0

    for idx, bpath in enumerate(bundles, 1):
        caption = f"📦 DaFont Archive [Part {idx}/{total_bundles}]\nExtracted TrueType & OpenType Fonts"
        if send_document(chat_id, bpath, caption):
            success_count += 1
        time.sleep(1)

    if success_count == total_bundles:
        send_message(chat_id, f"✅ *All {total_bundles} Font Bundles Successfully Uploaded!*\nCleaning up server storage now...")
        print(f"\n[✓] All {total_bundles} zip bundles sent to Telegram!")
        sys.exit(0)
    else:
        send_message(chat_id, f"⚠️ Upload finished with {total_bundles - success_count} failed bundles.")
        sys.exit(1)

if __name__ == "__main__":
    main()

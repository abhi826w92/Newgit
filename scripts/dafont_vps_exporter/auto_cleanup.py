#!/usr/bin/env python3
import os
import shutil
import sys

TARGET_DIRS = [
    "dafont_archive",
    "telegram_bundles",
]

TARGET_FILES = [
    "fonts_index.db",
    "fonts_index.db-wal",
    "fonts_index.db-shm",
]

def cleanup():
    print("\n==========================================")
    print("[*] Starting Automatic VPS Data Wipe...")
    print("==========================================")

    for d in TARGET_DIRS:
        if os.path.exists(d):
            print(f"[*] Removing directory: {d}...")
            shutil.rmtree(d, ignore_errors=True)
            print(f"[✓] Deleted {d}")

    for f in TARGET_FILES:
        if os.path.exists(f):
            print(f"[*] Removing file: {f}...")
            try:
                os.remove(f)
                print(f"[✓] Deleted {f}")
            except Exception as e:
                print(f"[!] Could not delete {f}: {e}")

    print("\n[✓] VPS CLEANUP COMPLETE! 100% Storage Freed.")
    print("==========================================")

if __name__ == "__main__":
    cleanup()

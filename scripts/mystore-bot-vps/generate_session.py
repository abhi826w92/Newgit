#!/usr/bin/env python3
"""
MyStore - Permanent User Session Generator
Generates a Telethon / Pyrogram String Session or .session file to bypass Bot API limits.
"""

import os
import sys
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("❌ Telethon is not installed! Run: pip install telethon")
    sys.exit(1)


def main():
    print("=" * 55)
    print("🔑 MyStore Permanent Telegram Session Generator")
    print("=" * 55)
    print("Get your API_ID and API_HASH from https://my.telegram.org/apps\n")

    api_id_env = os.getenv("TELEGRAM_API_ID", "")
    api_hash_env = os.getenv("TELEGRAM_API_HASH", "")

    api_id = input(f"Enter API_ID [{api_id_env}]: ").strip() or api_id_env
    api_hash = input(f"Enter API_HASH [{api_hash_env}]: ").strip() or api_hash_env

    if not api_id or not api_hash:
        print("❌ Error: API_ID and API_HASH are required!")
        sys.exit(1)

    try:
        api_id = int(api_id)
    except ValueError:
        print("❌ Error: API_ID must be an integer!")
        sys.exit(1)

    print("\nConnecting to Telegram...")
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        string_session = client.session.save()
        user = client.get_me()
        print("\n" + "=" * 55)
        print(f"✅ Logged in successfully as: {user.first_name} (@{user.username}) [ID: {user.id}]")
        print("=" * 55)
        print("\n📜 Generated Permanent String Session:\n")
        print(string_session)
        print("\n" + "=" * 55)

        # Save to .env
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        found_id, found_hash, found_session = False, False, False

        for line in lines:
            if line.startswith("TELEGRAM_API_ID="):
                new_lines.append(f"TELEGRAM_API_ID={api_id}\n")
                found_id = True
            elif line.startswith("TELEGRAM_API_HASH="):
                new_lines.append(f"TELEGRAM_API_HASH={api_hash}\n")
                found_hash = True
            elif line.startswith("TELEGRAM_STRING_SESSION="):
                new_lines.append(f"TELEGRAM_STRING_SESSION={string_session}\n")
                found_session = True
            else:
                new_lines.append(line)

        if not found_id:
            new_lines.append(f"TELEGRAM_API_ID={api_id}\n")
        if not found_hash:
            new_lines.append(f"TELEGRAM_API_HASH={api_hash}\n")
        if not found_session:
            new_lines.append(f"TELEGRAM_STRING_SESSION={string_session}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        print(f"💾 Automatically saved to {ENV_FILE}!")
        print("🚀 Storage channel (-1003887776900) is now ready for unlimited 2GB+ file uploads!\n")


if __name__ == "__main__":
    main()

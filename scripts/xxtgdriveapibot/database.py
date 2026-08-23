import sqlite3
import json
import logging
from config import DATABASE_PATH, DEFAULT_API_KEY, ADMIN_IDS, API_ID, API_HASH
from crypto import encrypt_api_key, decrypt_api_key

logger = logging.getLogger(__name__)

def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables and perform automatic schema migrations."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            api_id TEXT,
            api_hash TEXT,
            api_key TEXT DEFAULT '',
            username TEXT,
            first_name TEXT,
            current_folder_id TEXT DEFAULT 'root',
            current_folder_name TEXT DEFAULT 'Root (Saved Messages)',
            state TEXT,
            state_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            folder_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            parent_id TEXT DEFAULT 'root',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_folders (
            file_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            folder_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Auto-migrations for existing databases
    migrations = [
        "ALTER TABLE users ADD COLUMN api_id TEXT",
        "ALTER TABLE users ADD COLUMN api_hash TEXT",
        "ALTER TABLE users ADD COLUMN current_folder_name TEXT DEFAULT 'Root (Saved Messages)'"
    ]
    for mig in migrations:
        try:
            cursor.execute(mig)
        except Exception:
            pass

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully.")

def get_user(user_id: int):
    """Fetch user record by user_id."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None

def get_user_tg_credentials(user_id: int):
    """Get the decrypted Telegram API ID and API Hash for a user with persistent Admin fallback."""
    user = get_user(user_id)
    if user:
        raw_id = user.get("api_id")
        raw_hash = user.get("api_hash")
        if raw_id and raw_hash:
            dec_id = decrypt_api_key(raw_id)
            dec_hash = decrypt_api_key(raw_hash)
            if dec_id and dec_hash:
                return dec_id, dec_hash

    # Persistent fallback for Admins from config/.env
    if (user_id in ADMIN_IDS) and API_ID and API_HASH:
        set_user_tg_credentials(user_id, str(API_ID), API_HASH)
        return str(API_ID), API_HASH

    return None, None

def set_user_tg_credentials(user_id: int, api_id: str, api_hash: str, username: str = None, first_name: str = None):
    """Encrypt and save Telegram API ID and API Hash for user."""
    enc_id = encrypt_api_key(str(api_id).strip())
    enc_hash = encrypt_api_key(str(api_hash).strip())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, api_id, api_hash, api_key, username, first_name, last_active)
        VALUES (?, ?, ?, COALESCE((SELECT api_key FROM users WHERE user_id = ?), ''), ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            api_id = excluded.api_id,
            api_hash = excluded.api_hash,
            username = COALESCE(excluded.username, users.username),
            first_name = COALESCE(excluded.first_name, users.first_name),
            last_active = CURRENT_TIMESTAMP
    """, (user_id, enc_id, enc_hash, user_id, username, first_name))
    conn.commit()
    conn.close()
    logger.info(f"Saved encrypted Telegram API credentials for user {user_id}")

def get_user_api_key(user_id: int):
    """Get the decrypted TG Drive API key for a user with persistent fallback across bot restarts."""
    user = get_user(user_id)
    if user and user.get("api_key"):
        raw_key = user["api_key"]
        decrypted = decrypt_api_key(raw_key)
        if decrypted:
            # Auto-migrate legacy unencrypted keys to secure ciphertext in-place
            if not raw_key.startswith("enc_v1:"):
                set_user_api_key(user_id, decrypted, username=user.get("username"), first_name=user.get("first_name"))
            return decrypted

    # Fallback to DEFAULT_API_KEY if configured in .env for Admins
    if DEFAULT_API_KEY and (user_id in ADMIN_IDS):
        set_user_api_key(user_id, DEFAULT_API_KEY)
        return DEFAULT_API_KEY

    return None

def set_user_api_key(user_id: int, api_key: str, username: str = None, first_name: str = None):
    """Securely encrypt and persist user TG Drive API key in SQLite database."""
    if not api_key:
        return
    encrypted_key = encrypt_api_key(api_key.strip())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, api_key, username, first_name, last_active)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            api_key = excluded.api_key,
            username = COALESCE(excluded.username, users.username),
            first_name = COALESCE(excluded.first_name, users.first_name),
            last_active = CURRENT_TIMESTAMP,
            state = NULL,
            state_data = NULL
    """, (user_id, encrypted_key, username, first_name))
    conn.commit()
    conn.close()
    logger.info(f"Securely saved encrypted TG Drive API key for user {user_id}")

def delete_user_api_key(user_id: int):
    """Permanently delete user's encrypted credentials, folders, and session data upon logout."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM folders WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM file_folders WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"Permanently wiped all database records and credentials for user {user_id}")

def get_all_users():
    """Fetch all registered users for broadcasting and admin management."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, username, first_name, api_key, api_id, created_at, last_active 
        FROM users 
        ORDER BY last_active DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_bot_stats():
    """Calculate global bot and database statistics for admin dashboard."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE api_key IS NOT NULL AND api_key != ''")
    active_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users WHERE api_id IS NOT NULL AND api_id != ''")
    tg_configured = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM folders")
    total_folders = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM file_folders")
    total_files = cursor.fetchone()[0]

    conn.close()

    db_size = 0
    try:
        import os
        if os.path.exists(DATABASE_PATH):
            db_size = os.path.getsize(DATABASE_PATH)
    except Exception:
        pass

    return {
        "total_users": total_users,
        "active_users": active_users,
        "tg_configured": tg_configured,
        "total_folders": total_folders,
        "total_files": total_files,
        "db_size": db_size
    }

def sync_user_folders(user_id: int, folders_list: list):
    """Clean and sync local folders with live folders from Telegram Saved Messages API."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM folders WHERE user_id = ?", (user_id,))
    for f in folders_list:
        f_id = str(f.get("id") or f.get("message_id"))
        f_name = f.get("name")
        p_id = str(f.get("parentId") or f.get("parent_id") or "root")
        if f_id and f_name:
            cursor.execute("""
                INSERT OR REPLACE INTO folders (folder_id, user_id, name, parent_id)
                VALUES (?, ?, ?, ?)
            """, (f_id, user_id, f_name.strip(), p_id))
    conn.commit()
    conn.close()

def add_user_folder(user_id: int, folder_id: str, name: str, parent_id: str = "root"):
    """Add or update a custom folder in the database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO folders (folder_id, user_id, name, parent_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(folder_id) DO UPDATE SET name = excluded.name, parent_id = excluded.parent_id
    """, (str(folder_id), user_id, name.strip(), str(parent_id)))
    conn.commit()
    conn.close()

def get_user_folders(user_id: int, parent_id: str = "root"):
    """Get all custom folders for a user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT folder_id as id, name, parent_id, created_at FROM folders 
        WHERE user_id = ?
        ORDER BY created_at ASC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_folder_by_id(user_id: int, folder_id: str):
    """Retrieve folder metadata by folder_id."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT folder_id as id, name, parent_id, created_at FROM folders 
        WHERE user_id = ? AND folder_id = ?
    """, (user_id, str(folder_id)))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None

def delete_user_folder(user_id: int, folder_id: str):
    """Delete folder and any associated file mapping from database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM folders WHERE user_id = ? AND folder_id = ?", (user_id, str(folder_id)))
    cursor.execute("DELETE FROM file_folders WHERE user_id = ? AND folder_id = ?", (user_id, str(folder_id)))
    conn.commit()
    conn.close()

def set_file_folder(user_id: int, file_id: str, folder_id: str):
    """Link a file to a specific folder in SQLite."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO file_folders (file_id, user_id, folder_id)
        VALUES (?, ?, ?)
        ON CONFLICT(file_id) DO UPDATE SET folder_id = excluded.folder_id
    """, (str(file_id), user_id, str(folder_id)))
    conn.commit()
    conn.close()

def get_files_in_folder(user_id: int, folder_id: str):
    """Get list of file IDs assigned to a folder."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT file_id FROM file_folders WHERE user_id = ? AND folder_id = ?", (user_id, str(folder_id)))
    rows = cursor.fetchall()
    conn.close()
    return [str(r["file_id"]) for r in rows]

def get_user_folder(user_id: int):
    """Get user's current folder ID and Name. Returns (folder_id, folder_name)."""
    user = get_user(user_id)
    if user:
        f_id = user.get("current_folder_id") or "root"
        f_name = user.get("current_folder_name") or ("Root (Saved Messages)" if f_id == "root" else f_id)
        return f_id, f_name
    return "root", "Root (Saved Messages)"

def set_user_folder(user_id: int, folder_id: str, folder_name: str = "Root (Saved Messages)"):
    """Set user's active default target upload folder."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users 
        SET current_folder_id = ?, current_folder_name = ?, last_active = CURRENT_TIMESTAMP 
        WHERE user_id = ?
    """, (str(folder_id), folder_name, user_id))
    conn.commit()
    conn.close()

def reset_user_folder(user_id: int):
    """Reset target folder back to root."""
    set_user_folder(user_id, "root", "Root (Saved Messages)")

def set_user_state(user_id: int, state: str, state_data: dict = None):
    """Set a conversational state for user."""
    conn = get_connection()
    cursor = conn.cursor()
    data_str = json.dumps(state_data) if state_data else None
    cursor.execute("""
        UPDATE users SET state = ?, state_data = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?
    """, (state, data_str, user_id))
    if cursor.rowcount == 0:
        cursor.execute("""
            INSERT INTO users (user_id, api_key, state, state_data) VALUES (?, '', ?, ?)
        """, (user_id, state, data_str))
    conn.commit()
    conn.close()

def get_user_state(user_id: int):
    """Returns (state: str or None, state_data: dict or None)."""
    user = get_user(user_id)
    if not user:
        return None, None
    state = user.get("state")
    data_str = user.get("state_data")
    state_data = json.loads(data_str) if data_str else None
    return state, state_data

def clear_user_state(user_id: int):
    """Clear any active state for the user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET state = NULL, state_data = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def update_user_activity(user_id: int, username: str = None, first_name: str = None):
    """Update last active timestamp."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users SET
            username = COALESCE(?, username),
            first_name = COALESCE(?, first_name),
            last_active = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (username, first_name, user_id))
    conn.commit()
    conn.close()

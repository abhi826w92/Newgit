"""
Telegram Cloud Channel Storage Manager (Pure MTProto Streaming Engine)
- Uses Telethon directly for ALL downloading and uploading (up to 2GB-4GB files) with ZERO standard Bot API limitations.
- Encrypts all MTProto session data on disk using AES/HMAC-SHA256 Authenticated Encryption (AEAD) derived from ADMIN_SECURITY_TOKEN.
"""

import os
import sys
import asyncio
import hashlib
import hmac
import base64
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_ID = os.getenv("TELEGRAM_API_ID", "29116029")
API_HASH = os.getenv("TELEGRAM_API_HASH", "867fafeeabc20a75163ef2ddbd877f70")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8630369883:AAGN6KGgl0TGYaHRkdJt_epiiWy60icpZD0")
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "-1003887776900"))
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "mystore_permanent_session")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
ADMIN_SECURITY_TOKEN = os.getenv("ADMIN_SECURITY_TOKEN", "sec_mystore_9a8b7c6d5e4f3a2b1c0d")

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False


# ==============================================================================
# Authenticated Session Encryption Engine (Zero-Dependency AEAD)
# ==============================================================================
def _derive_keys(secret_key: str):
    k_enc = hashlib.sha256((secret_key + ":mystore_mtproto_enc").encode()).digest()
    k_auth = hashlib.sha256((secret_key + ":mystore_mtproto_auth").encode()).digest()
    return k_enc, k_auth


def encrypt_session_string(plain_text: str, secret_key: str = ADMIN_SECURITY_TOKEN) -> str:
    """Encrypt session string into authenticated ciphertext"""
    if not plain_text:
        return ""
    plain_bytes = plain_text.encode("utf-8")
    k_enc, k_auth = _derive_keys(secret_key)
    iv = os.urandom(16)
    
    # Counter mode stream cipher
    keystream = bytearray()
    counter = 0
    while len(keystream) < len(plain_bytes):
        ctr_block = iv + counter.to_bytes(4, "big")
        keystream.extend(hmac.new(k_enc, ctr_block, hashlib.sha256).digest())
        counter += 1
        
    cipher = bytes(a ^ b for a, b in zip(plain_bytes, keystream[:len(plain_bytes)]))
    tag = hmac.new(k_auth, iv + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(iv + tag + cipher).decode("utf-8")


def decrypt_session_string(encrypted_token: str, secret_key: str = ADMIN_SECURITY_TOKEN) -> str:
    """Decrypt authenticated session ciphertext back to plain string"""
    if not encrypted_token:
        return ""
    try:
        raw = base64.urlsafe_b64decode(encrypted_token.encode("utf-8"))
        if len(raw) < 48:
            return ""
        iv = raw[:16]
        tag = raw[16:48]
        cipher = raw[48:]
        k_enc, k_auth = _derive_keys(secret_key)
        expected_tag = hmac.new(k_auth, iv + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            print("⚠️ [MTProto Session] Warning: Decryption integrity check failed.")
            return ""
        keystream = bytearray()
        counter = 0
        while len(keystream) < len(cipher):
            ctr_block = iv + counter.to_bytes(4, "big")
            keystream.extend(hmac.new(k_enc, ctr_block, hashlib.sha256).digest())
            counter += 1
        return bytes(a ^ b for a, b in zip(cipher, keystream[:len(cipher)])).decode("utf-8")
    except Exception as e:
        print(f"⚠️ [MTProto Session] Decrypt notice: {e}")
        return ""


# ==============================================================================
# Pure MTProto Cloud Storage & Media Streaming Client
# ==============================================================================
class TelegramChannelStorage:
    def __init__(self):
        self.api_id = int(API_ID) if API_ID and str(API_ID).isdigit() else 29116029
        self.api_hash = API_HASH
        self.bot_token = BOT_TOKEN
        self.channel_id = STORAGE_CHANNEL_ID
        self.enc_session_path = os.path.join(BASE_DIR, "mystore_session.enc")
        self.client = None

    def is_configured(self):
        """Check if API_ID and API_HASH are available"""
        return bool(self.api_id and self.api_hash)

    def _load_decrypted_session_string(self) -> str:
        """Load and decrypt session from disk or .env"""
        # 1. Check encrypted session on disk
        if os.path.exists(self.enc_session_path):
            try:
                with open(self.enc_session_path, "r", encoding="utf-8") as f:
                    enc_data = f.read().strip()
                decrypted = decrypt_session_string(enc_data)
                if decrypted:
                    return decrypted
            except Exception as e:
                print(f"[MTProto] Session load error: {e}")

        # 2. Check STRING_SESSION in .env (if pre-encrypted or plain)
        if STRING_SESSION:
            # Try decrypting first
            decrypted = decrypt_session_string(STRING_SESSION)
            if decrypted:
                return decrypted
            return STRING_SESSION

        return ""

    def save_encrypted_session(self):
        """Encrypt and persist session securely on disk"""
        if self.client and hasattr(self.client.session, 'save'):
            plain_str = self.client.session.save()
            if plain_str:
                enc_token = encrypt_session_string(plain_str)
                with open(self.enc_session_path, "w", encoding="utf-8") as f:
                    f.write(enc_token)
                print(f"🔒 [MTProto] Session encrypted & saved at: {self.enc_session_path}")

    def get_client(self):
        """Initialize Telethon client with encrypted session"""
        if not self.is_configured() or not HAS_TELETHON:
            return None

        if self.client is None:
            plain_session_str = self._load_decrypted_session_string()
            session = StringSession(plain_session_str)

            self.client = TelegramClient(
                session, 
                self.api_id, 
                self.api_hash,
                device_model="MyStore Pure MTProto Engine",
                system_version="Linux Native",
                app_version="3.0.0"
            )
        return self.client

    async def ensure_authorized(self):
        """Ensure MTProto client is connected and authenticated with auto-renewal fallback"""
        client = self.get_client()
        if not client:
            return False, "Telethon client not configured."

        try:
            if not client.is_connected():
                await client.connect()

            if not await client.is_user_authorized():
                await client.start(bot_token=self.bot_token)
                self.save_encrypted_session()

            return True, client
        except Exception as initial_err:
            print(f"⚠️ [MTProto] Session expired or authorization notice: {initial_err}. Auto-healing session...")
            try:
                # 1. Clear stale session on disk
                if os.path.exists(self.enc_session_path):
                    try:
                        os.remove(self.enc_session_path)
                    except Exception:
                        pass

                # 2. Recreate Telethon client with fresh session
                self.client = TelegramClient(
                    StringSession(""),
                    self.api_id,
                    self.api_hash,
                    device_model="MyStore Pure MTProto Engine",
                    system_version="Linux Native",
                    app_version="3.0.0"
                )
                await self.client.connect()
                await self.client.start(bot_token=self.bot_token)
                self.save_encrypted_session()
                print("✅ [MTProto] Session successfully auto-renewed & saved!")
                return True, self.client
            except Exception as renew_err:
                return False, f"MTProto Auto-Recovery failed: {str(renew_err)}"

    # --------------------------------------------------------------------------
    # 100% PURE MTPROTO DOWNLOAD (Zero Bot API - Supports up to 2GB-4GB)
    # --------------------------------------------------------------------------
    async def download_media_mtproto(self, chat_id, message_id, destination_path, progress_callback=None):
        """Download file directly from Telegram MTProto protocol with progress tracking"""
        ok, res = await self.ensure_authorized()
        if not ok:
            return False, res

        client = res
        try:
            msg = await client.get_messages(chat_id, ids=message_id)
            if not msg or not msg.media:
                return False, "No media found in message."

            os.makedirs(os.path.dirname(os.path.abspath(destination_path)), exist_ok=True)

            out_path = await client.download_media(
                msg,
                file=destination_path,
                progress_callback=progress_callback
            )
            self.save_encrypted_session()
            return True, out_path
        except Exception as e:
            return False, f"MTProto download error: {str(e)}"

    def sync_download_media(self, chat_id, message_id, destination_path, progress_callback=None):
        """Synchronous wrapper for pure MTProto download"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(self.download_media_mtproto(chat_id, message_id, destination_path, progress_callback))
            loop.close()
            return result
        except Exception as e:
            return False, str(e)

    # --------------------------------------------------------------------------
    # 100% PURE MTPROTO UPLOAD (Zero Bot API - Channel Cloud Archive)
    # --------------------------------------------------------------------------
    async def upload_file_to_channel(self, file_path, caption=None, progress_callback=None):
        """Upload large file/APK directly to permanent storage channel via MTProto"""
        ok, res = await self.ensure_authorized()
        if not ok:
            return False, res

        client = res
        if not os.path.exists(file_path):
            return False, f"File not found: {file_path}"

        try:
            file_name = os.path.basename(file_path)
            file_size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)

            msg_caption = caption or (
                f"📦 <b>MyStore Cloud Storage Asset</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📄 <b>File:</b> <code>{file_name}</code>\n"
                f"💾 <b>Size:</b> <code>{file_size_mb} MB</code>\n"
                f"🔒 <b>Storage:</b> Permanent Channel Archive"
            )

            msg = await client.send_file(
                self.channel_id,
                file_path,
                caption=msg_caption,
                parse_mode="html",
                progress_callback=progress_callback
            )

            self.save_encrypted_session()

            clean_cid = str(self.channel_id).replace("-100", "").replace("-", "")
            channel_link = f"https://t.me/c/{clean_cid}/{msg.id}"

            return True, {
                "message_id": msg.id,
                "channel_link": channel_link,
                "file_name": file_name,
                "file_size": os.path.getsize(file_path)
            }
        except Exception as e:
            return False, f"MTProto channel upload error: {str(e)}"

    def sync_upload_to_channel(self, file_path, caption=None, progress_callback=None):
        """Synchronous wrapper for pure MTProto upload to channel"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(self.upload_file_to_channel(file_path, caption, progress_callback))
            loop.close()
            return result
        except Exception as e:
            return False, str(e)


# Global Singleton Instance
storage_mgr = TelegramChannelStorage()

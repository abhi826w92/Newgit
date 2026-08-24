import os
import hashlib
import hmac
import base64
import secrets
import logging
from config import BOT_TOKEN, API_HASH

logger = logging.getLogger(__name__)

# Fixed default master secret ensuring 100% persistence across server restarts
DEFAULT_VAULT_SECRET = "tgdrive_super_secure_vault_secret_key_2026"
CONFIG_SECRET = os.getenv('ENCRYPTION_SECRET', DEFAULT_VAULT_SECRET).strip() or DEFAULT_VAULT_SECRET

class SecretCipher:
    def __init__(self, secret: str):
        self.secret = secret
        salt = b'tgdrive_api_secure_salt_v1'
        # PBKDF2 key derivation (32 bytes encryption key + 32 bytes HMAC key)
        derived = hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), salt, 100000, 64)
        self.enc_key = derived[:32]
        self.hmac_key = derived[32:]

    def _keystream(self, iv: bytes, length: int) -> bytes:
        stream = bytearray()
        counter = 0
        while len(stream) < length:
            block = hmac.new(self.enc_key, iv + counter.to_bytes(4, 'big'), hashlib.sha256).digest()
            stream.extend(block)
            counter += 1
        return bytes(stream[:length])

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ''
        data = plaintext.encode('utf-8')
        iv = secrets.token_bytes(16)
        stream = self._keystream(iv, len(data))
        ciphertext = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(self.hmac_key, iv + ciphertext, hashlib.sha256).digest()
        payload = iv + tag + ciphertext
        return 'enc_v1:' + base64.urlsafe_b64encode(payload).decode('utf-8')

    def try_decrypt(self, raw_bytes: bytes) -> str:
        if len(raw_bytes) < 48:
            return ''
        iv = raw_bytes[:16]
        tag = raw_bytes[16:48]
        ciphertext = raw_bytes[48:]
        expected_tag = hmac.new(self.hmac_key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            return ''
        stream = self._keystream(iv, len(ciphertext))
        decrypted = bytes(a ^ b for a, b in zip(ciphertext, stream))
        return decrypted.decode('utf-8')

# Primary Cipher
_primary_cipher = SecretCipher(CONFIG_SECRET)

# Fallback Ciphers for 100% Guaranteed Decryption across Token Changes & VPS Reboots
_fallback_secrets = [
    DEFAULT_VAULT_SECRET,
    f"{BOT_TOKEN}_{API_HASH}",
    "tgdrive_permanent_vault_2026",
    "tgdrive_api_default_master_secret"
]
_fallback_ciphers = [SecretCipher(s) for s in _fallback_secrets if s != CONFIG_SECRET]

def encrypt_api_key(api_key: str) -> str:
    """Encrypt TG Drive API key / credentials securely before database storage."""
    return _primary_cipher.encrypt(api_key)

def decrypt_api_key(encoded: str) -> str:
    """Decrypt stored credentials from database with automatic multi-key recovery."""
    if not encoded:
        return ''
    if not str(encoded).startswith('enc_v1:'):
        # Plaintext fallback for backward-compatible migration
        return str(encoded).strip()
    try:
        raw = base64.urlsafe_b64decode(encoded[7:])
        
        # 1. Try primary configured cipher
        result = _primary_cipher.try_decrypt(raw)
        if result:
            return result
        
        # 2. Try fallback ciphers in case token or secret was rotated
        for cipher in _fallback_ciphers:
            result = cipher.try_decrypt(raw)
            if result:
                return result
                
        logger.warning('Could not decrypt token with active or fallback ciphers.')
        return ''
    except Exception as e:
        logger.error(f'Decryption error: {e}')
        return ''

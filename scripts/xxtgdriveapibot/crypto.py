import os
import hashlib
import hmac
import base64
import secrets
import logging
from config import BOT_TOKEN, API_HASH

logger = logging.getLogger(__name__)

# Master Secret derived from BOT_TOKEN + API_HASH or custom environment variable
MASTER_SECRET = os.getenv('ENCRYPTION_SECRET', f'{BOT_TOKEN}_{API_HASH}').strip()

class SecretCipher:
    def __init__(self, secret: str):
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

    def decrypt(self, encoded: str) -> str:
        if not encoded:
            return ''
        if not encoded.startswith('enc_v1:'):
            # Plaintext fallback for backward-compatible migration
            return encoded.strip()
        try:
            raw = base64.urlsafe_b64decode(encoded[7:])
            if len(raw) < 48: # 16 (IV) + 32 (Tag)
                return ''
            iv = raw[:16]
            tag = raw[16:48]
            ciphertext = raw[48:]
            expected_tag = hmac.new(self.hmac_key, iv + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected_tag):
                logger.error('API Key decryption HMAC signature verification failed')
                return ''
            stream = self._keystream(iv, len(ciphertext))
            decrypted = bytes(a ^ b for a, b in zip(ciphertext, stream))
            return decrypted.decode('utf-8')
        except Exception as e:
            logger.error(f'Decryption error: {e}')
            return ''

_cipher = SecretCipher(MASTER_SECRET)

def encrypt_api_key(api_key: str) -> str:
    """Encrypt TG Drive API key securely before database storage."""
    return _cipher.encrypt(api_key)

def decrypt_api_key(encrypted_str: str) -> str:
    """Decrypt stored TG Drive API key from database."""
    return _cipher.decrypt(encrypted_str)

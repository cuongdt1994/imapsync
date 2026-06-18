"""
Symmetric encryption service using Fernet.
Credentials are encrypted at rest in SQLite,
and only decrypted in-memory just before passing to imapsync subprocess.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet

from config import Config


_fernet: Fernet | None = None


def _load_or_create_key() -> bytes:
    """Load Fernet key from env var, or file, or generate a new one."""
    # 1. From environment variable
    env_key = Config.FERNET_KEY
    if env_key:
        return env_key.encode("utf-8")

    # 2. From key file
    key_file = Config.FERNET_KEY_FILE
    if key_file.exists():
        return key_file.read_bytes()

    # 3. Generate new key and save to file
    key = Fernet.generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    # Restrict permissions on the key file
    key_file.write_bytes(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass  # best effort on some platforms
    return key


def get_fernet() -> Fernet:
    """Get (or initialize) the Fernet instance."""
    global _fernet
    if _fernet is None:
        key = _load_or_create_key()
        _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns base64-encoded ciphertext."""
    f = get_fernet()
    token = f.encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token back to plaintext."""
    f = get_fernet()
    plaintext = f.decrypt(ciphertext.encode("utf-8"))
    return plaintext.decode("utf-8")

"""
AES-256-GCM encryption module.

Provides symmetric encryption for message content before storing in D1.
Key is read from the ``ENCRYPTION_KEY`` environment variable (64 hex chars = 32 bytes).
Database stores: ``encrypted_content`` (base64) + ``nonce`` (base64).
"""

import base64
import os

from Crypto.Cipher import AES

from mods.logger import setup_logger

logger = setup_logger(__name__)

_KEY_HEX = os.getenv("ENCRYPTION_KEY", "")

if not _KEY_HEX or len(_KEY_HEX) != 64:
    logger.warning(
        "ENCRYPTION_KEY is missing or invalid (need 64 hex chars). "
        "Generate one with: python -c \"import os; print(os.urandom(32).hex())\""
    )
    _KEY = b"\x00" * 32
else:
    try:
        _KEY = bytes.fromhex(_KEY_HEX)
    except ValueError:
        logger.error("ENCRYPTION_KEY contains non-hex characters")
        _KEY = b"\x00" * 32


def encrypt(plaintext: str) -> tuple[str, str]:
    """Encrypt *plaintext* with AES-256-GCM.

    Returns
    -------
    (encrypted_content_b64, nonce_b64)
        Both are base64 strings.  ``encrypted_content`` contains
        ciphertext **+ 16-byte GCM authentication tag**.
    """
    cipher = AES.new(_KEY, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    encrypted = base64.b64encode(ciphertext + tag).decode("ascii")
    nonce = base64.b64encode(cipher.nonce).decode("ascii")
    return encrypted, nonce


def decrypt(encrypted_b64: str, nonce_b64: str) -> str:
    """Decrypt content previously encrypted by :func:`encrypt`.

    Raises :class:`ValueError` if the GCM tag verification fails.
    """
    raw = base64.b64decode(encrypted_b64)
    nonce = base64.b64decode(nonce_b64)
    ciphertext, tag = raw[:-16], raw[-16:]
    cipher = AES.new(_KEY, AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    return plaintext.decode("utf-8")

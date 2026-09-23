"""Field encryption for the memory store.

One 256-bit data key per Windows user, generated once and kept next to the
database as a DPAPI blob (``CryptProtectData``, current-user scope): only this
Windows account on this machine can unwrap it, and no key material is ever in a
file in the clear.

Wire format of an encrypted TEXT column (identical to MaxMi's
``AESGCMFieldCipher``, so values are interchangeable)::

    "enc:v1:" + base64(nonce[12] || ciphertext || tag[16])     AES-256-GCM, no AAD

Binary columns (embedding vectors) use the same layout without the prefix and
without base64: ``nonce[12] || ciphertext || tag[16]``.

:class:`FieldCipher` also derives a separate HMAC-SHA256 key from the data key
for :meth:`FieldCipher.digest`, used for dedup hashes and thread lookup keys, so
equality checks never need a plaintext (or plain SHA) column.

Public API::

    load_or_create_key(key_path: Path) -> bytes     # 32 bytes, DPAPI-wrapped on disk
    FieldCipher(key: bytes)
        .encrypt(text: str) -> str                  # "enc:v1:..."
        .decrypt(stored: str) -> str                # passthrough when not prefixed (MaxMi parity)
        .encrypt_bytes(data: bytes) -> bytes
        .decrypt_bytes(blob: bytes) -> bytes
        .digest(*parts: str) -> str                 # keyed hex digest
    CipherError, IntegrityError, MalformedCiphertext
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16

#: Extra DPAPI entropy. Not a secret (it is in the source); it only stops some
#: other program's DPAPI blob from being mistaken for ours.
_DPAPI_ENTROPY = b"yuki-memory-data-key-v1"
_DPAPI_DESCRIPTION = "Yuki memory data key"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CipherError(Exception):
    """Base class for field-encryption failures."""


class IntegrityError(CipherError):
    """Authentication failed: tampered data or the wrong key."""


class MalformedCiphertext(CipherError):
    """Prefixed but not decodable as nonce + ciphertext + tag."""


def load_or_create_key(key_path: Path) -> bytes:
    """Return the data key stored (DPAPI-protected) at ``key_path``.

    The key is created on first use. Creation uses an exclusive open, so two
    processes starting together agree on a single key: the loser of the race
    reads the winner's file.

    Raises:
        CipherError: The file exists but cannot be unwrapped by this user, or
            does not hold a 32-byte key. Deliberately *not* regenerated: a new
            key would make every stored row unreadable.
    """
    import win32crypt  # pywin32; imported here so the module imports anywhere

    key_path = Path(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key = os.urandom(KEY_BYTES)
        blob = win32crypt.CryptProtectData(
            key, _DPAPI_DESCRIPTION, _DPAPI_ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
        try:
            with open(key_path, "xb") as fh:
                fh.write(blob)
            return key
        except FileExistsError:
            pass  # another process created it first; use theirs
    blob = key_path.read_bytes()
    try:
        _description, key = win32crypt.CryptUnprotectData(
            blob, _DPAPI_ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except Exception as exc:  # pywintypes.error
        raise CipherError(f"cannot unwrap memory key {key_path}: {exc}") from exc
    if len(key) != KEY_BYTES:
        raise CipherError(f"memory key {key_path} holds {len(key)} bytes, expected {KEY_BYTES}")
    return bytes(key)


class FieldCipher:
    """AES-256-GCM for individual column values, plus a keyed digest."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("AES-256 needs a 32-byte key")
        self._aead = AESGCM(key)
        self._mac_key = hmac.new(key, b"yuki-memory-hmac-v1", hashlib.sha256).digest()

    # -- text columns ------------------------------------------------------

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` into the ``enc:v1:`` wire format."""
        return PREFIX + base64.b64encode(self.encrypt_bytes(plaintext.encode("utf-8"))).decode("ascii")

    def decrypt(self, stored: str) -> str:
        """Decrypt an ``enc:v1:`` value; unprefixed values pass through unchanged."""
        if not stored.startswith(PREFIX):
            return stored
        try:
            blob = base64.b64decode(stored[len(PREFIX):], validate=True)
        except (ValueError, TypeError) as exc:
            raise MalformedCiphertext("not base64") from exc
        return self.decrypt_bytes(blob).decode("utf-8")

    # -- binary columns ----------------------------------------------------

    def encrypt_bytes(self, data: bytes) -> bytes:
        """``nonce || ciphertext || tag`` for ``data``."""
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, data, None)

    def decrypt_bytes(self, blob: bytes) -> bytes:
        """Inverse of :meth:`encrypt_bytes`."""
        if len(blob) < NONCE_BYTES + TAG_BYTES:
            raise MalformedCiphertext("too short for nonce + tag")
        try:
            return self._aead.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None)
        except InvalidTag as exc:
            raise IntegrityError("authentication failed (tampered data or wrong key)") from exc

    # -- keyed hashing -----------------------------------------------------

    def digest(self, *parts: str) -> str:
        """HMAC-SHA256 hex of ``parts`` (length-prefixed, so boundaries count)."""
        mac = hmac.new(self._mac_key, digestmod=hashlib.sha256)
        for part in parts:
            raw = part.encode("utf-8")
            mac.update(len(raw).to_bytes(8, "big"))
            mac.update(raw)
        return mac.hexdigest()

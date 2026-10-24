# arczen/chromium_crypto.py
"""Shared Chromium (Arc) secret-store key handling and value decryption.

Both the cookie jar and the password store use the same OS-held master key,
so it is derived once here and reused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# macOS Keychain generic-password items Arc is known to use, in priority order.
KEYCHAIN_SERVICES = ["Arc Safe Storage", "Arc", "Chromium Safe Storage"]

# Chromium's fixed PBKDF2 parameters for the macOS keyring path.
MAC_SALT = b"saltysalt"
MAC_ITERATIONS = 1003
MAC_KEY_LENGTH = 16
MAC_IV = b" " * 16


class CryptoError(RuntimeError):
    """Raised when Arc's encrypted stores cannot be opened."""


def _require_backend():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as error:  # pragma: no cover - import guard
        raise CryptoError(
            "Reading Arc's encrypted stores needs the cryptography package: "
            "pip install cryptography"
        ) from error
    return Cipher, algorithms, modes


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data

# --------------------------------------------------------------------------
# Key material
# --------------------------------------------------------------------------

def _macos_key(service_override: str | None) -> bytes:
    services = [service_override] if service_override else KEYCHAIN_SERVICES

    for service in services:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise CryptoError("The macOS 'security' tool is unavailable") from error

        secret = result.stdout.strip()
        if result.returncode == 0 and secret:
            logger.info("Using Keychain item %r for Arc decryption", service)
            return hashlib.pbkdf2_hmac(
                "sha1",
                secret.encode("utf-8"),
                MAC_SALT,
                MAC_ITERATIONS,
                MAC_KEY_LENGTH,
            )

    raise CryptoError(
        "Could not read Arc's Keychain secret. Approve the Keychain prompt when it "
        "appears, or pass --keychain-service with the exact item name shown in "
        "Keychain Access."
    )


def _windows_key(arc_root: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    local_state = arc_root / "User Data" / "Local State"
    if not local_state.is_file():
        raise CryptoError(f"Missing Arc Local State file: {local_state}")

    with open(local_state, encoding="utf-8") as handle:
        state = json.load(handle)

    encoded = (state.get("os_crypt") or {}).get("encrypted_key")
    if not encoded:
        raise CryptoError("Arc Local State has no os_crypt.encrypted_key")

    blob = base64.b64decode(encoded)
    if not blob.startswith(b"DPAPI"):
        raise CryptoError("Unexpected master key format in Arc Local State")
    blob = blob[5:]

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(blob, len(blob))
    source = DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    output = DataBlob()

    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise CryptoError(
            "CryptUnprotectData refused Arc's master key. The key is bound to your "
            "Windows account, so this must run as the same user that uses Arc."
        )

    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


@dataclass
class ChromiumKey:
    """The platform master key, plus the matching cipher mode."""

    platform: str
    material: bytes

    def decrypt(self, blob: bytes) -> str:
        """Decrypt one `password_value` blob to text."""

        Cipher, algorithms, modes = _require_backend()
        prefix = blob[:3]

        if prefix == b"v20":
            raise CryptoError(
                "This Arc build uses app-bound encryption (v20). Those values are tied "
                "to the browser binary and cannot be decrypted externally."
            )

        if self.platform == "darwin":
            payload = blob[3:] if prefix in (b"v10", b"v11") else blob
            if len(payload) % 16:
                raise CryptoError("Ciphertext is not a whole number of AES blocks")
            decryptor = Cipher(algorithms.AES(self.material), modes.CBC(MAC_IV)).decryptor()
            plaintext = _strip_pkcs7(decryptor.update(payload) + decryptor.finalize())

        elif self.platform == "win32":
            if prefix != b"v10":
                raise CryptoError("Unrecognised ciphertext prefix for a Windows store")
            nonce, ciphertext, tag = blob[3:15], blob[15:-16], blob[-16:]
            decryptor = Cipher(algorithms.AES(self.material), modes.GCM(nonce, tag)).decryptor()
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        else:  # pragma: no cover - Arc ships on macOS and Windows only
            raise CryptoError(f"Unsupported platform for Arc decryption: {self.platform}")

        return plaintext.decode("utf-8", errors="replace")


def load_key(arc_root: Path, keychain_service: str | None = None) -> ChromiumKey:
    """Derive Arc's master key from the OS secret store."""
    if sys.platform == "darwin":
        return ChromiumKey("darwin", _macos_key(keychain_service))
    if sys.platform == "win32":
        return ChromiumKey("win32", _windows_key(arc_root))
    raise CryptoError("Arc's encrypted stores can only be read on macOS and Windows")
"""
At-rest encryption for sensitive button values.

Sensitive values in ``buttons.json`` are encrypted with the Windows Data
Protection API (DPAPI), so the file on disk never holds the plaintext of a
password or token. Nothing else in the application changes: callers keep
working with plaintext and only the configuration file boundary calls
``encrypt_text`` / ``decrypt_text``.

DPAPI ties the ciphertext to the Windows user account that created it, so a
``buttons.json`` copied to another account or machine cannot be decrypted there.
``decrypt_text`` reports that as a ``CryptoError`` instead of returning junk, so
the caller can ask the user to re-enter the value.

Encrypted values are stored as ``"enc:v1:<base64>"``. The prefix makes the value
self-describing, so a plaintext value that is hand-typed into a sensitive entry
is still recognised and re-encrypted on the next save.

The functions are pure ctypes calls and do not touch Tk; like the rest of the
application's Win32 helpers they are called from the Tk main thread.
"""

import base64
import binascii
import ctypes
import sys
from ctypes import wintypes

# Marks a stored value as encrypted and identifies the format version.
PREFIX = "enc:v1:"

# Extra entropy mixed into the DPAPI key. This is a fixed application salt and
# not a secret: DPAPI still derives the real key from the Windows user account.
_ENTROPY = b"QuickFill.v1"

# Never show UI from CryptProtectData/CryptUnprotectData. The app runs under
# pythonw with no console, so an unexpected dialog would be invisible anyway.
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class CryptoError(Exception):
    """Raised when a value cannot be encrypted or decrypted."""


class _DataBlob(ctypes.Structure):
    """The DATA_BLOB structure used by the DPAPI entry points."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),        # pDataIn
        wintypes.LPCWSTR,                 # szDataDescr
        ctypes.POINTER(_DataBlob),        # pOptionalEntropy
        ctypes.c_void_p,                  # pvReserved
        ctypes.c_void_p,                  # pPromptStruct
        wintypes.DWORD,                   # dwFlags
        ctypes.POINTER(_DataBlob),        # pDataOut
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),        # pDataIn
        ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr
        ctypes.POINTER(_DataBlob),        # pOptionalEntropy
        ctypes.c_void_p,                  # pvReserved
        ctypes.c_void_p,                  # pPromptStruct
        wintypes.DWORD,                   # dwFlags
        ctypes.POINTER(_DataBlob),        # pDataOut
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.restype = wintypes.HANDLE
else:
    _crypt32 = None
    _kernel32 = None


# --- Internal helpers ------------------------------------------------------

def _input_blob(data):
    """
    Wraps bytes in a DATA_BLOB backed by a ctypes buffer.

    Returns a (buffer, blob) tuple. The buffer must be kept alive for as long as
    the blob is passed to a DPAPI call, because the blob points into it.
    """
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return buffer, blob


def _free_blob(blob):
    """Releases the LocalAlloc'd output buffer of a DPAPI call."""
    if blob.pbData:
        _kernel32.LocalFree(ctypes.cast(blob.pbData, wintypes.HANDLE))


def _protect(data):
    """Encrypts bytes with DPAPI and returns the opaque blob."""
    _in_buffer, in_blob = _input_blob(data)
    _entropy_buffer, entropy_blob = _input_blob(_ENTROPY)
    out_blob = _DataBlob()

    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "QuickFill",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise CryptoError(
            f"CryptProtectData failed (error {ctypes.get_last_error()})."
        )

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _free_blob(out_blob)


def _unprotect(data):
    """Decrypts a DPAPI blob and returns the original bytes."""
    _in_buffer, in_blob = _input_blob(data)
    _entropy_buffer, entropy_blob = _input_blob(_ENTROPY)
    out_blob = _DataBlob()

    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise CryptoError(
            f"Could not decrypt the value (error {ctypes.get_last_error()}). "
            "It was probably encrypted by a different Windows user or machine."
        )

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _free_blob(out_blob)


# --- Public API ------------------------------------------------------------

def is_supported():
    """True when value encryption is available (Windows only)."""
    return _IS_WINDOWS


def is_encrypted(value):
    """True when a stored value already carries the encrypted-value prefix."""
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_text(plaintext):
    """
    Returns plaintext as an ``enc:v1:`` string, or the value unchanged when it
    is empty, not a string, or already encrypted.

    Raises CryptoError when encryption is unavailable or fails. Callers must not
    fall back to writing the plaintext, so a failure stays visible.
    """
    if not isinstance(plaintext, str) or not plaintext or is_encrypted(plaintext):
        return plaintext
    if not _IS_WINDOWS:
        raise CryptoError("Value encryption requires Windows DPAPI.")

    try:
        blob = _protect(plaintext.encode("utf-8"))
    except UnicodeEncodeError as e:
        raise CryptoError(f"Value could not be encoded for encryption: {e}") from e

    return PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_text(stored):
    """
    Returns the plaintext behind an ``enc:v1:`` value.

    Raises CryptoError when the value is not encrypted, is malformed, or cannot
    be decrypted by the current Windows user.
    """
    if not is_encrypted(stored):
        raise CryptoError("Value is not encrypted.")
    if not _IS_WINDOWS:
        raise CryptoError("Value decryption requires Windows DPAPI.")

    try:
        blob = base64.b64decode(stored[len(PREFIX):], validate=True)
    except (binascii.Error, ValueError) as e:
        raise CryptoError(f"Malformed encrypted value: {e}") from e

    plaintext = _unprotect(blob)
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CryptoError(f"Decrypted value is not valid text: {e}") from e

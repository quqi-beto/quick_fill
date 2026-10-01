"""
Win32 clipboard access for QuickFill.

The Tk clipboard can only publish plain text, so this module talks to the Windows
clipboard directly with ctypes. That makes it possible to tag a copy as private
("keep this out of the clipboard history and do not upload it"), which Tk cannot
express.

``set_text``, ``get_text`` and ``clear`` are the functions the application uses.
They report failure with False/None instead of raising, and on non-Windows
platforms ``is_supported`` returns False so the caller can fall back to Tk.

Nothing here touches Tk, but the functions must be called from a single thread
(the Tk main thread) to avoid racing other clipboard users in the same process.
"""

import ctypes
import sys
import time
from ctypes import wintypes

# --- Win32 constants -------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Windows refuses OpenClipboard while another process holds the clipboard. That
# normally lasts a few milliseconds, so retry briefly instead of failing.
OPEN_ATTEMPTS = 10
OPEN_RETRY_DELAY = 0.02

# Clipboard formats that tell the shell how a copy may be reused. The two
# "Can..." formats carry a DWORD, where 0 means "no".
FORMAT_EXCLUDE_MONITOR = "ExcludeClipboardContentFromMonitorProcessing"
FORMAT_INCLUDE_IN_HISTORY = "CanIncludeInClipboardHistory"
FORMAT_UPLOAD_TO_CLOUD = "CanUploadToCloudClipboard"

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    _user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT

    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
else:
    _user32 = None
    _kernel32 = None


# --- Internal helpers ------------------------------------------------------

def _open_clipboard():
    """Opens the clipboard, retrying while another process is holding it."""
    for _ in range(OPEN_ATTEMPTS):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(OPEN_RETRY_DELAY)
    return False


def _alloc_bytes(data):
    """
    Copies data into a moveable global memory block and returns its handle.

    Returns None when the allocation fails. Ownership of the returned handle
    stays with the caller, who must either free it or hand it to the clipboard.
    """
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return None

    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        _kernel32.GlobalFree(handle)
        return None

    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        _kernel32.GlobalUnlock(handle)

    return handle


def _set_owned_format(format_id, data):
    """
    Stores one clipboard format, taking ownership of the memory block.

    On success the system owns the block; on failure this frees it so nothing
    leaks. Returns True when the format was stored.
    """
    handle = _alloc_bytes(data)
    if handle is None:
        return False

    if not _user32.SetClipboardData(format_id, handle):
        _kernel32.GlobalFree(handle)
        return False

    return True


def _mark_private():
    """Tags the current clipboard contents as excluded from history and cloud."""
    # Four zero bytes, so the value can also be read back safely as a DWORD.
    zero_dword = bytes(4)

    for name in (
        FORMAT_UPLOAD_TO_CLOUD,
        FORMAT_INCLUDE_IN_HISTORY,
        FORMAT_EXCLUDE_MONITOR,
    ):
        format_id = _user32.RegisterClipboardFormatW(name)
        if format_id:
            _set_owned_format(format_id, zero_dword)


# --- Public API ------------------------------------------------------------

def is_supported():
    """True when the Win32 clipboard is available (Windows only)."""
    return _IS_WINDOWS


def set_text(text, exclude_from_history=True):
    """
    Replaces the clipboard contents with text.

    Returns True on success. When exclude_from_history is set, the copy is also
    tagged so Windows keeps it out of the clipboard history (Win+V) and out of
    the cloud clipboard.
    """
    if not _IS_WINDOWS:
        return False

    # CF_UNICODETEXT is CRLF-delimited and NUL-terminated.
    payload = text.replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"

    if not _open_clipboard():
        return False

    try:
        if not _user32.EmptyClipboard():
            return False

        if exclude_from_history:
            _mark_private()

        return _set_owned_format(CF_UNICODETEXT, payload)
    finally:
        _user32.CloseClipboard()


def get_text():
    """
    Returns the clipboard text, or None when it does not hold plain text.

    Returns None rather than raising when the clipboard is empty, is locked by
    another process, or holds another format such as an image.
    """
    if not _IS_WINDOWS:
        return None

    if not _open_clipboard():
        return None

    try:
        if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None

        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None

        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None

        try:
            text = ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()

    return text.replace("\r\n", "\n")


def clear():
    """Empties the clipboard. Returns True when it was emptied."""
    if not _IS_WINDOWS:
        return False

    if not _open_clipboard():
        return False

    try:
        return bool(_user32.EmptyClipboard())
    finally:
        _user32.CloseClipboard()


def get_format_dword(name):
    """
    Diagnostic helper: reads a DWORD clipboard format, or None when absent.
    """
    if not _IS_WINDOWS:
        return None

    format_id = _user32.RegisterClipboardFormatW(name)
    if not format_id:
        return None

    if not _open_clipboard():
        return None

    try:
        if not _user32.IsClipboardFormatAvailable(format_id):
            return None

        handle = _user32.GetClipboardData(format_id)
        if not handle:
            return None

        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None

        try:
            return ctypes.c_uint32.from_address(pointer).value
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()

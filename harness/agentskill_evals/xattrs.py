"""Extended attributes, on the two platforms this harness runs on.

The workspace scrub has to account for every byte it archives, and on macOS not all of a
file's bytes are in the file. `xattr -w com.example.token <secret> note.txt` parks a
credential alongside the data where no `read()` will ever show it, `cp -p`, `ditto`, `rsync
-X` and zip all carry it along, and review reproduced exactly that: a scrub that reported
`lost == []` over a workspace still holding the secret in metadata.

CPython exposes ``os.listxattr`` and friends on Linux only, so darwin goes through libc.
Every call here is *no-follow* — a symlink's own attributes, never those of whatever it
points at — for the same reason the scrub never follows a link out of the artifact tree.

Failures surface as ``OSError``, which is the scrub's existing signal for "this entry could
not be certified"; on a platform with neither implementation every call raises, so an
unsupported platform quarantines its artifacts loudly instead of skipping their metadata in
silence.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os

__all__ = ["SUPPORTED", "listxattr", "getxattr", "setxattr", "removexattr"]

_NOFOLLOW = 0x0001  # XATTR_NOFOLLOW, <sys/xattr.h> on darwin
_libc = None

if not hasattr(os, "listxattr"):  # darwin, and anything else without the os-level API
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
                                    ctypes.c_int]
        _libc.listxattr.restype = ctypes.c_ssize_t
        _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        _libc.getxattr.restype = ctypes.c_ssize_t
        _libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        _libc.setxattr.restype = ctypes.c_int
        _libc.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        _libc.removexattr.restype = ctypes.c_int
    except (OSError, AttributeError, TypeError):  # pragma: no cover - exotic platform
        _libc = None

SUPPORTED = hasattr(os, "listxattr") or _libc is not None


def _unsupported(path):
    raise OSError(errno.ENOTSUP,
                  "extended attributes cannot be inspected on this platform, so this "
                  "entry's metadata cannot be certified free of secrets", str(path))


def _fail(path: str, call: str):
    code = ctypes.get_errno()
    raise OSError(code, f"{call}: {os.strerror(code)}", str(path))


def _sized(path: str, call: str, read, *, tries: int = 4) -> bytes:
    """Two-step libc read — ask for the size, then the bytes — retried if it grows.

    Another process can add an attribute between the two calls, which is a race the sizing
    convention has no way to express except by failing the second call; retrying is the
    whole handling it needs.
    """
    for _ in range(tries):
        size = read(None, 0)
        if size < 0:
            _fail(path, call)
        if size == 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = read(buf, size)
        if got >= 0:
            return buf.raw[:got]
        if ctypes.get_errno() != errno.ERANGE:
            _fail(path, call)
    _fail(path, call)


def listxattr(path: str) -> list[bytes]:
    """Every attribute name on *path* itself, as bytes — names are attacker-chosen text."""
    if hasattr(os, "listxattr"):
        return [n.encode("utf-8", "surrogateescape")
                for n in os.listxattr(path, follow_symlinks=False)]
    if _libc is None:
        _unsupported(path)
    raw = _sized(path, "listxattr",
                 lambda buf, size: _libc.listxattr(os.fsencode(path), buf, size, _NOFOLLOW))
    return [n for n in raw.split(b"\x00") if n]


def getxattr(path: str, name: bytes) -> bytes:
    if hasattr(os, "getxattr"):
        return os.getxattr(path, name, follow_symlinks=False)
    if _libc is None:
        _unsupported(path)
    return _sized(path, "getxattr",
                  lambda buf, size: _libc.getxattr(os.fsencode(path), name, buf, size, 0,
                                                   _NOFOLLOW))


def setxattr(path: str, name: bytes, value: bytes) -> None:
    if hasattr(os, "setxattr"):
        os.setxattr(path, name, value, follow_symlinks=False)
        return
    if _libc is None:
        _unsupported(path)
    if _libc.setxattr(os.fsencode(path), name, value, len(value), 0, _NOFOLLOW) != 0:
        _fail(path, "setxattr")


def removexattr(path: str, name: bytes) -> None:
    if hasattr(os, "removexattr"):
        os.removexattr(path, name, follow_symlinks=False)
        return
    if _libc is None:
        _unsupported(path)
    if _libc.removexattr(os.fsencode(path), name, _NOFOLLOW) != 0:
        _fail(path, "removexattr")

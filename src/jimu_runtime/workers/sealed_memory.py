"""Linux sealed anonymous artifact files across CPython build configurations."""

import ctypes
import errno
import fcntl
import os
import sys

# Linux UAPI: linux/memfd.h, linux/fcntl.h, asm-generic/fcntl.h.
# Python may omit names unavailable in the headers used to build it.
_MEMFD_FLAGS = 0x0001 | 0x0002  # MFD_CLOEXEC | MFD_ALLOW_SEALING
_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1024 + 9)
_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1024 + 10)
_REQUIRED_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008  # SEAL, SHRINK, GROW, WRITE
_ARTIFACT_NAME = "jimu-tool-artifact"


class SealedMemoryError(OSError):
    """The host could not create or verify an immutable anonymous artifact."""


def _require_linux() -> None:
    if sys.platform != "linux":
        raise SealedMemoryError(errno.ENOTSUP, "sealed artifacts require Linux")


def create_artifact_memfd() -> int:
    """Create an empty, close-on-exec Linux memfd with sealing enabled."""
    _require_linux()
    native = getattr(os, "memfd_create", None)
    try:
        if native is not None:
            return int(native(_ARTIFACT_NAME, _MEMFD_FLAGS))
        create = ctypes.CDLL(None, use_errno=True).memfd_create
        create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        create.restype = ctypes.c_int
        fd = int(create(_ARTIFACT_NAME.encode("ascii"), _MEMFD_FLAGS))
        if fd >= 0:
            return fd
        raise OSError(ctypes.get_errno(), "memfd_create failed")
    except AttributeError:
        raise SealedMemoryError(errno.ENOSYS, "memfd_create unavailable") from None
    except OSError as error:
        raise SealedMemoryError(
            error.errno, "sealed artifact creation failed"
        ) from None


def require_sealed_artifact_fd(fd: int) -> None:
    """Accept only an fd whose kernel-reported seals prevent mutation."""
    _require_linux()
    try:
        seals = fcntl.fcntl(fd, _GET_SEALS)
    except OSError as error:
        raise SealedMemoryError(error.errno, "artifact seals unavailable") from None
    if seals & _REQUIRED_SEALS != _REQUIRED_SEALS:
        raise SealedMemoryError(errno.EPERM, "artifact is not fully sealed")


def seal_artifact_fd(fd: int) -> None:
    """Apply all required seals after writing the complete artifact."""
    _require_linux()
    try:
        fcntl.fcntl(fd, _ADD_SEALS, _REQUIRED_SEALS)
    except OSError as error:
        raise SealedMemoryError(error.errno, "artifact sealing failed") from None
    require_sealed_artifact_fd(fd)

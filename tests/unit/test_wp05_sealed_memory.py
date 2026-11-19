import errno
import fcntl
import os

import pytest

from jimu_runtime.workers.sealed_memory import (
    SealedMemoryError,
    create_artifact_memfd,
    require_sealed_artifact_fd,
    seal_artifact_fd,
)


def test_kernel_enforces_sealed_artifact_and_close_on_exec():
    fd = create_artifact_memfd()
    try:
        assert not os.get_inheritable(fd)
        os.write(fd, b"immutable")
        with pytest.raises(SealedMemoryError):
            require_sealed_artifact_fd(fd)
        seal_artifact_fd(fd)
        require_sealed_artifact_fd(fd)
        with pytest.raises(PermissionError):
            os.pwrite(fd, b"tamper", 0)
        with pytest.raises(PermissionError):
            os.ftruncate(fd, 0)
        with pytest.raises(PermissionError):
            os.ftruncate(fd, 32)
        assert os.pread(fd, 32, 0) == b"immutable"
    finally:
        os.close(fd)


def test_ordinary_file_cannot_pass_artifact_validation(tmp_path):
    with (tmp_path / "ordinary").open("wb") as ordinary:
        with pytest.raises(SealedMemoryError):
            require_sealed_artifact_fd(ordinary.fileno())


def test_missing_kernel_sealing_support_is_typed_failure(monkeypatch):
    fd = create_artifact_memfd()

    def unsupported(*args):
        raise OSError(errno.ENOSYS, "test: sealing unavailable")

    try:
        monkeypatch.setattr(fcntl, "fcntl", unsupported)
        with pytest.raises(SealedMemoryError) as sealing:
            seal_artifact_fd(fd)
        assert sealing.value.errno == errno.ENOSYS
        with pytest.raises(SealedMemoryError) as reading:
            require_sealed_artifact_fd(fd)
        assert reading.value.errno == errno.ENOSYS
    finally:
        os.close(fd)

"""Private immutable files published using same-directory staging and hard links."""

import os
import stat
from collections.abc import Iterator
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import sha256
from heapq import nsmallest
from pathlib import Path
from re import fullmatch
from time import time

from jimu_runtime.inspection.media.types import MediaError


class LocalBytes:
    def __init__(self, root: Path) -> None:
        root = root.absolute()
        self._directory = root / "media-v1"
        self._locks = self._directory / ".locks"
        try:
            for path in (root, self._directory, self._locks):
                self._mkdir_durable(path)
                directory = path.lstat()
                if (
                    not stat.S_ISDIR(directory.st_mode)
                    or directory.st_uid != os.geteuid()
                    or directory.st_mode & 0o022
                    or (path != root and directory.st_mode & 0o077)
                ):
                    raise MediaError("media_directory_not_private")
        except OSError:
            raise MediaError("media_directory_unavailable") from None

    @classmethod
    def _mkdir_durable(cls, path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileNotFoundError:
            cls._mkdir_durable(path.parent)
            path.mkdir(mode=0o700, exist_ok=True)
        except FileExistsError:
            pass
        cls._sync_directory(path.parent)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def try_lock(self, shard: str) -> int | None:
        """Lock a fixed shard across processes without blocking the async caller."""
        fd = os.open(
            self._locks / shard,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            flock(fd, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except OSError:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def release_lock(fd: int) -> None:
        os.close(fd)

    def bind_database(self, identity: str) -> None:
        """Seal this private root to its trusted, provisioned PostgreSQL database."""
        path = self._directory / ".database"
        payload = identity.encode("ascii")
        try:
            if path.exists():
                if path.is_symlink() or path.read_bytes() != payload:
                    raise MediaError("media_database_identity_mismatch")
                return
            # The caller holds fixed shard 00. Atomic replacement is used only
            # for this private root marker's uncommitted temporary file.
            staged = self._directory / ".database.stage"
            staged.unlink(missing_ok=True)
            with staged.open("xb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            staged.replace(path)
            self._sync_directory(self._directory)
        except OSError:
            raise MediaError("media_database_identity_unavailable") from None

    def orphan_candidates(
        self, minimum_age: int, scan_limit: int, after_key: str
    ) -> tuple[str, ...]:
        """Enumerate a bounded lexical page from the private root."""
        cutoff = time() - minimum_age
        with os.scandir(self._directory) as entries:

            def eligible() -> Iterator[str]:
                for entry in entries:
                    key = entry.name.removesuffix(".stage")
                    if key <= after_key or not fullmatch(r"[0-9a-f]{64}", key):
                        continue
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime <= cutoff:
                            yield key
                    except FileNotFoundError:
                        continue

            # Keep adjacent aliases in one returned page. Only these two
            # names can share a key, so twice the bound preserves enough keys.
            return tuple(
                sorted(set(nsmallest(2 * scan_limit, eligible())))[:scan_limit]
            )

    def read(self, key: str, digest: str, size: int, limit: int) -> bytes:
        if not 1 <= size <= limit:
            raise MediaError("media_size_limit")
        try:
            path = self._directory / key
            descriptor = os.open(
                path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            )
            with os.fdopen(descriptor, "rb", buffering=0) as file:
                info = os.fstat(file.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077
                    or info.st_size != size
                ):
                    raise MediaError("media_file_metadata_mismatch")
                content = file.read(size)
                # Catch growth after opening without reading any byte past
                # the accepted budget, even at an exact-cap read.
                if os.fstat(file.fileno()).st_size != size:
                    raise MediaError("media_file_metadata_mismatch")
        except OSError:
            raise MediaError("media_file_unavailable") from None
        if len(content) != size or sha256(content).hexdigest() != digest:
            raise MediaError("media_content_mismatch")
        return content

    def publish(
        self, key: str, content: bytes, digest: str, *, recover_orphan: bool
    ) -> None:
        """The caller holds the filesystem shard lock through DB transaction exit."""
        path = self._directory / key
        stage = self._directory / f"{key}.stage"
        try:
            stage.unlink(missing_ok=True)
            try:
                self.read(key, digest, len(content), len(content))
            except MediaError:
                if not recover_orphan:
                    raise
                # The scope binding is absent under its lock. No reader can
                # acquire this key through a committed binding.
                path.unlink(missing_ok=True)
            else:
                self._sync_directory(self._directory)
                return

            fd = os.open(
                stage,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(content)
                    file.flush()
                    os.fchmod(file.fileno(), 0o400)
                    os.fsync(file.fileno())
                self.read(stage.name, digest, len(content), len(content))
                os.link(stage, path, follow_symlinks=False)
            finally:
                stage.unlink(missing_ok=True)
            self._sync_directory(self._directory)
        except OSError:
            raise MediaError("media_publication_failed") from None

    def remove_orphan(self, key: str, minimum_age: int = 0) -> bool:
        """Use only after checking absence under the same publication locks."""
        removed = False
        cutoff = time() - minimum_age
        try:
            for path in (self._directory / f"{key}.stage", self._directory / key):
                try:
                    if minimum_age and path.lstat().st_mtime > cutoff:
                        continue
                    path.unlink()
                    removed = True
                except FileNotFoundError:
                    continue
            if removed:
                self._sync_directory(self._directory)
        except OSError:
            raise MediaError("media_orphan_cleanup_failed") from None
        return removed

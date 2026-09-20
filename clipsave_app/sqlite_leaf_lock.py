from __future__ import annotations

import os
import stat
from pathlib import Path

from . import storage


class SQLiteLeafLock:
    """Hold a managed database leaf stable while SQLite opens it by path."""

    def __init__(
        self,
        path: Path,
        managed_root: Path,
        handle: int,
        identity: tuple[int, int],
        *,
        created: bool,
        writable: bool,
        replaceable: bool,
    ):
        self.path = storage.normalized_absolute_path(path)
        self.managed_root = Path(managed_root)
        self.handle = handle
        self.identity = identity
        self.created = created
        self.writable = writable
        self.replaceable = replaceable

    @classmethod
    def acquire(
        cls,
        path: Path,
        managed_root: Path,
        *,
        create: bool,
        writable: bool,
        replaceable: bool = False,
    ) -> "SQLiteLeafLock":
        candidate = Path(os.path.abspath(path))
        root = Path(os.path.abspath(managed_root))
        if replaceable and writable:
            raise ValueError("Replaceable SQLite identity locks must be read-only")
        storage.validate_managed_write_path(candidate, root)
        if os.name == "nt":
            creator = None
            created_candidate = False
            handle = None
            try:
                if create:
                    creator = storage.create_windows_identity_leaf(
                        candidate,
                        writable=writable,
                        replaceable=replaceable,
                    )
                    created_candidate = True
                handle = storage.open_verified_windows_identity(
                    candidate,
                    root,
                    writable=writable,
                    replaceable=replaceable,
                )
            except BaseException:
                if handle is not None:
                    storage.close_windows_handle(handle)
                if creator is not None:
                    storage.close_windows_handle(creator)
                    creator = None
                if created_candidate:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                raise
            finally:
                if creator is not None:
                    storage.close_windows_handle(creator)
            try:
                information = storage.inspect_windows_identity(handle)
            except BaseException:
                storage.close_windows_handle(handle)
                raise
            identity = (
                information.volume_serial_number,
                information.file_index,
            )
            return cls(
                candidate,
                root,
                handle,
                identity,
                created=create,
                writable=writable,
                replaceable=replaceable,
            )

        flags = os.O_RDWR if writable else os.O_RDONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        handle = os.open(candidate, flags, 0o600)
        file_stat = os.fstat(handle)
        identity = (int(file_stat.st_dev), int(file_stat.st_ino))
        lock = cls(
            candidate,
            root,
            handle,
            identity,
            created=create,
            writable=writable,
            replaceable=replaceable,
        )
        try:
            lock.verify()
        except BaseException:
            lock.close()
            lock.remove_created_path()
            raise
        return lock

    def verify(self, path: Path | None = None) -> None:
        if self.handle is None:
            raise RuntimeError(f"SQLite leaf identity lock is closed: {self.path}")
        candidate = Path(os.path.abspath(path or self.path))
        storage.validate_managed_write_path(candidate, self.managed_root)
        if os.name == "nt":
            information = storage.inspect_windows_identity(self.handle)
            identity = (
                information.volume_serial_number,
                information.file_index,
            )
            expected_path = storage.normalized_windows_requested_path(candidate)
            if information.final_path != expected_path:
                raise RuntimeError(
                    f"SQLite database leaf identity changed during open: {candidate}"
                )
            if information.number_of_links != 1:
                raise RuntimeError(
                    f"SQLite database leaf has multiple hard links: {candidate}"
                )
            probe = storage.open_verified_windows_identity(
                candidate,
                self.managed_root,
                writable=self.writable,
                replaceable=self.replaceable,
            )
            try:
                probe_information = storage.inspect_windows_identity(probe)
                probe_identity = (
                    probe_information.volume_serial_number,
                    probe_information.file_index,
                )
            finally:
                storage.close_windows_handle(probe)
            if identity != self.identity or probe_identity != self.identity:
                raise RuntimeError(
                    f"SQLite database leaf identity changed during open: {candidate}"
                )
            return

        handle_stat = os.fstat(self.handle)
        try:
            path_stat = candidate.stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"SQLite database leaf changed during open: {candidate}"
            ) from exc
        if (
            not stat.S_ISREG(handle_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or handle_stat.st_nlink != 1
            or path_stat.st_nlink != 1
            or (int(handle_stat.st_dev), int(handle_stat.st_ino)) != self.identity
            or (int(path_stat.st_dev), int(path_stat.st_ino)) != self.identity
        ):
            raise RuntimeError(
                f"SQLite database leaf identity changed or has multiple hard links: {candidate}"
            )

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        if os.name == "nt":
            storage.close_windows_handle(handle)
        else:
            os.close(handle)

    def remove_created_path(self) -> None:
        if not self.created:
            return
        try:
            path_stat = self.path.stat(follow_symlinks=False)
        except OSError:
            return
        expected_inode = self.identity[1]
        if int(path_stat.st_ino) == expected_inode:
            try:
                self.path.unlink()
            except OSError:
                pass


_SQLiteLeafLock = SQLiteLeafLock


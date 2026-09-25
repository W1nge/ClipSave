from __future__ import annotations

import hashlib
import io
import ntpath
import os
import shutil as _shutil
import uuid
from ctypes import wintypes
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable


# Compatibility seam: tests and older callers patch ``storage.shutil.move`` to
# prove migration never falls back to an unsafe cross-volume move.
shutil = _shutil

from .app_paths import AppPaths
from .database_files import copy_database_snapshot
from . import windows_storage as _windows_storage

from .storage_migration import (
    StorageMigrationOps,
    StorageMigrationPaths,
    copy_or_move_contents as _run_copy_or_move_contents,
    copy_verify_delete_file as _run_copy_verify_delete_file,
    migrate_legacy_layout as _run_storage_migration,
)

from .constants import (
    BASE_DIR,
    DATA_DIR,
    LEGACY_DATA_DIR,
    LEGACY_MARKDOWN_DIR,
    LEGACY_PICTURE_DIR,
    LIBRARY_DIR,
    LOCAL_ROOT,
    MAINTENANCE_DIR,
    MARKDOWN_DIR,
    PICTURE_DIR,
    USAGE_DIR,
)


if os.name == "nt":
    import msvcrt


_GENERIC_READ = _windows_storage._GENERIC_READ
_GENERIC_WRITE = _windows_storage._GENERIC_WRITE
_DELETE = _windows_storage._DELETE
_FILE_READ_ATTRIBUTES = _windows_storage._FILE_READ_ATTRIBUTES
_FILE_SHARE_READ = _windows_storage._FILE_SHARE_READ
_FILE_SHARE_WRITE = _windows_storage._FILE_SHARE_WRITE
_FILE_SHARE_DELETE = _windows_storage._FILE_SHARE_DELETE
_CREATE_NEW = _windows_storage._CREATE_NEW
_OPEN_EXISTING = _windows_storage._OPEN_EXISTING
_OPEN_ALWAYS = _windows_storage._OPEN_ALWAYS
_FILE_ATTRIBUTE_NORMAL = _windows_storage._FILE_ATTRIBUTE_NORMAL
_FILE_ATTRIBUTE_REPARSE_POINT = _windows_storage._FILE_ATTRIBUTE_REPARSE_POINT
_FILE_FLAG_BACKUP_SEMANTICS = _windows_storage._FILE_FLAG_BACKUP_SEMANTICS
_FILE_FLAG_OPEN_REPARSE_POINT = _windows_storage._FILE_FLAG_OPEN_REPARSE_POINT
_ByHandleFileInformation = _windows_storage._ByHandleFileInformation
_FileDispositionInfo = _windows_storage._FileDispositionInfo
_kernel32_function = _windows_storage._kernel32_function
_create_file = _windows_storage._create_file
_close_handle = _windows_storage._close_handle
_final_path_from_handle = _windows_storage._final_path_from_handle
_long_requested_path = _windows_storage._long_requested_path
_normalized_requested_path = _windows_storage._normalized_requested_path
normalized_absolute_path = _windows_storage.normalized_absolute_path
_file_information = _windows_storage._file_information
_truncate_handle = _windows_storage._truncate_handle
_hash_handle = _windows_storage._hash_handle
_mark_handle_for_delete = _windows_storage._mark_handle_for_delete



def _verified_windows_handle(
    path: Path,
    managed_root: Path,
    desired_access: int,
    disposition: int,
    share_mode: int = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
) -> int:
    root = normalized_absolute_path(managed_root)
    candidate = normalized_absolute_path(path)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Managed file is outside its local root: {candidate}") from exc

    root_handle = _create_file(
        root,
        _FILE_READ_ATTRIBUTES,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        root_information = _file_information(root_handle)
        if root_information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(f"Managed root is a reparse point: {root}")
        final_root = _final_path_from_handle(root_handle)
        requested_root = _normalized_requested_path(root)
        if final_root != requested_root:
            raise RuntimeError(f"Managed root contains a reparse point: {root}")

        handle_access = desired_access | _FILE_READ_ATTRIBUTES
        if disposition == _CREATE_NEW:
            handle_access |= _DELETE
        handle = _create_file(candidate, handle_access, disposition, share_mode=share_mode)
        try:
            information = _file_information(handle)
            final_candidate = _final_path_from_handle(handle)
            try:
                contained = ntpath.commonpath((final_root, final_candidate)) == final_root
            except ValueError:
                contained = False
            if not contained or final_candidate == final_root:
                raise RuntimeError(f"Managed file escaped its local root: {candidate}")
            if information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError(f"Managed file is a reparse point: {candidate}")
            if information.number_of_links != 1:
                raise RuntimeError(f"Managed file has multiple hard links: {candidate}")
            return handle
        except BaseException:
            if disposition == _CREATE_NEW:
                try:
                    _mark_handle_for_delete(handle)
                except OSError:
                    pass
            _close_handle(handle)
            raise
    finally:
        _close_handle(root_handle)


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    volume_serial_number: int
    file_index: int
    number_of_links: int
    final_path: str


def create_windows_identity_leaf(
    path: Path,
    *,
    writable: bool,
    replaceable: bool = False,
) -> int:
    """Create a Windows leaf handle for a later identity-verified path open."""
    if replaceable and writable:
        raise ValueError("Replaceable Windows identity handles must be read-only")
    access = _GENERIC_READ | _FILE_READ_ATTRIBUTES
    if writable:
        access |= _GENERIC_WRITE
    share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE
    if replaceable:
        share_mode = _FILE_SHARE_READ | _FILE_SHARE_DELETE
    return _create_file(
        Path(path),
        access,
        _CREATE_NEW,
        share_mode=share_mode,
    )


def open_verified_windows_identity(
    path: Path,
    managed_root: Path,
    *,
    writable: bool,
    replaceable: bool = False,
) -> int:
    """Open a managed Windows leaf with identity/reparse/hard-link checks."""
    if replaceable and writable:
        raise ValueError("Replaceable Windows identity handles must be read-only")
    access = _GENERIC_READ | (_GENERIC_WRITE if writable else 0)
    share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE
    if replaceable:
        share_mode = _FILE_SHARE_READ | _FILE_SHARE_DELETE
    return _verified_windows_handle(
        Path(path),
        Path(managed_root),
        access,
        _OPEN_EXISTING,
        share_mode,
    )


def inspect_windows_identity(handle: int) -> WindowsFileIdentity:
    """Return the stable identity and canonical path of an open Windows handle."""
    information = _file_information(handle)
    return WindowsFileIdentity(
        volume_serial_number=int(information.volume_serial_number),
        file_index=(int(information.file_index_high) << 32)
        | int(information.file_index_low),
        number_of_links=int(information.number_of_links),
        final_path=_final_path_from_handle(handle),
    )


def normalized_windows_requested_path(path: Path) -> str:
    return _normalized_requested_path(Path(path))


def close_windows_handle(handle: int) -> None:
    _close_handle(handle)


def open_managed_binary(
    path: Path,
    mode: str = "xb",
    managed_root: Path = LIBRARY_DIR,
    *,
    identity_locked: bool = False,
) -> BinaryIO:
    """Open a managed regular file after validating the object actually opened.

    Supported modes are ``rb``, ``r+b``, ``wb``, ``xb``, and ``ab``.
    Windows rejects reparse points, hardlinks, and final paths outside the
    verified managed root before returning a stream that can write payloads.
    """
    modes = {
        "rb": (_GENERIC_READ, _OPEN_EXISTING, os.O_RDONLY, "rb"),
        "r+b": (_GENERIC_READ | _GENERIC_WRITE, _OPEN_EXISTING, os.O_RDWR, "r+b"),
        "wb": (_GENERIC_WRITE, _OPEN_ALWAYS, os.O_WRONLY, "wb"),
        "xb": (_GENERIC_WRITE, _CREATE_NEW, os.O_WRONLY, "wb"),
        "ab": (_GENERIC_WRITE, _OPEN_ALWAYS, os.O_WRONLY | os.O_APPEND, "ab"),
    }
    if mode not in modes:
        raise ValueError(f"Unsupported managed binary mode: {mode}")
    candidate = Path(path)
    root = Path(managed_root)
    if os.name != "nt":
        validated = validate_managed_write_path(candidate, root)
        return validated.open(mode)

    desired_access, disposition, descriptor_flags, descriptor_mode = modes[mode]
    share_mode = (
        _FILE_SHARE_READ
        if mode != "rb" or identity_locked
        else _FILE_SHARE_READ | _FILE_SHARE_DELETE
    )
    created = disposition == _CREATE_NEW
    if disposition == _OPEN_ALWAYS:
        try:
            handle = _verified_windows_handle(
                candidate, root, desired_access, _OPEN_EXISTING, share_mode
            )
            created = False
        except FileNotFoundError:
            handle = _verified_windows_handle(
                candidate, root, desired_access, _CREATE_NEW, share_mode
            )
            created = True
    else:
        handle = _verified_windows_handle(candidate, root, desired_access, disposition, share_mode)
    try:
        if mode == "wb":
            _truncate_handle(handle)
        descriptor = msvcrt.open_osfhandle(handle, descriptor_flags | os.O_BINARY)
    except BaseException:
        if created:
            try:
                _mark_handle_for_delete(handle)
            except OSError:
                pass
        _close_handle(handle)
        raise
    return io.open(descriptor, descriptor_mode, closefd=True)


@contextmanager
def hold_managed_directory(path: Path, managed_root: Path | None = None):
    """Hold a verified directory identity so it cannot be replaced on Windows."""
    candidate = normalized_absolute_path(path)
    root = normalized_absolute_path(managed_root or candidate)
    validate_managed_directory(candidate, root)
    if os.name != "nt":
        yield candidate
        return

    handle = _create_file(
        candidate,
        _FILE_READ_ATTRIBUTES,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
    )
    try:
        information = _file_information(handle)
        if information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(f"Managed directory is a reparse point: {candidate}")
        expected_path = _normalized_requested_path(candidate)
        if _final_path_from_handle(handle) != expected_path:
            raise RuntimeError(f"Managed directory contains a reparse point: {candidate}")
        try:
            yield candidate
        finally:
            final_information = _file_information(handle)
            if (
                final_information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or _final_path_from_handle(handle) != expected_path
            ):
                raise RuntimeError(
                    f"Managed directory identity changed during operation: {candidate}"
                )
    finally:
        _close_handle(handle)


def delete_managed_file(
    path: Path,
    managed_root: Path = LIBRARY_DIR,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    """Permanently delete a verified managed regular file."""
    candidate = Path(path)
    root = Path(managed_root)
    if os.name != "nt":
        validated = validate_managed_write_path(candidate, root)
        if expected_sha256 is not None or expected_size is not None:
            with validated.open("rb") as source:
                digest = hashlib.sha256()
                total = 0
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    total += len(chunk)
            if expected_size is not None and total != expected_size:
                raise RuntimeError("Managed file size changed before deletion")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise RuntimeError("Managed file content changed before deletion")
        validated.unlink()
        return

    access = _DELETE | (_GENERIC_READ if expected_sha256 is not None or expected_size is not None else 0)
    handle = _verified_windows_handle(candidate, root, access, _OPEN_EXISTING, _FILE_SHARE_READ)
    try:
        if expected_sha256 is not None or expected_size is not None:
            digest, total = _hash_handle(handle)
            if expected_size is not None and total != expected_size:
                raise RuntimeError("Managed file size changed before deletion")
            if expected_sha256 is not None and digest != expected_sha256:
                raise RuntimeError("Managed file content changed before deletion")
        _mark_handle_for_delete(handle)
    finally:
        _close_handle(handle)


def recycle_managed_file(
    path: Path,
    managed_root: Path,
    recycler: Callable[[str], None],
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    """Recycle a verified copy, then delete the opened original identity.

    The staged file keeps the original filename inside a random sibling directory.
    If restored, startup reconciliation can find it inside the managed library.
    """
    candidate = Path(path)
    root = Path(managed_root)
    staging_dir = candidate.parent / f".clipsave-recycle-{uuid.uuid4().hex}"
    validate_managed_directory(staging_dir, root)
    staging_dir.mkdir()
    validate_managed_directory(staging_dir, root)
    staged = staging_dir / candidate.name

    def cleanup_staging() -> None:
        try:
            if staged.exists():
                delete_managed_file(staged, root)
        except (OSError, RuntimeError):
            pass
        try:
            staging_dir.rmdir()
        except OSError:
            pass

    if os.name != "nt":
        try:
            digest = hashlib.sha256()
            total = 0
            with open_managed_binary(candidate, "rb", root) as source, open_managed_binary(
                staged, "xb", root
            ) as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            if expected_size is not None and total != expected_size:
                raise RuntimeError("Managed file size changed before recycling")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise RuntimeError("Managed file content changed before recycling")
            recycler(str(staged))
            delete_managed_file(
                candidate,
                root,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        except BaseException:
            cleanup_staging()
            raise
        cleanup_staging()
        return

    handle = _verified_windows_handle(
        candidate,
        root,
        _GENERIC_READ | _DELETE,
        _OPEN_EXISTING,
        _FILE_SHARE_READ | _FILE_SHARE_DELETE,
    )
    descriptor = None
    handle_owned = True
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle_owned = False
        source = io.open(descriptor, "rb", closefd=True)
        descriptor = None
        native_handle = msvcrt.get_osfhandle(source.fileno())
        with source:
            digest = hashlib.sha256()
            total = 0
            with open_managed_binary(staged, "xb", root) as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            if expected_size is not None and total != expected_size:
                raise RuntimeError("Managed file size changed before recycling")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise RuntimeError("Managed file content changed before recycling")
            recycler(str(staged))
            _mark_handle_for_delete(native_handle)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        elif handle_owned:
            _close_handle(handle)
        cleanup_staging()
        raise
    cleanup_staging()


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction and is_junction():
            return True
        if os.name == "nt":
            get_attributes = _kernel32_function(
                "GetFileAttributesW", [wintypes.LPCWSTR], wintypes.DWORD
            )
            attributes = int(get_attributes(str(path)))
            if attributes != 0xFFFFFFFF and attributes & 0x400:
                return True
        return False
    except OSError:
        return True


def path_has_reparse_ancestor(path: Path, stop_at: Path | None = None) -> bool:
    candidate = normalized_absolute_path(path)
    stop = normalized_absolute_path(stop_at) if stop_at is not None else None
    while True:
        if candidate.exists() and _is_link_or_junction(candidate):
            return True
        if stop is not None and candidate == stop:
            return False
        parent = candidate.parent
        if parent == candidate:
            return stop is not None
        candidate = parent


def iter_safe_files(root: Path, suffixes: tuple[str, ...] | None = None) -> Iterator[Path]:
    root = Path(root)
    if not root.is_dir() or _is_link_or_junction(root):
        return
    normalized_suffixes = {value.lower() for value in suffixes} if suffixes else None
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if _is_link_or_junction(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False) and (
                    normalized_suffixes is None or path.suffix.lower() in normalized_suffixes
                ):
                    yield path
            except OSError:
                continue


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _is_remote_or_unc_path(path: Path) -> bool:
    path = Path(path)
    if str(path).startswith(("\\\\", "//")):
        return True
    if os.name != "nt" or not path.drive:
        return False
    try:
        root = f"{path.drive}\\"
        get_drive_type = _kernel32_function(
            "GetDriveTypeW", [wintypes.LPCWSTR], wintypes.UINT
        )
        return int(get_drive_type(root)) == 4  # DRIVE_REMOTE
    except (AttributeError, OSError, TypeError, ValueError):
        return True


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first_resolved = _resolved(first)
        second_resolved = _resolved(second)
        return (
            first_resolved == second_resolved
            or first_resolved in second_resolved.parents
            or second_resolved in first_resolved.parents
        )
    except (OSError, RuntimeError):
        return True


def _delete_source_if_identical(
    source: Path, destination: Path, source_root: Path, destination_root: Path
) -> bool:
    if os.name != "nt":
        source_stat = source.stat()
        destination_stat = destination.stat()
        if source_stat.st_size != destination_stat.st_size:
            return False
        with source.open("rb") as source_handle, destination.open("rb") as destination_handle:
            source_digest = hashlib.sha256()
            destination_digest = hashlib.sha256()
            while source_chunk := source_handle.read(1024 * 1024):
                source_digest.update(source_chunk)
            while destination_chunk := destination_handle.read(1024 * 1024):
                destination_digest.update(destination_chunk)
        if source_digest.digest() != destination_digest.digest():
            return False
        current = source.stat()
        if (
            current.st_size != source_stat.st_size
            or current.st_mtime_ns != source_stat.st_mtime_ns
            or getattr(current, "st_ino", None) != getattr(source_stat, "st_ino", None)
        ):
            return False
        source.unlink()
        return True

    source_handle = _verified_windows_handle(
        source,
        source_root,
        _GENERIC_READ | _DELETE,
        _OPEN_EXISTING,
        _FILE_SHARE_READ,
    )
    try:
        destination_handle = _verified_windows_handle(
            destination,
            destination_root,
            _GENERIC_READ,
            _OPEN_EXISTING,
            _FILE_SHARE_READ,
        )
        try:
            source_digest, source_size = _hash_handle(source_handle)
            destination_digest, destination_size = _hash_handle(destination_handle)
            if source_size != destination_size or source_digest != destination_digest:
                return False
            _mark_handle_for_delete(source_handle)
            return True
        finally:
            _close_handle(destination_handle)
    finally:
        _close_handle(source_handle)


def _copy_verify_delete_file(
    source: Path, destination: Path, source_root: Path, destination_root: Path
) -> bool:
    return _run_copy_verify_delete_file(
        source,
        destination,
        source_root,
        destination_root,
        _migration_ops(),
    )


def _copy_or_move_contents(source: Path, target: Path) -> int:
    return _run_copy_or_move_contents(source, target, _migration_ops())


def _copy_legacy_database_snapshot(source: Path, destination: Path) -> None:
    validate_managed_write_path(destination, destination.parent)
    temporary = destination.with_name(
        f".{destination.name}.migration-{uuid.uuid4().hex}.tmp"
    )
    validate_managed_write_path(temporary, destination.parent)
    copy_database_snapshot(source, destination, temporary)


def _path_key(path: Path | str) -> str:
    value = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(value)))


def _migration_ops() -> StorageMigrationOps:
    return StorageMigrationOps(
        is_link_or_junction=_is_link_or_junction,
        paths_overlap=_paths_overlap,
        open_managed_binary=open_managed_binary,
        delete_managed_file=delete_managed_file,
        delete_source_if_identical=_delete_source_if_identical,
        copy_legacy_database_snapshot=_copy_legacy_database_snapshot,
    )


def migrate_legacy_layout(paths: AppPaths | None = None) -> dict[str, int]:
    if paths is None:
        migration_paths = StorageMigrationPaths(
            base_dir=BASE_DIR,
            legacy_data_dir=LEGACY_DATA_DIR,
            legacy_picture_dir=LEGACY_PICTURE_DIR,
            legacy_markdown_dir=LEGACY_MARKDOWN_DIR,
            data_dir=DATA_DIR,
            library_dir=LIBRARY_DIR,
            picture_dir=PICTURE_DIR,
            markdown_dir=MARKDOWN_DIR,
        )
    else:
        migration_paths = StorageMigrationPaths(
            base_dir=paths.base_dir,
            legacy_data_dir=paths.legacy_data_dir,
            legacy_picture_dir=paths.legacy_picture_dir,
            legacy_markdown_dir=paths.legacy_markdown_dir,
            data_dir=paths.data_dir,
            library_dir=paths.library_dir,
            picture_dir=paths.picture_dir,
            markdown_dir=paths.markdown_dir,
        )
    return _run_storage_migration(
        migration_paths,
        _migration_ops(),
    )


def validate_storage_layout(paths: AppPaths | None = None) -> None:
    if paths is None:
        local_root = LOCAL_ROOT
        layout_paths = (
            LOCAL_ROOT,
            DATA_DIR,
            LIBRARY_DIR,
            PICTURE_DIR,
            MARKDOWN_DIR,
            USAGE_DIR,
            MAINTENANCE_DIR,
        )
    else:
        local_root = paths.local_root
        layout_paths = (
            paths.local_root,
            paths.data_dir,
            paths.library_dir,
            paths.picture_dir,
            paths.markdown_dir,
            paths.usage_dir,
            paths.maintenance_dir,
        )
    if _is_remote_or_unc_path(local_root):
        raise RuntimeError(
            f"ClipSave local storage cannot use a network path: {local_root}"
        )
    if path_has_reparse_ancestor(local_root):
        raise RuntimeError(
            f"ClipSave local storage cannot be below a symlink or Junction: {local_root}"
        )
    for path in layout_paths:
        if _is_link_or_junction(path):
            raise RuntimeError(f"ClipSave 本地存储路径不能是符号链接或 Junction：{path}")
    root = Path(os.path.abspath(local_root))
    for path in layout_paths[1:]:
        try:
            Path(os.path.abspath(path)).relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"ClipSave 本地存储路径超出预期目录：{path}") from exc


def ensure_storage_directories(paths: AppPaths | None = None) -> None:
    validate_storage_layout(paths)
    directories = (
        (DATA_DIR, LIBRARY_DIR, PICTURE_DIR, MARKDOWN_DIR, USAGE_DIR, MAINTENANCE_DIR)
        if paths is None
        else (
            paths.data_dir,
            paths.library_dir,
            paths.picture_dir,
            paths.markdown_dir,
            paths.usage_dir,
            paths.maintenance_dir,
        )
    )
    for path in directories:
        path.mkdir(parents=True, exist_ok=True)
    validate_storage_layout(paths)


def is_under_local_store(path: Path, library_dir: Path | None = None) -> bool:
    try:
        root = normalized_absolute_path(LIBRARY_DIR if library_dir is None else library_dir)
        candidate = normalized_absolute_path(path)
        relative = candidate.relative_to(root)
        current = root
        if _is_link_or_junction(current):
            return False
        for part in relative.parts:
            current = current / part
            if _is_link_or_junction(current):
                return False
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def validate_managed_write_path(path: Path, managed_root: Path = LIBRARY_DIR) -> Path:
    candidate = normalized_absolute_path(path)
    root = normalized_absolute_path(managed_root)
    try:
        candidate.parent.relative_to(root)
        candidate.parent.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError(f"Write target is outside the managed local library: {candidate}")
    if path_has_reparse_ancestor(candidate.parent, root):
        raise RuntimeError(f"Write target contains a reparse point: {candidate}")
    if candidate.exists():
        if _is_link_or_junction(candidate):
            raise RuntimeError(f"Write target is a reparse point: {candidate}")
        try:
            if candidate.stat().st_nlink != 1:
                raise RuntimeError(f"Write target has multiple hard links: {candidate}")
        except OSError as exc:
            raise RuntimeError(f"Write target cannot be validated: {candidate}") from exc
    return candidate


def validate_managed_directory(path: Path, managed_root: Path) -> Path:
    candidate = normalized_absolute_path(path)
    root = normalized_absolute_path(managed_root)
    try:
        candidate.relative_to(root)
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Managed directory is outside its local root: {candidate}") from exc
    if path_has_reparse_ancestor(candidate, root):
        raise RuntimeError(f"Managed directory contains a reparse point: {candidate}")
    return candidate

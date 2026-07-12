from __future__ import annotations

import ctypes
import hashlib
import io
import ntpath
import os
import shutil
import sqlite3
import stat
import uuid
from ctypes import wintypes
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable

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
    THUMB_DIR,
)


if os.name == "nt":
    import msvcrt
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _KERNEL32 = None


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_NAME_NORMALIZED = 0x0
_VOLUME_NAME_DOS = 0x0
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOL)]


def _kernel32_function(name: str, argtypes: list[object], restype: object):
    if _KERNEL32 is None:
        raise OSError("Windows handle APIs are unavailable")
    function = getattr(_KERNEL32, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _create_file(
    path: Path,
    desired_access: int,
    creation_disposition: int,
    flags: int = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
    share_mode: int = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
) -> int:
    create_file = _kernel32_function(
        "CreateFileW",
        [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ],
        wintypes.HANDLE,
    )
    handle = create_file(
        str(path),
        desired_access,
        share_mode,
        None,
        creation_disposition,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close_handle(handle: int) -> None:
    close_handle = _kernel32_function("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    close_handle(handle)


def _final_path_from_handle(handle: int) -> str:
    get_final_path = _kernel32_function(
        "GetFinalPathNameByHandleW",
        [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD],
        wintypes.DWORD,
    )
    required = get_final_path(handle, None, 0, _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS)
    if not required:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final_path(handle, buffer, len(buffer), _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS)
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.abspath(path))


def _file_information(handle: int) -> _ByHandleFileInformation:
    get_information = _kernel32_function(
        "GetFileInformationByHandle",
        [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)],
        wintypes.BOOL,
    )
    information = _ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return information


def _truncate_handle(handle: int) -> None:
    set_file_pointer = _kernel32_function(
        "SetFilePointerEx",
        [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD],
        wintypes.BOOL,
    )
    set_end_of_file = _kernel32_function("SetEndOfFile", [wintypes.HANDLE], wintypes.BOOL)
    if not set_file_pointer(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not set_end_of_file(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _hash_handle(handle: int) -> tuple[str, int]:
    set_file_pointer = _kernel32_function(
        "SetFilePointerEx",
        [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD],
        wintypes.BOOL,
    )
    read_file = _kernel32_function(
        "ReadFile",
        [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID],
        wintypes.BOOL,
    )
    if not set_file_pointer(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    digest = hashlib.sha256()
    total = 0
    buffer = ctypes.create_string_buffer(1024 * 1024)
    while True:
        amount = wintypes.DWORD()
        if not read_file(handle, buffer, len(buffer), ctypes.byref(amount), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if amount.value == 0:
            break
        digest.update(buffer.raw[: amount.value])
        total += amount.value
    return digest.hexdigest(), total


def _mark_handle_for_delete(handle: int) -> None:
    set_information = _kernel32_function(
        "SetFileInformationByHandle",
        [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
        wintypes.BOOL,
    )
    disposition = _FileDispositionInfo(True)
    if not set_information(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _verified_windows_handle(
    path: Path,
    managed_root: Path,
    desired_access: int,
    disposition: int,
    share_mode: int = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
) -> int:
    root = Path(os.path.abspath(managed_root))
    candidate = Path(os.path.abspath(path))
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
        requested_root = ntpath.normcase(ntpath.abspath(str(root)))
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
    handle = _verified_windows_handle(candidate, root, desired_access, disposition, share_mode)
    try:
        if mode == "wb":
            _truncate_handle(handle)
        descriptor = msvcrt.open_osfhandle(handle, descriptor_flags | os.O_BINARY)
    except BaseException:
        _close_handle(handle)
        raise
    return io.open(descriptor, descriptor_mode, closefd=True)


@contextmanager
def hold_managed_directory(path: Path, managed_root: Path | None = None):
    """Hold a verified directory identity so it cannot be replaced on Windows."""
    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(managed_root or candidate))
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
        expected_path = ntpath.normcase(ntpath.abspath(str(candidate)))
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


def verify_managed_file(
    path: Path,
    managed_root: Path = LIBRARY_DIR,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    with open_managed_binary(path, "rb", managed_root) as source:
        digest = hashlib.sha256()
        total = 0
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
    if expected_size is not None and total != expected_size:
        raise RuntimeError("Managed file size changed before operation")
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise RuntimeError("Managed file content changed before operation")


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
            get_attributes = ctypes.windll.kernel32.GetFileAttributesW
            get_attributes.argtypes = [wintypes.LPCWSTR]
            get_attributes.restype = wintypes.DWORD
            attributes = int(get_attributes(str(path)))
            if attributes != 0xFFFFFFFF and attributes & 0x400:
                return True
        return False
    except OSError:
        return True


def path_has_reparse_ancestor(path: Path, stop_at: Path | None = None) -> bool:
    candidate = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(stop_at)) if stop_at is not None else None
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
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = [wintypes.LPCWSTR]
        get_drive_type.restype = wintypes.UINT
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
    if _is_link_or_junction(source) or _is_link_or_junction(destination):
        return False
    copied_digest = hashlib.sha256()
    copied_size = 0
    created_destination = False
    try:
        with open_managed_binary(
            source, "rb", source_root, identity_locked=True
        ) as source_handle, open_managed_binary(
            destination, "xb", destination_root
        ) as destination_handle:
            created_destination = True
            source_stat = os.fstat(source_handle.fileno())
            if not stat.S_ISREG(source_stat.st_mode):
                raise RuntimeError(f"Migration source is not a regular file: {source}")
            while chunk := source_handle.read(1024 * 1024):
                destination_handle.write(chunk)
                copied_digest.update(chunk)
                copied_size += len(chunk)
        try:
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError:
            pass
        if _delete_source_if_identical(source, destination, source_root, destination_root):
            return True
    except FileExistsError:
        return _delete_source_if_identical(
            source, destination, source_root, destination_root
        )
    except (OSError, RuntimeError):
        if created_destination:
            try:
                delete_managed_file(
                    destination,
                    destination_root,
                    expected_sha256=copied_digest.hexdigest(),
                    expected_size=copied_size,
                )
            except (OSError, RuntimeError):
                pass
        return False
    return False


def _copy_or_move_contents(source: Path, target: Path) -> int:
    if _is_link_or_junction(source) or _is_link_or_junction(target) or _paths_overlap(source, target):
        return 0
    if not source.exists() or not source.is_dir():
        return 0
    if target.exists() and not target.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(target):
        return 0
    moved = 0
    for child in list(source.iterdir()):
        if _is_link_or_junction(child):
            continue
        destination = target / child.name
        if _is_link_or_junction(destination):
            continue
        if destination.exists():
            if child.is_dir():
                moved += _copy_or_move_contents(child, destination)
                try:
                    child.rmdir()
                except OSError:
                    pass
            else:
                if destination.is_file() and _delete_source_if_identical(
                    child, destination, source, target
                ):
                    moved += 1
                    continue
                # Keep both files if a same-named file already exists.
                stem, suffix = child.stem, child.suffix
                index = 2
                while destination.exists():
                    destination = target / f"{stem} (迁移 {index}){suffix}"
                    index += 1
                if _copy_verify_delete_file(child, destination, source, target):
                    moved += 1
        else:
            if child.is_dir():
                moved += _copy_or_move_contents(child, destination)
            elif _copy_verify_delete_file(child, destination, source, target):
                moved += 1
    try:
        source.rmdir()
    except OSError:
        pass
    return moved


def migrate_legacy_layout() -> dict[str, int]:
    """Move the first-generation local store out of the install directory.

    The operation is local-only and preserves every source file. Existing
    database rows are repaired by the normal content-hash scan afterward.
    """
    result = {"pictures": 0, "markdown": 0, "data": 0}
    legacy_database = LEGACY_DATA_DIR / "clipsave.db"
    active_database = DATA_DIR / "clipsave.db"
    if legacy_database.is_file() and active_database.is_file():
        identical = False
        try:
            def logical_hash(path: Path) -> str:
                digest = hashlib.sha256()
                connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
                try:
                    digest.update(
                        str(connection.execute("PRAGMA user_version").fetchone()[0]).encode("ascii")
                    )
                    for statement in connection.iterdump():
                        digest.update(statement.encode("utf-8"))
                        digest.update(b"\n")
                finally:
                    connection.close()
                return digest.hexdigest()

            identical = logical_hash(legacy_database) == logical_hash(active_database)
        except (OSError, sqlite3.Error):
            identical = False
        if not identical:
            raise RuntimeError(
                "Both the legacy and current ClipSave databases exist. "
                f"They were left unchanged to prevent history loss: {legacy_database} ; {active_database}"
            )
        preserved = legacy_database.with_name(f"{legacy_database.name}.migrated-duplicate")
        index = 2
        while preserved.exists():
            preserved = legacy_database.with_name(
                f"{legacy_database.name}.migrated-duplicate-{index}"
            )
            index += 1
        moves = [(legacy_database, preserved)]
        for suffix in ("-wal", "-shm"):
            source = Path(f"{legacy_database}{suffix}")
            if source.exists():
                moves.append((source, Path(f"{preserved}{suffix}")))
        completed: list[tuple[Path, Path]] = []
        try:
            for source, destination in moves:
                os.replace(source, destination)
                completed.append((source, destination))
        except OSError:
            for source, destination in reversed(completed):
                try:
                    os.replace(destination, source)
                except OSError:
                    pass
            raise
    if _is_link_or_junction(LIBRARY_DIR) or _is_link_or_junction(DATA_DIR):
        return result
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(LIBRARY_DIR) or _is_link_or_junction(DATA_DIR):
        return result
    result["pictures"] = _copy_or_move_contents(LEGACY_PICTURE_DIR, PICTURE_DIR)
    result["markdown"] = _copy_or_move_contents(LEGACY_MARKDOWN_DIR, MARKDOWN_DIR)
    result["data"] = _copy_or_move_contents(LEGACY_DATA_DIR, DATA_DIR)
    legacy_history = BASE_DIR / "clipsave_history.json"
    history_target = DATA_DIR / legacy_history.name
    if (
        legacy_history.exists()
        and not _is_link_or_junction(legacy_history)
        and not _is_link_or_junction(history_target)
        and not _paths_overlap(legacy_history, history_target)
        and not history_target.exists()
    ):
        if _copy_verify_delete_file(legacy_history, history_target, BASE_DIR, DATA_DIR):
            result["data"] += 1
    return result


def validate_storage_layout() -> None:
    paths = (LOCAL_ROOT, DATA_DIR, LIBRARY_DIR, PICTURE_DIR, MARKDOWN_DIR, THUMB_DIR, MAINTENANCE_DIR)
    if _is_remote_or_unc_path(LOCAL_ROOT):
        raise RuntimeError(f"ClipSave local storage cannot use a network path: {LOCAL_ROOT}")
    if path_has_reparse_ancestor(LOCAL_ROOT):
        raise RuntimeError(f"ClipSave local storage cannot be below a symlink or Junction: {LOCAL_ROOT}")
    for path in paths:
        if _is_link_or_junction(path):
            raise RuntimeError(f"ClipSave 本地存储路径不能是符号链接或 Junction：{path}")
    root = Path(os.path.abspath(LOCAL_ROOT))
    for path in paths[1:]:
        try:
            Path(os.path.abspath(path)).relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"ClipSave 本地存储路径超出预期目录：{path}") from exc


def ensure_storage_directories() -> None:
    validate_storage_layout()
    for path in (DATA_DIR, LIBRARY_DIR, PICTURE_DIR, MARKDOWN_DIR, THUMB_DIR, MAINTENANCE_DIR):
        path.mkdir(parents=True, exist_ok=True)
    validate_storage_layout()


def is_under_local_store(path: Path) -> bool:
    try:
        root = Path(os.path.abspath(LIBRARY_DIR))
        candidate = Path(os.path.abspath(path))
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
    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(managed_root))
    try:
        candidate.parent.relative_to(root)
        candidate.parent.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
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
    candidate = Path(os.path.abspath(path))
    root = Path(os.path.abspath(managed_root))
    try:
        candidate.relative_to(root)
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Managed directory is outside its local root: {candidate}") from exc
    if path_has_reparse_ancestor(candidate, root):
        raise RuntimeError(f"Managed directory contains a reparse point: {candidate}")
    return candidate

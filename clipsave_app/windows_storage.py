from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
from ctypes import wintypes
from pathlib import Path


if os.name == "nt":
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


def _long_requested_path(path: Path) -> str:
    """Expand short names in an existing path without resolving reparse points."""
    requested = ntpath.abspath(str(path))
    if os.name != "nt":
        return requested
    get_long_path = _kernel32_function(
        "GetLongPathNameW",
        [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD],
        wintypes.DWORD,
    )
    required = get_long_path(requested, None, 0)
    if not required:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_long_path(requested, buffer, len(buffer))
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return ntpath.abspath(buffer.value)


def _normalized_requested_path(path: Path) -> str:
    return ntpath.normcase(_long_requested_path(path))


def normalized_absolute_path(path: Path) -> Path:
    """Expand Windows short names while preserving a non-existent leaf suffix."""
    candidate = Path(os.path.abspath(path))
    if os.name != "nt":
        return candidate
    existing = candidate
    suffix: list[str] = []
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            return candidate
        suffix.append(existing.name)
        existing = parent
    normalized = Path(_long_requested_path(existing))
    return normalized.joinpath(*reversed(suffix))


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


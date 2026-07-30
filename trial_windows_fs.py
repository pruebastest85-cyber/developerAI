"""Private handle-relative Windows filesystem primitives for trial IPC.

This module intentionally exposes no general-purpose path API.  Every child
object is opened relative to an already-open directory handle through
NtCreateFile, and rename/delete operations act on the opened object handle.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
SYNCHRONIZE = 0x00100000
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_ATTRIBUTES = 0x0100

FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4

FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3

FILE_DIRECTORY_FILE = 0x00000001
FILE_WRITE_THROUGH = 0x00000002
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

FILE_RENAME_INFO_EX = 22
FILE_DISPOSITION_INFO_EX = 21
FILE_RENAME_REPLACE_IF_EXISTS = 0x1
FILE_RENAME_POSIX_SEMANTICS = 0x2
FILE_DISPOSITION_DELETE = 0x1
FILE_DISPOSITION_POSIX_SEMANTICS = 0x2
FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x10

FILE_ID_BOTH_DIR_INFO = 10
ERROR_NO_MORE_FILES = 18


class NativeFileError(OSError):
    pass


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_ssize_t),
        ("Information", ctypes.c_size_t),
    ]


class _FileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD)]


class _FileRenameHeader(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
    ]


def _kernel():
    kernel = ctypes.windll.kernel32
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel.FlushFileBuffers.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    return kernel


def _raise_last_error():
    raise NativeFileError(ctypes.windll.kernel32.GetLastError())


def close(handle):
    if handle not in {None, INVALID_HANDLE_VALUE}:
        if not _kernel().CloseHandle(handle):
            _raise_last_error()


def open_relative(
    root_handle,
    name,
    *,
    directory=False,
    create=False,
    exclusive=False,
    read=False,
    write=False,
    delete=False,
):
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "\\" in name
        or "/" in name
        or "\x00" in name
    ):
        raise NativeFileError("invalid relative name")
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UnicodeString(
        len(name.encode("utf-16-le")),
        len(name.encode("utf-16-le")) + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        root_handle,
        ctypes.pointer(unicode_name),
        0x40,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    desired = SYNCHRONIZE | READ_CONTROL | FILE_READ_ATTRIBUTES
    if read or directory:
        desired |= FILE_READ_DATA
    if write:
        desired |= FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES
    if create:
        desired |= WRITE_DAC
    if delete:
        desired |= DELETE
    disposition = (
        FILE_CREATE if exclusive else FILE_OPEN_IF if create else FILE_OPEN
    )
    options = (
        FILE_SYNCHRONOUS_IO_NONALERT
        | FILE_OPEN_REPARSE_POINT
        | (FILE_DIRECTORY_FILE if directory else FILE_NON_DIRECTORY_FILE)
    )
    handle = wintypes.HANDLE()
    iosb = _IoStatusBlock()
    ntcreate = ctypes.windll.ntdll.NtCreateFile
    ntcreate.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong), wintypes.ULONG, wintypes.ULONG,
        wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG,
    ]
    ntcreate.restype = ctypes.c_long
    status = ntcreate(
        ctypes.byref(handle), desired, ctypes.byref(attributes),
        ctypes.byref(iosb), None, 0,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        disposition, options, None, 0,
    )
    if status < 0 or handle.value in {None, INVALID_HANDLE_VALUE}:
        raise NativeFileError(f"ntstatus:{status & 0xffffffff:08x}")
    return handle.value


def read_all(handle, limit):
    chunks = []
    total = 0
    while True:
        size = min(65536, limit + 1 - total)
        buffer = ctypes.create_string_buffer(size)
        received = wintypes.DWORD()
        if not _kernel().ReadFile(
            handle, buffer, size, ctypes.byref(received), None
        ):
            error = ctypes.windll.kernel32.GetLastError()
            if error == 38:  # ERROR_HANDLE_EOF
                break
            _raise_last_error()
        if received.value == 0:
            break
        chunks.append(buffer.raw[:received.value])
        total += received.value
        if total > limit:
            raise NativeFileError("size limit")
    return b"".join(chunks)


def write_all(handle, value):
    offset = 0
    while offset < len(value):
        chunk = value[offset:offset + 65536]
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(chunk)
        if not _kernel().WriteFile(
            handle, buffer, len(chunk), ctypes.byref(written), None
        ):
            _raise_last_error()
        if written.value <= 0:
            raise NativeFileError("short write")
        offset += written.value


def flush(handle):
    if not _kernel().FlushFileBuffers(handle):
        _raise_last_error()


def rename(handle, root_handle, destination, *, replace):
    encoded = destination.encode("utf-16-le")
    header_size = _FileRenameHeader.FileNameLength.offset + 4
    buffer = ctypes.create_string_buffer(header_size + len(encoded))
    header = _FileRenameHeader.from_buffer(buffer)
    header.Flags = FILE_RENAME_REPLACE_IF_EXISTS if replace else 0
    header.RootDirectory = root_handle
    header.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + header_size, encoded, len(encoded))
    iosb = _IoStatusBlock()
    ntset = ctypes.windll.ntdll.NtSetInformationFile
    ntset.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG,
    ]
    ntset.restype = ctypes.c_long
    # FileRenameInformationEx (65) accepts RootDirectory-relative names.
    status = ntset(
        handle, ctypes.byref(iosb), buffer, len(buffer), 65
    )
    if status < 0:
        raise NativeFileError(f"ntstatus:{status & 0xffffffff:08x}")


def delete(handle):
    information = _FileDispositionInfoEx(
        FILE_DISPOSITION_DELETE
        | FILE_DISPOSITION_POSIX_SEMANTICS
        | FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
    )
    if not _kernel().SetFileInformationByHandle(
        handle, FILE_DISPOSITION_INFO_EX,
        ctypes.byref(information), ctypes.sizeof(information),
    ):
        _raise_last_error()


def entries(directory_handle):
    """Return direct entry names and attributes from the opened directory."""
    buffer = ctypes.create_string_buffer(64 * 1024)
    result = []
    restart = True
    while True:
        if not _kernel().GetFileInformationByHandleEx(
            directory_handle, FILE_ID_BOTH_DIR_INFO,
            buffer, len(buffer),
        ):
            error = ctypes.windll.kernel32.GetLastError()
            if error == ERROR_NO_MORE_FILES:
                break
            _raise_last_error()
        offset = 0
        while True:
            base = ctypes.addressof(buffer) + offset
            next_offset = ctypes.c_ulong.from_address(base).value
            attributes = ctypes.c_ulong.from_address(base + 56).value
            name_length = ctypes.c_ulong.from_address(base + 60).value
            name = ctypes.wstring_at(base + 104, name_length // 2)
            if name not in {".", ".."}:
                result.append((name, attributes))
            if next_offset == 0:
                break
            offset += next_offset
        restart = False
    return result

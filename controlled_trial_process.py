"""Detached owner and closed local IPC for controlled real-model trials.

Public evidence is observational only.  The live process retains the operator,
approval controllers, callbacks and every other executable capability.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import trial_windows_fs as _winfs

from brain.programming_operator import create_operator_from_env
from brain.programming_operator import ProgrammingOperatorView
from controlled_trial_harness import (
    ControlledTrialHarness,
    TrialEvidenceError,
    evidence_to_view,
    view_to_evidence,
)


_ROOT_PREFIX = "developerai-real-trial-"
_OWNER_ID = re.compile(r"^otr_[0-9a-f]{32}$", re.ASCII)
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$", re.ASCII)
_ACTIONS = frozenset({
    "approve_plan", "reject_plan", "cancel_plan",
    "approve_operation", "reject_operation", "cancel_operation",
})
_STATUS_FIELDS = frozenset({
    "version", "owner_runtime_id", "state", "pid", "heartbeat_ns",
    "session_id", "workflow_runtime_id", "plan_id", "approval_request_id",
    "error_code", "evidence_path", "last_sequence", "transport_calls",
})
_COMMAND_FIELDS = frozenset({
    "version", "sequence", "owner_runtime_id", "session_id",
    "workflow_runtime_id", "plan_id", "expected_state", "action",
    "request_id", "auth",
})
_SPEC_FIELDS = frozenset({
    "version", "owner_runtime_id", "source_repository", "evidence_path",
    "root_device", "root_inode",
})
_STATES = frozenset({
    "starting", "planning", "pending_plan", "running", "awaiting_approval",
    "awaiting_correction", "completed", "failed", "rejected", "cancelled",
    "lost", "stopped",
})
_LOCAL_PROCESSES = {}
_CAPABILITY_BYTES = 32
_CAPABILITY_FILE = "owner-capability.bin"
_CURRENT_USER_SID = None
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_OWNER_RIGHTS_SID = "S-1-3-4"


class TrialProcessError(RuntimeError):
    """Closed lifecycle/IPC error."""

    __slots__ = ("code",)

    def __init__(self, code="trial_process_failed"):
        allowed = {
            "trial_process_failed", "invalid_trial_root", "invalid_status",
            "invalid_command", "stale_command", "duplicate_command",
            "owner_not_alive", "owner_start_failed", "client_timeout",
            "evidence_unavailable",
        }
        self.code = code if code in allowed else "trial_process_failed"
        super().__init__(f"Fallo cerrado del proceso de ensayo: {self.code}.")


def _open_windows_directory(path: Path):
    """Open a directory handle by pathname, for identity derivation only."""
    kernel = ctypes.windll.kernel32
    create_file = kernel.CreateFileW
    create_file.restype = ctypes.c_void_p
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    handle = create_file(
        str(path),
        0x0001 | 0x0080 | 0x00010000 | 0x00020000,
        0x1 | 0x2 | 0x4,
        None,
        3,                 # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS|OPEN_REPARSE
        None,
    )
    if handle in {None, ctypes.c_void_p(-1).value}:
        raise OSError("CreateFileW failed")
    return handle


def _close_windows_handle(handle) -> None:
    if handle in {None, ctypes.c_void_p(-1).value}:
        return
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(handle)


def _path_identity(path: Path):
    """Directory identity, always derived from a single source.

    On Windows this opens a handle instead of calling os.stat. Python's
    st_dev is not guaranteed to hold the same value as the volume serial
    reported by GetFileInformationByHandle, and mixing both sources is
    exactly what forced every comparison to ignore the volume and trust
    only the MFT index -- which is unique per volume, not per machine.
    """
    if os.name == "nt":
        try:
            handle = _open_windows_directory(path)
        except OSError as error:
            raise TrialProcessError("invalid_trial_root") from error
        try:
            return _windows_handle_identity(handle)[0]
        finally:
            _close_windows_handle(handle)
    try:
        value = path.stat()
    except OSError as error:
        raise TrialProcessError("invalid_trial_root") from error
    return value.st_dev, value.st_ino


def _trial_root(path, expected_identity=None) -> Path:
    supplied = Path(path).absolute()
    try:
        resolved = supplied.resolve(strict=True)
        temporary = Path(tempfile.gettempdir()).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TrialProcessError("invalid_trial_root") from error
    if (
        not resolved.is_dir()
        or resolved.parent != temporary
        or not resolved.name.startswith(_ROOT_PREFIX)
        or expected_identity is not None
        and (
            _path_identity(resolved) != expected_identity
            if os.name == "nt"
            else _path_identity(resolved) != expected_identity
        )
    ):
        raise TrialProcessError("invalid_trial_root")
    return resolved


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_ulong),
        ("creation_low", ctypes.c_ulong),
        ("creation_high", ctypes.c_ulong),
        ("access_low", ctypes.c_ulong),
        ("access_high", ctypes.c_ulong),
        ("write_low", ctypes.c_ulong),
        ("write_high", ctypes.c_ulong),
        ("volume_serial", ctypes.c_ulong),
        ("size_high", ctypes.c_ulong),
        ("size_low", ctypes.c_ulong),
        ("links", ctypes.c_ulong),
        ("index_high", ctypes.c_ulong),
        ("index_low", ctypes.c_ulong),
    ]


def _windows_handle_identity(handle):
    information = _ByHandleFileInformation()
    kernel = ctypes.windll.kernel32
    kernel.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)
    ]
    kernel.GetFileInformationByHandle.restype = ctypes.c_int
    if not kernel.GetFileInformationByHandle(
        handle, ctypes.byref(information)
    ):
        raise TrialProcessError("invalid_trial_root")
    return (
        information.volume_serial,
        information.index_high << 32 | information.index_low,
    ), information.attributes


class _StableRoot:
    """Own the directory handle used by every IPC filesystem operation."""

    __slots__ = ("path", "identity", "_handle")

    def __init__(self, path, expected_identity=None):
        self.path = _trial_root(path, expected_identity)
        # On Windows the identity is taken from the real handle below, so
        # deriving it here from the pathname would open a second handle
        # and throw the result away.
        self.identity = None if os.name == "nt" else _path_identity(self.path)
        self._handle = None
        try:
            if os.name == "nt":
                kernel = ctypes.windll.kernel32
                create_file = kernel.CreateFileW
                create_file.restype = ctypes.c_void_p
                create_file.argtypes = [
                    ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                    ctypes.c_void_p,
                ]
                handle = create_file(
                    str(self.path),
                    0x0001 | 0x0080 | 0x00010000 | 0x00020000,
                    0x1 | 0x2 | 0x4,
                    None,
                    3,                 # OPEN_EXISTING
                    0x02000000 | 0x00200000,  # BACKUP_SEMANTICS|OPEN_REPARSE
                    None,
                )
                if handle in {None, ctypes.c_void_p(-1).value}:
                    raise OSError
                self._handle = handle
                handle_identity, _attributes = _windows_handle_identity(handle)
                if (
                    expected_identity is not None
                    and handle_identity != expected_identity
                ):
                    raise TrialProcessError("invalid_trial_root")
                self.identity = handle_identity
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                self._handle = os.open(self.path, flags)
            self.verify()
        except (OSError, TrialProcessError) as error:
            self.close()
            raise TrialProcessError("invalid_trial_root") from error

    def verify(self):
        if os.name == "nt":
            identity, attributes = _windows_handle_identity(self._handle)
            if (
                identity != self.identity
                or attributes
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise TrialProcessError("invalid_trial_root")
            _verify_windows_security_handle(self._handle)
        else:
            value = os.fstat(self._handle)
            if (
                (value.st_dev, value.st_ino) != self.identity
                or
                value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) != 0o700
            ):
                raise TrialProcessError("invalid_trial_root")
        return self

    def open_child(
        self, name, *, directory=False, create=False, exclusive=False,
        read=False, write=False, delete=False,
    ):
        self.verify()
        if os.name == "nt":
            return _winfs.open_relative(
                self._handle, name, directory=directory, create=create,
                exclusive=exclusive, read=read, write=write, delete=delete,
            )
        flags = getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_DIRECTORY if directory else 0
        flags |= os.O_RDWR if read and write else os.O_WRONLY if write else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        return os.open(name, flags, 0o600, dir_fd=self._handle)

    def close_child(self, handle):
        if os.name == "nt":
            _winfs.close(handle)
        else:
            os.close(handle)

    def file_exists(self, name):
        """Answer existence only.

        A hostile or malformed entry must never be reported as a fatal
        condition here: content and ACL validation belong to the read
        path, whose errors the owner loop treats as a rejected command.
        """
        handle = None
        try:
            handle = self.open_child(name, read=True)
            _validate_open_file(handle, require_protected=False)
            return True
        except (FileNotFoundError, _winfs.NativeFileError):
            return False
        # A TrialProcessError here means the entry exists but is not a
        # legitimate regular file (reparse point, directory, hostile
        # ACL). It must NOT be reported as absent: the owner loop treats
        # it as a rejected command, deletes it and keeps the session.
        # Swallowing it would leave a hostile entry invisible forever.
        finally:
            if handle is not None:
                self.close_child(handle)

    def delete_file(self, name):
        handle = self.open_child(name, delete=True)
        try:
            _validate_open_file(handle, require_protected=False)
            if os.name == "nt":
                _winfs.delete(handle)
            else:
                os.unlink(name, dir_fd=self._handle)
        finally:
            self.close_child(handle)

    def close(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if os.name == "nt":
            close_handle = ctypes.windll.kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(handle)
        else:
            os.close(handle)


def _sid_string(sid_pointer) -> str:
    converted = ctypes.c_wchar_p()
    advapi = ctypes.windll.advapi32
    if not advapi.ConvertSidToStringSidW(
        sid_pointer, ctypes.byref(converted)
    ):
        raise TrialProcessError("invalid_trial_root")
    try:
        return converted.value
    finally:
        ctypes.windll.kernel32.LocalFree(converted)


def _current_user_sid() -> str:
    global _CURRENT_USER_SID
    if _CURRENT_USER_SID is not None:
        return _CURRENT_USER_SID
    try:
        output = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="strict", timeout=10,
        ).stdout
        rows = list(csv.reader(output.splitlines()))
        if (
            len(rows) != 1
            or len(rows[0]) < 2
            or re.fullmatch(r"S-1(?:-\d+)+", rows[0][1]) is None
        ):
            raise TrialProcessError("invalid_trial_root")
        _CURRENT_USER_SID = rows[0][1]
        return _CURRENT_USER_SID
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise TrialProcessError("invalid_trial_root") from error


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("ace_count", ctypes.c_ulong),
        ("bytes_in_use", ctypes.c_ulong),
        ("bytes_free", ctypes.c_ulong),
    ]


def _windows_security_value(owner, dacl, descriptor):
    if not owner or not dacl or not descriptor:
        raise TrialProcessError("invalid_trial_root")
    try:
        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        advapi = ctypes.windll.advapi32
        if not advapi.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise TrialProcessError("invalid_trial_root")
        information = _AclSizeInformation()
        if not advapi.GetAclInformation(
            dacl, ctypes.byref(information),
            ctypes.sizeof(information), 2,
        ):
            raise TrialProcessError("invalid_trial_root")
        entries = []
        for index in range(information.ace_count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise TrialProcessError("invalid_trial_root")
            header = (ctypes.c_ubyte * 4).from_address(ace.value)
            ace_type, ace_flags = header[0], header[1]
            mask = ctypes.c_ulong.from_address(ace.value + 4).value
            sid = _sid_string(ctypes.c_void_p(ace.value + 8))
            entries.append((ace_type, ace_flags, mask, sid))
        return _sid_string(owner), bool(control.value & 0x1000), entries
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)


def _windows_security(path: Path):
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi = ctypes.windll.advapi32
    result = advapi.GetNamedSecurityInfoW(
        str(path), 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise TrialProcessError("invalid_trial_root")
    return _windows_security_value(owner, dacl, descriptor)


def _windows_security_handle(handle):
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi = ctypes.windll.advapi32
    result = advapi.GetSecurityInfo(
        ctypes.c_void_p(handle), 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise TrialProcessError("invalid_trial_root")
    return _windows_security_value(owner, dacl, descriptor)


def _verify_windows_security(path: Path, *, require_protected=True) -> None:
    _verify_windows_security_value(
        _windows_security(path), require_protected=require_protected
    )


def _verify_windows_security_handle(
    handle, *, require_protected=True
) -> None:
    _verify_windows_security_value(
        _windows_security_handle(handle), require_protected=require_protected
    )


def _verify_windows_security_value(value, *, require_protected=True) -> None:
    owner, protected, entries = value
    allowed = {
        _current_user_sid(), _SYSTEM_SID, _ADMINISTRATORS_SID,
        _OWNER_RIGHTS_SID,
    }
    granted = {sid for ace_type, _flags, _mask, sid in entries
               if ace_type == 0}
    if (
        owner != _current_user_sid()
        or require_protected and not protected
        or any(
            ace_type != 0 or sid not in allowed or mask != 0x1F01FF
            for ace_type, _flags, mask, sid in entries
        )
        or not {
            _SYSTEM_SID, _ADMINISTRATORS_SID
        }.issubset(granted)
        or not (
            _current_user_sid() in granted
            or _OWNER_RIGHTS_SID in granted
        )
    ):
        raise TrialProcessError("invalid_trial_root")


def _protect_windows_child(root_handle, child_handle) -> None:
    """Copy the verified root DACL to an opened child and protect it."""
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi = ctypes.windll.advapi32
    result = advapi.GetSecurityInfo(
        ctypes.c_void_p(root_handle), 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not owner or not dacl or not descriptor:
        raise TrialProcessError("invalid_trial_root")
    try:
        result = advapi.SetSecurityInfo(
            ctypes.c_void_p(child_handle), 1,
            0x00000004 | 0x80000000,
            None, None, dacl, None,
        )
        if result != 0:
            raise TrialProcessError("invalid_trial_root")
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)
    _verify_windows_security_handle(child_handle)


def _secure_private_path(path: Path, *, directory: bool) -> None:
    """Apply and verify an owner-private ACL/mode at the trial boundary."""
    try:
        if os.name == "nt":
            inheritance = "(OI)(CI)" if directory else ""
            subprocess.run(
                ["icacls", str(path), "/inheritance:r"],
                check=True,
                capture_output=True,
                timeout=10,
            )
            _owner, _protected, existing = _windows_security(path)
            for sid in {entry[3] for entry in existing}:
                subprocess.run(
                    [
                        "icacls", str(path),
                        "/remove:g", f"*{sid}",
                        "/remove:d", f"*{sid}",
                    ],
                    check=True, capture_output=True, timeout=10,
                )
            subprocess.run(
                [
                    "icacls", str(path), "/grant:r",
                    f"*{_current_user_sid()}:{inheritance}(F)",
                    f"*{_SYSTEM_SID}:{inheritance}(F)",
                    f"*{_ADMINISTRATORS_SID}:{inheritance}(F)",
                ],
                check=True, capture_output=True, timeout=10,
            )
            _verify_windows_security(path)
        else:
            os.chmod(path, 0o700 if directory else 0o600)
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != (0o700 if directory else 0o600):
                raise TrialProcessError("invalid_trial_root")
    except TrialProcessError:
        raise
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise TrialProcessError("invalid_trial_root") from error


def _entry_name(path: Path, root: _StableRoot) -> str:
    if type(root) is not _StableRoot or not isinstance(path, Path):
        raise TrialProcessError("invalid_trial_root")
    name = path.name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise TrialProcessError("invalid_trial_root")
    return name


def _validate_open_file(handle, *, require_protected=True):
    if os.name == "nt":
        identity, attributes = _windows_handle_identity(handle)
        if attributes & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ) or attributes & 0x10:
            raise TrialProcessError("invalid_status")
        try:
            _verify_windows_security_handle(
                handle, require_protected=require_protected
            )
        except TrialProcessError:
            # Still fails closed; only the code is made accurate. This
            # validates a file inside the root, not the root itself.
            raise TrialProcessError("invalid_status") from None
        return identity
    value = os.fstat(handle)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) & 0o077
    ):
        raise TrialProcessError("invalid_status")
    return value.st_dev, value.st_ino


def _atomic_json(
    path: Path, value: dict, root: _StableRoot, *, replace=True,
    patience=2.0,
) -> None:
    """Reemplazo atomico con reintento acotado ante fallos transitorios.

    Las herramientas de la sesion escriben dentro del mismo arbol que los
    archivos IPC (`temp_parent=self.root`), asi que una colision
    momentanea -- un antivirus, un git.exe hijo, un lector con el archivo
    abierto -- es esperable, no hipotetica. El hallazgo Q ya demostro que
    ocurre de verdad sobre owner-status.json.

    Sin este reintento, un fallo de un instante en la ruta de un comando
    ya aprobado sube hasta el `except BaseException` del propietario y
    destruye la sesion viva de forma permanente, que es exactamente lo
    que el punto 4 dice que nunca debe pasar.

    Solo se reintenta el fallo generico `trial_process_failed`, que es el
    que producen los errores nativos. Los codigos especificos
    (`duplicate_command`, `invalid_status`, `invalid_trial_root`) son
    decisiones, no accidentes, y se propagan de inmediato.
    """
    deadline = time.monotonic() + patience
    while True:
        try:
            return _atomic_json_once(path, value, root, replace=replace)
        except TrialProcessError as error:
            if (
                error.code != "trial_process_failed"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.05)


def _atomic_json_once(
    path: Path, value: dict, root: _StableRoot, *, replace=True
) -> None:
    if type(root) is not _StableRoot:
        raise TrialProcessError("invalid_trial_root")
    destination_name = _entry_name(path, root)
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    descriptor = None
    temporary_name = None
    try:
        for _attempt in range(32):
            temporary_name = (
                ".developerai-ipc-" + secrets.token_hex(16) + ".tmp"
            )
            try:
                descriptor = root.open_child(
                    temporary_name, create=True, exclusive=True,
                    write=True, delete=True,
                )
                break
            except (OSError, _winfs.NativeFileError):
                descriptor = None
        else:
            raise TrialProcessError()
        if os.name == "nt":
            _protect_windows_child(root._handle, descriptor)
            _validate_open_file(descriptor)
            _winfs.write_all(descriptor, serialized)
            _winfs.flush(descriptor)
            if not replace:
                existing = None
                try:
                    existing = root.open_child(destination_name, read=True)
                    raise TrialProcessError("duplicate_command")
                except _winfs.NativeFileError:
                    pass
                finally:
                    if existing is not None:
                        root.close_child(existing)
            _winfs.rename(
                descriptor, root._handle, destination_name, replace=replace
            )
            _validate_open_file(descriptor)
        else:
            os.write(descriptor, serialized)
            os.fsync(descriptor)
            if not replace:
                try:
                    check = root.open_child(destination_name, read=True)
                except FileNotFoundError:
                    check = None
                if check is not None:
                    root.close_child(check)
                    raise TrialProcessError("duplicate_command")
            os.rename(
                temporary_name, destination_name,
                src_dir_fd=root._handle, dst_dir_fd=root._handle,
            ) if not replace else os.replace(
                temporary_name, destination_name,
                src_dir_fd=root._handle, dst_dir_fd=root._handle,
            )
        root.close_child(descriptor)
        descriptor = None
        temporary_name = None
    except TrialProcessError:
        raise
    except (OSError, UnicodeError, _winfs.NativeFileError) as error:
        # Chain the native cause. The closed error code is unchanged and
        # still leaks nothing to public evidence, but discarding the
        # original made every failure here undiagnosable.
        raise TrialProcessError() from error
    finally:
        if descriptor is not None:
            try:
                if temporary_name is not None:
                    if os.name == "nt":
                        _winfs.delete(descriptor)
                    else:
                        os.unlink(temporary_name, dir_fd=root._handle)
            except OSError:
                pass
            finally:
                try:
                    root.close_child(descriptor)
                except OSError:
                    pass


def _read_json(path: Path, expected: frozenset[str], root: _StableRoot) -> dict:
    if type(root) is not _StableRoot:
        raise TrialProcessError("invalid_trial_root")
    name = _entry_name(path, root)
    descriptor = None
    try:
        descriptor = root.open_child(name, read=True)
        identity = _validate_open_file(descriptor)
        if os.name == "nt":
            content = _winfs.read_all(descriptor, 1024 * 1024)
        else:
            chunks = []
            total = 0
            while True:
                chunk = os.read(
                    descriptor, min(65536, 1024 * 1024 + 1 - total)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 1024 * 1024:
                    raise TrialProcessError("invalid_status")
            content = b"".join(chunks)
        if _validate_open_file(descriptor) != identity:
            raise TrialProcessError("invalid_status")
        text = content.decode("utf-8")
    except TrialProcessError:
        raise
    except (OSError, UnicodeError, _winfs.NativeFileError) as error:
        raise TrialProcessError("invalid_status") from error
    finally:
        if descriptor is not None:
            try:
                root.close_child(descriptor)
            except OSError:
                pass

    try:
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise TrialProcessError("invalid_status")
                result[key] = item
            return result

        value = json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError:
        raise TrialProcessError("invalid_status") from None
    if type(value) is not dict or set(value) != expected:
        raise TrialProcessError("invalid_status")
    return value


def _write_capability(root: _StableRoot, capability: bytes) -> None:
    if type(capability) is not bytes or len(capability) != _CAPABILITY_BYTES:
        raise TrialProcessError("owner_start_failed")
    descriptor = None
    try:
        descriptor = root.open_child(
            _CAPABILITY_FILE, create=True, exclusive=True,
            write=True, delete=True,
        )
        if os.name == "nt":
            _protect_windows_child(root._handle, descriptor)
            _winfs.write_all(descriptor, capability)
            _winfs.flush(descriptor)
        else:
            if os.write(descriptor, capability) != len(capability):
                raise TrialProcessError("owner_start_failed")
            os.fsync(descriptor)
        _validate_open_file(descriptor)
    except TrialProcessError:
        raise
    except (OSError, _winfs.NativeFileError) as error:
        raise TrialProcessError("owner_start_failed") from error
    finally:
        if descriptor is not None:
            try:
                root.close_child(descriptor)
            except OSError:
                pass


def _read_capability(root: _StableRoot) -> bytes:
    descriptor = None
    try:
        descriptor = root.open_child(_CAPABILITY_FILE, read=True)
        identity = _validate_open_file(descriptor)
        capability = (
            _winfs.read_all(descriptor, _CAPABILITY_BYTES)
            if os.name == "nt"
            else os.read(descriptor, _CAPABILITY_BYTES + 1)
        )
        if (
            _validate_open_file(descriptor) != identity
            or len(capability) != _CAPABILITY_BYTES
        ):
            raise TrialProcessError("invalid_status")
        return capability
    except TrialProcessError:
        raise
    except (OSError, _winfs.NativeFileError) as error:
        raise TrialProcessError("invalid_status") from error
    finally:
        if descriptor is not None:
            try:
                root.close_child(descriptor)
            except OSError:
                pass


def _optional_string(value) -> bool:
    return value is None or type(value) is str and bool(value)


def _valid_status(value: dict, root: Path) -> bool:
    evidence = value.get("evidence_path")
    try:
        evidence_path = Path(evidence).resolve(strict=False)
    except (TypeError, OSError, RuntimeError):
        return False
    return (
        type(value.get("version")) is int and value["version"] == 1
        and type(value.get("owner_runtime_id")) is str
        and _OWNER_ID.fullmatch(value["owner_runtime_id"]) is not None
        and type(value.get("state")) is str and value["state"] in _STATES
        and type(value.get("pid")) is int and value["pid"] > 0
        and type(value.get("heartbeat_ns")) is int
        and value["heartbeat_ns"] >= 0
        and all(_optional_string(value.get(name)) for name in (
            "session_id", "workflow_runtime_id", "plan_id",
            "approval_request_id", "error_code",
        ))
        and type(value.get("last_sequence")) is int
        and value["last_sequence"] >= 0
        and type(value.get("transport_calls")) is int
        and value["transport_calls"] >= 0
        and evidence_path == root / "public-evidence.json"
    )


def _safe_error_code(value) -> str:
    return (
        value
        if type(value) is str and _ERROR_CODE.fullmatch(value) is not None
        else "owner_failed"
    )


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(
            synchronize, False, pid
        )
        if not handle:
            return False
        try:
            return (
                ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
                == wait_timeout
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _JobBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _bind_owner_to_job():
    """Make the owner's descendants die with it (hallazgo P).

    TerminateProcess ends one process, not its tree: a git.exe spawned by
    the isolated environment can outlive the owner while still holding
    entries open, which is what forced the retry in _remove_stable_contents.

    Assigning the owner itself to a job with KILL_ON_JOB_CLOSE fixes the
    cause: children inherit job membership, so closing the last job handle
    -- which happens when the owner dies -- terminates every descendant.
    The initiator is NOT in the job, so a detached owner still outlives
    the process that launched it. The premise of phase 8.5 is untouched.

    This is cleanup hardening, not a security boundary. If the platform
    refuses the assignment the owner keeps running: the retry mitigation
    is still in place, and killing the session would be worse.
    """
    if os.name != "nt":
        return None
    kernel = ctypes.windll.kernel32
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
    ]
    kernel.SetInformationJobObject.restype = ctypes.c_int
    kernel.AssignProcessToJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        return None
    information = _JobExtendedLimit()
    information.BasicLimitInformation.LimitFlags = (
        0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if not kernel.SetInformationJobObject(
        ctypes.c_void_p(job),
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(information),
        ctypes.sizeof(information),
    ) or not kernel.AssignProcessToJobObject(
        ctypes.c_void_p(job), kernel.GetCurrentProcess()
    ):
        _close_windows_handle(job)
        return None
    # El handle se conserva abierto a proposito: al morir el propietario,
    # Windows lo cierra y el job termina a todos sus descendientes.
    return job


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    creation_time: int
    executable: str


def _windows_identity_from_handle(handle, pid):
    try:
        kernel = ctypes.windll.kernel32
        kernel.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        kernel.GetProcessTimes.restype = ctypes.c_int
        kernel.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        if not kernel.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise TrialProcessError("invalid_status")
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            raise TrialProcessError("invalid_status")
        executable = os.path.normcase(
            str(Path(buffer.value).resolve(strict=True))
        )
        return _ProcessIdentity(pid, creation.value, executable)
    except (OSError, RuntimeError) as error:
        raise TrialProcessError("invalid_status") from error


class _ProcessReference:
    """Stable process object used for identity, termination and waiting."""

    __slots__ = ("pid", "_handle")

    def __init__(self, pid):
        if type(pid) is not int or pid <= 0:
            raise TrialProcessError("invalid_status")
        self.pid = pid
        self._handle = None
        try:
            if os.name == "nt":
                kernel = ctypes.windll.kernel32
                open_process = kernel.OpenProcess
                open_process.restype = ctypes.c_void_p
                open_process.argtypes = [
                    ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong
                ]
                self._handle = open_process(
                    0x1000 | 0x0001 | 0x00100000, False, pid
                )
                if not self._handle:
                    raise OSError
            else:
                if not hasattr(os, "pidfd_open"):
                    raise OSError
                self._handle = os.pidfd_open(pid, 0)
        except OSError:
            self.close()
            raise TrialProcessError("owner_not_alive") from None

    def identity(self):
        if self._handle is None:
            raise TrialProcessError("invalid_status")
        if os.name == "nt":
            return _windows_identity_from_handle(self._handle, self.pid)
        try:
            stat_fields = Path(f"/proc/{self.pid}/stat").read_text(
                encoding="ascii"
            ).split()
            executable = str(
                Path(f"/proc/{self.pid}/exe").resolve(strict=True)
            )
            return _ProcessIdentity(
                self.pid, int(stat_fields[21]), executable
            )
        except (OSError, UnicodeError, ValueError, IndexError) as error:
            raise TrialProcessError("invalid_status") from error

    def terminate_and_wait(self, timeout=5.0):
        if self._handle is None:
            raise TrialProcessError("invalid_status")
        if os.name == "nt":
            kernel = ctypes.windll.kernel32
            kernel.TerminateProcess.argtypes = [
                ctypes.c_void_p, ctypes.c_uint
            ]
            kernel.TerminateProcess.restype = ctypes.c_int
            kernel.WaitForSingleObject.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong
            ]
            kernel.WaitForSingleObject.restype = ctypes.c_ulong
            if not kernel.TerminateProcess(self._handle, 1):
                raise TrialProcessError("owner_not_alive")
            milliseconds = max(0, int(timeout * 1000))
            if kernel.WaitForSingleObject(
                self._handle, milliseconds
            ) != 0:
                raise TrialProcessError("owner_not_alive")
            return
        try:
            signal.pidfd_send_signal(
                self._handle, signal.SIGTERM, None, 0
            )
            deadline = time.monotonic() + timeout
            while _is_alive(self.pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _is_alive(self.pid):
                raise TrialProcessError("owner_not_alive")
        except (OSError, AttributeError) as error:
            raise TrialProcessError("owner_not_alive") from error

    def close(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if os.name == "nt":
            close_handle = ctypes.windll.kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(handle)
        else:
            os.close(handle)


def _process_identity(pid: int) -> _ProcessIdentity:
    reference = _ProcessReference(pid)
    try:
        return reference.identity()
    finally:
        reference.close()


@dataclass(frozen=True)
class TrialProcessHandle:
    root: Path
    owner_runtime_id: str
    pid: int
    process_identity: _ProcessIdentity
    root_identity: tuple


class _CommandAuthority:
    """Opaque, non-copyable command signer; never exposes its key."""

    __slots__ = ("__key",)

    def __init__(self, capability):
        if type(capability) is not bytes or len(capability) != _CAPABILITY_BYTES:
            raise TrialProcessError("invalid_status")
        self.__key = bytearray(capability)

    def sign(self, command):
        return _command_auth(bytes(self.__key), command)

    def verify(self, command):
        supplied = command.get("auth") if type(command) is dict else None
        return (
            type(supplied) is str
            and len(supplied) == 64
            and hmac.compare_digest(supplied, self.sign(command))
        )

    def __repr__(self):
        return "<authorized trial command signer>"

    __str__ = __repr__

    def __copy__(self):
        raise TypeError("La autoridad del ensayo no puede copiarse.")

    def __deepcopy__(self, _memo):
        raise TypeError("La autoridad del ensayo no puede copiarse.")

    def __reduce_ex__(self, _protocol):
        raise TypeError("La autoridad del ensayo no puede serializarse.")

    def close(self):
        for index in range(len(self.__key)):
            self.__key[index] = 0


class TrialProcessLauncher:
    """Start a detached owner and return without waiting for planning."""

    @staticmethod
    def start(source_repository, trial_root, user_request, *, environ=None):
        root = _trial_root(trial_root)
        _secure_private_path(root, directory=True)
        root_identity = _path_identity(root)
        source = Path(source_repository).resolve(strict=True)
        if source.parent != root or not source.is_dir():
            raise TrialProcessError("invalid_trial_root")
        if type(user_request) is not str or not user_request:
            raise TrialProcessError("owner_start_failed")
        root_guard = _StableRoot(root, root_identity)
        root_identity = root_guard.identity
        owner_id = "otr_" + uuid.uuid4().hex
        capability = secrets.token_bytes(_CAPABILITY_BYTES)
        _write_capability(root_guard, capability)
        spec_path = root / "owner-spec.json"
        evidence_path = root / "public-evidence.json"
        _atomic_json(spec_path, {
            "version": 1,
            "owner_runtime_id": owner_id,
            "source_repository": str(source),
            "evidence_path": str(evidence_path),
            "root_device": root_identity[0],
            "root_inode": root_identity[1],
        }, root_guard)
        child_environment = dict(os.environ if environ is None else environ)
        child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parent)
        flags = 0
        if os.name == "nt":
            flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "controlled_trial_process",
                 "--owner", str(spec_path)],
                cwd=str(Path(__file__).resolve().parent),
                env=child_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
            bootstrap = json.dumps(
                {
                    "request": user_request,
                    "capability": capability.hex(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            process.stdin.write(bootstrap)
            process.stdin.close()
        except (OSError, BrokenPipeError):
            # The root handle must stay open until the spec cleanup that
            # needs it is done; closing first left _handle as None and the
            # POSIX branch would resolve the name against the cwd instead.
            spec_handle = None
            try:
                spec_handle = root_guard.open_child(
                    spec_path.name, delete=True
                )
                if os.name == "nt":
                    _winfs.delete(spec_handle)
                else:
                    os.unlink(spec_path.name, dir_fd=root_guard._handle)
            except (OSError, TrialProcessError, _winfs.NativeFileError):
                pass
            finally:
                if spec_handle is not None:
                    try:
                        root_guard.close_child(spec_handle)
                    except OSError:
                        pass
                root_guard.close()
            if "process" in locals():
                process.kill()
                process.wait(timeout=5)
            raise TrialProcessError("owner_start_failed") from None
        _LOCAL_PROCESSES[process.pid] = process
        try:
            identity = _process_identity(process.pid)
        except TrialProcessError:
            process.kill()
            process.wait(timeout=5)
            root_guard.close()
            raise TrialProcessError("owner_start_failed") from None
        capability = b""
        root_guard.close()
        return TrialProcessHandle(
            root, owner_id, process.pid, identity, root_identity
        )


class TrialProcessClient:
    """Observe and command one live owner without recovering its authority."""

    def __init__(self, handle: TrialProcessHandle):
        if type(handle) is not TrialProcessHandle:
            raise TrialProcessError("invalid_status")
        self.handle = handle
        if (
            type(handle.process_identity) is not _ProcessIdentity
            or type(handle.root_identity) is not tuple
        ):
            raise TrialProcessError("invalid_status")
        self.root = _trial_root(handle.root, handle.root_identity)
        self._root_guard = _StableRoot(self.root, handle.root_identity)
        self._authority = _CommandAuthority(
            _read_capability(self._root_guard)
        )
        self._cleaned = False
        self._status_path = self.root / "owner-status.json"
        self._command_path = self.root / "owner-command.json"

    def __repr__(self):
        return (
            f"<authorized trial client owner={self.handle.owner_runtime_id}>"
        )

    __str__ = __repr__

    def __copy__(self):
        raise TypeError("El cliente autorizado no puede copiarse.")

    def __deepcopy__(self, _memo):
        raise TypeError("El cliente autorizado no puede copiarse.")

    def __reduce_ex__(self, _protocol):
        raise TypeError("El cliente autorizado no puede serializarse.")

    def status(self, *, timeout=0.0):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise TrialProcessError("client_timeout")
        deadline = time.monotonic() + float(timeout)
        while True:
            if self._root_guard.file_exists(self._status_path.name):
                try:
                    value = _read_json(
                        self._status_path, _STATUS_FIELDS,
                        self._root_guard,
                    )
                except TrialProcessError:
                    if (
                        _is_alive(self.handle.pid)
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        continue
                    raise
                if (
                    not _valid_status(value, self.root)
                    or value["owner_runtime_id"] != self.handle.owner_runtime_id
                    or value["pid"] != self.handle.pid
                ):
                    raise TrialProcessError("invalid_status")
                if not _is_alive(value["pid"]) and value["state"] not in {
                    "completed", "failed", "rejected", "cancelled", "lost",
                    "stopped",
                }:
                    value = dict(
                        value, state="lost", error_code="owner_lost",
                        heartbeat_ns=time.time_ns(),
                    )
                    _atomic_json(
                        self._status_path, value, self._root_guard,
                    )
                return value
            if not _is_alive(self.handle.pid):
                value = {
                    "version": 1,
                    "owner_runtime_id": self.handle.owner_runtime_id,
                    "state": "lost",
                    "pid": self.handle.pid,
                    "heartbeat_ns": time.time_ns(),
                    "session_id": None,
                    "workflow_runtime_id": None,
                    "plan_id": None,
                    "approval_request_id": None,
                    "error_code": "owner_lost",
                    "evidence_path": str(
                        self.root / "public-evidence.json"
                    ),
                    "last_sequence": 0,
                    "transport_calls": 0,
                }
                _atomic_json(
                    self._status_path, value, self._root_guard
                )
                return value
            if time.monotonic() >= deadline:
                raise TrialProcessError("client_timeout")
            time.sleep(0.05)

    def public_view(self):
        status = self.status()
        if status["state"] == "planning":
            raise TrialProcessError("evidence_unavailable")
        try:
            evidence = _read_json(
                self.root / "public-evidence.json",
                frozenset(ProgrammingOperatorView.__dataclass_fields__),
                self._root_guard,
            )
            return evidence_to_view(evidence)
        except (TrialProcessError, TrialEvidenceError):
            raise TrialProcessError("evidence_unavailable") from None

    def command(
        self, action, *, session_id, owner_runtime_id, plan_id,
        workflow_runtime_id=None, request_id=None, sequence=None,
    ):
        status = self.status()
        expected_sequence = status["last_sequence"] + 1
        supplied_sequence = expected_sequence if sequence is None else sequence
        command = {
            "version": 1,
            "sequence": supplied_sequence,
            "owner_runtime_id": owner_runtime_id,
            "session_id": session_id,
            "workflow_runtime_id": workflow_runtime_id,
            "plan_id": plan_id,
            "expected_state": status["state"],
            "action": action,
            "request_id": request_id,
            "auth": None,
        }
        command["auth"] = self._authority.sign(command)
        _validate_command(command, status, self._authority)
        _atomic_json(
            self._command_path, command, self._root_guard,
            replace=False,
        )
        return supplied_sequence

    def wait_for_sequence(self, sequence, *, timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            status = self.status(timeout=min(0.1, max(0.0, deadline-time.monotonic())))
            if status["last_sequence"] >= sequence:
                return status
            if time.monotonic() >= deadline:
                raise TrialProcessError("client_timeout")
            time.sleep(0.05)

    def terminate_and_cleanup(self):
        if self._cleaned:
            return
        root = self._root_guard
        process = _LOCAL_PROCESSES.get(self.handle.pid)
        if self._root_guard.file_exists(self._status_path.name):
            status = _read_json(
                self._status_path, _STATUS_FIELDS, self._root_guard,
            )
            if (
                not _valid_status(status, self.root)
                or status["pid"] != self.handle.pid
                or status["owner_runtime_id"] != self.handle.owner_runtime_id
            ):
                raise TrialProcessError("invalid_status")
        elif process is None:
            raise TrialProcessError("invalid_status")
        alive = _is_alive(self.handle.pid)
        reference = None
        if alive:
            try:
                reference = _ProcessReference(self.handle.pid)
                observed_identity = reference.identity()
            except TrialProcessError:
                if reference is not None:
                    reference.close()
                raise TrialProcessError("invalid_status") from None
            if observed_identity != self.handle.process_identity:
                reference.close()
                raise TrialProcessError("invalid_status")
            try:
                reference.terminate_and_wait(timeout=5)
            finally:
                reference.close()
        _LOCAL_PROCESSES.pop(self.handle.pid, None)
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise TrialProcessError("owner_not_alive") from error
        if _is_alive(self.handle.pid):
            raise TrialProcessError("owner_not_alive")
        try:
            self._root_guard.verify()
            _remove_stable_contents(self._root_guard)
            if os.name == "nt":
                _winfs.delete(self._root_guard._handle)
            else:
                parent = os.open(
                    self.root.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    self._root_guard.close()
                    os.rmdir(self.root.name, dir_fd=parent)
                finally:
                    os.close(parent)
            self._root_guard.close()
            _verify_trial_is_gone(self.root, self.handle.root_identity)
            self._cleaned = True
        except (OSError, _winfs.NativeFileError) as error:
            raise TrialProcessError() from error
        finally:
            # Both handles close in success and in error. _StableRoot.close
            # is idempotent, so the POSIX branch closing early is harmless.
            try:
                self._root_guard.close()
            except OSError:
                pass
            self._authority.close()


def _verify_trial_is_gone(root: Path, identity) -> None:
    """Criterion 7: the exact trial must no longer exist after cleanup.

    Checking the pathname is not enough and would be wrong: a different
    directory may legitimately occupy the same name once ours is gone,
    and an adversarial test does exactly that. What must have
    disappeared is this identity, not this name.
    """
    try:
        remaining = _path_identity(root)
    except TrialProcessError:
        return          # the pathname resolves to nothing: correct
    if remaining == identity:
        raise TrialProcessError()


def _remove_stable_contents(root: _StableRoot, *, patience=15.0) -> None:
    """Delete only entries opened relative to the verified root handle."""

    deadline = time.monotonic() + patience

    def open_entry(directory_handle, name, directory):
        """Open one entry for deletion, tolerating a transient holder.

        TerminateProcess ends the owner but not its descendants, so a
        git.exe spawned by the isolated environment can still hold an
        entry open for a moment. Windows reports that as
        STATUS_SHARING_VIOLATION (0xc0000043). Retry until the handle is
        released or patience runs out, then fail closed. This is
        criterion 13: partial deletion must be retryable and idempotent.
        """
        while True:
            try:
                return _winfs.open_relative(
                    directory_handle, name, directory=directory,
                    read=not directory, delete=True,
                )
            except _winfs.NativeFileError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def delete_entry(handle):
        while True:
            try:
                _winfs.delete(handle)
                return
            except _winfs.NativeFileError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def remove_from(directory_handle, root_identity):
        if os.name == "nt":
            entries = _winfs.entries(directory_handle)
        else:
            entries = []
            for name in os.listdir(directory_handle):
                value = os.stat(
                    name, dir_fd=directory_handle, follow_symlinks=False
                )
                entries.append((name, 0x10 if stat.S_ISDIR(value.st_mode) else 0))
        for name, attributes in entries:
            if attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise TrialProcessError("invalid_trial_root")
            directory = bool(attributes & 0x10)
            if os.name == "nt":
                handle = open_entry(directory_handle, name, directory)
            else:
                flags = getattr(os, "O_NOFOLLOW", 0) | (
                    os.O_DIRECTORY if directory else os.O_RDONLY
                )
                handle = os.open(name, flags, dir_fd=directory_handle)
            try:
                if os.name == "nt":
                    identity, observed_attributes = _windows_handle_identity(
                        handle
                    )
                    if (
                        bool(observed_attributes & 0x10) != directory
                        or observed_attributes & getattr(
                            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                        )
                        or identity[0] != root_identity[0]
                    ):
                        raise TrialProcessError("invalid_trial_root")
                    _verify_windows_security_handle(
                        handle, require_protected=False
                    )
                else:
                    value = os.fstat(handle)
                    if (
                        value.st_dev != root_identity[0]
                        or value.st_uid != os.geteuid()
                    ):
                        raise TrialProcessError("invalid_trial_root")
                if directory:
                    remove_from(handle, root_identity)
                if os.name == "nt":
                    delete_entry(handle)
                elif directory:
                    os.rmdir(name, dir_fd=directory_handle)
                else:
                    os.unlink(name, dir_fd=directory_handle)
            finally:
                if os.name == "nt":
                    _winfs.close(handle)
                else:
                    os.close(handle)

    root.verify()
    remove_from(root._handle, root.identity)


def _command_auth(capability: bytes, command: dict) -> str:
    if type(capability) is not bytes or len(capability) != _CAPABILITY_BYTES:
        raise TrialProcessError("invalid_command")
    signed = {key: value for key, value in command.items() if key != "auth"}
    try:
        payload = json.dumps(
            signed, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise TrialProcessError("invalid_command") from None
    return hmac.new(capability, payload, hashlib.sha256).hexdigest()


def _validate_command(command: dict, status: dict, authority) -> None:
    if (
        type(command) is not dict or set(command) != _COMMAND_FIELDS
        or type(command["version"]) is not int
        or command["version"] != 1
        or type(command["sequence"]) is not int
        or command["sequence"] <= status["last_sequence"]
    ):
        raise TrialProcessError("duplicate_command")
    if (
        type(authority) is not _CommandAuthority
        or not authority.verify(command)
    ):
        raise TrialProcessError("invalid_command")
    if (
        command["sequence"] != status["last_sequence"] + 1
        or command["action"] not in _ACTIONS
        or command["owner_runtime_id"] != status["owner_runtime_id"]
        or command["session_id"] != status["session_id"]
        or command["workflow_runtime_id"] != status["workflow_runtime_id"]
        or command["plan_id"] != status["plan_id"]
        or type(command["expected_state"]) is not str
        or command["expected_state"] not in _STATES
        or command["expected_state"] != status["state"]
        or not _optional_string(command["request_id"])
    ):
        raise TrialProcessError("stale_command")
    plan_action = command["action"].endswith("_plan")
    if (
        plan_action and (
            status["state"] != "pending_plan"
            or command["request_id"] is not None
        )
        or not plan_action and (
            status["state"] != "awaiting_approval"
            or command["request_id"] != status["approval_request_id"]
        )
    ):
        raise TrialProcessError("invalid_command")


class _CountingTransport:
    def __init__(self):
        from brain.model_transport import HttpClientTransport
        self._inner = HttpClientTransport()
        self.calls = 0

    def send(self, request):
        self.calls += 1
        return self._inner.send(request)


class _SecureControlledTrialHarness(ControlledTrialHarness):
    """Keep the public harness contract while securing its evidence write."""

    def __init__(self, operator, evidence_path, root_guard):
        super().__init__(operator, evidence_path)
        self._root_guard = root_guard

    def persist(self, view):
        try:
            evidence = view_to_evidence(view)
            _atomic_json(
                self._evidence_path, evidence, self._root_guard
            )
        except TrialEvidenceError:
            raise
        except TrialProcessError:
            raise TrialEvidenceError(
                "trial_evidence_write_failed", category="write"
            ) from None


class _Owner:
    def __init__(self, spec_path: Path):
        self.spec_path = spec_path
        self.root = _trial_root(spec_path.parent)
        self.root_guard = _StableRoot(self.root)
        initial_identity = self.root_guard.identity
        self.spec = _read_json(
            spec_path, _SPEC_FIELDS, self.root_guard
        )
        self.root_identity = (
            self.spec["root_device"], self.spec["root_inode"]
        )
        if self.root_identity != initial_identity:
            raise TrialProcessError("owner_start_failed")
        if self.root_guard.identity != self.root_identity:
            raise TrialProcessError("owner_start_failed")
        self._validate_spec()
        self.status_path = self.root / "owner-status.json"
        self.command_path = self.root / "owner-command.json"
        self.evidence_path = self.root / "public-evidence.json"
        self.lock = threading.Lock()
        self.transport = _CountingTransport()
        self.authority = None
        self.current = self._status("starting")
        self.stop_heartbeat = threading.Event()

    def _validate_spec(self):
        try:
            source = Path(self.spec["source_repository"]).resolve(strict=True)
            evidence = Path(self.spec["evidence_path"]).resolve(strict=False)
        except (KeyError, TypeError, OSError, RuntimeError) as error:
            raise TrialProcessError("owner_start_failed") from error
        if (
            type(self.spec["version"]) is not int
            or self.spec["version"] != 1
            or type(self.spec["owner_runtime_id"]) is not str
            or _OWNER_ID.fullmatch(self.spec["owner_runtime_id"]) is None
            or type(self.spec["root_device"]) is not int
            or type(self.spec["root_inode"]) is not int
            or self.spec["root_device"] < 0
            or self.spec["root_inode"] < 0
            or not source.is_dir()
            or source.parent != self.root
            or evidence != self.root / "public-evidence.json"
        ):
            raise TrialProcessError("owner_start_failed")

    def _status(self, state, view=None, *, error_code=None, sequence=None):
        previous = getattr(self, "current", {})
        return {
            "version": 1,
            "owner_runtime_id": self.spec["owner_runtime_id"],
            "state": state,
            "pid": os.getpid(),
            "heartbeat_ns": time.time_ns(),
            "session_id": (
                view.session_id if view is not None
                else previous.get("session_id")
            ),
            "workflow_runtime_id": (
                view.workflow_runtime_id if view is not None
                else previous.get("workflow_runtime_id")
            ),
            "plan_id": (
                view.plan_id if view is not None else previous.get("plan_id")
            ),
            "approval_request_id": (
                view.approval_request_id if view is not None
                else previous.get("approval_request_id")
            ),
            "error_code": error_code if error_code is not None else (
                view.error_code if view is not None else previous.get("error_code")
            ),
            "evidence_path": str(self.evidence_path),
            "last_sequence": (
                previous.get("last_sequence", 0)
                if sequence is None else sequence
            ),
            "transport_calls": self.transport.calls,
        }

    def publish(self, state, view=None, *, error_code=None, sequence=None):
        with self.lock:
            self.current = self._status(
                state, view, error_code=error_code, sequence=sequence
            )
            _atomic_json(
                self.status_path, self.current, self.root_guard
            )

    def heartbeat(self):
        while not self.stop_heartbeat.wait(0.25):
            try:
                with self.lock:
                    if self.current["state"] in {"starting", "planning"}:
                        self.current["heartbeat_ns"] = time.time_ns()
                        _atomic_json(
                            self.status_path, self.current, self.root_guard
                        )
            except TrialProcessError:
                # A single failed heartbeat write must never kill the
                # thread: an unhandled exception here only ends the
                # thread, leaving the owner alive but permanently
                # silent, and the client then declares the session
                # lost. Skip this tick and retry on the next one.
                continue

    def run(self):
        try:
            # Hallazgo P: los hijos del propietario deben morir con el.
            self.job = _bind_owner_to_job()
            raw_request = sys.stdin.buffer.read(256 * 1024 + 1)
            if len(raw_request) > 256 * 1024:
                raise TrialProcessError("owner_start_failed")
            try:
                request_payload = json.loads(raw_request.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                raise TrialProcessError("owner_start_failed") from None
            if (
                type(request_payload) is not dict
                or set(request_payload) != {"request", "capability"}
            ):
                raise TrialProcessError("owner_start_failed")
            request = request_payload["request"]
            encoded_capability = request_payload["capability"]
            if (
                type(request) is not str or not request
                or type(encoded_capability) is not str
                or len(encoded_capability) != _CAPABILITY_BYTES * 2
            ):
                raise TrialProcessError("owner_start_failed")
            try:
                capability = bytes.fromhex(encoded_capability)
            except ValueError:
                raise TrialProcessError("owner_start_failed") from None
            if len(capability) != _CAPABILITY_BYTES:
                raise TrialProcessError("owner_start_failed")
            self.authority = _CommandAuthority(capability)
            capability = b""
            request_payload = None
            raw_request = b""
            encoded_capability = None
            if self.root_guard.file_exists(self.spec_path.name):
                self.root_guard.delete_file(self.spec_path.name)
            self.publish("planning")
            heartbeat = threading.Thread(target=self.heartbeat, daemon=True)
            heartbeat.start()
            operator = create_operator_from_env(
                self.spec["source_repository"],
                environ=os.environ,
                keep_environment=True,
                transport=self.transport,
                temp_parent=self.root,
            )
            harness = _SecureControlledTrialHarness(
                operator, self.evidence_path, self.root_guard
            )
            view = harness.execute("programar: " + request)
            self.publish(view.state, view)
            while True:
                command = None
                try:
                    if self.root_guard.file_exists(self.command_path.name):
                        command = _read_json(
                            self.command_path, _COMMAND_FIELDS,
                            self.root_guard,
                        )
                        _validate_command(
                            command, self.current, self.authority
                        )
                except TrialProcessError as error:
                    try:
                        self.root_guard.delete_file(
                            self.command_path.name
                        )
                    except (OSError, TrialProcessError):
                        time.sleep(0.01)
                        continue
                    self.publish(
                        self.current["state"],
                        error_code=error.code,
                    )
                    continue
                if command is not None:
                    try:
                        self.root_guard.delete_file(self.command_path.name)
                    except (OSError, TrialProcessError):
                        # Mismo cuidado que la ruta de comando rechazado,
                        # que si lo tenia. Un handle retenido un instante
                        # no puede costar la sesion: el comando sigue en
                        # disco, intacto y sin consumir, y se reintenta en
                        # la vuelta siguiente.
                        time.sleep(0.05)
                        continue
                    text = {
                        "approve_plan": "aprobar-plan",
                        "reject_plan": "rechazar-plan",
                        "cancel_plan": "cancelar-plan",
                        "approve_operation": "aprobar",
                        "reject_operation": "rechazar",
                        "cancel_operation": "cancelar",
                    }[command["action"]]
                    identifier = (
                        command["plan_id"]
                        if command["action"].endswith("_plan")
                        else command["request_id"]
                    )
                    view = harness.execute(f"{text} {identifier}")
                    self.publish(
                        view.state, view, sequence=command["sequence"]
                    )
                if self.current["state"] in {
                    "completed", "failed", "rejected", "cancelled",
                }:
                    break
                time.sleep(0.05)
        except BaseException as error:
            self.publish(
                "failed",
                error_code=_safe_error_code(
                    getattr(error, "code", "owner_failed")
                ),
            )
        finally:
            self.stop_heartbeat.set()
            if self.authority is not None:
                self.authority.close()
            self.root_guard.close()


def _owner_main(spec_path):
    _Owner(Path(spec_path)).run()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--owner", required=True)
    arguments = parser.parse_args(argv)
    return _owner_main(arguments.owner)


if __name__ == "__main__":
    raise SystemExit(main())

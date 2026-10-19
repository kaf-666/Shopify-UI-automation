"""Confined run results, audit and summary writes with fixed public errors.

Windows holds no-delete directory handles through atomic replacement. POSIX
uses no-follow directory descriptors. The artifact workspace and its ancestors
must be owned by the runner/admin: hostile concurrent ancestor relocation on
POSIX is outside this trust boundary; resolve alone is not a TOCTOU defence.
"""
import ctypes
import json
import os
import stat
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path, PureWindowsPath

from utils.result import ResultWriteError


def _ordinary(path, *, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError


@contextmanager
def _windows_directory(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Share READ/WRITE, never DELETE. Open reparse point itself, never follow it.
    handle = create(str(path), 0x80000000, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("ARTIFACT_WRITE_REJECTED")
    try:
        _ordinary(path, directory=True)
        yield
    finally:
        close(handle)


def write_run_json(data, run_root, relative):
    _write_run(data, run_root, relative)


def write_run_text(text, run_root, relative):
    if relative not in {"summary.html", "summary.css"} or not isinstance(text, str):
        raise ResultWriteError("ARTIFACT_WRITE_REJECTED")
    _write_run(text, run_root, relative, text_mode=True)


def _write_run(data, run_root, relative, *, text_mode=False):
    """Reject linked roots/parents/targets; update ordinary files atomically."""
    try:
        rel = Path(relative)
        win = PureWindowsPath(relative)
        if (not isinstance(relative, str) or rel.is_absolute() or win.is_absolute()
                or win.drive or "\\" in relative or ".." in rel.parts
                or relative not in {"results.json", "desktop/cart-cleanup.json",
                                    "mobile/cart-cleanup.json", "run_context.json",
                                    "stability_record.json", "publication.json", "summary.json",
                                    "summary.html", "summary.css"}
                or (relative in {"summary.html", "summary.css"}) != text_mode):
            raise ValueError
        root = Path(run_root).absolute()
        with ExitStack() as stack:
            if os.name == "nt":
                # Lock every existing ancestor so neither root nor parents can
                # be renamed/replaced while path-based Windows IO is in flight.
                for ancestor in reversed((root, *root.parents)):
                    stack.enter_context(_windows_directory(ancestor))
                parent = root / rel.parent
                if parent != root:
                    parent.mkdir(exist_ok=True)
                    stack.enter_context(_windows_directory(parent))
                target = root / rel
                if not target.resolve().is_relative_to(root.resolve()):
                    raise ValueError
                if os.path.lexists(target):
                    _ordinary(target)
                temporary = parent / (".full-json-" + uuid.uuid4().hex)
                def remove_temporary():
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                stack.callback(remove_temporary)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    if text_mode:
                        stream.write(data)
                    else:
                        json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.path.lexists(target):
                    _ordinary(target)
                os.replace(temporary, target)
            elif os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                directory_fd = os.open(root.anchor, flags)
                stack.callback(os.close, directory_fd)
                for part in root.parts[1:]:
                    directory_fd = os.open(part, flags, dir_fd=directory_fd)
                    stack.callback(os.close, directory_fd)
                for part in rel.parts[:-1]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                    except FileExistsError:
                        pass
                    directory_fd = os.open(part, flags, dir_fd=directory_fd)
                    stack.callback(os.close, directory_fd)
                name = rel.name
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError
                except FileNotFoundError:
                    pass
                temporary_name = ".full-json-" + uuid.uuid4().hex
                fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        if text_mode:
                            stream.write(data)
                        else:
                            json.dump(data, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary_name, name, src_dir_fd=directory_fd,
                               dst_dir_fd=directory_fd)
                finally:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
            else:
                raise ValueError
    except Exception:
        raise ResultWriteError("ARTIFACT_WRITE_REJECTED") from None

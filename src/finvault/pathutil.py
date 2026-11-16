"""Small pure helpers for comparing and writing paths."""

from __future__ import annotations

import os
import stat
import tempfile

from finvault.errors import FinvaultError


def is_under(path: str, root: str) -> bool:
    """True when ``path`` equals ``root`` or sits inside it (by path components)."""
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    if path == root:
        return True
    prefix = root if root.endswith("/") else root + "/"
    return path.startswith(prefix)


def is_under_any(path: str, roots: tuple[str, ...] | list[str]) -> bool:
    return any(is_under(path, r) for r in roots)


def path_variants(path: str) -> tuple[str, ...]:
    """The path as written plus its resolved form (e.g. /tmp vs /private/tmp)."""
    norm = os.path.normpath(path)
    real = os.path.realpath(norm)
    return (norm,) if real == norm else (norm, real)


def same_path(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_temp_beside(path: str, data: bytes, mode: int) -> str:
    """Write ``data`` to a fsynced temp file in ``path``'s folder and return its name.

    The temp may hold vault lines, so it is removed on any failure, Ctrl-C included.
    """
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=folder)
    written = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        written = True
    finally:
        if not written:
            os.unlink(tmp)
    return tmp


def append_fsync(path: str, data: bytes, mode: int = 0o600) -> None:
    """Append and fsync. Refuses (ELOOP) when ``path`` is a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def trim_partial_line(path: str) -> int:
    """Cut a last line that lacks its newline and return how many bytes were cut.

    Every append finvault makes ends in a newline, so an unterminated tail is
    the remains of an append that failed part way (a full disk, a crash). Its
    full line is still at its source and is appended again on the retry.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    try:
        size = os.fstat(fd).st_size
        if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
            return 0
        keep = 0
        end = size
        while end > 0:
            start = max(0, end - (1 << 16))
            cut = os.pread(fd, end - start, start).rfind(b"\n")
            if cut >= 0:
                keep = start + cut + 1
                break
            end = start
        os.ftruncate(fd, keep)
        os.fsync(fd)
        return size - keep
    finally:
        os.close(fd)


def ensure_dir_inside(path: str, root: str, mode: int = 0o700) -> None:
    """Create ``path`` folder by folder below ``root``, never through a symlink.

    Every component between ``root`` and ``path`` must be a real folder, so
    nothing written under ``path`` can land outside ``root``.
    """
    root = os.path.normpath(root)
    path = os.path.normpath(path)
    hint = "finvault keeps swept data only inside the vault; move that entry away and run the command again"
    if not is_under(path, root):
        raise FinvaultError(f"{path} is not inside {root}", hint)
    if os.path.islink(root) or not os.path.isdir(root):
        raise FinvaultError(f"{root} is not a folder", hint)
    rel = os.path.relpath(path, root)
    if rel == ".":
        return
    cur = root
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, mode)
            st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise FinvaultError(f"{cur} is a symlink or not a folder", hint)

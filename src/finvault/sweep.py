"""Move a vault's traces out of Claude Code's shared files.

The functions above ``plan_sweep`` are pure and take sample data directly. The
functions below it read and write the file system; their decisions come from the
pure layer.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field

from finvault.config import SweepSettings
from finvault.errors import ConcurrentChangeError, FinvaultError
from finvault.jsonutil import as_dict
from finvault.pathutil import append_fsync, fsync_dir, is_under, is_under_any, write_temp_beside

HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ARCHIVE_HISTORY = "history.jsonl"
ARCHIVE_FILE_HISTORY = "file-history"
ARCHIVE_PASTE_CACHE = "paste-cache"
DIR_MODE = 0o700
FILE_MODE = 0o600

# ---------------------------------------------------------------- pure layer


def split_lines(data: bytes) -> list[bytes]:
    """Split on b"\\n" only, keeping each line's ending (the last may lack one)."""
    if not data:
        return []
    parts = data.split(b"\n")
    lines = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def ensure_newline(line: bytes) -> bytes:
    return line if line.endswith(b"\n") else line + b"\n"


def parse_line(line: bytes) -> dict[str, object] | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return as_dict(obj)


def is_vaulted_line(line: bytes, roots: tuple[str, ...]) -> bool:
    obj = parse_line(line)
    if obj is None:
        return False
    project = obj.get("project")
    return isinstance(project, str) and bool(project) and is_under_any(project, roots)


def split_history(lines: list[bytes], roots: tuple[str, ...]) -> tuple[list[bytes], list[bytes]]:
    """Return (kept, vaulted). Unparseable and blank lines are always kept."""
    kept: list[bytes] = []
    vaulted: list[bytes] = []
    for line in lines:
        (vaulted if is_vaulted_line(line, roots) else kept).append(line)
    return kept, vaulted


def paste_hashes(line: bytes) -> set[str]:
    """The paste-cache hashes a history line refers to (only safe file-name hashes)."""
    obj = parse_line(line)
    if obj is None:
        return set()
    pasted = as_dict(obj.get("pastedContents"))
    if pasted is None:
        return set()
    found: set[str] = set()
    for value in pasted.values():
        entry = as_dict(value)
        if entry is not None:
            h = entry.get("contentHash")
            if isinstance(h, str) and HASH_RE.match(h):
                found.add(h)
    return found


def pastes_to_move(kept: list[bytes], vaulted: list[bytes]) -> set[str]:
    """Hashes referenced by vaulted lines and by no kept line."""
    in_vaulted: set[str] = set()
    for line in vaulted:
        in_vaulted |= paste_hashes(line)
    in_kept: set[str] = set()
    for line in kept:
        in_kept |= paste_hashes(line)
    return in_vaulted - in_kept


def session_ids_from_names(names: list[str]) -> set[str]:
    """Session ids from ``<sessionId>.jsonl`` file names in a projects folder."""
    ids: set[str] = set()
    for name in names:
        if name.endswith(".jsonl"):
            sid = name[: -len(".jsonl")]
            if SESSION_RE.match(sid):
                ids.add(sid)
    return ids


def file_history_to_move(names: list[str], session_ids: set[str]) -> list[str]:
    return sorted(n for n in names if n in session_ids)


def scratch_matches(names: list[str], patterns: tuple[str, ...]) -> list[str]:
    return sorted(n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns))


def new_archive_lines(existing: list[bytes], vaulted: list[bytes]) -> list[bytes]:
    """Vaulted lines not already in the archive, newline-terminated, in order."""
    seen = {ensure_newline(line) for line in existing}
    out: list[bytes] = []
    for line in vaulted:
        norm = ensure_newline(line)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _effective_timestamps(lines: list[bytes]) -> list[float]:
    """Each line's timestamp; a line without one inherits the previous line's."""
    out: list[float] = []
    prev = 0.0
    for line in lines:
        obj = parse_line(line)
        ts = obj.get("timestamp") if obj else None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            prev = float(ts)
        out.append(prev)
    return out


def merge_history(current: list[bytes], archived: list[bytes]) -> list[bytes]:
    """Merge archived lines back into history, sorted by timestamp, without duplicates."""
    cur = [ensure_newline(line) for line in current]
    arc = [ensure_newline(line) for line in archived]
    present = set(cur)
    items: list[tuple[float, int, int, bytes]] = [
        (ts, 0, i, line) for i, (ts, line) in enumerate(zip(_effective_timestamps(cur), cur, strict=True))
    ]
    for i, (ts, line) in enumerate(zip(_effective_timestamps(arc), arc, strict=True)):
        if line not in present:
            present.add(line)
            items.append((ts, 1, i, line))
    items.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in items]


# ------------------------------------------------------------ file system layer

FileStamp = tuple[int, int, int]


def file_stamp(path: str) -> FileStamp | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _list(path: str) -> list[str] | None:
    """Names in ``path``; [] when it is missing; None when it can't be read (locked)."""
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        return []
    except (PermissionError, NotADirectoryError):
        return None


@dataclass
class SweepPlan:
    history_path: str
    history_stamp: FileStamp | None
    kept: list[bytes] = field(default_factory=list[bytes])
    vaulted: list[bytes] = field(default_factory=list[bytes])
    pastes: list[str] = field(default_factory=list[str])
    file_history: list[str] = field(default_factory=list[str])
    scratch: list[str] = field(default_factory=list[str])
    unreadable_projects: list[str] = field(default_factory=list[str])

    def counts(self) -> dict[str, object]:
        return {
            "history_lines": len(self.vaulted),
            "paste_files": len(self.pastes),
            "file_history_folders": len(self.file_history),
            "scratch_folders": len(self.scratch),
            "unreadable_project_folders": len(self.unreadable_projects),
        }

    def is_empty(self) -> bool:
        return not (self.vaulted or self.pastes or self.file_history or self.scratch)


def expand_roots(paths: tuple[str, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    for p in paths:
        for v in (os.path.normpath(p), os.path.realpath(p)):
            if v not in roots:
                roots.append(v)
    return tuple(roots)


def plan_sweep(settings: SweepSettings, vault_paths: tuple[str, ...]) -> SweepPlan:
    """Read the shared files and decide what a sweep would move. Changes nothing."""
    roots = expand_roots(vault_paths)
    stamp = file_stamp(settings.history)
    plan = SweepPlan(history_path=settings.history, history_stamp=stamp)
    if stamp is not None:
        with open(settings.history, "rb") as fh:
            lines = split_lines(fh.read())
        if file_stamp(settings.history) != stamp:
            raise ConcurrentChangeError(settings.history)
        plan.kept, plan.vaulted = split_history(lines, roots)
        wanted = pastes_to_move(plan.kept, plan.vaulted)
        plan.pastes = sorted(h for h in wanted if os.path.isfile(os.path.join(settings.paste_cache, f"{h}.txt")))

    session_ids: set[str] = set()
    for root in vault_paths:
        if not is_under(root, settings.projects_dir) or root == os.path.normpath(settings.projects_dir):
            continue
        names = _list(root)
        if names is None:
            plan.unreadable_projects.append(root)
            continue
        session_ids |= session_ids_from_names(names)
    fh_names = _list(settings.file_history) or []
    plan.file_history = [
        n for n in file_history_to_move(fh_names, session_ids) if os.path.isdir(os.path.join(settings.file_history, n))
    ]

    scratch_names = _list(settings.scratch_root) or []
    plan.scratch = [
        os.path.join(settings.scratch_root, n) for n in scratch_matches(scratch_names, settings.scratch_globs)
    ]
    return plan


def _free_name(dst: str) -> str:
    n = 1
    while os.path.lexists(f"{dst}.{n}"):
        n += 1
    return f"{dst}.{n}"


def _same_content(a: str, b: str) -> bool:
    if os.path.islink(a) or os.path.islink(b):
        return os.path.islink(a) and os.path.islink(b) and os.readlink(a) == os.readlink(b)
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca = fa.read(1 << 20)
            cb = fb.read(1 << 20)
            if ca != cb:
                return False
            if not ca:
                return True


def move_file(src: str, dst: str) -> str:
    """Move one file into the vault; an existing different file is kept beside it."""
    os.makedirs(os.path.dirname(dst), mode=DIR_MODE, exist_ok=True)
    if os.path.lexists(dst):
        if _same_content(src, dst):
            os.unlink(src)
            return dst
        dst = _free_name(dst)
    if os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)
        with open(dst, "rb") as fh:
            os.fsync(fh.fileno())
    os.unlink(src)
    return dst


def move_tree(src: str, dst: str) -> int:
    """Move a folder into the vault file by file, merging with what is there."""
    moved = 0
    for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, mode=DIR_MODE, exist_ok=True)
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                move_file(full, os.path.join(target, name))
                dirnames.remove(name)
                moved += 1
        for name in filenames:
            move_file(os.path.join(dirpath, name), os.path.join(target, name))
            moved += 1
    for dirpath, _dirnames, _files in os.walk(src, topdown=False):
        os.rmdir(dirpath)
    return moved


def _delete_scratch(path: str, scratch_root: str) -> None:
    if os.path.dirname(path) != os.path.normpath(scratch_root):
        raise FinvaultError(f"refusing to delete {path}: not directly under {scratch_root}")
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _commit_history(path: str, stamp: FileStamp, new_content: bytes, archive_file: str, add: list[bytes]) -> None:
    """Rewrite history atomically; append ``add`` to the archive just before the rename."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    tmp = write_temp_beside(path, new_content, mode)
    committed = False
    try:
        if file_stamp(path) != stamp:
            raise ConcurrentChangeError(path)
        if add:
            append_fsync(archive_file, b"".join(add), FILE_MODE)
        os.replace(tmp, path)
        committed = True
    finally:
        if not committed and os.path.exists(tmp):
            os.unlink(tmp)
    fsync_dir(os.path.dirname(path))


def read_lines(path: str) -> list[bytes]:
    try:
        with open(path, "rb") as fh:
            return split_lines(fh.read())
    except FileNotFoundError:
        return []


def apply_sweep(plan: SweepPlan, settings: SweepSettings, archive_dir: str) -> dict[str, object]:
    """Carry out a plan. History goes first: if it changed, nothing else moves."""
    report = dict(plan.counts())
    if plan.is_empty():
        return report
    os.makedirs(archive_dir, mode=DIR_MODE, exist_ok=True)
    if plan.vaulted and plan.history_stamp is not None:
        archive_file = os.path.join(archive_dir, ARCHIVE_HISTORY)
        add = new_archive_lines(read_lines(archive_file), plan.vaulted)
        _commit_history(plan.history_path, plan.history_stamp, b"".join(plan.kept), archive_file, add)
        report["history_lines_archived"] = len(add)
    for h in plan.pastes:
        src = os.path.join(settings.paste_cache, f"{h}.txt")
        if os.path.lexists(src):
            move_file(src, os.path.join(archive_dir, ARCHIVE_PASTE_CACHE, f"{h}.txt"))
    for sid in plan.file_history:
        move_tree(os.path.join(settings.file_history, sid), os.path.join(archive_dir, ARCHIVE_FILE_HISTORY, sid))
    for path in plan.scratch:
        if os.path.lexists(path):
            _delete_scratch(path, settings.scratch_root)
    return report


def restore_history(settings: SweepSettings, archive_dir: str) -> dict[str, object]:
    """Merge archived history lines back and copy back missing paste files."""
    archive_file = os.path.join(archive_dir, ARCHIVE_HISTORY)
    archived = read_lines(archive_file)
    stamp = file_stamp(settings.history)
    current = read_lines(settings.history) if stamp is not None else []
    merged = merge_history(current, archived)
    added = len(merged) - len(current)
    if added:
        if stamp is None:
            os.makedirs(os.path.dirname(settings.history), mode=DIR_MODE, exist_ok=True)
            tmp = write_temp_beside(settings.history, b"".join(merged), FILE_MODE)
            if os.path.lexists(settings.history):
                os.unlink(tmp)
                raise ConcurrentChangeError(settings.history)
            os.replace(tmp, settings.history)
        else:
            _commit_history(settings.history, stamp, b"".join(merged), archive_file, [])
    pastes_restored = 0
    paste_src = os.path.join(archive_dir, ARCHIVE_PASTE_CACHE)
    for name in _list(paste_src) or []:
        dst = os.path.join(settings.paste_cache, name)
        if name.endswith(".txt") and not os.path.lexists(dst):
            os.makedirs(settings.paste_cache, mode=DIR_MODE, exist_ok=True)
            shutil.copy2(os.path.join(paste_src, name), dst)
            pastes_restored += 1
    return {"history_lines_restored": added, "paste_files_restored": pastes_restored}

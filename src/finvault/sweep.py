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
import tempfile
from dataclasses import dataclass, field

from finvault.config import SweepSettings
from finvault.errors import ConcurrentChangeError, FinvaultError
from finvault.jsonutil import as_dict
from finvault.pathutil import (
    append_fsync,
    ensure_dir_inside,
    fsync_dir,
    is_under,
    is_under_any,
    trim_partial_line,
    write_temp_beside,
)

HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ARCHIVE_HISTORY = "history.jsonl"
ARCHIVE_FILE_HISTORY = "file-history"
ARCHIVE_PASTE_CACHE = "paste-cache"
# Late lines for other projects that could not go back into the shared history
# (a full main disk). restore-history merges them back.
ARCHIVE_OVERFLOW = "history-overflow.jsonl"
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
    """Hashes referenced by vaulted lines and by no kept line.

    Pass the lines already in the vault's archive along with ``vaulted``, so a
    paste whose move failed after its history line was archived is found again.
    """
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
    problems: list[str] = field(default_factory=list[str])
    session_ids: set[str] = field(default_factory=set[str])
    paste_hashes: set[str] = field(default_factory=set[str])

    def counts(self) -> dict[str, object]:
        return {
            "history_lines": len(self.vaulted),
            "paste_files": len(self.pastes),
            "file_history_folders": len(self.file_history),
            "scratch_folders": len(self.scratch),
            "unreadable_project_folders": len(self.unreadable_projects),
            "problems": list(self.problems),
        }

    def is_empty(self) -> bool:
        return not (self.vaulted or self.pastes or self.file_history or self.scratch)

    def is_complete(self) -> bool:
        """False when part of what the sweep must check could not be checked."""
        return not self.problems


def expand_roots(paths: tuple[str, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    for p in paths:
        for v in (os.path.normpath(p), os.path.realpath(p)):
            if v not in roots:
                roots.append(v)
    return tuple(roots)


def read_archived_lines(archive_dir: str | None) -> list[bytes]:
    """History lines already in the vault's archive; [] when it is missing or locked."""
    if archive_dir is None:
        return []
    try:
        return read_lines(os.path.join(archive_dir, ARCHIVE_HISTORY))
    except (PermissionError, NotADirectoryError):
        return []


def plan_sweep(settings: SweepSettings, vault_paths: tuple[str, ...], archive_dir: str | None = None) -> SweepPlan:
    """Read the shared files and decide what a sweep would move. Changes nothing.

    ``archive_dir`` is the vault's sweep archive; its history lines name pastes
    that earlier, interrupted sweeps did not finish moving.
    """
    roots = expand_roots(vault_paths)
    stamp = file_stamp(settings.history)
    plan = SweepPlan(history_path=settings.history, history_stamp=stamp)
    if stamp is not None:
        links = os.lstat(settings.history).st_nlink
        if links > 1:
            plan.problems.append(
                f"{settings.history} has {links} hard links; another name for it would keep the swept lines"
            )
        with open(settings.history, "rb") as fh:
            lines = split_lines(fh.read())
        if file_stamp(settings.history) != stamp:
            raise ConcurrentChangeError(settings.history)
        plan.kept, plan.vaulted = split_history(lines, roots)
    plan.problems.extend(
        f"{leftover} was left by an interrupted rewrite and may hold vault history; check it, then delete it"
        for leftover in _leftover_rewrites(settings.history)
    )
    plan.problems.extend(
        f"{leftover} was left by an interrupted paste restore and holds a vault paste; check it, then delete it"
        for leftover in _leftover_temps(settings.paste_cache, ".finvault-")
    )
    archived = read_archived_lines(archive_dir)
    wanted = pastes_to_move(plan.kept, plan.vaulted + archived)
    # Every paste the vault's lines name, shared or not: traces_left drops the
    # ones still shared when it runs, since sharing can end during the close.
    for vault_line in plan.vaulted + archived:
        plan.paste_hashes |= paste_hashes(vault_line)
    if wanted and _list(settings.paste_cache) is None:
        plan.problems.append(f"can't read {settings.paste_cache}")
    for h in sorted(wanted):
        paste = os.path.join(settings.paste_cache, f"{h}.txt")
        if os.path.isfile(paste):
            plan.pastes.append(h)
            plan.problems.extend(_hard_link_problems(paste))

    session_ids: set[str] = set()
    for root in vault_paths:
        if is_under(settings.projects_dir, root):
            # The whole projects folder is in the vault: every session there is a vault session.
            session_ids |= _all_project_sessions(settings.projects_dir, plan)
            continue
        if not is_under(root, settings.projects_dir):
            continue
        names = _list(root)
        if names is None:
            plan.unreadable_projects.append(root)
            plan.problems.append(f"can't read {root}, so its sessions' file-history can't be found")
            continue
        session_ids |= session_ids_from_names(names)
    plan.session_ids = session_ids
    fh_names = _list(settings.file_history)
    if fh_names is None:
        plan.problems.append(f"can't read {settings.file_history}")
    for name in file_history_to_move(fh_names or [], session_ids):
        entry = os.path.join(settings.file_history, name)
        mode = os.lstat(entry).st_mode
        if stat.S_ISLNK(mode):
            plan.problems.append(f"{entry} is a symlink; finvault won't follow it out of {settings.file_history}")
        elif stat.S_ISDIR(mode):
            plan.file_history.append(name)
            for dirpath, _dirs, files in os.walk(entry, onerror=_raise_walk_error):
                for f in files:
                    plan.problems.extend(_hard_link_problems(os.path.join(dirpath, f)))

    scratch_names = _list(settings.scratch_root)
    if scratch_names is None:
        plan.problems.append(f"can't read {settings.scratch_root}")
    plan.scratch = [
        os.path.join(settings.scratch_root, n) for n in scratch_matches(scratch_names or [], settings.scratch_globs)
    ]
    return plan


def _all_project_sessions(projects_dir: str, plan: SweepPlan) -> set[str]:
    """Session ids of every project folder in ``projects_dir``; unreadable ones become problems."""
    ids: set[str] = set()
    names = _list(projects_dir)
    if names is None:
        plan.unreadable_projects.append(projects_dir)
        plan.problems.append(f"can't read {projects_dir}, so its sessions' file-history can't be found")
        return ids
    ids |= session_ids_from_names(names)
    for name in names:
        entry = os.path.join(projects_dir, name)
        mode = os.lstat(entry).st_mode
        if stat.S_ISLNK(mode):
            plan.problems.append(f"{entry} is a symlink; finvault won't follow it to find sessions")
        elif stat.S_ISDIR(mode):
            inner = _list(entry)
            if inner is None:
                plan.unreadable_projects.append(entry)
                plan.problems.append(f"can't read {entry}, so its sessions' file-history can't be found")
            else:
                ids |= session_ids_from_names(inner)
    return ids


def _raise_walk_error(err: OSError) -> None:
    raise err


def _hard_link_problems(path: str) -> list[str]:
    st = os.lstat(path)
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        return [f"{path} has {st.st_nlink} hard links; another name for it would keep its contents after the sweep"]
    return []


def _leftover_temps(folder: str, prefix: str) -> list[str]:
    """Temp files named ``<prefix>*.tmp`` that a killed finvault left in ``folder``."""
    return [os.path.join(folder, n) for n in _list(folder) or [] if n.startswith(prefix) and n.endswith(".tmp")]


def _leftover_rewrites(history: str) -> list[str]:
    """Temp files a killed history rewrite left beside ``history`` (named by ``write_temp_beside``)."""
    return _leftover_temps(os.path.dirname(history) or ".", f".{os.path.basename(history)}.")


def vaulted_history_lines(settings: SweepSettings, vault_paths: tuple[str, ...]) -> int:
    """How many lines in the shared history belong to the vault right now."""
    return len(split_history(read_lines(settings.history), expand_roots(vault_paths))[1])


def traces_left(
    settings: SweepSettings, vault_paths: tuple[str, ...], session_ids: set[str], known_pastes: set[str]
) -> dict[str, object]:
    """What belongs in the vault but sits outside it now, for a check after the images close.

    ``session_ids`` and ``known_pastes`` come from the last plan, made while the
    projects folders and the archive could still be read. Pastes named by vault
    lines still in the shared history are checked too.
    """
    out: dict[str, object] = {}
    kept, vaulted = split_history(read_lines(settings.history), expand_roots(vault_paths))
    if vaulted:
        out["history_lines"] = len(vaulted)
    unreadable: list[str] = []
    roots = expand_roots(vault_paths)

    def cant_check(folder: str) -> bool:
        # A folder that can't be listed hides what is in it, so it is never taken
        # as empty, unless it is itself inside the vault (a closed, mode-000 image).
        return _list(folder) is None and not is_under_any(folder, roots)

    for folder in (settings.paste_cache, os.path.dirname(settings.history) or "."):
        if cant_check(folder):
            unreadable.append(folder)
    # A paste another project's lines still use is shared, not a vault trace (as in pastes_to_move).
    shared: set[str] = set()
    for line in kept:
        shared |= paste_hashes(line)
    hashes = (known_pastes | pastes_to_move(kept, vaulted)) - shared
    pastes = sorted(h for h in hashes if os.path.lexists(os.path.join(settings.paste_cache, f"{h}.txt")))
    if pastes:
        out["paste_files"] = pastes
    leftovers = _leftover_rewrites(settings.history) + _leftover_temps(settings.paste_cache, ".finvault-")
    if leftovers:
        out["leftover_temps"] = leftovers
    fh_names = _list(settings.file_history)
    if cant_check(settings.file_history):
        unreadable.append(settings.file_history)
    backups = file_history_to_move(fh_names or [], session_ids)
    if backups:
        out["file_history_folders"] = backups
    scratch_names = _list(settings.scratch_root)
    if cant_check(settings.scratch_root):
        unreadable.append(settings.scratch_root)
    scratch = scratch_matches(scratch_names or [], settings.scratch_globs)
    if scratch:
        out["scratch_folders"] = [os.path.join(settings.scratch_root, n) for n in scratch]
    if unreadable:
        # Can't tell what is there, so this is not a clean lock either.
        out["unreadable"] = unreadable
    return out


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


def _identity(st: os.stat_result) -> tuple[int, ...]:
    # ctime moves on every write and can't be set back, unlike mtime.
    return (st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _unlink_unchanged(src: str, before: os.stat_result) -> None:
    """Delete ``src`` only if nothing wrote to it since ``before``; otherwise keep it for a later pass."""
    if _identity(os.lstat(src)) != _identity(before):
        raise ConcurrentChangeError(src)
    os.unlink(src)


def _link_in(tmp: str, dst: str) -> str:
    """Give ``tmp`` the name ``dst`` (or a free ``dst.N``), never replacing or following an entry."""
    target = dst
    while True:
        try:
            os.link(tmp, target)
            return target
        except FileExistsError:
            target = _free_name(dst)


def _copy_in(src: str, dst: str, before: os.stat_result) -> str:
    """Copy regular file ``src`` into a new private file and link it in at ``dst``."""
    folder = os.path.dirname(dst)
    src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(src_fd, "rb") as inp:
        if _identity(os.fstat(inp.fileno())) != _identity(before):
            raise ConcurrentChangeError(src)
        fd, tmp = tempfile.mkstemp(prefix=".finvault-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(inp, out, 1 << 20)
                out.flush()
                os.fchmod(out.fileno(), stat.S_IMODE(before.st_mode))
                os.fsync(out.fileno())
            os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
            return _link_in(tmp, dst)
        finally:
            os.unlink(tmp)


def _symlink_in(link: str, dst: str) -> str:
    target = dst
    while True:
        try:
            os.symlink(link, target)
            return target
        except FileExistsError:
            if os.path.islink(target) and os.readlink(target) == link:
                return target
            target = _free_name(dst)


def move_file(src: str, dst: str, root: str) -> str:
    """Move one file into the vault below ``root``; an existing different file is kept beside it.

    The copy is written to a new private file and linked in, so an entry put
    at ``dst`` meanwhile is never replaced or followed. ``src`` is deleted only
    after the copy's folder entry is on disk and only if ``src`` is unchanged.
    """
    folder = os.path.dirname(dst)
    ensure_dir_inside(folder, root, DIR_MODE)
    before = os.lstat(src)
    problems = _hard_link_problems(src)
    if problems:
        raise FinvaultError(problems[0], "remove the other names for the file, then run the command again")
    if os.path.lexists(dst):
        dst_before = os.lstat(dst)
        if _same_content(src, dst):
            fsync_dir(folder)
            # The copy already there counts only if it is still the one compared.
            if _identity(os.lstat(dst)) != _identity(dst_before):
                raise ConcurrentChangeError(dst)
            _unlink_unchanged(src, before)
            return dst
    if stat.S_ISLNK(before.st_mode):
        placed = _symlink_in(os.readlink(src), dst)
    elif stat.S_ISREG(before.st_mode):
        placed = _copy_in(src, dst, before)
    else:
        raise FinvaultError(f"{src} is not a regular file or symlink", "move or delete it by hand, then lock again")
    fsync_dir(folder)
    _unlink_unchanged(src, before)
    return placed


def move_tree(src: str, dst: str, root: str) -> int:
    """Move a folder into the vault below ``root`` file by file, merging with what is there."""
    if os.path.islink(src) or not os.path.isdir(src):
        raise FinvaultError(f"{src} is a symlink or not a folder", "finvault won't follow it; move it by hand")
    moved = 0
    for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        ensure_dir_inside(target, root, DIR_MODE)
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                move_file(full, os.path.join(target, name), root)
                dirnames.remove(name)
                moved += 1
        for name in filenames:
            move_file(os.path.join(dirpath, name), os.path.join(target, name), root)
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


def _read_tail(fd: int, offset: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.pread(fd, 1 << 20, offset):
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _commit_history(
    path: str,
    stamp: FileStamp,
    new_content: bytes,
    archive_file: str,
    add: list[bytes],
    *,
    roots: tuple[str, ...],
) -> int:
    """Rewrite history atomically and return how many late-appended lines were recovered.

    The stamp is checked before ``add`` goes to the archive and again after,
    right before the rename (a retry skips lines already archived). Claude Code appends
    without locking, so a line can still land on the old file between that check
    and the rename: it is read back from the old file and split again, vaulted
    lines to the archive and the rest onto the new history.

    The tail is read before anything else can fail, so a failed folder sync
    still saves the late lines before the error is raised.
    """
    mode = stat.S_IMODE(os.stat(path).st_mode)
    tmp = write_temp_beside(path, new_content, mode)
    committed = False
    tail = b""
    try:
        if file_stamp(path) != stamp:
            raise ConcurrentChangeError(path)
        if add:
            append_fsync(archive_file, b"".join(add), FILE_MODE)
        old_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(old_fd)
            if (st.st_size, st.st_mtime_ns, st.st_ino) != stamp or file_stamp(path) != stamp:
                raise ConcurrentChangeError(path)
            if st.st_nlink > 1:
                raise FinvaultError(
                    f"{path} has {st.st_nlink} hard links; another name for it would keep the swept lines",
                    "remove the other names for the file, then run the command again",
                )
            os.replace(tmp, path)
            committed = True
            tail = _read_tail(old_fd, stamp[0])
        finally:
            os.close(old_fd)
    finally:
        if not committed and os.path.exists(tmp):
            os.unlink(tmp)
    failures: list[OSError] = []
    try:
        fsync_dir(os.path.dirname(path))
    except OSError as exc:
        failures.append(exc)
    late = split_lines(tail)
    if late:
        failures.extend(_save_late_lines(path, mode, archive_file, *split_history(late, roots)))
    if failures:
        raise failures[0]
    return len(late)


def _save_late_lines(path: str, mode: int, archive_file: str, kept: list[bytes], vaulted: list[bytes]) -> list[OSError]:
    """Write late lines where they belong, each destination tried on its own.

    Memory holds the only copy, so a failure never stops the other write. Vaulted
    lines that can't reach the archive go back into history for the next sweep;
    other lines that can't reach history are parked in ``ARCHIVE_OVERFLOW``.
    """
    failures: list[OSError] = []
    if kept:
        try:
            append_fsync(path, b"".join(kept), mode)
        except OSError as exc:
            failures.append(exc)
            # Park them inside the vault rather than lose them; restore-history returns them.
            overflow = os.path.join(os.path.dirname(archive_file), ARCHIVE_OVERFLOW)
            try:
                trim_partial_line(overflow)
                append_fsync(overflow, b"".join(ensure_newline(line) for line in kept), FILE_MODE)
            except OSError as again:
                failures.append(again)
    if vaulted:
        try:
            append_fsync(archive_file, b"".join(new_archive_lines(read_lines(archive_file), vaulted)), FILE_MODE)
        except OSError as exc:
            failures.append(exc)
            try:
                append_fsync(path, b"".join(vaulted), mode)
            except OSError as again:
                failures.append(again)
    return failures


def read_lines(path: str) -> list[bytes]:
    try:
        with open(path, "rb") as fh:
            return split_lines(fh.read())
    except FileNotFoundError:
        return []


def apply_sweep(
    plan: SweepPlan, settings: SweepSettings, archive_dir: str, vault_root: str, vault_paths: tuple[str, ...]
) -> dict[str, object]:
    """Carry out a plan. History goes first: if it changed, nothing else moves.

    Everything is written below ``archive_dir``, which must sit inside the
    unlocked ``vault_root`` with no symlink on the way. A plan that could not
    check everything is refused: moving part of the traces would let ``lock``
    report a clean vault.
    """
    if not plan.is_complete():
        raise FinvaultError(
            "the sweep can't check everything it must move: " + "; ".join(plan.problems),
            "fix each problem listed (or use lock --skip-sweep to close without sweeping), then run it again",
            details={"problems": list(plan.problems)},
        )
    report = dict(plan.counts())
    if plan.is_empty():
        return report
    ensure_dir_inside(archive_dir, vault_root, DIR_MODE)
    for name in (ARCHIVE_HISTORY, ARCHIVE_PASTE_CACHE, ARCHIVE_FILE_HISTORY, ARCHIVE_OVERFLOW):
        entry = os.path.join(archive_dir, name)
        if os.path.islink(entry):
            raise FinvaultError(
                f"{entry} is a symlink",
                "finvault keeps swept data only inside the vault; move it away and run the command again",
            )
    archive_file = os.path.join(archive_dir, ARCHIVE_HISTORY)
    trimmed = trim_partial_line(archive_file)
    if trimmed:
        report["archive_partial_bytes_trimmed"] = trimmed
    if plan.vaulted and plan.history_stamp is not None:
        add = new_archive_lines(read_lines(archive_file), plan.vaulted)
        late = _commit_history(
            plan.history_path,
            plan.history_stamp,
            b"".join(plan.kept),
            archive_file,
            add,
            roots=expand_roots(vault_paths),
        )
        report["history_lines_archived"] = len(add)
        report["history_lines_written_meanwhile"] = late
    for h in plan.pastes:
        src = os.path.join(settings.paste_cache, f"{h}.txt")
        if os.path.lexists(src):
            move_file(src, os.path.join(archive_dir, ARCHIVE_PASTE_CACHE, f"{h}.txt"), vault_root)
    for sid in plan.file_history:
        move_tree(
            os.path.join(settings.file_history, sid),
            os.path.join(archive_dir, ARCHIVE_FILE_HISTORY, sid),
            vault_root,
        )
    for path in plan.scratch:
        if os.path.lexists(path):
            _delete_scratch(path, settings.scratch_root)
    return report


def restore_history(settings: SweepSettings, archive_dir: str) -> dict[str, object]:
    """Merge archived history lines back and copy back missing paste files."""
    archive_file = os.path.join(archive_dir, ARCHIVE_HISTORY)
    overflow = os.path.join(archive_dir, ARCHIVE_OVERFLOW)
    trim_partial_line(archive_file)
    trim_partial_line(overflow)
    archived = read_lines(archive_file) + read_lines(overflow)
    stamp = file_stamp(settings.history)
    current = read_lines(settings.history) if stamp is not None else []
    merged = merge_history(current, archived)
    added = len(merged) - len(current)
    if added:
        if stamp is None:
            os.makedirs(os.path.dirname(settings.history), mode=DIR_MODE, exist_ok=True)
            tmp = write_temp_beside(settings.history, b"".join(merged), FILE_MODE)
            try:
                if os.path.lexists(settings.history):
                    raise ConcurrentChangeError(settings.history)
                os.link(tmp, settings.history)
            finally:
                os.unlink(tmp)
        else:
            _commit_history(settings.history, stamp, b"".join(merged), archive_file, [], roots=())
    pastes_restored = 0
    conflicts: list[str] = []
    paste_src = os.path.join(archive_dir, ARCHIVE_PASTE_CACHE)
    for name in _list(paste_src) or []:
        src = os.path.join(paste_src, name)
        dst = os.path.join(settings.paste_cache, name)
        if not name.endswith(".txt") or not os.path.isfile(src) or os.path.islink(src):
            continue
        if os.path.lexists(dst):
            if not _same_content(src, dst):
                conflicts.append(dst)
            continue
        os.makedirs(settings.paste_cache, mode=DIR_MODE, exist_ok=True)
        if _restore_copy(src, dst):
            pastes_restored += 1
        else:
            conflicts.append(dst)
    report: dict[str, object] = {"history_lines_restored": added, "paste_files_restored": pastes_restored}
    if conflicts:
        report["paste_files_differing"] = conflicts
    return report


def _restore_copy(src: str, dst: str) -> bool:
    """Copy ``src`` to a new ``dst`` through a private temp file; False if ``dst`` appeared meanwhile."""
    fd, tmp = tempfile.mkstemp(prefix=".finvault-", suffix=".tmp", dir=os.path.dirname(dst))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
            out.flush()
            os.fchmod(out.fileno(), FILE_MODE)
            os.fsync(out.fileno())
        try:
            os.link(tmp, dst)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)

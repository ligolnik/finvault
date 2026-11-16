"""The finvault commands. Each takes a ``Config`` and ``Deps`` and returns an ``Outcome``.

Images are identified by their image path as ``hdiutil info`` reports it, never by
mountpoint or device name. Every image a vault owns lives under its own
``state_dir``, so two vaults on one Mac never touch each other's images.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from finvault import manifest as mf
from finvault import sweep as sw
from finvault.config import Config, Folder
from finvault.errors import (
    EXIT_BUSY,
    EXIT_FAILURE,
    EXIT_MISMATCH,
    EXIT_OK,
    ConcurrentChangeError,
    FinvaultError,
)
from finvault.jsonutil import as_dict
from finvault.pathutil import same_path
from finvault.platform import Attached, BusyTerminal, Platform, Process, match_terminals, size_mb

STATE_SCHEMA_VERSION = 1
STATE_MARKER = "vault.json"
DIR_MODE = 0o700
LOCKED_MODE = 0o000
MIN_PASSPHRASE_LEN = 12
KILL_WAIT_SEC = 15.0
KILL_POLL_SEC = 0.5
SWEEP_ATTEMPTS = 3
SWEEP_RETRY_SEC = 1.0
HOLDER_CHECKS = 3
HOLDER_RECHECK_SEC = 0.2
SIZE_UNITS = {"m": 1, "g": 1024, "t": 1024 * 1024}


@dataclass
class Deps:
    platform: Platform
    ask_passphrase: Callable[[], str]
    ask_new_passphrase: Callable[[], str]
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    warn: Callable[[str], None]


@dataclass
class Outcome:
    data: dict[str, object] = field(default_factory=dict[str, object])
    exit_code: int = EXIT_OK


# ------------------------------------------------------------------ state


def _marker_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, STATE_MARKER)


def check_state(cfg: Config) -> None:
    """Refuse a state folder that another config already owns."""
    path = _marker_path(cfg)
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, PermissionError) as exc:
        raise FinvaultError(f"cannot read {path}: {exc}", "fix or remove that file by hand") from None
    marker = as_dict(data)
    if marker is None or marker.get("schema_version") != STATE_SCHEMA_VERSION:
        raise FinvaultError(f"{path} has an unknown format", "update finvault")
    owner = marker.get("config_path")
    if owner != cfg.config_path:
        raise FinvaultError(
            f"state folder {cfg.state_dir} belongs to another vault ({owner})",
            "give each vault its own state_dir; if you moved this config, edit config_path in " + path,
        )


def ensure_state(cfg: Config) -> None:
    """Create the state folders (mode 700) and claim them for this config."""
    check_state(cfg)
    for d in (cfg.state_dir, cfg.images_dir, cfg.archive_dir, cfg.mnt_dir):
        os.makedirs(d, mode=DIR_MODE, exist_ok=True)
        os.chmod(d, DIR_MODE)
    path = _marker_path(cfg)
    if not os.path.exists(path):
        data = {"schema_version": STATE_SCHEMA_VERSION, "config_path": cfg.config_path}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.write("\n")


# ---------------------------------------------------------------- helpers


def attached_by_folder(cfg: Config, deps: Deps) -> dict[str, Attached]:
    """This vault's attached images, keyed by folder name (matched on image path)."""
    attached = deps.platform.attached_images()
    out: dict[str, Attached] = {}
    for folder in cfg.folders:
        image = os.path.realpath(cfg.image_path(folder))
        for att in attached:
            if att.image_path == image:
                out[folder.name] = att
    return out


def is_unlocked_at_path(folder: Folder, att: Attached | None) -> bool:
    if att is None:
        return False
    return any(same_path(mp, folder.path) for mp in att.mount_points)


def _mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return None


def _is_empty_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.listdir(path)


def _detach_image(cfg: Config, deps: Deps, image: str) -> None:
    """Close one image, looking up its current device by image path first."""
    real = os.path.realpath(image)
    for att in deps.platform.attached_images():
        if att.image_path == real:
            if att.device is None:
                raise FinvaultError(f"can't find the device for {image}", "close it with Disk Utility")
            deps.platform.detach(att.device)
            return


def _parse_size(size: str) -> int:
    return int(size[:-1]) * SIZE_UNITS[size[-1]]


def disk_usage(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        total += os.lstat(dirpath).st_blocks * 512
        for name in files:
            total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
    return total


def image_size_mb(cfg: Config, folder: Folder) -> int:
    if folder.size:
        return _parse_size(folder.size)
    used = disk_usage(folder.path) if os.path.isdir(folder.path) else 0
    return max(cfg.image.min_size_gb * 1024, size_mb(used * cfg.image.size_factor))


def _existing_unattached_image(cfg: Config, deps: Deps, exclude: str | None = None) -> str | None:
    attached = {a.image_path for a in deps.platform.attached_images()}
    for f in cfg.folders:
        image = cfg.image_path(f)
        if image != exclude and os.path.isdir(image) and os.path.realpath(image) not in attached:
            return image
    return None


def _vault_passphrase(cfg: Config, deps: Deps, exclude: str | None = None) -> str:
    """Ask for the vault's passphrase and check it against one of its images.

    A closed image is preferred; an unlocked one is checked too, so a new image
    or archive can never get a different passphrase.
    """
    images = [
        cfg.image_path(f) for f in cfg.folders if cfg.image_path(f) != exclude and os.path.isdir(cfg.image_path(f))
    ]
    if not images:
        return deps.ask_new_passphrase()
    probe = _existing_unattached_image(cfg, deps, exclude) or images[0]
    pw = deps.ask_passphrase()
    if not deps.platform.check_passphrase(probe, pw):
        raise FinvaultError("wrong passphrase for this vault", "type the passphrase this vault's images use")
    return pw


def _busy_report(procs: list[Process]) -> list[dict[str, object]]:
    return [{"pid": p.pid, "command": p.command, "files": p.files[:5]} for p in procs]


def _busy_terminals(deps: Deps, procs: list[Process]) -> list[BusyTerminal] | None:
    """The hc terminals the busy processes run under, found by process ancestry.

    None when hc is unavailable; [] when nothing is busy (hc is not asked).
    """
    if not procs:
        return []
    terms = deps.platform.hc_terminals()
    if terms is None:
        return None
    parents = deps.platform.process_parents()
    return match_terminals([p.pid for p in procs], parents, terms, os.getpid())


def _term_report(terms: list[BusyTerminal] | None) -> list[dict[str, object]] | None:
    if terms is None:
        return None
    return [
        {
            "address": b.terminal.address,
            "cwd": b.terminal.cwd,
            "where": b.terminal.where,
            "busy_pids": list(b.busy_pids),
            "self": b.is_self,
        }
        for b in terms
    ]


def _manifest_excludes(cfg: Config, folder: Folder) -> frozenset[str]:
    if cfg.sweep is not None and cfg.sweep.archive_folder == folder.name:
        return frozenset({cfg.sweep.archive_subdir})
    return frozenset()


# ------------------------------------------------------------------- init


def init(cfg: Config, deps: Deps, name: str) -> Outcome:
    folder = cfg.folder(name)
    image = cfg.image_path(folder)
    ensure_state(cfg)
    if os.path.exists(image):
        return Outcome({"name": name, "image": image, "created": False, "reason": "image already exists"})
    size = image_size_mb(cfg, folder)
    pw = _vault_passphrase(cfg, deps, exclude=image)
    deps.platform.create_image(image, size, f"finvault-{name}", pw)
    return Outcome({"name": name, "image": image, "created": True, "size_mb": size})


# ---------------------------------------------------------------- migrate


def _require_idle(deps: Deps, path: str, *, recursive: bool) -> None:
    procs = deps.platform.open_files(path, recursive=recursive)
    if procs:
        raise FinvaultError(
            f"{len(procs)} process(es) have files open under {path}",
            "quit them (see busy) and run the command again",
            EXIT_BUSY,
            {"busy": _busy_report(procs)},
        )


def _mismatch(what: str, hint: str, diff: mf.ManifestDiff) -> FinvaultError:
    return FinvaultError(what, hint, EXIT_MISMATCH, {"diff": diff.summary()})


def migrate(cfg: Config, deps: Deps, name: str) -> Outcome:
    folder = cfg.folder(name)
    src = folder.path
    plain = src + ".plain"
    image = cfg.image_path(folder)
    check_state(cfg)
    if not os.path.isdir(image):
        raise FinvaultError(f"no image for {name}", f"run finvault init {name} first")
    if name in attached_by_folder(cfg, deps):
        raise FinvaultError(f"the image for {name} is unlocked", "run finvault lock first; migrate only runs once")
    if os.path.islink(src) or not os.path.isdir(src):
        raise FinvaultError(f"{src} is not a folder", "check the path in the config")
    if os.path.ismount(src):
        raise FinvaultError(f"{src} is already a mountpoint", "it looks migrated already")
    if os.path.lexists(plain):
        raise FinvaultError(f"{plain} already exists", "migrate stopped earlier; see the rollback steps in the README")
    if os.path.lexists(os.path.join(src, mf.MANIFEST_DIR)):
        raise FinvaultError(f"{src} contains {mf.MANIFEST_DIR}", "that name is reserved for finvault's checksum list")
    for reserved in _manifest_excludes(cfg, folder):
        if os.path.lexists(os.path.join(src, reserved)):
            raise FinvaultError(
                f"{src} contains {reserved}, the sweep archive_subdir",
                "rename that entry, or set a different [sweep] archive_subdir in the config",
            )
    _require_idle(deps, src, recursive=True)
    pw = deps.ask_passphrase()
    if not deps.platform.check_passphrase(image, pw):
        raise FinvaultError(f"wrong passphrase for {image}", "type the passphrase this vault's images use")

    # 1. checksum list of the source
    expected = mf.build_manifest(src)

    # 2-3. copy into the image at a temporary path and check the copy
    ensure_state(cfg)
    tmp = tempfile.mkdtemp(prefix=f"{name}-", dir=cfg.mnt_dir)
    try:
        deps.platform.attach(image, tmp, pw)
        deps.platform.rsync(src, tmp)
        diff = mf.compare(expected, mf.build_manifest(tmp, volume_root=True))
        if not diff.ok():
            raise _mismatch(
                f"the copy of {name} does not match the original",
                "the original is untouched; check the differences and run migrate again",
                diff,
            )
        mf.save_manifest(tmp, expected)
    finally:
        _detach_image(cfg, deps, image)
        os.rmdir(tmp)

    # 4. confirm nothing changed meanwhile, rename, unlock at the real path
    _require_idle(deps, src, recursive=True)
    diff = mf.compare(expected, mf.build_manifest(src))
    if not diff.ok():
        raise _mismatch(
            f"{src} changed during the copy",
            "make sure nothing writes there, then run migrate again",
            diff,
        )
    src_mode = stat.S_IMODE(os.stat(src).st_mode)
    os.rename(src, plain)
    os.mkdir(src, DIR_MODE)
    try:
        deps.platform.attach(image, src, pw)
    except FinvaultError:
        os.rmdir(src)
        os.rename(plain, src)
        raise
    os.chmod(src, src_mode)
    diff = mf.compare(expected, mf.build_manifest(src, volume_root=True))
    if not diff.ok():
        raise _mismatch(
            f"{name} unlocked at {src} does not match the original",
            f"the original is safe at {plain}; lock, then rename it back (README: Rollback)",
            diff,
        )
    return Outcome(
        {
            "name": name,
            "path": src,
            "plain_copy": plain,
            "files": expected.file_count(),
            "image": image,
            "next": f"check your programs work, then run finvault archive; delete {plain} by hand later",
        }
    )


# ---------------------------------------------------------------- archive


def archive(cfg: Config, deps: Deps) -> Outcome:
    check_state(cfg)
    plains = [(f, f.path + ".plain") for f in cfg.folders if os.path.isdir(f.path + ".plain")]
    if not plains:
        raise FinvaultError("no .plain folders to archive", "run finvault migrate first")
    names = [os.path.basename(p) for _f, p in plains]
    clash = sorted({n for n in names if names.count(n) > 1})
    if clash:
        raise FinvaultError(f".plain folders share a name: {', '.join(clash)}", "rename one folder before archiving")
    ensure_state(cfg)
    out = os.path.join(cfg.archive_dir, f"plain-{deps.now().strftime('%Y%m%d-%H%M%S')}.dmg")
    if os.path.lexists(out):
        raise FinvaultError(f"{out} already exists", "wait a second and run archive again")
    pw = _vault_passphrase(cfg, deps)
    expected = {os.path.basename(p): mf.build_manifest(p) for _f, p in plains}
    deps.platform.create_archive(out, [p for _f, p in plains], "finvault-archive", pw)
    os.chmod(out, 0o600)
    tmp = tempfile.mkdtemp(prefix="archive-", dir=cfg.mnt_dir)
    try:
        deps.platform.attach(out, tmp, pw, readonly=True)
        for base, want in expected.items():
            # hdiutil puts a lone -srcfolder's contents at the volume root, and
            # several -srcfolder entries each in their own top-level folder.
            if len(expected) == 1:
                got = mf.build_manifest(tmp, volume_root=True)
            else:
                got = mf.build_manifest(os.path.join(tmp, base))
            diff = mf.compare(want, got)
            if not diff.ok():
                raise _mismatch(
                    f"archive copy of {base} does not match",
                    f"delete {out} and run finvault archive again",
                    diff,
                )
    finally:
        _detach_image(cfg, deps, out)
        os.rmdir(tmp)
    return Outcome(
        {
            "archive": out,
            "folders": [f.name for f, _p in plains],
            "files": sum(m.file_count() for m in expected.values()),
            "next": "delete the .plain folders by hand once you are sure",
        }
    )


# ----------------------------------------------------------------- unlock


def _prepare_mountpoint(path: str) -> str | None:
    """Make ``path`` an empty mode-700 folder. Returns a problem, or None when ready."""
    if not os.path.lexists(path):
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            return f"parent folder {parent} is missing"
        os.mkdir(path, DIR_MODE)
        return None
    if os.path.islink(path) or not os.path.isdir(path):
        return "not a folder"
    was_locked = _mode(path) == LOCKED_MODE
    if was_locked:
        os.chmod(path, DIR_MODE)
    if os.listdir(path):
        if was_locked:
            os.chmod(path, LOCKED_MODE)
        return "folder has files in it; migrate it first or move them away"
    return None


def unlock(cfg: Config, deps: Deps) -> Outcome:
    check_state(cfg)
    att = attached_by_folder(cfg, deps)
    already: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    todo: list[Folder] = []
    for f in cfg.folders:
        if not os.path.isdir(cfg.image_path(f)):
            skipped.append({"name": f.name, "reason": "no image yet (run finvault init)"})
        elif f.name in att:
            if is_unlocked_at_path(f, att[f.name]):
                already.append(f.name)
            else:
                where = ", ".join(att[f.name].mount_points) or "no mountpoint"
                failed.append({"name": f.name, "reason": f"unlocked elsewhere ({where}); run finvault lock"})
        else:
            todo.append(f)
    ready: list[Folder] = []
    for f in todo:
        problem = _prepare_mountpoint(f.path)
        if problem:
            failed.append({"name": f.name, "reason": f"{f.path}: {problem}"})
        else:
            ready.append(f)
    unlocked: list[str] = []
    if ready:
        try:
            pw = deps.ask_passphrase()
            for i, f in enumerate(ready):
                try:
                    deps.platform.attach(cfg.image_path(f), f.path, pw)
                except FinvaultError as exc:
                    if i == 0 and "wrong passphrase" in exc.message:
                        raise
                    failed.append({"name": f.name, "reason": str(exc)})
                    continue
                unlocked.append(f.name)
        finally:
            # A cancelled prompt, a wrong passphrase or a failed attach must not
            # leave a writable plain folder where the vault belongs.
            for f in ready:
                if f.name not in unlocked:
                    os.chmod(f.path, LOCKED_MODE)
        now_att = attached_by_folder(cfg, deps)
        for name in list(unlocked):
            f = cfg.folder(name)
            if not is_unlocked_at_path(f, now_att.get(name)):
                unlocked.remove(name)
                failed.append({"name": name, "reason": "hdiutil reported success but the image is not at its path"})
            elif not os.path.exists(mf.manifest_path(f.path)):
                deps.warn(f"{name}: no checksum list inside the image; was it filled by finvault migrate?")
    data: dict[str, object] = {"unlocked": unlocked, "already_unlocked": already, "skipped": skipped, "failed": failed}
    return Outcome(data, EXIT_FAILURE if failed else EXIT_OK)


# ------------------------------------------------------------------- lock


def _relock_mountpoints(cfg: Config, deps: Deps, att: dict[str, Attached]) -> list[str]:
    """Set each closed vault folder to mode 000. Never touches a folder with files."""
    done: list[str] = []
    for f in cfg.folders:
        if f.name in att or not os.path.isdir(cfg.image_path(f)) or not os.path.isdir(f.path):
            continue
        if _mode(f.path) == LOCKED_MODE:
            done.append(f.name)
        elif _is_empty_dir(f.path):
            os.chmod(f.path, LOCKED_MODE)
            done.append(f.name)
        else:
            deps.warn(f"{f.path} has files but its image is closed; left as is (migrate it, or move them in)")
    return done


def _open_in_vault(deps: Deps, att: dict[str, Attached]) -> list[Process]:
    seen: dict[int, Process] = {}
    for a in att.values():
        for mp in a.mount_points:
            for p in deps.platform.open_files(mp):
                if p.pid in seen:
                    seen[p.pid].files.extend(p.files)
                else:
                    seen[p.pid] = p
    return sorted(seen.values(), key=lambda p: p.pid)


def _add_counts(total: dict[str, object], more: dict[str, object]) -> dict[str, object]:
    out = dict(total)
    for key, value in more.items():
        prev = out.get(key, 0)
        if isinstance(value, int) and isinstance(prev, int):
            out[key] = prev + value
    return out


def _history_holders(cfg: Config, deps: Deps) -> list[Process]:
    """Processes other than finvault that have the shared history file open."""
    assert cfg.sweep is not None
    if not os.path.lexists(cfg.sweep.history):
        return []
    return [p for p in deps.platform.open_files(cfg.sweep.history) if p.pid != os.getpid()]


def _require_history_released(cfg: Config, deps: Deps) -> None:
    """Refuse to rewrite history while a process holds it open.

    The rewrite renames a new file over the old one; a process that already has
    the old file open would keep appending to it, and those lines would be lost.
    """
    assert cfg.sweep is not None
    holders = _history_holders(cfg, deps)
    for _ in range(HOLDER_CHECKS - 1):
        if not holders:
            break
        # An append that opens and closes the file is caught now and then; only
        # a process still holding it a moment later blocks the rewrite.
        deps.sleep(HOLDER_RECHECK_SEC)
        holders = _history_holders(cfg, deps)
    if holders:
        raise FinvaultError(
            f"{len(holders)} process(es) keep {cfg.sweep.history} open; rewriting it now would lose what they "
            "write next",
            "quit them (see busy), or use lock --skip-sweep to close without sweeping, then run it again",
            EXIT_BUSY,
            {"busy": _busy_report(holders)},
        )


def _run_sweep(cfg: Config, deps: Deps) -> dict[str, object]:
    """Sweep, then plan again until nothing is left, so a late write or an
    interrupted earlier sweep cannot leave traces behind."""
    assert cfg.sweep is not None
    archive_dir = cfg.sweep_archive_dir()
    assert archive_dir is not None
    vault_root = cfg.folder(cfg.sweep.archive_folder).path
    total: dict[str, object] | None = None
    for attempt in range(1, SWEEP_ATTEMPTS + 2):
        try:
            plan = sw.plan_sweep(cfg.sweep, cfg.vault_paths(), archive_dir)
            if not plan.is_complete():
                raise FinvaultError(
                    "the sweep can't check everything it must move; nothing was closed",
                    "fix each problem listed (unlock every folder first), or use lock --skip-sweep, then lock again",
                    details={"problems": list(plan.problems)},
                )
            if plan.is_empty():
                return plan.counts() if total is None else total
            if attempt > SWEEP_ATTEMPTS:
                break
            _require_history_released(cfg, deps)
            done = sw.apply_sweep(plan, cfg.sweep, archive_dir, vault_root, cfg.vault_paths())
            total = done if total is None else _add_counts(total, done)
        except ConcurrentChangeError:
            if attempt >= SWEEP_ATTEMPTS:
                raise
            deps.sleep(SWEEP_RETRY_SEC)
    raise FinvaultError(
        f"vault traces kept appearing after {SWEEP_ATTEMPTS} sweep passes; nothing was closed",
        "a Claude Code session is still writing about the vault; close it (finvault status), then lock again",
    )


def _require_archive_unlocked(cfg: Config, att: dict[str, Attached]) -> None:
    assert cfg.sweep is not None
    folder = cfg.folder(cfg.sweep.archive_folder)
    if not is_unlocked_at_path(folder, att.get(folder.name)):
        raise FinvaultError(
            f"the sweep archive lives in {folder.name}, which is not unlocked",
            "run finvault unlock first (or lock --skip-sweep to close without sweeping)",
        )


def lock(cfg: Config, deps: Deps, *, force: bool = False, skip_sweep: bool = False) -> Outcome:
    check_state(cfg)
    att = attached_by_folder(cfg, deps)
    if not att:
        return Outcome({"locked": [], "already_locked": _relock_mountpoints(cfg, deps, att), "sweep": None})

    killed: list[str] = []
    procs = _open_in_vault(deps, att)
    terms = _busy_terminals(deps, procs)
    to_kill = [b.terminal.address for b in terms or [] if not b.is_self]
    if procs and force and to_kill:
        for address in to_kill:
            deps.platform.hc_kill(address)
            killed.append(address)
        waited = 0.0
        while procs and waited < KILL_WAIT_SEC:
            deps.sleep(KILL_POLL_SEC)
            waited += KILL_POLL_SEC
            procs = _open_in_vault(deps, att)
        terms = _busy_terminals(deps, procs)
    if procs:
        hint = "quit them (busy, terminals) or use --force to end the listed hc terminals"
        if any(b.is_self for b in terms or []):
            hint = "this terminal itself works in the vault (self: true); cd out of the vault and run lock again"
        raise FinvaultError(
            f"{len(procs)} process(es) still have files open in the vault",
            hint,
            EXIT_BUSY,
            {"busy": _busy_report(procs), "terminals": _term_report(terms), "killed": killed},
        )

    sweep_report: dict[str, object] | None = None
    if cfg.sweep is not None and not skip_sweep:
        _require_archive_unlocked(cfg, att)
        sweep_report = _run_sweep(cfg, deps)

    locked: list[str] = []
    failed: list[dict[str, str]] = []
    for name in att:
        try:
            _detach_image(cfg, deps, cfg.image_path(cfg.folder(name)))
            locked.append(name)
        except FinvaultError as exc:
            failed.append({"name": name, "reason": str(exc)})
    att_after = attached_by_folder(cfg, deps)
    relocked = _relock_mountpoints(cfg, deps, att_after)
    data: dict[str, object] = {
        "locked": locked,
        "mode_000": relocked,
        "failed": failed,
        "sweep": sweep_report,
        "killed": killed,
        "terminals": _term_report(terms),
    }
    late = 0
    if sweep_report is not None and cfg.sweep is not None:
        # A session outside the vault can still write about it after the last
        # sweep pass; say so rather than report a clean lock.
        late = sw.vaulted_history_lines(cfg.sweep, cfg.vault_paths())
        if late:
            data["history_lines_written_while_locking"] = late
            deps.warn(
                f"{late} vault history line(s) were written while locking and are still outside the vault; "
                "close the Claude Code session writing them, then unlock and lock again"
            )
    return Outcome(data, EXIT_FAILURE if failed or late else EXIT_OK)


# ----------------------------------------------------------------- status


def status(cfg: Config, deps: Deps) -> Outcome:
    check_state(cfg)
    att = attached_by_folder(cfg, deps)
    folders: list[dict[str, object]] = []
    warnings: list[str] = []
    for f in cfg.folders:
        a = att.get(f.name)
        mode = _mode(f.path)
        entry: dict[str, object] = {
            "name": f.name,
            "path": f.path,
            "image": os.path.isdir(cfg.image_path(f)),
            "unlocked": is_unlocked_at_path(f, a),
            "mountpoint_mode": None if mode is None else oct(mode),
            "plain_copy": os.path.isdir(f.path + ".plain"),
        }
        if a is not None:
            entry["busy_processes"] = len(_open_in_vault(deps, {f.name: a}))
            if not is_unlocked_at_path(f, a):
                warnings.append(f"{f.name} is unlocked at {', '.join(a.mount_points)}, not at {f.path}")
        elif entry["image"] and mode not in (None, LOCKED_MODE) and os.path.isdir(f.path) and os.listdir(f.path):
            warnings.append(f"{f.path} holds files while its image is closed")
        folders.append(entry)
    pending: dict[str, object] | None = None
    history_holders: list[dict[str, object]] | None = None
    if cfg.sweep is not None:
        history_holders = _busy_report(_history_holders(cfg, deps))
        try:
            pending = sw.plan_sweep(cfg.sweep, cfg.vault_paths(), cfg.sweep_archive_dir()).counts()
        except ConcurrentChangeError:
            pending = {"error": "history changed while reading; run status again"}
        if not att and pending.get("history_lines"):
            warnings.append("vault history lines are waiting outside the vault; unlock, then lock to sweep them")
    data: dict[str, object] = {
        "folders": folders,
        "terminals": _term_report(_busy_terminals(deps, _open_in_vault(deps, att))),
        "sweep_pending": pending,
        "history_holders": history_holders,
        "warnings": warnings,
    }
    return Outcome(data)


# ----------------------------------------------------------------- verify


def verify(cfg: Config, deps: Deps) -> Outcome:
    check_state(cfg)
    att = attached_by_folder(cfg, deps)
    results: list[dict[str, object]] = []
    bad = False
    checked = 0
    for f in cfg.folders:
        if not is_unlocked_at_path(f, att.get(f.name)):
            results.append({"name": f.name, "result": "locked, not checked"})
            continue
        checked += 1
        excludes = _manifest_excludes(cfg, f)
        expected = mf.load_manifest(f.path).without(excludes)
        actual = mf.build_manifest(f.path, volume_root=True, exclude=excludes)
        diff = mf.compare(expected, actual)
        results.append({"name": f.name, "result": "ok" if diff.ok() else "differs", **diff.summary()})
        bad = bad or not diff.ok()
    if not checked:
        raise FinvaultError("no image is unlocked, so nothing can be verified", "run finvault unlock first")
    return Outcome({"images": results}, EXIT_MISMATCH if bad else EXIT_OK)


# ------------------------------------------------------------ sweep/restore


def _require_sweep(cfg: Config) -> None:
    if cfg.sweep is None:
        raise FinvaultError(f"{cfg.config_path} has no [sweep] section", "add one (see config.example.toml)")


def sweep(cfg: Config, deps: Deps, *, dry_run: bool) -> Outcome:
    check_state(cfg)
    _require_sweep(cfg)
    assert cfg.sweep is not None
    if dry_run:
        plan = sw.plan_sweep(cfg.sweep, cfg.vault_paths(), cfg.sweep_archive_dir())
        return Outcome({"dry_run": True, **plan.counts()})
    _require_archive_unlocked(cfg, attached_by_folder(cfg, deps))
    return Outcome({"dry_run": False, **_run_sweep(cfg, deps)})


def restore_history(cfg: Config, deps: Deps) -> Outcome:
    check_state(cfg)
    _require_sweep(cfg)
    assert cfg.sweep is not None
    _require_archive_unlocked(cfg, attached_by_folder(cfg, deps))
    archive_dir = cfg.sweep_archive_dir()
    assert archive_dir is not None
    _require_history_released(cfg, deps)
    return Outcome(sw.restore_history(cfg.sweep, archive_dir))

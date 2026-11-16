"""The macOS tools finvault drives: hdiutil, plutil, rsync, lsof and hc.

``parse_*`` functions are pure. ``MacPlatform`` runs the real commands; tests swap
in a fake with the same methods. A passphrase only ever travels on stdin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from finvault.errors import EXIT_BUSY, FinvaultError
from finvault.jsonutil import as_dict, as_list

WHOLE_DISK_RE = re.compile(r"^/dev/disk[0-9]+$")


@dataclass(frozen=True)
class Attached:
    """One attached image, identified by its image path (never by device name)."""

    image_path: str
    mount_points: tuple[str, ...]
    device: str | None
    writeable: bool | None = None


@dataclass
class Process:
    pid: int
    command: str
    files: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Terminal:
    """A live local hc terminal. ``pids`` are its program's pid and its foreground pid."""

    address: str
    cwd: str
    agent: str = ""
    where: str = ""
    pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BusyTerminal:
    """A terminal some busy processes descend from. ``is_self`` marks finvault's own."""

    terminal: Terminal
    busy_pids: tuple[int, ...]
    is_self: bool = False


class Platform(Protocol):
    def attached_images(self) -> list[Attached]: ...
    def create_image(self, image: str, size_mb: int, volname: str, passphrase: str) -> None: ...
    def attach(self, image: str, mountpoint: str, passphrase: str, *, readonly: bool = False) -> None: ...
    def detach(self, device: str) -> None: ...
    def check_passphrase(self, image: str, passphrase: str) -> bool: ...
    def create_archive(self, out: str, srcfolders: Sequence[str], volname: str, passphrase: str) -> None: ...
    def rsync(self, src: str, dst: str) -> None: ...
    def open_files(self, path: str, *, recursive: bool = False) -> list[Process]: ...
    def hc_terminals(self) -> list[Terminal] | None: ...
    def hc_kill(self, address: str) -> None: ...
    def process_parents(self) -> dict[int, int]: ...


# ------------------------------------------------------------------- parsers


def parse_hdiutil_info(data: object) -> list[Attached]:
    """Turn ``hdiutil info -plist`` (decoded) into a list of ``Attached``."""
    table = as_dict(data)
    if table is None:
        raise FinvaultError("hdiutil info returned an unexpected shape", "report this with your macOS version")
    out: list[Attached] = []
    for item in as_list(table.get("images")) or []:
        im = as_dict(item)
        if im is None:
            continue
        path = im.get("image-path")
        if not isinstance(path, str):
            continue
        mounts: list[str] = []
        device: str | None = None
        for ent_item in as_list(im.get("system-entities")) or []:
            ent = as_dict(ent_item)
            if ent is None:
                continue
            dev = ent.get("dev-entry")
            if device is None and isinstance(dev, str) and WHOLE_DISK_RE.match(dev):
                device = dev
            mp = ent.get("mount-point")
            if isinstance(mp, str):
                mounts.append(os.path.realpath(mp))
        writeable = im.get("writeable")
        out.append(
            Attached(
                image_path=os.path.realpath(path),
                mount_points=tuple(mounts),
                device=device,
                writeable=writeable if isinstance(writeable, bool) else None,
            )
        )
    return out


def parse_lsof(text: str) -> list[Process]:
    """Parse ``lsof -F pcn`` output into processes and the files they hold."""
    procs: list[Process] = []
    current: Process | None = None
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            current = Process(pid=int(value), command="")
            procs.append(current)
        elif current is None:
            continue
        elif tag == "c":
            current.command = value
        elif tag == "n":
            current.files.append(value)
    return procs


def parse_hc_ls(data: object) -> list[Terminal]:
    """Live local terminals from ``hc ls --json``."""
    table = as_dict(data)
    if table is None:
        return []
    out: list[Terminal] = []
    for item in as_list(table.get("terminals")) or []:
        t = as_dict(item)
        if t is None:
            continue
        address, cwd = t.get("address"), t.get("cwd")
        if not isinstance(address, str) or not isinstance(cwd, str):
            continue
        if t.get("state", "live") != "live" or "/" in address:
            continue
        out.append(Terminal(address=address, cwd=cwd, agent=str(t.get("agent", "")), where=str(t.get("where", ""))))
    return out


def _pid(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def parse_hc_get(data: object) -> tuple[tuple[int, ...], str | None]:
    """(pids, current cwd) from ``hc get <t> --json``: the program's pid and the
    foreground process's pid, and the foreground process's working folder."""
    table = as_dict(data)
    if table is None:
        return (), None
    pids: list[int] = []
    fg = as_dict(table.get("foreground"))
    cwd: str | None = None
    for value in (table.get("pid"), fg.get("pid") if fg else None):
        pid = _pid(value)
        if pid is not None and pid not in pids:
            pids.append(pid)
    if fg is not None and isinstance(fg.get("cwd"), str) and fg.get("cwd"):
        cwd = str(fg.get("cwd"))
    return tuple(pids), cwd


def parse_ps(text: str) -> dict[int, int]:
    """pid -> parent pid from ``ps -A -o pid=,ppid=``."""
    parents: dict[int, int] = {}
    for row in text.splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def owning_terminal(pid: int, parents: dict[int, int], by_pid: dict[int, Terminal]) -> Terminal | None:
    """Walk ``pid``'s parents up to the first terminal pid; None when no terminal owns it."""
    seen: set[int] = set()
    cur = pid
    while cur > 1 and cur not in seen:
        if cur in by_pid:
            return by_pid[cur]
        seen.add(cur)
        cur = parents.get(cur, 0)
    return None


def match_terminals(
    busy_pids: Sequence[int], parents: dict[int, int], terminals: Sequence[Terminal], self_pid: int
) -> list[BusyTerminal]:
    """The terminals the busy processes descend from, in hc ls order.

    hc reports the folder a terminal started in, not where it is now, so this
    goes by process ancestry, never by cwd. Every terminal finvault itself runs
    under, however deeply nested, is marked ``is_self`` so it is never ended.
    """
    by_pid: dict[int, Terminal] = {}
    for t in terminals:
        for pid in t.pids:
            by_pid.setdefault(pid, t)
    owned: dict[str, list[int]] = {}
    for pid in busy_pids:
        t = owning_terminal(pid, parents, by_pid)
        if t is not None:
            owned.setdefault(t.address, []).append(pid)
    own = self_terminals(self_pid, parents, by_pid)
    return [BusyTerminal(t, tuple(owned[t.address]), t.address in own) for t in terminals if t.address in owned]


def self_terminals(self_pid: int, parents: dict[int, int], by_pid: dict[int, Terminal]) -> set[str]:
    """Every terminal on finvault's own ancestry, nearest and outer ones alike."""
    found: set[str] = set()
    seen: set[int] = set()
    cur = self_pid
    while cur > 1 and cur not in seen:
        if cur in by_pid:
            found.add(by_pid[cur].address)
        seen.add(cur)
        cur = parents.get(cur, 0)
    return found


def size_mb(nbytes: int) -> int:
    return max(1, -(-nbytes // (1 << 20)))


# --------------------------------------------------------------- real platform


def _stdin_pass(passphrase: str) -> bytes:
    if not passphrase or "\0" in passphrase:
        raise FinvaultError("the passphrase is empty or contains a NUL character", "use a different passphrase")
    return passphrase.encode() + b"\0"


class MacPlatform:
    """Runs the real macOS commands."""

    def __init__(self, hc_command: str = "hc") -> None:
        self.hc_command = hc_command

    def _run(self, argv: Sequence[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(list(argv), input=stdin, capture_output=True, check=False)
        except FileNotFoundError:
            raise FinvaultError(
                f"{argv[0]} not found", "finvault needs macOS with its built-in tools on PATH"
            ) from None

    def _check(self, proc: subprocess.CompletedProcess[bytes], what: str, hint: str) -> None:
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            if "Authentication error" in err:
                raise FinvaultError(f"{what}: wrong passphrase", "type the vault's passphrase again")
            raise FinvaultError(f"{what} failed (exit {proc.returncode}): {err}", hint)

    def _plist(self, data: bytes) -> object:
        proc = self._run(["plutil", "-convert", "json", "-o", "-", "-"], stdin=data)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        try:
            # Imported late: plistlib loads pyexpat, which fails on some Homebrew Pythons.
            import plistlib  # noqa: PLC0415 - fallback only when plutil fails
        except ImportError as exc:
            raise FinvaultError(
                f"cannot read hdiutil output: plutil failed and plistlib is unavailable ({exc})",
                "run finvault with a Python whose pyexpat module loads",
            ) from None
        return plistlib.loads(data)

    def attached_images(self) -> list[Attached]:
        proc = self._run(["hdiutil", "info", "-plist"])
        self._check(proc, "hdiutil info", "check that disk images work on this Mac")
        return parse_hdiutil_info(self._plist(proc.stdout))

    def create_image(self, image: str, size_mb: int, volname: str, passphrase: str) -> None:
        argv = [
            "hdiutil", "create", "-size", f"{size_mb}m", "-type", "SPARSEBUNDLE", "-fs", "APFS",
            "-encryption", "AES-256", "-stdinpass", "-volname", volname, image,
        ]  # fmt: skip
        proc = self._run(argv, stdin=_stdin_pass(passphrase))
        self._check(proc, f"creating {image}", "check free disk space and that the image does not exist")

    def attach(self, image: str, mountpoint: str, passphrase: str, *, readonly: bool = False) -> None:
        argv = ["hdiutil", "attach", "-stdinpass", "-nobrowse", "-owners", "on", "-mountpoint", mountpoint]
        if readonly:
            argv.append("-readonly")
        argv.append(image)
        proc = self._run(argv, stdin=_stdin_pass(passphrase))
        self._check(proc, f"unlocking {image}", "check the image path and that the mountpoint is an empty folder")

    def detach(self, device: str) -> None:
        """Close ``device``. A volume still in use fails with exit code ``EXIT_BUSY``."""
        proc = self._run(["hdiutil", "detach", device])
        err = proc.stderr.decode(errors="replace").strip()
        if proc.returncode != 0 and "Resource busy" in err:
            raise FinvaultError(
                f"closing {device} failed: {err}",
                "quit programs using the vault, then run finvault lock again",
                EXIT_BUSY,
            )
        self._check(proc, f"closing {device}", "quit programs using the vault, then run finvault lock again")

    def check_passphrase(self, image: str, passphrase: str) -> bool:
        """True when ``passphrase`` opens ``image``, attached or not.

        hdiutil checks the passphrase before it opens the image, so on an
        attached image the right passphrase gets "Resource temporarily
        unavailable" (the image is in use) and a wrong one "Authentication error".
        """
        proc = self._run(["hdiutil", "imageinfo", "-stdinpass", image], stdin=_stdin_pass(passphrase))
        if proc.returncode == 0:
            return True
        err = proc.stderr.decode(errors="replace").strip()
        if "Authentication error" in err:
            return False
        if "Resource temporarily unavailable" in err:
            return True
        raise FinvaultError(
            f"checking the passphrase for {image} failed (exit {proc.returncode}): {err}",
            "check that the image exists and disk images work on this Mac, then run the command again",
        )

    def create_archive(self, out: str, srcfolders: Sequence[str], volname: str, passphrase: str) -> None:
        argv = ["hdiutil", "create", "-fs", "APFS", "-format", "UDZO", "-encryption", "AES-256", "-stdinpass"]
        argv += ["-volname", volname]
        for src in srcfolders:
            argv += ["-srcfolder", src]
        argv.append(out)
        proc = self._run(argv, stdin=_stdin_pass(passphrase))
        self._check(proc, f"creating archive {out}", "check free disk space in the state folder")

    def rsync(self, src: str, dst: str) -> None:
        proc = self._run(["rsync", "-aE", src.rstrip("/") + "/", dst.rstrip("/") + "/"])
        self._check(proc, f"copying {src}", "check the image has room (raise size in the config and init again)")

    def open_files(self, path: str, *, recursive: bool = False) -> list[Process]:
        """Processes with files open on ``path``'s volume (a mountpoint) or under it (``recursive``)."""
        target = ["+D", path] if recursive else ["--", path]
        proc = self._run(["lsof", "-w", "-n", "-P", "-F", "pcn", *target])
        if proc.returncode not in (0, 1):
            err = proc.stderr.decode(errors="replace").strip()
            raise FinvaultError(f"lsof failed on {path}: {err}", "run lsof by hand to see why")
        procs = parse_lsof(proc.stdout.decode(errors="replace"))
        return [p for p in procs if p.pid != os.getpid()]

    def hc_terminals(self) -> list[Terminal] | None:
        try:
            proc = subprocess.run([self.hc_command, "ls", "--json"], capture_output=True, check=False)
        except FileNotFoundError:
            print(f"warning: {self.hc_command} not found; can't list terminals", file=sys.stderr)
            return None
        if proc.returncode != 0:
            print(f"warning: hc ls failed: {proc.stderr.decode(errors='replace').strip()}", file=sys.stderr)
            return None
        try:
            terms = parse_hc_ls(json.loads(proc.stdout))
        except json.JSONDecodeError:
            print("warning: hc ls --json printed something other than JSON", file=sys.stderr)
            return None
        return [self._with_pids(t) for t in terms]

    def _with_pids(self, t: Terminal) -> Terminal:
        """Add pids (and the current folder) from ``hc get``; hc ls has neither."""
        proc = subprocess.run([self.hc_command, "get", t.address, "--json"], capture_output=True, check=False)
        if proc.returncode != 0:
            print(f"warning: hc get {t.address} failed; it may have just ended", file=sys.stderr)
            return t
        try:
            pids, cwd = parse_hc_get(json.loads(proc.stdout))
        except json.JSONDecodeError:
            print(f"warning: hc get {t.address} --json printed something other than JSON", file=sys.stderr)
            return t
        return Terminal(t.address, cwd or t.cwd, t.agent, t.where, pids)

    def process_parents(self) -> dict[int, int]:
        proc = self._run(["ps", "-A", "-o", "pid=,ppid="])
        self._check(proc, "ps", "run ps -A by hand to see why")
        return parse_ps(proc.stdout.decode(errors="replace"))

    def hc_kill(self, address: str) -> None:
        proc = self._run([self.hc_command, "kill", address])
        self._check(proc, f"hc kill {address}", "quit that terminal by hand")

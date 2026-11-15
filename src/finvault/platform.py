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

from finvault.errors import FinvaultError
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
    address: str
    cwd: str
    agent: str = ""
    where: str = ""


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
        proc = self._run(["hdiutil", "detach", device])
        self._check(proc, f"closing {device}", "quit programs using the vault, then run finvault lock again")

    def check_passphrase(self, image: str, passphrase: str) -> bool:
        proc = self._run(["hdiutil", "imageinfo", "-stdinpass", image], stdin=_stdin_pass(passphrase))
        return proc.returncode == 0

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
            return parse_hc_ls(json.loads(proc.stdout))
        except json.JSONDecodeError:
            print("warning: hc ls --json printed something other than JSON", file=sys.stderr)
            return None

    def hc_kill(self, address: str) -> None:
        proc = self._run([self.hc_command, "kill", address])
        self._check(proc, f"hc kill {address}", "quit that terminal by hand")

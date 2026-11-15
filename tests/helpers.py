"""Shared test helpers: temp-folder configs and a fake macOS platform."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from collections.abc import Sequence
from datetime import datetime

from finvault import vault
from finvault.config import Config, parse_config
from finvault.errors import FinvaultError
from finvault.platform import Attached, Platform, Process, Terminal

FIXED_NOW = datetime(2025, 3, 14, 9, 26, 53)
PASSPHRASE = "correct horse battery staple"


class TempDirTest(unittest.TestCase):
    """Gives each test its own temp folder, removed afterwards."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="finvault-test-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for dirpath, dirnames, _files in os.walk(self.tmp):
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if not os.path.islink(full):
                    os.chmod(full, 0o700)
        shutil.rmtree(self.tmp)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def write(self, rel: str, data: bytes | str, mode: int = 0o644) -> str:
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
        os.chmod(full, mode)
        return full


def make_config(root: str, data: dict[str, object], name: str = "config.toml") -> Config:
    return parse_config(data, config_path=os.path.join(root, name), home=root, uid=501)


class FakePlatform:
    """Simulates hdiutil by moving an image's content in and out of its mountpoint.

    An image is a folder holding ``passphrase`` and a ``content`` folder. Attaching
    moves ``content``'s entries into the mountpoint; detaching moves them back.
    """

    def __init__(self) -> None:
        self.mounts: dict[str, str] = {}  # real image path -> mountpoint
        self.devices: dict[str, str] = {}
        self.busy: dict[str, list[Process]] = {}
        self.terminals: list[Terminal] | None = []
        self.killed: list[str] = []
        self.kill_clears: bool = True
        self.calls: list[tuple[str, ...]] = []
        self._next_disk = 10

    # image helpers
    @staticmethod
    def _content(image: str) -> str:
        return os.path.join(image, "content")

    def _check_pw(self, image: str, passphrase: str) -> bool:
        with open(os.path.join(image, "passphrase")) as fh:
            return fh.read() == passphrase

    def attached_images(self) -> list[Attached]:
        return [
            Attached(image_path=img, mount_points=(os.path.realpath(mp),), device=self.devices[img])
            for img, mp in self.mounts.items()
        ]

    def create_image(self, image: str, size_mb: int, volname: str, passphrase: str) -> None:
        self.calls.append(("create", image, str(size_mb), volname))
        os.makedirs(self._content(image))
        with open(os.path.join(image, "passphrase"), "w") as fh:
            fh.write(passphrase)

    def attach(self, image: str, mountpoint: str, passphrase: str, *, readonly: bool = False) -> None:
        self.calls.append(("attach", image, mountpoint))
        real = os.path.realpath(image)
        if real in self.mounts:
            raise FinvaultError(f"unlocking {image} failed: already attached")
        if os.path.isfile(image):
            # an archive "dmg": a folder copy made by create_archive
            real_src = image + ".d"
            if not self._check_pw(real_src, passphrase):
                raise FinvaultError(f"unlocking {image}: wrong passphrase")
            content = self._content(real_src)
        else:
            if not self._check_pw(image, passphrase):
                raise FinvaultError(f"unlocking {image}: wrong passphrase")
            content = self._content(image)
        if os.listdir(mountpoint):
            raise FinvaultError(f"unlocking {image} failed: mountpoint not empty")
        for name in os.listdir(content):
            os.rename(os.path.join(content, name), os.path.join(mountpoint, name))
        self.mounts[real] = mountpoint
        self.devices[real] = f"/dev/disk{self._next_disk}"
        self._next_disk += 1

    def detach(self, device: str) -> None:
        self.calls.append(("detach", device))
        for img, dev in list(self.devices.items()):
            if dev == device:
                mp = self.mounts.pop(img)
                del self.devices[img]
                content = self._content(img + ".d" if os.path.isfile(img) else img)
                for name in os.listdir(mp):
                    os.rename(os.path.join(mp, name), os.path.join(content, name))
                return
        raise FinvaultError(f"closing {device} failed: no such device")

    def check_passphrase(self, image: str, passphrase: str) -> bool:
        return self._check_pw(image, passphrase)

    def create_archive(self, out: str, srcfolders: Sequence[str], volname: str, passphrase: str) -> None:
        self.calls.append(("archive", out, *srcfolders))
        store = out + ".d"
        os.makedirs(self._content(store))
        with open(os.path.join(store, "passphrase"), "w") as fh:
            fh.write(passphrase)
        # Like hdiutil: one source lands at the volume root, several get a folder each.
        if len(srcfolders) == 1:
            shutil.copytree(srcfolders[0], self._content(store), symlinks=True, dirs_exist_ok=True)
        else:
            for src in srcfolders:
                shutil.copytree(src, os.path.join(self._content(store), os.path.basename(src)), symlinks=True)
        with open(out, "w") as fh:
            fh.write("fake dmg\n")

    def rsync(self, src: str, dst: str) -> None:
        self.calls.append(("rsync", src, dst))
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)

    def open_files(self, path: str, *, recursive: bool = False) -> list[Process]:
        return [Process(p.pid, p.command, list(p.files)) for p in self.busy.get(os.path.realpath(path), [])]

    def hc_terminals(self) -> list[Terminal] | None:
        return None if self.terminals is None else list(self.terminals)

    def hc_kill(self, address: str) -> None:
        self.killed.append(address)
        if self.terminals is not None:
            self.terminals = [t for t in self.terminals if t.address != address]
        if self.kill_clears:
            self.busy.clear()


def fake_deps(platform: Platform, passphrase: str = PASSPHRASE) -> vault.Deps:
    warnings: list[str] = []
    deps = vault.Deps(
        platform=platform,
        ask_passphrase=lambda: passphrase,
        ask_new_passphrase=lambda: passphrase,
        now=lambda: FIXED_NOW,
        sleep=lambda _s: None,
        warn=warnings.append,
    )
    return deps

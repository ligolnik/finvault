"""End-to-end runs against real hdiutil, rsync and lsof on throwaway images.

Every image, mountpoint and config lives in a temp folder. hc is replaced by a
fake and never called. Skipped off macOS, or when FINVAULT_SKIP_HDIUTIL=1.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from finvault import vault
from finvault.config import Config
from finvault.errors import EXIT_BUSY, FinvaultError
from finvault.platform import MacPlatform, Terminal
from tests.helpers import TempDirTest, fake_deps, make_config


def _spotlight_processes() -> list[str]:
    """Spotlight processes running now, for the failure message (ps lists other users' processes)."""
    ps = subprocess.run(["ps", "-axo", "pid=,user=,comm="], capture_output=True, text=True, check=False)
    return [row.strip() for row in ps.stdout.splitlines() if "/md" in row or " md" in row]


def _skip_reason() -> str:
    if sys.platform != "darwin" or shutil.which("hdiutil") is None:
        return "needs macOS hdiutil"
    if os.environ.get("FINVAULT_SKIP_HDIUTIL") == "1":
        return "FINVAULT_SKIP_HDIUTIL=1"
    return ""


SKIP_REASON = _skip_reason()


class FakeHcMacPlatform(MacPlatform):
    """Real hdiutil/rsync/lsof; hc is faked so no terminal is ever touched."""

    def __init__(self) -> None:
        super().__init__(hc_command="/nonexistent/hc")
        self.terminals: list[Terminal] = []
        self.killed: list[str] = []

    def hc_terminals(self) -> list[Terminal] | None:
        return list(self.terminals)

    def hc_kill(self, address: str) -> None:
        self.killed.append(address)


@unittest.skipIf(SKIP_REASON, SKIP_REASON)
class HdiutilIntegrationTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.platform = FakeHcMacPlatform()
        self.addCleanup(self._detach_everything)

    def _detach_everything(self) -> None:
        for att in self.platform.attached_images():
            if att.image_path.startswith(self.tmp) and att.device:
                subprocess.run(["hdiutil", "detach", "-force", att.device], capture_output=True, check=False)

    def make_vault(self, label: str, passphrase: str, *, two_folders: bool = False) -> tuple[Config, vault.Deps]:
        folders = [{"name": "docs", "path": f"~/work-{label}/Docs", "size": "64m"}]
        if two_folders:
            folders.append({"name": "notes", "path": f"~/work-{label}/Notes", "size": "64m"})
            self.write(f"work-{label}/Notes/todo.md", "- item\n")
        cfg = make_config(
            self.tmp,
            {
                "state_dir": f"~/state-{label}",
                "folders": folders,
                "sweep": {
                    "claude_dir": "~/claude",
                    "scratch_root": self.path("scratch"),
                    "scratch_globs": [f"*work-{label}*"],
                },
            },
            name=f"config-{label}.toml",
        )
        self.write(f"work-{label}/Docs/secret.txt", f"marker-{label}\n", 0o600)
        self.write(f"work-{label}/Docs/deep/nested/file.bin", bytes(range(256)) * 64)
        os.symlink("deep/nested/file.bin", self.path(f"work-{label}", "Docs", "link"))
        return cfg, fake_deps(self.platform, passphrase)

    def test_full_lifecycle(self) -> None:
        cfg, deps = self.make_vault("a", "integration passphrase a", two_folders=True)
        docs = cfg.folder("docs").path
        vault.init(cfg, deps, "docs")
        vault.init(cfg, deps, "notes")
        image = cfg.image_path(cfg.folder("docs"))
        self.assertTrue(os.path.isdir(image))
        with self.assertRaises(FileNotFoundError):
            Path(image, "secret.txt").read_text()

        out = vault.migrate(cfg, deps, "docs")
        self.assertEqual(out.data["files"], 3)
        self.assertTrue(os.path.ismount(docs))
        self.assertEqual(Path(docs, "secret.txt").read_text(), "marker-a\n")
        self.assertEqual(Path(docs + ".plain", "secret.txt").read_text(), "marker-a\n")
        vault.migrate(cfg, deps, "notes")
        self.assertEqual(vault.verify(cfg, deps).exit_code, 0)
        # With every image attached, the passphrase is still checked against one of them.
        self.assertTrue(self.platform.check_passphrase(image, "integration passphrase a"))
        self.assertFalse(self.platform.check_passphrase(image, "not the passphrase"))

        # lock refuses while another process works in the vault, and names the terminal it
        # runs under: a shell (the "terminal", started elsewhere) whose child sits in the vault.
        holder = subprocess.Popen(
            ["/bin/sh", "-c", f"cd {shlex.quote(docs)} && /bin/sleep 120 & wait"],
            cwd=self.tmp,
            start_new_session=True,
        )
        self.platform.terminals = [Terminal("t9", self.tmp, pids=(holder.pid,))]
        try:
            for _ in range(100):
                if self.platform.open_files(docs):
                    break
                time.sleep(0.05)
            with self.assertRaises(FinvaultError) as ctx:
                vault.lock(cfg, deps)
            self.assertEqual(ctx.exception.exit_code, EXIT_BUSY)
            terms = ctx.exception.details["terminals"]
            self.assertEqual([(t["address"], t["self"]) for t in terms], [("t9", False)])  # type: ignore[union-attr, index]
            self.assertTrue(os.path.ismount(docs))
        finally:
            os.killpg(holder.pid, signal.SIGKILL)
            holder.wait()
            for _ in range(100):
                if not self.platform.open_files(docs):
                    break
                time.sleep(0.05)
        self.platform.terminals = []

        for cycle in range(5):
            out = vault.lock(cfg, deps)
            self.assertEqual(out.exit_code, 0, {"cycle": cycle, "lock": out.data, "spotlight": _spotlight_processes()})
            self.assertFalse(os.path.ismount(docs))
            self.assertEqual(os.stat(docs).st_mode & 0o777, 0o000)
            with self.assertRaises(PermissionError):
                os.listdir(docs)
            vault.unlock(cfg, deps)
            self.assertTrue(os.path.ismount(docs))
            self.assertEqual(Path(docs, "secret.txt").read_text(), "marker-a\n")

        # the raw image never holds the marker in plain text
        vault.lock(cfg, deps)
        for dirpath, _dirs, files in os.walk(image):
            for name in files:
                self.assertNotIn(b"marker-a", Path(dirpath, name).read_bytes())
        vault.unlock(cfg, deps)

        arch = vault.archive(cfg, deps)
        self.assertEqual(arch.data["folders"], ["docs", "notes"])
        self.assertTrue(os.path.isfile(str(arch.data["archive"])))
        self.assertNotIn(b"marker-a", Path(str(arch.data["archive"])).read_bytes())
        self.assertEqual(self.platform.killed, [])

    def test_two_vaults_with_different_passphrases(self) -> None:
        cfg_a, deps_a = self.make_vault("a", "integration passphrase a")
        cfg_b, deps_b = self.make_vault("b", "integration passphrase b")
        for cfg, deps in ((cfg_a, deps_a), (cfg_b, deps_b)):
            vault.init(cfg, deps, "docs")
            vault.migrate(cfg, deps, "docs")
        vault.lock(cfg_a, deps_a)
        self.assertFalse(os.path.ismount(cfg_a.folder("docs").path))
        self.assertTrue(os.path.ismount(cfg_b.folder("docs").path))
        with self.assertRaises(FinvaultError):
            vault.unlock(cfg_a, deps_b)
        vault.unlock(cfg_a, deps_a)
        self.assertEqual(Path(cfg_a.folder("docs").path, "secret.txt").read_text(), "marker-a\n")
        self.assertEqual(Path(cfg_b.folder("docs").path, "secret.txt").read_text(), "marker-b\n")
        single = vault.archive(cfg_b, deps_b)
        self.assertEqual(single.data["folders"], ["docs"])
        self.assertTrue(str(single.data["archive"]).startswith(cfg_b.archive_dir))


if __name__ == "__main__":
    unittest.main()

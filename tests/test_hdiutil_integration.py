"""End-to-end runs against real hdiutil, rsync and lsof on throwaway images.

Every image, mountpoint and config lives in a temp folder. hc is replaced by a
fake and never called. Skipped off macOS, or when FINVAULT_SKIP_HDIUTIL=1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from finvault import vault
from finvault.config import Config
from finvault.errors import EXIT_BUSY, FinvaultError
from finvault.platform import MacPlatform, Terminal
from tests.helpers import TempDirTest, fake_deps, make_config


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

        # lock refuses while another process works in the vault, and names the terminal
        self.platform.terminals = [Terminal("t9", docs)]
        holder = subprocess.Popen(["/bin/sleep", "120"], cwd=docs)
        try:
            with self.assertRaises(FinvaultError) as ctx:
                vault.lock(cfg, deps)
            self.assertEqual(ctx.exception.exit_code, EXIT_BUSY)
            self.assertIn(holder.pid, [p["pid"] for p in ctx.exception.details["busy"]])  # type: ignore[union-attr]
            self.assertEqual([t["address"] for t in ctx.exception.details["terminals"]], ["t9"])  # type: ignore[union-attr]
            self.assertTrue(os.path.ismount(docs))
        finally:
            holder.kill()
            holder.wait()
        self.platform.terminals = []

        for _ in range(5):
            vault.lock(cfg, deps)
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

"""Command behaviour against a fake platform: no real images, hc never called."""

from __future__ import annotations

import io
import json
import os
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from finvault import cli, vault
from finvault.config import Config, Folder
from finvault.errors import EXIT_BUSY, EXIT_MISMATCH, FinvaultError
from finvault.platform import Process, Terminal
from tests.helpers import FakePlatform, TempDirTest, fake_deps, make_config


def mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class VaultTestBase(TempDirTest):
    def vault_config(self, label: str = "a", sweep: bool = True) -> Config:
        root = self.path(label)
        os.makedirs(root, exist_ok=True)
        data: dict[str, object] = {
            "state_dir": f"~/state-{label}",
            "folders": [
                {"name": "docs", "path": f"~/work-{label}/Docs"},
                {"name": "notes", "path": f"~/work-{label}/Notes"},
            ],
        }
        if sweep:
            data["sweep"] = {
                "claude_dir": "~/claude",
                "scratch_root": self.path("scratch"),
                "scratch_globs": [f"*work-{label}*"],
            }
        cfg = make_config(self.tmp, data, name=f"config-{label}.toml")
        for f in cfg.folders:
            os.makedirs(f.path, exist_ok=True)
        self.write(f"work-{label}/Docs/report.txt", f"report {label}", 0o600)
        self.write(f"work-{label}/Docs/sub/data.csv", "1,2,3\n")
        os.symlink("sub/data.csv", self.path(f"work-{label}", "Docs", "link.csv"))
        self.write(f"work-{label}/Notes/todo.md", "- item\n")
        return cfg

    def setup_vault(self, label: str = "a") -> tuple[Config, FakePlatform, vault.Deps]:
        cfg = self.vault_config(label)
        platform = FakePlatform()
        deps = fake_deps(platform)
        for f in cfg.folders:
            vault.init(cfg, deps, f.name)
            vault.migrate(cfg, deps, f.name)
        return cfg, platform, deps


class InitMigrateTest(VaultTestBase):
    def test_init_creates_image_in_mode_700_state_dir(self) -> None:
        cfg = self.vault_config()
        platform = FakePlatform()
        out = vault.init(cfg, fake_deps(platform), "docs")
        self.assertTrue(out.data["created"])
        self.assertTrue(os.path.isdir(cfg.image_path(cfg.folder("docs"))))
        self.assertEqual(mode(cfg.images_dir), 0o700)
        self.assertEqual(mode(cfg.state_dir), 0o700)
        self.assertEqual(out.data["size_mb"], 1024)

    def test_init_twice_is_idempotent(self) -> None:
        cfg = self.vault_config()
        deps = fake_deps(FakePlatform())
        vault.init(cfg, deps, "docs")
        self.assertFalse(vault.init(cfg, deps, "docs").data["created"])

    def test_second_image_must_use_the_same_passphrase(self) -> None:
        cfg = self.vault_config()
        platform = FakePlatform()
        vault.init(cfg, fake_deps(platform), "docs")
        with self.assertRaises(FinvaultError) as ctx:
            vault.init(cfg, fake_deps(platform, "a different passphrase"), "notes")
        self.assertIn("wrong passphrase", ctx.exception.message)

    def test_new_image_checks_the_passphrase_even_when_every_image_is_unlocked(self) -> None:
        cfg = self.vault_config()
        platform = FakePlatform()
        deps = fake_deps(platform)
        vault.init(cfg, deps, "docs")
        vault.migrate(cfg, deps, "docs")
        with self.assertRaises(FinvaultError) as ctx:
            vault.init(cfg, fake_deps(platform, "a different passphrase"), "notes")
        self.assertIn("wrong passphrase", ctx.exception.message)
        self.assertFalse(os.path.exists(cfg.image_path(cfg.folder("notes"))))
        self.assertTrue(vault.init(cfg, deps, "notes").data["created"])

    def test_migrate_keeps_plain_copy_and_unlocks_at_real_path(self) -> None:
        cfg, _platform, deps = self.setup_vault()
        docs = cfg.folder("docs").path
        self.assertTrue(os.path.isdir(docs + ".plain"))
        self.assertEqual(Path(docs + ".plain", "report.txt").read_text(), "report a")
        self.assertEqual(Path(docs, "report.txt").read_text(), "report a")
        self.assertEqual(mode(os.path.join(docs, "report.txt")), 0o600)
        self.assertEqual(os.readlink(os.path.join(docs, "link.csv")), "sub/data.csv")
        self.assertTrue(os.path.exists(os.path.join(docs, ".finvault", "manifest.json")))
        self.assertEqual(set(vault.attached_by_folder(cfg, deps)), {"docs", "notes"})
        self.assertEqual(os.listdir(cfg.mnt_dir), [])

    def test_migrate_refuses_when_files_are_open(self) -> None:
        cfg = self.vault_config()
        platform = FakePlatform()
        deps = fake_deps(platform)
        vault.init(cfg, deps, "docs")
        platform.busy[os.path.realpath(cfg.folder("docs").path)] = [Process(42, "editor", ["x"])]
        with self.assertRaises(FinvaultError) as ctx:
            vault.migrate(cfg, deps, "docs")
        self.assertEqual(ctx.exception.exit_code, EXIT_BUSY)
        self.assertFalse(os.path.exists(cfg.folder("docs").path + ".plain"))

    def test_migrate_detects_a_bad_copy_and_leaves_the_original(self) -> None:
        cfg = self.vault_config()
        platform = FakePlatform()
        deps = fake_deps(platform)
        vault.init(cfg, deps, "docs")

        def bad_rsync(src: str, dst: str) -> None:
            FakePlatform.rsync(platform, src, dst)
            Path(dst, "report.txt").write_text("corrupted")

        platform.rsync = bad_rsync  # type: ignore[method-assign]  # inject a faulty copy
        with self.assertRaises(FinvaultError) as ctx:
            vault.migrate(cfg, deps, "docs")
        self.assertEqual(ctx.exception.exit_code, EXIT_MISMATCH)
        self.assertEqual(Path(cfg.folder("docs").path, "report.txt").read_text(), "report a")
        self.assertFalse(os.path.exists(cfg.folder("docs").path + ".plain"))
        self.assertEqual(platform.mounts, {})

    def test_migrate_refuses_a_folder_that_already_has_the_sweep_archive_name(self) -> None:
        cfg = self.vault_config()
        deps = fake_deps(FakePlatform())
        self.write("work-a/Docs/.finvault-sweep/mine.txt", "user data")
        vault.init(cfg, deps, "docs")
        with self.assertRaises(FinvaultError) as ctx:
            vault.migrate(cfg, deps, "docs")
        self.assertIn("archive_subdir", ctx.exception.message)
        self.assertFalse(os.path.exists(cfg.folder("docs").path + ".plain"))

    def test_migrate_never_deletes_the_source(self) -> None:
        cfg, _platform, _deps = self.setup_vault()
        for f in cfg.folders:
            self.assertTrue(os.path.isdir(f.path + ".plain"))

    def test_archive_contains_every_plain_folder(self) -> None:
        cfg, platform, deps = self.setup_vault()
        out = vault.archive(cfg, deps)
        self.assertEqual(out.data["archive"], os.path.join(cfg.archive_dir, "plain-20250314-092653.dmg"))
        self.assertEqual(out.data["folders"], ["docs", "notes"])
        self.assertEqual(mode(str(out.data["archive"])), 0o600)
        archive_call = next(c for c in platform.calls if c[0] == "archive")
        self.assertEqual(archive_call[2:], tuple(f.path + ".plain" for f in cfg.folders))


class LockUnlockTest(VaultTestBase):
    def test_five_lock_unlock_cycles(self) -> None:
        cfg, platform, deps = self.setup_vault()
        for _ in range(5):
            locked = vault.lock(cfg, deps)
            self.assertEqual(sorted(locked.data["locked"]), ["docs", "notes"])  # type: ignore[arg-type]
            for f in cfg.folders:
                self.assertEqual(mode(f.path), 0o000)
            self.assertEqual(platform.mounts, {})
            unlocked = vault.unlock(cfg, deps)
            self.assertEqual(unlocked.exit_code, 0)
            self.assertEqual(sorted(unlocked.data["unlocked"]), ["docs", "notes"])  # type: ignore[arg-type]
            self.assertEqual(Path(cfg.folder("docs").path, "report.txt").read_text(), "report a")

    def test_lock_and_unlock_are_idempotent(self) -> None:
        cfg, _platform, deps = self.setup_vault()
        self.assertEqual(vault.unlock(cfg, deps).data["already_unlocked"], ["docs", "notes"])
        vault.lock(cfg, deps)
        again = vault.lock(cfg, deps)
        self.assertEqual(again.data["already_locked"], ["docs", "notes"])

    def test_lock_finds_busy_terminals_by_process_ancestry_not_cwd(self) -> None:
        cfg, platform, deps = self.setup_vault()
        docs = cfg.folder("docs").path
        # t1 started elsewhere and cd'd into the vault: its zsh (500) runs vim (77).
        # t2 started in the vault but holds nothing there now.
        platform.busy[os.path.realpath(docs)] = [Process(500, "zsh", [docs]), Process(77, "vim", [docs + "/x"])]
        platform.parents = {77: 500, 500: 400, 400: 1, 600: 400}
        platform.terminals = [
            Terminal("t2", docs, "zsh", "Other", pids=(600,)),
            Terminal("t1", self.path("elsewhere"), "zsh", "Work / shell", pids=(400, 500)),
        ]
        with self.assertRaises(FinvaultError) as ctx:
            vault.lock(cfg, deps)
        self.assertEqual(ctx.exception.exit_code, EXIT_BUSY)
        terms = ctx.exception.details["terminals"]
        self.assertEqual(
            terms,
            [
                {
                    "address": "t1",
                    "cwd": self.path("elsewhere"),
                    "where": "Work / shell",
                    "busy_pids": [77, 500],
                    "self": False,
                }
            ],
        )
        self.assertEqual(platform.killed, [])
        self.assertEqual(set(platform.mounts), {os.path.realpath(cfg.image_path(f)) for f in cfg.folders})

    def test_force_ends_only_vault_terminals_then_locks(self) -> None:
        cfg, platform, deps = self.setup_vault()
        docs = cfg.folder("docs").path
        platform.busy[os.path.realpath(docs)] = [Process(77, "claude", [docs])]
        platform.parents = {77: 70, 70: 1, 80: 1}
        platform.terminals = [
            Terminal("t2", docs, "zsh", pids=(80,)),
            Terminal("t1", self.path("elsewhere"), "claude-code", pids=(70,)),
        ]
        out = vault.lock(cfg, deps, force=True)
        self.assertEqual(platform.killed, ["t1"])
        self.assertEqual(out.data["killed"], ["t1"])
        self.assertEqual(platform.mounts, {})

    def test_force_still_refuses_when_a_non_hc_process_holds_files(self) -> None:
        cfg, platform, deps = self.setup_vault()
        docs = cfg.folder("docs").path
        platform.busy[os.path.realpath(docs)] = [Process(88, "Preview", [docs])]
        platform.parents = {88: 1, 70: 1}
        platform.terminals = [Terminal("t1", docs, pids=(70,))]
        platform.kill_clears = False
        with self.assertRaises(FinvaultError) as ctx:
            vault.lock(cfg, deps, force=True)
        self.assertEqual(ctx.exception.exit_code, EXIT_BUSY)
        self.assertEqual(platform.killed, [])
        self.assertEqual(ctx.exception.details["terminals"], [])
        self.assertNotEqual(platform.mounts, {})

    def test_force_never_ends_the_terminal_finvault_runs_in(self) -> None:
        cfg, platform, deps = self.setup_vault()
        docs = cfg.folder("docs").path
        platform.busy[os.path.realpath(docs)] = [Process(70, "zsh", [docs])]
        platform.parents = {os.getpid(): 70, 70: 1}
        platform.terminals = [Terminal("t1", docs, pids=(70,))]
        platform.kill_clears = False
        with self.assertRaises(FinvaultError) as ctx:
            vault.lock(cfg, deps, force=True)
        self.assertEqual(platform.killed, [])
        self.assertIn("cd out of the vault", ctx.exception.hint)
        self.assertTrue(ctx.exception.details["terminals"][0]["self"])  # type: ignore[index]

    def test_lock_asks_hc_nothing_when_nothing_is_busy(self) -> None:
        cfg, platform, deps = self.setup_vault()
        platform.terminals = None  # hc missing would make this None; idle lock must not need it
        out = vault.lock(cfg, deps)
        self.assertEqual(out.data["terminals"], [])
        self.assertEqual(platform.mounts, {})

    def test_wrong_passphrase_unlocks_nothing_and_relocks_mountpoints(self) -> None:
        cfg, platform, deps = self.setup_vault()
        vault.lock(cfg, deps)
        with self.assertRaises(FinvaultError):
            vault.unlock(cfg, fake_deps(platform, "wrong wrong wrong"))
        self.assertEqual(platform.mounts, {})
        for f in cfg.folders:
            self.assertEqual(mode(f.path), 0o000)

    def test_cancelled_passphrase_prompt_leaves_mountpoints_locked(self) -> None:
        cfg, platform, deps = self.setup_vault()
        vault.lock(cfg, deps)

        def no_passphrase() -> str:
            raise FinvaultError("no passphrase entered")

        def interrupted() -> str:
            raise KeyboardInterrupt

        for prompt in (no_passphrase, interrupted):
            with self.subTest(prompt=prompt.__name__), self.assertRaises((FinvaultError, KeyboardInterrupt)):
                vault.unlock(cfg, replace(deps, ask_passphrase=prompt))
            self.assertEqual(platform.mounts, {})
            for f in cfg.folders:
                self.assertEqual(mode(f.path), 0o000)

    def test_unlock_refuses_a_folder_that_still_has_plain_files(self) -> None:
        cfg = self.vault_config()
        platform = FakePlatform()
        deps = fake_deps(platform)
        vault.init(cfg, deps, "docs")
        out = vault.unlock(cfg, deps)
        self.assertEqual(out.exit_code, 1)
        self.assertIn("has files", out.data["failed"][0]["reason"])  # type: ignore[index]
        self.assertEqual(platform.mounts, {})

    def test_lock_never_sets_000_on_a_folder_with_files(self) -> None:
        cfg = self.vault_config()
        deps = fake_deps(FakePlatform())
        vault.init(cfg, deps, "docs")
        vault.lock(cfg, deps)
        self.assertNotEqual(mode(cfg.folder("docs").path), 0o000)

    def test_lock_runs_the_sweep_before_closing(self) -> None:
        cfg, _platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        docs = cfg.folder("docs").path
        entry: dict[str, object] = {
            "display": "x",
            "pastedContents": {},
            "timestamp": 1,
            "project": docs,
            "sessionId": "s",
        }
        self.write("claude/history.jsonl", json.dumps(entry) + "\n")
        out = vault.lock(cfg, deps)
        self.assertEqual(out.data["sweep"]["history_lines"], 1)  # type: ignore[index]
        self.assertEqual(Path(cfg.sweep.history).read_text(), "")
        vault.unlock(cfg, deps)
        archived = Path(docs, ".finvault-sweep", "history.jsonl").read_text()
        self.assertIn('"timestamp": 1', archived)

    def test_lock_refuses_when_the_sweep_cant_read_a_source(self) -> None:
        cfg, platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        backup = self.write("claude/file-history/s/backup", "SECRET")
        os.chmod(cfg.sweep.file_history, 0o000)
        try:
            with self.assertRaises(FinvaultError) as ctx:
                vault.lock(cfg, deps)
        finally:
            os.chmod(cfg.sweep.file_history, 0o700)
        self.assertIn("nothing was closed", ctx.exception.message)
        self.assertEqual(len(platform.mounts), 2)
        self.assertEqual(Path(backup).read_text(), "SECRET")

    def test_lock_reports_history_written_while_it_closed_the_images(self) -> None:
        cfg, platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        docs = cfg.folder("docs").path
        late: dict[str, object] = {
            "display": "x",
            "pastedContents": {},
            "timestamp": 9,
            "project": docs,
            "sessionId": "s",
        }
        real_detach = platform.detach

        def write_then_detach(device: str) -> None:
            self.write("claude/history.jsonl", json.dumps(late) + "\n")
            real_detach(device)

        platform.detach = write_then_detach  # type: ignore[method-assign]  # a writer races the close
        out = vault.lock(cfg, deps)
        self.assertEqual(out.exit_code, 1)
        self.assertEqual(out.data["history_lines_written_while_locking"], 1)
        self.assertEqual(platform.mounts, {})

    def test_lock_refuses_to_sweep_while_a_process_holds_history_open(self) -> None:
        cfg, platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        docs = cfg.folder("docs").path
        entry: dict[str, object] = {"display": "x", "pastedContents": {}, "timestamp": 1, "project": docs}
        self.write("claude/history.jsonl", json.dumps(entry) + "\n")
        before = Path(cfg.sweep.history).read_bytes()
        platform.busy[os.path.realpath(cfg.sweep.history)] = [Process(4242, "claude", [cfg.sweep.history])]
        status = vault.status(cfg, deps).data
        self.assertEqual(status["history_holders"], [{"pid": 4242, "command": "claude", "files": [cfg.sweep.history]}])
        with self.assertRaises(FinvaultError) as ctx:
            vault.lock(cfg, deps)
        self.assertEqual(ctx.exception.exit_code, EXIT_BUSY)
        self.assertEqual(ctx.exception.details["busy"], status["history_holders"])
        self.assertEqual(len(platform.mounts), 2)
        self.assertEqual(Path(cfg.sweep.history).read_bytes(), before)
        with self.assertRaises(FinvaultError):
            vault.restore_history(cfg, deps)
        del platform.busy[os.path.realpath(cfg.sweep.history)]
        self.assertEqual(vault.lock(cfg, deps).data["sweep"]["history_lines"], 1)  # type: ignore[index]

    def test_a_history_holder_does_not_block_a_sweep_that_leaves_history_alone(self) -> None:
        cfg, platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        entry: dict[str, object] = {"display": "x", "pastedContents": {}, "timestamp": 1, "project": "/unrelated"}
        self.write("claude/history.jsonl", json.dumps(entry) + "\n")
        scratch = self.path("scratch", "-x-work-a-Docs")
        os.makedirs(scratch)
        platform.busy[os.path.realpath(cfg.sweep.history)] = [Process(4242, "claude", [cfg.sweep.history])]
        out = vault.lock(cfg, deps)
        self.assertEqual(out.exit_code, 0)
        self.assertFalse(os.path.exists(scratch))
        self.assertEqual(json.loads(Path(cfg.sweep.history).read_text()), entry)

    def test_a_brief_history_append_does_not_block_lock(self) -> None:
        cfg, platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        docs = cfg.folder("docs").path
        entry: dict[str, object] = {"display": "x", "pastedContents": {}, "timestamp": 1, "project": docs}
        self.write("claude/history.jsonl", json.dumps(entry) + "\n")
        key = os.path.realpath(cfg.sweep.history)
        platform.busy[key] = [Process(4242, "claude", [cfg.sweep.history])]
        real_open = platform.open_files
        checks: list[str] = []

        def closes_after_first_look(path: str, *, recursive: bool = False) -> list[Process]:
            found = real_open(path, recursive=recursive)
            if os.path.realpath(path) == key:
                checks.append(path)
                platform.busy.pop(key, None)
            return found

        platform.open_files = closes_after_first_look  # type: ignore[method-assign]  # the append finishes
        out = vault.lock(cfg, deps)
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(len(checks), 2)
        self.assertEqual(Path(cfg.sweep.history).read_text(), "")

    def test_lock_reports_a_backup_written_while_it_closed_the_images(self) -> None:
        cfg, platform, deps = self.setup_vault()
        assert cfg.sweep is not None
        proj = self.path("claude", "projects", "session-vault")
        self.write("claude/projects/session-vault/sess-a.jsonl", "{}\n")
        cfg = replace(cfg, folders=(*cfg.folders, Folder("sessions", proj)))
        vault.init(cfg, deps, "sessions")
        vault.migrate(cfg, deps, "sessions")
        real_detach = platform.detach

        def write_then_detach(device: str) -> None:
            self.write("claude/file-history/sess-a/backup", "LATE BACKUP")
            real_detach(device)

        platform.detach = write_then_detach  # type: ignore[method-assign]  # a writer races the close
        out = vault.lock(cfg, deps)
        self.assertEqual(out.exit_code, 1)
        self.assertEqual(out.data["written_while_locking"], {"file_history_folders": ["sess-a"]})
        self.assertEqual(platform.mounts, {})

    def test_verify_ignores_the_sweep_archive_written_after_migrate(self) -> None:
        cfg, _platform, deps = self.setup_vault()
        docs = cfg.folder("docs").path
        entry: dict[str, object] = {
            "display": "x",
            "pastedContents": {},
            "timestamp": 1,
            "project": docs,
            "sessionId": "s",
        }
        self.write("claude/history.jsonl", json.dumps(entry) + "\n")
        vault.lock(cfg, deps)
        vault.unlock(cfg, deps)
        self.assertTrue(os.path.isdir(os.path.join(docs, ".finvault-sweep")))
        self.assertEqual(vault.verify(cfg, deps).exit_code, 0)

    def test_status_and_verify(self) -> None:
        cfg, _platform, deps = self.setup_vault()
        st = vault.status(cfg, deps)
        self.assertTrue(all(f["unlocked"] for f in st.data["folders"]))  # type: ignore[union-attr, index]
        self.assertEqual(vault.verify(cfg, deps).exit_code, 0)
        Path(cfg.folder("docs").path, "report.txt").write_text("edited")
        self.assertEqual(vault.verify(cfg, deps).exit_code, EXIT_MISMATCH)
        vault.lock(cfg, deps)
        st = vault.status(cfg, deps)
        self.assertEqual([f["mountpoint_mode"] for f in st.data["folders"]], ["0o0", "0o0"])  # type: ignore[index, union-attr]
        with self.assertRaises(FinvaultError):
            vault.verify(cfg, deps)


class TwoVaultsTest(VaultTestBase):
    def test_two_vaults_keep_separate_state_and_passphrases(self) -> None:
        cfg_a = self.vault_config("a")
        cfg_b = self.vault_config("b")
        platform = FakePlatform()
        deps_a = fake_deps(platform, "passphrase for vault a")
        deps_b = fake_deps(platform, "passphrase for vault b")
        for cfg, deps in ((cfg_a, deps_a), (cfg_b, deps_b)):
            for f in cfg.folders:
                vault.init(cfg, deps, f.name)
                vault.migrate(cfg, deps, f.name)
        self.assertNotEqual(cfg_a.state_dir, cfg_b.state_dir)
        self.assertEqual(sorted(os.listdir(cfg_a.images_dir)), ["docs.sparsebundle", "notes.sparsebundle"])
        self.assertEqual(sorted(os.listdir(cfg_b.images_dir)), ["docs.sparsebundle", "notes.sparsebundle"])

        # Locking vault a leaves vault b's images attached.
        vault.lock(cfg_a, deps_a)
        self.assertEqual(vault.attached_by_folder(cfg_a, deps_a), {})
        self.assertEqual(set(vault.attached_by_folder(cfg_b, deps_b)), {"docs", "notes"})

        # Vault b's passphrase does not open vault a.
        with self.assertRaises(FinvaultError):
            vault.unlock(cfg_a, deps_b)
        self.assertEqual(vault.unlock(cfg_a, deps_a).data["unlocked"], ["docs", "notes"])

        # Each vault's checksum lists live in its own images.
        self.assertEqual(Path(cfg_a.folder("docs").path, "report.txt").read_text(), "report a")
        self.assertEqual(Path(cfg_b.folder("docs").path, "report.txt").read_text(), "report b")

    def test_a_state_dir_cannot_be_shared_by_two_configs(self) -> None:
        cfg_a = self.vault_config("a")
        deps = fake_deps(FakePlatform())
        vault.init(cfg_a, deps, "docs")
        clash = make_config(
            self.tmp,
            {"state_dir": "~/state-a", "folders": [{"name": "x", "path": "~/work-x/X"}]},
            name="config-other.toml",
        )
        with self.assertRaises(FinvaultError) as ctx:
            vault.init(clash, deps, "x")
        self.assertIn("belongs to another vault", ctx.exception.message)


class CliTest(VaultTestBase):
    def run_cli(self, argv: list[str], deps: vault.Deps) -> tuple[int, dict[str, object], str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv, deps)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_cli_prints_json_and_actionable_errors(self) -> None:
        deps = fake_deps(FakePlatform())
        code, payload, err = self.run_cli(["--config", self.path("missing.toml"), "status"], deps)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("config.example.toml", str(payload["hint"]))
        self.assertIn("what to do", err)

    def test_cli_status_reads_the_given_config(self) -> None:
        cfg = self.vault_config()
        config_text = f'state_dir = "{cfg.state_dir}"\n' + "".join(
            f'[[folders]]\nname = "{f.name}"\npath = "{f.path}"\n' for f in cfg.folders
        )
        self.write("cli.toml", config_text)
        code, payload, _err = self.run_cli(["status", "--config", self.path("cli.toml")], fake_deps(FakePlatform()))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual([f["name"] for f in payload["folders"]], ["docs", "notes"])  # type: ignore[union-attr, index]


if __name__ == "__main__":
    unittest.main()

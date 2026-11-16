"""Plain-text output, built from fixed payloads shaped like each command's JSON."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from finvault import cli, render

HOME = "/Users/example"


def text(payload: dict[str, object]) -> str:
    return render.render(payload, HOME)


class RenderTest(unittest.TestCase):
    def test_status_shows_each_folder_terminals_holders_and_pending_sweep(self) -> None:
        out = text(
            {
                "ok": True,
                "command": "status",
                "folders": [
                    {
                        "name": "docs",
                        "path": f"{HOME}/Docs",
                        "image": True,
                        "unlocked": True,
                        "busy_processes": 2,
                        "mountpoint_mode": "0o700",
                        "plain_copy": True,
                    },
                    {
                        "name": "notes",
                        "path": f"{HOME}/Notes",
                        "image": True,
                        "unlocked": False,
                        "mountpoint_mode": "0o0",
                        "plain_copy": False,
                    },
                    {
                        "name": "new",
                        "path": "/elsewhere/New",
                        "image": False,
                        "unlocked": False,
                        "mountpoint_mode": "0o755",
                        "plain_copy": False,
                    },
                ],
                "terminals": [
                    {"address": "t9", "where": "Work / 1", "busy_pids": [41, 42], "self": False},
                    {"address": "t1", "where": "", "busy_pids": [7], "self": True},
                ],
                "history_holders": [{"pid": 4242, "command": "claude", "files": []}],
                "sweep_pending": {
                    "history_lines": 2,
                    "paste_files": 1,
                    "file_history_folders": 0,
                    "scratch_folders": 0,
                    "unreadable_project_folders": 0,
                    "problems": ["can't read /x"],
                },
                "warnings": ["something to look at"],
            }
        )
        self.assertEqual(
            out.splitlines(),
            [
                "Vault: 3 folders",
                "  docs   UNLOCKED, 2 processes busy  ~/Docs",
                "         plain copy still at ~/Docs.plain",
                "  notes  locked (mode 000)  ~/Notes",
                "  new    no image yet (run finvault init)  /elsewhere/New",
                "Terminals working in the vault:",
                "  t9 (Work / 1), busy pids 41, 42",
                "  t1, busy pids 7  <- this terminal; lock --force never ends it",
                "Holding the shared history open (a sweep would refuse):",
                "  pid 4242 claude",
                "Waiting to be swept: 2 history lines, 1 paste file",
                "The sweep can't check everything it must move:",
                "  - can't read /x",
                "Warning: something to look at",
            ],
        )

    def test_status_with_nothing_pending_and_no_sweep_section(self) -> None:
        base: dict[str, object] = {
            "ok": True,
            "command": "status",
            "folders": [],
            "terminals": None,
            "history_holders": None,
            "warnings": [],
        }
        self.assertEqual(text({**base, "sweep_pending": None}), "Vault: 0 folders\n")
        empty: dict[str, object] = {"history_lines": 0, "paste_files": 0, "problems": []}
        self.assertIn("Sweep: nothing waiting", text({**base, "sweep_pending": empty}))
        self.assertIn("Sweep: history changed", text({**base, "sweep_pending": {"error": "history changed"}}))

    def test_lock_reports_what_closed_what_moved_and_what_needs_another_lock(self) -> None:
        out = text(
            {
                "ok": False,
                "command": "lock",
                "locked": ["docs"],
                "mode_000": ["docs"],
                "failed": [{"name": "notes", "reason": "Resource busy"}],
                "sweep": {"history_lines": 3, "paste_files": 0, "file_history_folders": 1, "scratch_folders": 0},
                "killed": ["t9"],
                "terminals": [],
                "written_while_locking": {"history_lines": 1},
                "not_mode_000": ["notes"],
            }
        )
        self.assertEqual(
            out.splitlines(),
            [
                "Locked: docs",
                "Swept into the vault: 3 history lines, 1 file-history folder",
                "Ended terminals: t9",
                "Could not close:",
                "  notes: Resource busy",
                "Written while locking, still outside the vault (unlock and lock again):",
                "  history_lines: 1",
                "Closed but not set to mode 000: notes (run finvault lock again)",
            ],
        )

    def test_lock_when_already_locked(self) -> None:
        out = text({"ok": True, "command": "lock", "locked": [], "already_locked": ["docs"], "sweep": None})
        self.assertEqual(out, "Already locked: docs\n")

    def test_unlock_init_migrate_archive(self) -> None:
        self.assertEqual(
            text(
                {
                    "ok": False,
                    "command": "unlock",
                    "unlocked": ["docs"],
                    "already_unlocked": [],
                    "skipped": [{"name": "new", "reason": "no image yet"}],
                    "failed": [],
                }
            ).splitlines(),
            ["Unlocked: docs", "Skipped:", "  new: no image yet"],
        )
        self.assertEqual(
            text(
                {
                    "ok": True,
                    "command": "init",
                    "name": "docs",
                    "created": True,
                    "size_mb": 1024,
                    "image": f"{HOME}/.finvault/images/docs.sparsebundle",
                }
            ).splitlines(),
            ["Created the encrypted image for docs (1024 MB)", "  ~/.finvault/images/docs.sparsebundle"],
        )
        self.assertIn(
            "docs: image already exists",
            text(
                {
                    "ok": True,
                    "command": "init",
                    "name": "docs",
                    "created": False,
                    "reason": "image already exists",
                    "image": "/i",
                }
            ),
        )
        migrate = text(
            {
                "ok": True,
                "command": "migrate",
                "name": "docs",
                "files": 1,
                "path": f"{HOME}/Docs",
                "plain_copy": f"{HOME}/Docs.plain",
                "image": "/i",
                "next": "run finvault archive",
            }
        )
        self.assertEqual(
            migrate.splitlines(),
            [
                "Moved docs into its image: 1 file",
                "  unlocked at ~/Docs",
                "  original kept at ~/Docs.plain",
                "Next: run finvault archive",
            ],
        )
        archive = text(
            {
                "ok": True,
                "command": "archive",
                "files": 12,
                "folders": ["docs", "notes"],
                "archive": f"{HOME}/.finvault/archive/plain.dmg",
                "next": "delete the .plain folders",
            }
        )
        self.assertIn("Archived 12 files from docs, notes", archive)
        self.assertIn("~/.finvault/archive/plain.dmg", archive)

    def test_verify_sweep_and_restore(self) -> None:
        verify = text(
            {
                "ok": False,
                "command": "verify",
                "images": [
                    {"name": "docs", "result": "ok"},
                    {
                        "name": "notes",
                        "result": "differs",
                        "missing": 1,
                        "changed": 0,
                        "added": 2,
                        "examples": ["gone.txt", "new.txt"],
                    },
                    {"name": "more", "result": "locked, not checked"},
                ],
            }
        )
        self.assertEqual(
            verify.splitlines(),
            [
                "docs: ok",
                "notes: differs from its checksum list:",
                "  1 missing, 0 changed, 2 added",
                "    gone.txt",
                "    new.txt",
                "more: locked, not checked",
            ],
        )
        dry: dict[str, object] = {
            "ok": True,
            "command": "sweep",
            "dry_run": True,
            "history_lines": 1,
            "paste_files": 2,
            "problems": [],
        }
        self.assertEqual(text(dry), "Would move into the vault: 1 history line, 2 paste files\n")
        self.assertEqual(text({**dry, "dry_run": False, "history_lines": 0, "paste_files": 0}), "Nothing to sweep\n")
        restore = text(
            {
                "ok": True,
                "command": "restore-history",
                "history_lines_restored": 2,
                "paste_files_restored": 0,
                "paste_files_differing": [f"{HOME}/.claude/paste-cache/a.txt"],
            }
        )
        self.assertEqual(
            restore.splitlines(),
            [
                "Restored 2 history lines and 0 paste files",
                "Left alone, content differs from the vault's copy:",
                "  ~/.claude/paste-cache/a.txt",
            ],
        )

    def test_errors_show_only_the_details_stderr_lacks(self) -> None:
        busy = text(
            {
                "ok": False,
                "command": "lock",
                "error": "2 process(es) still have files open",
                "hint": "quit them",
                "busy": [{"pid": 41, "command": "zsh", "files": [f"{HOME}/Docs"]}],
                "terminals": [{"address": "t9", "where": "", "busy_pids": [41], "self": False}],
                "killed": [],
            }
        )
        self.assertEqual(
            busy.splitlines(),
            [
                "Processes with files open:",
                "  pid 41 zsh: ~/Docs",
                "Terminals working in the vault:",
                "  t9, busy pids 41",
            ],
        )
        self.assertNotIn("quit them", busy)
        self.assertEqual(text({"ok": False, "command": "verify", "error": "nothing unlocked", "hint": "unlock"}), "")
        problems = text({"ok": False, "command": "lock", "error": "x", "hint": "y", "problems": ["can't read /p"]})
        self.assertIn("  - can't read /p", problems)

    def test_paths_outside_home_are_shown_in_full(self) -> None:
        out = text(
            {
                "ok": True,
                "command": "migrate",
                "name": "d",
                "files": 0,
                "path": "/Users/exampleother/D",
                "plain_copy": "/Volumes/X/D.plain",
                "image": "/i",
                "next": "",
            }
        )
        self.assertIn("/Users/exampleother/D", out)
        self.assertIn("/Volumes/X/D.plain", out)


class OutputModeTest(unittest.TestCase):
    def run_cli(self, argv: list[str], *, tty: bool) -> str:
        out = io.StringIO()
        out.isatty = lambda: tty  # type: ignore[method-assign]  # stand in for a terminal
        outcome_data: dict[str, object] = {"history_lines": 0, "paste_files": 0, "problems": [], "dry_run": True}
        with (
            redirect_stdout(out),
            mock.patch.object(cli, "load_config"),
            mock.patch.object(cli.vault, "sweep", return_value=cli.vault.Outcome(outcome_data)),
        ):
            self.assertEqual(cli.main(argv, deps=mock.Mock()), 0)
        return out.getvalue()

    def test_a_terminal_gets_text_and_a_pipe_gets_json(self) -> None:
        self.assertEqual(self.run_cli(["sweep", "--dry-run"], tty=True), "Nothing to sweep\n")
        self.assertEqual(json.loads(self.run_cli(["sweep", "--dry-run"], tty=False))["command"], "sweep")

    def test_flags_override_the_terminal_check_in_either_position(self) -> None:
        self.assertEqual(json.loads(self.run_cli(["sweep", "--dry-run", "--json"], tty=True))["dry_run"], True)
        self.assertEqual(json.loads(self.run_cli(["--json", "sweep", "--dry-run"], tty=True))["dry_run"], True)
        self.assertEqual(self.run_cli(["sweep", "--dry-run", "--text"], tty=False), "Nothing to sweep\n")

    def test_json_and_text_together_are_refused(self) -> None:
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["status", "--json", "--text"])


if __name__ == "__main__":
    unittest.main()

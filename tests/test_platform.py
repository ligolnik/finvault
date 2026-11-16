"""Parsers for hdiutil, lsof and hc output, and how the passphrase is passed."""

from __future__ import annotations

import subprocess
import unittest
from typing import Any
from unittest import mock

from finvault.errors import FinvaultError
from finvault.platform import (
    MacPlatform,
    Terminal,
    match_terminals,
    parse_hc_get,
    parse_hc_ls,
    parse_hdiutil_info,
    parse_lsof,
    parse_ps,
    size_mb,
)

SECRET = "pass phrase that must stay off argv"


class ParserTest(unittest.TestCase):
    def test_hdiutil_info_maps_image_path_to_mountpoint_and_whole_disk(self) -> None:
        data: dict[str, object] = {
            "images": [
                {
                    "image-path": "/vault/images/docs.sparsebundle",
                    "writeable": True,
                    "system-entities": [
                        {"dev-entry": "/dev/disk6"},
                        {"dev-entry": "/dev/disk6s1"},
                        {"dev-entry": "/dev/disk7"},
                        {"dev-entry": "/dev/disk7s1", "mount-point": "/work/Docs"},
                    ],
                },
                {"image-path": "/other.dmg", "system-entities": []},
                {"no-path": True},
            ]
        }
        images = parse_hdiutil_info(data)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].image_path, "/vault/images/docs.sparsebundle")
        self.assertEqual(images[0].mount_points, ("/work/Docs",))
        self.assertEqual(images[0].device, "/dev/disk6")
        self.assertTrue(images[0].writeable)
        self.assertEqual(images[1].mount_points, ())
        self.assertIsNone(images[1].device)

    def test_hdiutil_info_rejects_wrong_shape(self) -> None:
        with self.assertRaises(FinvaultError):
            parse_hdiutil_info([1, 2])

    def test_lsof_field_output(self) -> None:
        text = "p101\nczsh\nfcwd\nn/work/Docs\np202\ncclaude\nf3\nn/work/Docs/a.txt\nf4\nn/work/Docs/b.txt\n"
        procs = parse_lsof(text)
        self.assertEqual([(p.pid, p.command) for p in procs], [(101, "zsh"), (202, "claude")])
        self.assertEqual(procs[1].files, ["/work/Docs/a.txt", "/work/Docs/b.txt"])
        self.assertEqual(parse_lsof(""), [])

    def test_hc_ls_keeps_live_local_terminals(self) -> None:
        data = {
            "terminals": [
                {"address": "t1", "cwd": "/work/Docs", "state": "live", "agent": "claude-code", "where": "W / 1"},
                {"address": "t2", "cwd": "/work/Docs", "state": "finished"},
                {"address": "box/t3", "cwd": "/work/Docs", "state": "live"},
                {"address": "t4"},
            ],
            "machines": [],
        }
        terms = parse_hc_ls(data)
        self.assertEqual([t.address for t in terms], ["t1"])
        self.assertEqual(parse_hc_ls("nonsense"), [])

    def test_hc_get_gives_program_and_foreground_pids_and_current_cwd(self) -> None:
        data = {"address": "t1", "pid": 500, "cwd": "/start", "foreground": {"pid": 77, "cwd": "/work/Docs"}}
        self.assertEqual(parse_hc_get(data), ((500, 77), "/work/Docs"))
        self.assertEqual(parse_hc_get({"pid": 500, "foreground": {"pid": 500}}), ((500,), None))
        self.assertEqual(parse_hc_get({"pid": True, "foreground": None}), ((), None))
        self.assertEqual(parse_hc_get("nonsense"), ((), None))

    def test_ps_rows_map_pid_to_parent(self) -> None:
        self.assertEqual(parse_ps("    1     0\n  500     1\n   77   500\nPID PPID\n\n"), {1: 0, 500: 1, 77: 500})

    def test_busy_pids_map_to_the_nearest_terminal_ancestor(self) -> None:
        parents = {77: 60, 60: 500, 500: 1, 88: 1, 900: 901, 901: 900, 42: 10, 10: 1}
        terms = [
            Terminal("t2", "/x", pids=(10,)),
            Terminal("t1", "/start", pids=(400, 500)),
            Terminal("t3", "/y", pids=(60,)),
        ]
        got = match_terminals([77, 88, 900, 500, 42], parents, terms, self_pid=42)
        self.assertEqual(
            [(b.terminal.address, b.busy_pids, b.is_self) for b in got],
            [("t2", (42,), True), ("t1", (500,), False), ("t3", (77,), False)],
        )

    def test_size_mb_rounds_up(self) -> None:
        self.assertEqual(size_mb(0), 1)
        self.assertEqual(size_mb(1 << 20), 1)
        self.assertEqual(size_mb((1 << 20) + 1), 2)


class PassphraseTransportTest(unittest.TestCase):
    def run_with_fake_subprocess(self, action: str) -> list[Any]:
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with mock.patch("finvault.platform.subprocess.run", return_value=done) as run:
            p = MacPlatform(hc_command="hc-not-called")
            if action == "create":
                p.create_image("/v/i.sparsebundle", 1024, "finvault-docs", SECRET)
            elif action == "attach":
                p.attach("/v/i.sparsebundle", "/work/Docs", SECRET)
            elif action == "check":
                p.check_passphrase("/v/i.sparsebundle", SECRET)
            else:
                p.create_archive("/v/a.dmg", ["/work/Docs.plain"], "finvault-archive", SECRET)
        return run.call_args_list

    def test_passphrase_goes_only_to_stdin(self) -> None:
        for action in ("create", "attach", "check", "archive"):
            with self.subTest(action=action):
                calls = self.run_with_fake_subprocess(action)
                self.assertEqual(len(calls), 1)
                argv = calls[0].args[0]
                kwargs = calls[0].kwargs
                self.assertNotIn(SECRET, " ".join(argv))
                self.assertIn("-stdinpass", argv)
                self.assertEqual(kwargs["input"], SECRET.encode() + b"\0")
                self.assertNotIn("env", kwargs)

    def test_wrong_passphrase_message(self) -> None:
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"hdiutil: attach failed - Authentication error"
        )
        with (
            mock.patch("finvault.platform.subprocess.run", return_value=failed),
            self.assertRaises(FinvaultError) as ctx,
        ):
            MacPlatform().attach("/v/i.sparsebundle", "/work/Docs", SECRET)
        self.assertIn("wrong passphrase", ctx.exception.message)
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_empty_passphrase_is_refused_before_running_anything(self) -> None:
        with mock.patch("finvault.platform.subprocess.run") as run, self.assertRaises(FinvaultError):
            MacPlatform().attach("/v/i.sparsebundle", "/work/Docs", "")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

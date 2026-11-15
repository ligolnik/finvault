"""Sweep logic on fixed sample data built in each test."""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from finvault import sweep as sw
from finvault.config import SweepSettings
from finvault.errors import ConcurrentChangeError
from tests.helpers import TempDirTest

VAULT = ("/Users/example/Example/Docs", "/Users/example/.claude/projects/-Users-example-Example-Docs")


def line(
    project: str, ts: int, display: str = "hi", pastes: Mapping[str, object] | None = None, sid: str = "s1"
) -> bytes:
    obj = {
        "display": display,
        "pastedContents": dict(pastes or {}),
        "timestamp": ts,
        "project": project,
        "sessionId": sid,
    }
    return (json.dumps(obj) + "\n").encode()


class PureSweepTest(unittest.TestCase):
    def test_split_lines_keeps_endings_and_last_partial_line(self) -> None:
        self.assertEqual(sw.split_lines(b"a\nb\nc"), [b"a\n", b"b\n", b"c"])
        self.assertEqual(sw.split_lines(b"a\n"), [b"a\n"])
        self.assertEqual(sw.split_lines(b""), [])

    def test_vaulted_means_equal_or_under_a_vault_path(self) -> None:
        roots = VAULT[:1]
        self.assertTrue(sw.is_vaulted_line(line("/Users/example/Example/Docs", 1), roots))
        self.assertTrue(sw.is_vaulted_line(line("/Users/example/Example/Docs/sub", 1), roots))
        self.assertFalse(sw.is_vaulted_line(line("/Users/example/Example/Docs2", 1), roots))
        self.assertFalse(sw.is_vaulted_line(line("/Users/example/Example", 1), roots))

    def test_unparseable_and_projectless_lines_are_kept(self) -> None:
        lines = [b"not json\n", b"\n", b'{"display": "x"}\n', b"[1,2]\n", line(VAULT[0], 5)]
        kept, vaulted = sw.split_history(lines, VAULT)
        self.assertEqual(kept, lines[:4])
        self.assertEqual(vaulted, lines[4:])

    def test_pastes_moved_only_when_referenced_by_vaulted_lines_alone(self) -> None:
        shared = {"1": {"id": 1, "type": "text", "contentHash": "aaa111"}}
        only_vault = {"1": {"id": 1, "type": "text", "contentHash": "bbb222"}}
        inline = {"1": {"id": 1, "type": "text", "content": "short paste"}}
        vaulted = [
            line(VAULT[0], 1, pastes=shared),
            line(VAULT[0], 2, pastes=only_vault),
            line(VAULT[0], 3, pastes=inline),
        ]
        kept = [line("/Users/example/Other", 4, pastes=shared)]
        self.assertEqual(sw.pastes_to_move(kept, vaulted), {"bbb222"})

    def test_paste_hash_must_be_a_safe_file_name(self) -> None:
        evil = {"1": {"id": 1, "type": "text", "contentHash": "../../etc/passwd"}}
        self.assertEqual(sw.paste_hashes(line(VAULT[0], 1, pastes=evil)), set())

    def test_session_ids_come_from_jsonl_names(self) -> None:
        names = ["abc-123.jsonl", "memory", "notes.txt", "def-456.jsonl", "../x.jsonl"]
        self.assertEqual(sw.session_ids_from_names(names), {"abc-123", "def-456"})
        self.assertEqual(
            sw.file_history_to_move(["def-456", "zzz", "abc-123"], {"abc-123", "def-456"}), ["abc-123", "def-456"]
        )

    def test_scratch_matches_use_case_sensitive_globs(self) -> None:
        names = ["-Users-example-Example-Docs", "-Users-example-Other", "x-example-docs"]
        self.assertEqual(sw.scratch_matches(names, ("*Example-Docs*",)), ["-Users-example-Example-Docs"])

    def test_new_archive_lines_skip_lines_already_archived(self) -> None:
        a, b = line(VAULT[0], 1), line(VAULT[0], 2)
        self.assertEqual(sw.new_archive_lines([a], [a, b, b.rstrip(b"\n")]), [b])

    def test_merge_history_sorts_by_timestamp_and_dedupes(self) -> None:
        cur = [line("/o", 10), line("/o", 30), b"garbage\n", line("/o", 50)]
        arc = [line(VAULT[0], 20), line(VAULT[0], 40), line("/o", 30)]
        merged = sw.merge_history(cur, arc)
        ts = [json.loads(x)["timestamp"] if x.startswith(b"{") else None for x in merged]
        self.assertEqual(ts, [10, 20, 30, None, 40, 50])
        self.assertEqual(len(merged), 6)


class SweepFilesTest(TempDirTest):
    def settings(self) -> SweepSettings:
        claude = self.path("claude")
        return SweepSettings(
            archive_folder="docs",
            archive_subdir=".finvault-sweep",
            history=os.path.join(claude, "history.jsonl"),
            file_history=os.path.join(claude, "file-history"),
            paste_cache=os.path.join(claude, "paste-cache"),
            projects_dir=os.path.join(claude, "projects"),
            scratch_root=self.path("scratch"),
            scratch_globs=("*Example-Docs*",),
        )

    def build(self) -> tuple[SweepSettings, tuple[str, ...], list[bytes]]:
        s = self.settings()
        docs = self.path("Example", "Docs")
        proj = os.path.join(s.projects_dir, "-x-Example-Docs")
        os.makedirs(docs)
        lines = [
            line("/elsewhere", 100, "keep me", {"1": {"id": 1, "type": "text", "contentHash": "keep01"}}),
            line(docs, 200, "secret", {"1": {"id": 1, "type": "text", "contentHash": "vault01"}}, sid="sess-a"),
            line(docs + "/sub", 300, "secret 2", sid="sess-a"),
            b"not json\n",
            line("/elsewhere", 400, "keep 2"),
        ]
        self.write("claude/history.jsonl", b"".join(lines), 0o600)
        self.write("claude/paste-cache/keep01.txt", "kept paste")
        self.write("claude/paste-cache/vault01.txt", "vault paste")
        self.write("claude/projects/-x-Example-Docs/sess-a.jsonl", "{}\n")
        self.write("claude/file-history/sess-a/abc@v1", "backup")
        self.write("claude/file-history/sess-other/def@v1", "other backup")
        os.makedirs(self.path("scratch", "-x-Example-Docs", "inner"))
        os.makedirs(self.path("scratch", "-x-Other"))
        return s, (docs, proj), lines

    def test_dry_run_counts_and_changes_nothing(self) -> None:
        s, roots, _lines = self.build()
        before = Path(s.history).read_bytes()
        plan = sw.plan_sweep(s, roots)
        self.assertEqual(
            plan.counts(),
            {
                "history_lines": 2,
                "paste_files": 1,
                "file_history_folders": 1,
                "scratch_folders": 1,
                "unreadable_project_folders": 0,
            },
        )
        self.assertEqual(Path(s.history).read_bytes(), before)

    def test_apply_moves_everything_and_keeps_the_rest(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        mode_before = os.stat(s.history).st_mode
        sw.apply_sweep(sw.plan_sweep(s, roots), s, archive)
        self.assertEqual(Path(s.history).read_bytes(), lines[0] + lines[3] + lines[4])
        self.assertEqual(os.stat(s.history).st_mode, mode_before)
        self.assertEqual(Path(os.path.join(archive, "history.jsonl")).read_bytes(), lines[1] + lines[2])
        self.assertTrue(os.path.exists(os.path.join(s.paste_cache, "keep01.txt")))
        self.assertFalse(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))
        self.assertEqual(Path(os.path.join(archive, "paste-cache", "vault01.txt")).read_text(), "vault paste")
        self.assertFalse(os.path.exists(os.path.join(s.file_history, "sess-a")))
        self.assertEqual(Path(os.path.join(archive, "file-history", "sess-a", "abc@v1")).read_text(), "backup")
        self.assertTrue(os.path.exists(os.path.join(s.file_history, "sess-other", "def@v1")))
        self.assertFalse(os.path.exists(self.path("scratch", "-x-Example-Docs")))
        self.assertTrue(os.path.exists(self.path("scratch", "-x-Other")))

    def test_second_sweep_is_a_no_op(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        sw.apply_sweep(sw.plan_sweep(s, roots), s, archive)
        archived = Path(os.path.join(archive, "history.jsonl")).read_bytes()
        plan = sw.plan_sweep(s, roots)
        self.assertTrue(plan.is_empty())
        sw.apply_sweep(plan, s, archive)
        self.assertEqual(Path(os.path.join(archive, "history.jsonl")).read_bytes(), archived)

    def test_concurrent_write_aborts_with_no_changes(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        plan = sw.plan_sweep(s, roots)
        with open(s.history, "ab") as fh:
            fh.write(line("/elsewhere", 999, "written meanwhile"))
        with self.assertRaises(ConcurrentChangeError):
            sw.apply_sweep(plan, s, archive)
        self.assertEqual(Path(s.history).read_bytes(), b"".join(lines) + line("/elsewhere", 999, "written meanwhile"))
        self.assertFalse(os.path.exists(os.path.join(archive, "history.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))
        self.assertTrue(os.path.exists(os.path.join(s.file_history, "sess-a")))
        self.assertEqual([n for n in os.listdir(os.path.dirname(s.history)) if n.endswith(".tmp")], [])

    def test_change_between_read_and_stat_aborts_planning(self) -> None:
        s, roots, _lines = self.build()
        stamps = iter([(1, 1, 1), (2, 2, 2)])

        def changing_stamp(_path: str) -> tuple[int, int, int]:
            return next(stamps)

        with mock.patch.object(sw, "file_stamp", side_effect=changing_stamp), self.assertRaises(ConcurrentChangeError):
            sw.plan_sweep(s, roots)

    def test_locked_projects_folder_is_reported_not_guessed(self) -> None:
        s, roots, _lines = self.build()
        os.chmod(roots[1], 0o000)
        plan = sw.plan_sweep(s, roots)
        self.assertEqual(plan.file_history, [])
        self.assertEqual(plan.unreadable_projects, [roots[1]])

    def test_restore_history_puts_lines_back_in_order(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        sw.apply_sweep(sw.plan_sweep(s, roots), s, archive)
        report = sw.restore_history(s, archive)
        self.assertEqual(report, {"history_lines_restored": 2, "paste_files_restored": 1})
        restored = Path(s.history).read_bytes()
        self.assertEqual(sorted(sw.split_lines(restored)), sorted(lines))
        self.assertEqual(sw.restore_history(s, archive), {"history_lines_restored": 0, "paste_files_restored": 0})

    def test_move_file_keeps_a_differing_existing_copy(self) -> None:
        src = self.write("a/f.txt", "new")
        self.write("b/f.txt", "old")
        dst = sw.move_file(src, self.path("b", "f.txt"))
        self.assertEqual(dst, self.path("b", "f.txt.1"))
        self.assertEqual(Path(self.path("b", "f.txt")).read_text(), "old")
        self.assertFalse(os.path.exists(src))


if __name__ == "__main__":
    unittest.main()

"""Sweep logic on fixed sample data built in each test."""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from finvault import sweep as sw
from finvault.config import SweepSettings
from finvault.errors import ConcurrentChangeError, FinvaultError
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
                "problems": [],
            },
        )
        self.assertEqual(Path(s.history).read_bytes(), before)

    def test_apply_moves_everything_and_keeps_the_rest(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        mode_before = os.stat(s.history).st_mode
        sw.apply_sweep(sw.plan_sweep(s, roots), s, archive, roots[0], roots)
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
        sw.apply_sweep(sw.plan_sweep(s, roots), s, archive, roots[0], roots)
        archived = Path(os.path.join(archive, "history.jsonl")).read_bytes()
        plan = sw.plan_sweep(s, roots)
        self.assertTrue(plan.is_empty())
        sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertEqual(Path(os.path.join(archive, "history.jsonl")).read_bytes(), archived)

    def test_concurrent_write_aborts_with_no_changes(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        plan = sw.plan_sweep(s, roots)
        with open(s.history, "ab") as fh:
            fh.write(line("/elsewhere", 999, "written meanwhile"))
        with self.assertRaises(ConcurrentChangeError):
            sw.apply_sweep(plan, s, archive, roots[0], roots)
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
        self.assertFalse(plan.is_complete())
        archive = os.path.join(roots[0], ".finvault-sweep")
        with self.assertRaises(FinvaultError):
            sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertFalse(os.path.exists(archive))

    def check_unreadable_source_makes_the_plan_incomplete(self, folder: str) -> None:
        s, roots, _lines = self.build()
        path = str(getattr(s, folder))
        os.chmod(path, 0o000)
        try:
            plan = sw.plan_sweep(s, roots)
        finally:
            os.chmod(path, 0o700)
        self.assertFalse(plan.is_complete())
        self.assertIn(path, plan.problems[0])

    def test_unreadable_file_history_makes_the_plan_incomplete(self) -> None:
        self.check_unreadable_source_makes_the_plan_incomplete("file_history")

    def test_unreadable_scratch_root_makes_the_plan_incomplete(self) -> None:
        self.check_unreadable_source_makes_the_plan_incomplete("scratch_root")

    def test_history_with_a_second_hard_link_is_refused(self) -> None:
        s, roots, lines = self.build()
        os.link(s.history, self.path("history-alias"))
        archive = os.path.join(roots[0], ".finvault-sweep")
        plan = sw.plan_sweep(s, roots, archive)
        self.assertFalse(plan.is_complete())
        with self.assertRaises(FinvaultError):
            sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertEqual(Path(s.history).read_bytes(), b"".join(lines))

    def test_symlinked_file_history_session_is_refused_not_followed(self) -> None:
        s, roots, _lines = self.build()
        session = Path(s.file_history, "sess-a")
        for child in session.iterdir():
            child.unlink()
        session.rmdir()
        external = self.write("external-session/only-copy", "important")
        os.symlink(os.path.dirname(external), session)
        archive = os.path.join(roots[0], ".finvault-sweep")
        plan = sw.plan_sweep(s, roots, archive)
        self.assertEqual(plan.file_history, [])
        self.assertIn("symlink", plan.problems[0])
        with self.assertRaises(FinvaultError):
            sw.move_tree(str(session), os.path.join(archive, "file-history", "sess-a"), roots[0])
        self.assertEqual(Path(external).read_text(), "important")

    def test_partial_archive_line_is_trimmed_before_the_retry(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        real_append = sw.append_fsync

        def partial(path: str, data: bytes, mode: int = 0o600) -> None:
            real_append(path, data[:19], mode)
            raise OSError(28, "No space left on device")

        with mock.patch.object(sw, "append_fsync", side_effect=partial), self.assertRaises(OSError):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertEqual(Path(s.history).read_bytes(), b"".join(lines))
        report = sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertEqual(report["archive_partial_bytes_trimmed"], 19)
        self.assertEqual(Path(archive, "history.jsonl").read_bytes(), lines[1] + lines[2])
        sw.restore_history(s, archive)
        self.assertEqual(sorted(sw.read_lines(s.history)), sorted(lines))

    def test_late_lines_are_saved_when_the_folder_sync_fails(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        late = line("/elsewhere", 999, "late")
        real_replace = os.replace

        def race_then_replace(src: str, dst: str) -> None:
            if dst == s.history:
                with open(s.history, "ab") as fh:
                    fh.write(late)
            real_replace(src, dst)

        plan = sw.plan_sweep(s, roots, archive)
        with (
            mock.patch.object(sw.os, "replace", side_effect=race_then_replace),
            mock.patch.object(sw, "fsync_dir", side_effect=OSError(5, "Input/output error")),
            self.assertRaises(OSError),
        ):
            sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertEqual(Path(s.history).read_bytes(), lines[0] + lines[3] + lines[4] + late)

    def test_restore_history_puts_lines_back_in_order(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        sw.apply_sweep(sw.plan_sweep(s, roots), s, archive, roots[0], roots)
        report = sw.restore_history(s, archive)
        self.assertEqual(report, {"history_lines_restored": 2, "paste_files_restored": 1})
        restored = Path(s.history).read_bytes()
        self.assertEqual(sorted(sw.split_lines(restored)), sorted(lines))
        self.assertEqual(sw.restore_history(s, archive), {"history_lines_restored": 0, "paste_files_restored": 0})

    def test_line_appended_while_the_archive_is_written_is_not_lost(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        late = line("/elsewhere", 999, "written meanwhile")
        real_append = sw.append_fsync

        def append_then_race(path: str, data: bytes, mode: int = 0o600) -> None:
            real_append(path, data, mode)
            if path.startswith(archive):
                with open(s.history, "ab") as fh:
                    fh.write(late)

        plan = sw.plan_sweep(s, roots, archive)
        with (
            mock.patch.object(sw, "append_fsync", side_effect=append_then_race),
            self.assertRaises(ConcurrentChangeError),
        ):
            sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertTrue(Path(s.history).read_bytes().endswith(late))
        sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertEqual(Path(s.history).read_bytes(), lines[0] + lines[3] + lines[4] + late)
        self.assertEqual(Path(archive, "history.jsonl").read_bytes(), lines[1] + lines[2])

    def test_lines_appended_to_the_old_file_during_the_rename_are_recovered(self) -> None:
        s, roots, lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        late_kept = line("/elsewhere", 998, "other project")
        late_vaulted = line(roots[0], 999, "vault, written meanwhile")
        real_replace = os.replace

        def race_then_replace(src: str, dst: str) -> None:
            if dst == s.history:
                with open(s.history, "ab") as fh:
                    fh.write(late_kept + late_vaulted)
            real_replace(src, dst)

        plan = sw.plan_sweep(s, roots, archive)
        with mock.patch.object(sw.os, "replace", side_effect=race_then_replace):
            report = sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertEqual(report["history_lines_written_meanwhile"], 2)
        self.assertEqual(Path(s.history).read_bytes(), lines[0] + lines[3] + lines[4] + late_kept)
        self.assertEqual(Path(archive, "history.jsonl").read_bytes(), lines[1] + lines[2] + late_vaulted)

    def test_paste_left_by_a_failed_sweep_is_found_again(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        with (
            mock.patch.object(sw, "move_file", side_effect=OSError(28, "No space left on device")),
            self.assertRaises(OSError),
        ):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertTrue(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))
        plan = sw.plan_sweep(s, roots, archive)
        self.assertEqual(plan.vaulted, [])
        self.assertEqual(plan.pastes, ["vault01"])
        sw.apply_sweep(plan, s, archive, roots[0], roots)
        self.assertFalse(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))
        self.assertEqual(Path(archive, "paste-cache", "vault01.txt").read_text(), "vault paste")
        self.assertTrue(sw.plan_sweep(s, roots, archive).is_empty())

    def test_archive_folder_symlinked_outside_the_vault_is_refused(self) -> None:
        s, roots, lines = self.build()
        outside = self.path("outside")
        os.makedirs(outside)
        archive = os.path.join(roots[0], ".finvault-sweep")
        os.symlink(outside, archive)
        with self.assertRaises(FinvaultError):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(Path(s.history).read_bytes(), b"".join(lines))
        self.assertTrue(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))

    def check_symlinked_entry_is_refused(self, entry: str) -> None:
        s, roots, lines = self.build()
        outside = self.path("outside")
        os.makedirs(os.path.join(outside, "dir"))
        archive = os.path.join(roots[0], ".finvault-sweep")
        os.makedirs(archive)
        target = os.path.join(outside, "history.jsonl") if entry == "history.jsonl" else os.path.join(outside, "dir")
        os.symlink(target, os.path.join(archive, entry))
        with self.assertRaises(FinvaultError):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        written = [f for _d, _dirs, files in os.walk(outside) for f in files]
        self.assertEqual(written, [])
        self.assertEqual(Path(s.history).read_bytes(), b"".join(lines))

    def test_archive_history_symlinked_outside_is_refused(self) -> None:
        self.check_symlinked_entry_is_refused("history.jsonl")

    def test_archive_paste_cache_symlinked_outside_is_refused(self) -> None:
        self.check_symlinked_entry_is_refused("paste-cache")

    def test_archive_file_history_symlinked_outside_is_refused(self) -> None:
        self.check_symlinked_entry_is_refused("file-history")

    def test_hard_linked_paste_is_refused(self) -> None:
        s, roots, _lines = self.build()
        os.link(os.path.join(s.paste_cache, "vault01.txt"), self.path("plaintext-alias"))
        archive = os.path.join(roots[0], ".finvault-sweep")
        plan = sw.plan_sweep(s, roots, archive)
        self.assertFalse(plan.is_complete())
        self.assertIn("hard links", plan.problems[0])
        with self.assertRaises(FinvaultError):
            sw.move_file(os.path.join(s.paste_cache, "vault01.txt"), self.path("v", "p.txt"), self.tmp)
        self.assertTrue(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))

    def test_hard_linked_backup_is_refused(self) -> None:
        s, roots, _lines = self.build()
        os.link(os.path.join(s.file_history, "sess-a", "abc@v1"), self.path("backup-alias"))
        plan = sw.plan_sweep(s, roots, os.path.join(roots[0], ".finvault-sweep"))
        self.assertFalse(plan.is_complete())
        self.assertIn("abc@v1", plan.problems[0])

    def test_late_vaulted_line_goes_back_to_history_when_the_archive_is_full(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        late = line(roots[0], 999, "UNIQUE LATE SECRET")
        real_replace, real_append = os.replace, sw.append_fsync

        def race_then_replace(src: str, dst: str) -> None:
            if dst == s.history:
                with open(s.history, "ab") as fh:
                    fh.write(late)
            real_replace(src, dst)

        def archive_full(path: str, data: bytes, mode: int = 0o600) -> None:
            if b"UNIQUE LATE SECRET" in data and path.startswith(archive):
                real_append(path, data[:19], mode)
                raise OSError(28, "No space left on device")
            real_append(path, data, mode)

        with (
            mock.patch.object(sw.os, "replace", side_effect=race_then_replace),
            mock.patch.object(sw, "append_fsync", side_effect=archive_full),
            self.assertRaises(OSError),
        ):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertIn(late, sw.read_lines(s.history))
        sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertNotIn(late, sw.read_lines(s.history))
        self.assertIn(late, sw.read_lines(os.path.join(archive, "history.jsonl")))

    def test_late_vaulted_line_reaches_the_archive_when_history_is_full(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        late_kept = line("/elsewhere", 998, "late, other project")
        late_vaulted = line(roots[0], 999, "late, vault")
        real_replace, real_append = os.replace, sw.append_fsync

        def race_then_replace(src: str, dst: str) -> None:
            if dst == s.history:
                with open(s.history, "ab") as fh:
                    fh.write(late_kept + late_vaulted)
            real_replace(src, dst)

        def history_full(path: str, data: bytes, mode: int = 0o600) -> None:
            if path == s.history:
                raise OSError(28, "No space left on device")
            real_append(path, data, mode)

        with (
            mock.patch.object(sw.os, "replace", side_effect=race_then_replace),
            mock.patch.object(sw, "append_fsync", side_effect=history_full),
            self.assertRaises(OSError),
        ):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertIn(late_vaulted, sw.read_lines(os.path.join(archive, "history.jsonl")))

    def test_a_leftover_paste_restore_temp_makes_the_plan_incomplete(self) -> None:
        s, roots, _lines = self.build()
        leftover = self.write("claude/paste-cache/.finvault-abc123.tmp", "vault paste")
        plan = sw.plan_sweep(s, roots)
        self.assertFalse(plan.is_complete())
        self.assertTrue(any(leftover in p for p in plan.problems))

    def test_move_file_keeps_the_source_when_the_existing_copy_changes_during_the_sync(self) -> None:
        src = self.write("source", "ONLY ORIGINAL")
        dst = self.write("vault/dest", "ONLY ORIGINAL")
        real_sync = sw.fsync_dir

        def sync_then_change(path: str) -> None:
            real_sync(path)
            Path(dst).write_text("replacement")

        with mock.patch.object(sw, "fsync_dir", side_effect=sync_then_change), self.assertRaises(ConcurrentChangeError):
            sw.move_file(src, dst, self.path("vault"))
        self.assertEqual(Path(src).read_text(), "ONLY ORIGINAL")

    def test_traces_left_reports_a_folder_it_cant_read(self) -> None:
        s, roots, _lines = self.build()
        os.chmod(s.file_history, 0o000)
        try:
            left = sw.traces_left(s, roots, {"sess-a"}, set())
        finally:
            os.chmod(s.file_history, 0o700)
        self.assertEqual(left["unreadable"], [s.file_history])

    def test_late_line_for_another_project_is_parked_in_the_vault_when_history_is_full(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        late = line("/elsewhere", 999, "UNIQUE LATE OTHER PROJECT")
        real_replace, real_append = os.replace, sw.append_fsync

        def race_then_replace(src: str, dst: str) -> None:
            if dst == s.history:
                with open(s.history, "ab") as fh:
                    fh.write(late)
            real_replace(src, dst)

        def history_full(path: str, data: bytes, mode: int = 0o600) -> None:
            if path == s.history:
                raise OSError(28, "No space left on device")
            real_append(path, data, mode)

        with (
            mock.patch.object(sw.os, "replace", side_effect=race_then_replace),
            mock.patch.object(sw, "append_fsync", side_effect=history_full),
            self.assertRaises(OSError),
        ):
            sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        self.assertEqual(sw.read_lines(os.path.join(archive, sw.ARCHIVE_OVERFLOW)), [late])
        sw.restore_history(s, archive)
        self.assertIn(late, sw.read_lines(s.history))

    def test_traces_left_finds_vault_pastes_and_leftover_temps(self) -> None:
        s, roots, _lines = self.build()
        self.write("claude/.history.jsonl.abc.tmp", "x")
        left = sw.traces_left(s, roots, set(), {"vault01", "gone99"})
        self.assertEqual(left["paste_files"], ["vault01"])
        self.assertEqual(left["leftover_temps"], [os.path.join(os.path.dirname(s.history), ".history.jsonl.abc.tmp")])

    def test_traces_left_reports_a_paste_cache_it_cant_read(self) -> None:
        s, roots, _lines = self.build()
        os.chmod(s.paste_cache, 0o000)
        try:
            left = sw.traces_left(s, roots, set(), {"vault01"})
        finally:
            os.chmod(s.paste_cache, 0o700)
        self.assertEqual(left["unreadable"], [s.paste_cache])

    def test_vaulting_the_whole_projects_folder_finds_every_session(self) -> None:
        s, roots, _lines = self.build()
        self.write("claude/projects/other-project/sess-other.jsonl", "{}\n")
        plan = sw.plan_sweep(s, (roots[0], s.projects_dir))
        self.assertEqual(plan.file_history, ["sess-a", "sess-other"])
        self.assertTrue(plan.is_complete())

    def test_interrupted_restore_leaves_no_temp_file(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        with (
            mock.patch("finvault.pathutil.os.fsync", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            sw.restore_history(s, archive)
        self.assertEqual(list(Path(s.history).parent.glob(".history.jsonl.*.tmp")), [])

    def test_a_leftover_rewrite_temp_makes_the_plan_incomplete(self) -> None:
        s, roots, _lines = self.build()
        leftover = self.write("claude/.history.jsonl.abc123.tmp", line(roots[0], 1, "secret"))
        plan = sw.plan_sweep(s, roots)
        self.assertFalse(plan.is_complete())
        self.assertIn(leftover, plan.problems[0])

    def test_restore_retries_a_paste_and_reports_one_that_differs(self) -> None:
        s, roots, _lines = self.build()
        archive = os.path.join(roots[0], ".finvault-sweep")
        sw.apply_sweep(sw.plan_sweep(s, roots, archive), s, archive, roots[0], roots)
        real_copy = sw.shutil.copyfileobj

        def full(fsrc: BinaryIO, fdst: BinaryIO, length: int = 0) -> None:
            fdst.write(fsrc.read(3))
            raise OSError(28, "No space left on device")

        with mock.patch.object(sw.shutil, "copyfileobj", side_effect=full), self.assertRaises(OSError):
            sw.restore_history(s, archive)
        self.assertFalse(os.path.exists(os.path.join(s.paste_cache, "vault01.txt")))
        self.assertIs(sw.shutil.copyfileobj, real_copy)
        self.assertEqual(sw.restore_history(s, archive)["paste_files_restored"], 1)
        self.assertEqual(Path(s.paste_cache, "vault01.txt").read_text(), "vault paste")
        Path(s.paste_cache, "vault01.txt").write_text("vau")
        report = sw.restore_history(s, archive)
        self.assertEqual(report["paste_files_differing"], [os.path.join(s.paste_cache, "vault01.txt")])

    def test_move_file_keeps_the_source_when_the_existing_copy_changes(self) -> None:
        src = self.write("source", "ONLY ORIGINAL")
        dst = self.write("vault/dest", "ONLY ORIGINAL")
        real_compare = sw._same_content  # pyright: ignore[reportPrivateUsage] -- inject a change after the compare

        def compare_then_change(a: str, b: str) -> bool:
            same = real_compare(a, b)
            Path(b).write_text("replacement")
            return same

        with (
            mock.patch.object(sw, "_same_content", side_effect=compare_then_change),
            self.assertRaises(ConcurrentChangeError),
        ):
            sw.move_file(src, dst, self.path("vault"))
        self.assertEqual(Path(src).read_text(), "ONLY ORIGINAL")

    def test_move_file_keeps_a_differing_existing_copy(self) -> None:
        src = self.write("a/f.txt", "new")
        self.write("b/f.txt", "old")
        dst = sw.move_file(src, self.path("b", "f.txt"), self.tmp)
        self.assertEqual(dst, self.path("b", "f.txt.1"))
        self.assertEqual(Path(self.path("b", "f.txt")).read_text(), "old")
        self.assertFalse(os.path.exists(src))

    def test_move_file_keeps_a_source_written_during_the_copy(self) -> None:
        src = self.write("a/backup", "first")
        real_copy = sw.shutil.copyfileobj

        def copy_then_append(fsrc: BinaryIO, fdst: BinaryIO, length: int = 0) -> None:
            real_copy(fsrc, fdst, length)
            with open(src, "a") as out:
                out.write(" second")

        os.makedirs(self.path("vault"))
        with (
            mock.patch.object(sw.shutil, "copyfileobj", side_effect=copy_then_append),
            self.assertRaises(ConcurrentChangeError),
        ):
            sw.move_file(src, self.path("vault", "backup"), self.path("vault"))
        self.assertEqual(Path(src).read_text(), "first second")
        sw.move_file(src, self.path("vault", "backup"), self.path("vault"))
        self.assertEqual(Path(self.path("vault", "backup.1")).read_text(), "first second")
        self.assertFalse(os.path.exists(src))

    def test_move_file_never_writes_through_a_symlink_put_at_the_destination(self) -> None:
        src = self.write("a/paste", "secret")
        outside = self.write("outside", "precious")
        os.makedirs(self.path("vault"))
        dst = self.path("vault", "paste")
        real_mkstemp = sw.tempfile.mkstemp

        def plant_then_mkstemp(prefix: str, suffix: str, dir: str) -> tuple[int, str]:
            os.symlink(outside, dst)
            return real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

        with mock.patch.object(sw.tempfile, "mkstemp", side_effect=plant_then_mkstemp):
            placed = sw.move_file(src, dst, self.path("vault"))
        self.assertEqual(Path(outside).read_text(), "precious")
        self.assertEqual(placed, dst + ".1")
        self.assertEqual(Path(placed).read_text(), "secret")
        self.assertEqual(sorted(os.listdir(self.path("vault"))), ["paste", "paste.1"])


if __name__ == "__main__":
    unittest.main()

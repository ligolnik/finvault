"""Checksum lists built from folders made in each test."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from finvault import manifest as mf
from finvault.errors import FinvaultError
from tests.helpers import TempDirTest


class ManifestTest(TempDirTest):
    def build_tree(self) -> str:
        self.write("root/a.txt", "a")
        self.write("root/history/old.txt", "old")
        os.symlink("a.txt", self.path("root", "link"))
        return self.path("root")

    def test_compare_reports_missing_changed_and_added(self) -> None:
        root = self.build_tree()
        before = mf.build_manifest(root)
        Path(root, "a.txt").write_text("edited")
        os.unlink(os.path.join(root, "link"))
        self.write("root/new.txt", "new")
        diff = mf.compare(before, mf.build_manifest(root))
        self.assertEqual((diff.missing, diff.changed, diff.added), (["link"], ["a.txt"], ["new.txt"]))

    def test_without_drops_top_level_names_like_build_exclude(self) -> None:
        root = self.build_tree()
        full = mf.build_manifest(root)
        excluded = mf.build_manifest(root, exclude=frozenset({"history"}))
        self.assertTrue(mf.compare(full.without(frozenset({"history"})), excluded).ok())
        self.assertFalse(mf.compare(full, excluded).ok())
        self.assertIs(full.without(frozenset()), full)

    def test_saved_manifest_round_trips(self) -> None:
        root = self.build_tree()
        m = mf.build_manifest(root)
        mf.save_manifest(root, m)
        self.assertTrue(mf.compare(m, mf.load_manifest(root)).ok())
        self.assertTrue(mf.compare(m, mf.build_manifest(root, volume_root=True)).ok())

    def test_a_folder_that_cant_be_read_fails_the_walk(self) -> None:
        root = self.build_tree()
        os.chmod(self.path("root", "history"), 0o000)
        try:
            with self.assertRaises(FinvaultError) as ctx:
                mf.build_manifest(root)
        finally:
            os.chmod(self.path("root", "history"), 0o700)
        self.assertIn("history", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()

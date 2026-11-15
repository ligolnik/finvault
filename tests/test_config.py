"""Config parsing and validation on fixed tables."""

from __future__ import annotations

import os
import unittest

from finvault.config import Config, load_config, parse_config
from finvault.errors import FinvaultError
from tests.helpers import TempDirTest

HOME = "/Users/example"


def parse(data: dict[str, object]) -> Config:
    return parse_config(data, config_path=f"{HOME}/.finvault/config.toml", home=HOME, uid=501)


def base(**extra: object) -> dict[str, object]:
    data: dict[str, object] = {
        "state_dir": "~/.finvault",
        "folders": [
            {"name": "docs", "path": "~/Example/Docs"},
            {"name": "docs-history", "path": "~/.claude/projects/-Users-example-Example-Docs", "size": "2g"},
        ],
    }
    data.update(extra)
    return data


class ParseConfigTest(unittest.TestCase):
    def test_minimal_config_expands_home_and_sets_state_layout(self) -> None:
        cfg = parse(base())
        self.assertEqual(cfg.state_dir, f"{HOME}/.finvault")
        self.assertEqual(cfg.images_dir, f"{HOME}/.finvault/images")
        self.assertEqual(cfg.image_path(cfg.folder("docs")), f"{HOME}/.finvault/images/docs.sparsebundle")
        self.assertEqual(cfg.folder("docs").path, f"{HOME}/Example/Docs")
        self.assertEqual(cfg.folder("docs-history").size, "2g")
        self.assertEqual(cfg.image.min_size_gb, 1)
        self.assertEqual(cfg.image.size_factor, 4)
        self.assertIsNone(cfg.sweep)

    def test_sweep_defaults_live_under_claude_dir(self) -> None:
        cfg = parse(base(sweep={"scratch_globs": ["*Example-Docs*"]}))
        assert cfg.sweep is not None
        self.assertEqual(cfg.sweep.history, f"{HOME}/.claude/history.jsonl")
        self.assertEqual(cfg.sweep.file_history, f"{HOME}/.claude/file-history")
        self.assertEqual(cfg.sweep.paste_cache, f"{HOME}/.claude/paste-cache")
        self.assertEqual(cfg.sweep.projects_dir, f"{HOME}/.claude/projects")
        self.assertEqual(cfg.sweep.scratch_root, "/private/tmp/claude-501")
        self.assertEqual(cfg.sweep.archive_folder, "docs")
        self.assertEqual(cfg.sweep_archive_dir(), f"{HOME}/Example/Docs/.finvault-sweep")

    def test_sweep_can_be_disabled(self) -> None:
        self.assertIsNone(parse(base(sweep={"enabled": False})).sweep)

    def test_rejections_name_the_problem(self) -> None:
        cases: dict[str, dict[str, object]] = {
            "state_dir": {"folders": [{"name": "a", "path": "~/A"}]},
            "unknown key": base(colour="blue"),
            "not a valid name": base(folders=[{"name": "Bad Name", "path": "~/A"}]),
            "duplicate": base(folders=[{"name": "a", "path": "~/A"}, {"name": "a", "path": "~/B"}]),
            "nested": base(folders=[{"name": "a", "path": "~/A"}, {"name": "b", "path": "~/A/B"}]),
            "too broad": base(folders=[{"name": "a", "path": "~"}]),
            "overlap": base(state_dir="~/A/state", folders=[{"name": "a", "path": "~/A"}]),
            "absolute": base(folders=[{"name": "a", "path": "relative/path"}]),
            "is not a size": base(folders=[{"name": "a", "path": "~/A", "size": "10"}]),
            "not a configured folder": base(sweep={"archive_folder": "nope"}),
            "too broad ": base(sweep={"scratch_globs": ["*"]}),
            "may not contain": base(sweep={"scratch_globs": ["../x*"]}),
            "positive integer": base(image={"size_factor": 0}),
            "no [[folders]]": {"state_dir": "~/.finvault"},
        }
        for fragment, data in cases.items():
            with self.subTest(fragment=fragment):
                with self.assertRaises(FinvaultError) as ctx:
                    parse(data)
                self.assertIn(fragment.strip(), str(ctx.exception))


class LoadConfigTest(TempDirTest):
    def test_missing_file_says_how_to_create_one(self) -> None:
        with self.assertRaises(FinvaultError) as ctx:
            load_config(self.path("nope.toml"))
        self.assertIn("config.example.toml", ctx.exception.hint)

    def test_invalid_toml_is_reported(self) -> None:
        self.write("bad.toml", "state_dir = \n")
        with self.assertRaises(FinvaultError) as ctx:
            load_config(self.path("bad.toml"))
        self.assertIn("not valid TOML", ctx.exception.message)

    def test_example_config_parses(self) -> None:
        example = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.example.toml")
        cfg = load_config(example)
        self.assertGreaterEqual(len(cfg.folders), 1)
        self.assertIsNotNone(cfg.sweep)


if __name__ == "__main__":
    unittest.main()

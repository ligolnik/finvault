"""Keep the shipped agent instructions aligned with the CLI and JSON output."""

from __future__ import annotations

import ast
import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path

from finvault.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "finvault" / "SKILL.md"
JSON_KEYS = (
    "folders",
    "path",
    "unlocked",
    "busy_processes",
    "terminals",
    "self",
    "sweep_pending",
    "history_holders",
    "warnings",
    "error",
    "problems",
    "busy",
    "failed",
    "written_while_locking",
    "history_lines_written_while_locking",
    "not_mode_000",
)
PREAMBLE = (
    "This skill is an action router — pick the step that matches the user's intent and execute only that step. "
    "Do not run other steps; do not parallelize."
)


class SkillTest(unittest.TestCase):
    def test_frontmatter_and_router(self) -> None:
        text = SKILL.read_text()
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        fields = dict(line.split(":", 1) for line in match[1].splitlines())
        self.assertEqual(fields["name"].strip(), "finvault")
        self.assertTrue(fields["description"].strip())
        body = text[match.end() :].strip().splitlines()
        self.assertTrue(body[0].startswith("# "))
        self.assertEqual(next(line for line in body[1:] if line.strip()), PREAMBLE)
        headings = re.findall(r"^#{2,} .+$", text, re.MULTILINE)
        steps = re.findall(r"^## Step (\d+) — .+$", text, re.MULTILINE)
        self.assertTrue(steps)
        self.assertEqual(len(headings), len(steps))
        self.assertEqual([int(step) for step in steps], list(range(1, len(steps) + 1)))

    def test_command_examples_parse(self) -> None:
        commands = re.findall(r"`(finvault\s+[^`\n]+)`", SKILL.read_text())
        self.assertTrue(commands)
        parser = build_parser()
        parser.allow_abbrev = False
        for command in commands:
            with self.subTest(command=command):
                args = shlex.split(command)[1:]
                parsed = parser.parse_args(args)
                flags = set(re.findall(r"(?<![\w-])--?[a-z][a-z-]*", self._command_help(str(parsed.command))))
                # argparse accepts abbreviated flags in subparsers too; require exact spellings.
                for token in args:
                    if token.startswith("-"):
                        self.assertIn(token.split("=", 1)[0], flags)

    @staticmethod
    def _command_help(command: str) -> str:
        # Ask the real parser for help without executing a command or building another option list.
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.suppress(SystemExit):
            build_parser().parse_args([command, "--help"])
        return output.getvalue()

    def test_json_keys_are_source_literals(self) -> None:
        literals = {
            node.value
            for name in ("vault.py", "sweep.py")
            for node in ast.walk(ast.parse((ROOT / "src" / "finvault" / name).read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        text = SKILL.read_text()
        for key in JSON_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, text)
                self.assertIn(key, literals)

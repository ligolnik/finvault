"""Keep the shipped agent instructions aligned with the CLI and JSON output."""

from __future__ import annotations

import ast
import contextlib
import io
import re
import shlex
import unittest
from dataclasses import dataclass
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


# Phrasings that offer to lock on the human's behalf; the skill must only ask.
OFFER = re.compile(
    r"(?i)\b(shall|should|can|may) I (lock|run)\b|\bwant me to\b|\bI('ll| will| can) (lock|run `finvault lock)"
    r"|\blet me (lock|run `finvault lock)|\bI could lock\b"
)


@dataclass
class Step:
    number: int
    title: str
    body: str


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)


def _steps(text: str) -> list[Step]:
    parts = re.split(r"^## Step (\d+) — (.+)$", text, flags=re.MULTILINE)
    return [Step(int(parts[i]), parts[i + 1], parts[i + 2]) for i in range(1, len(parts), 3)]


def _step_titled(text: str, word: str) -> Step:
    found = [s for s in _steps(text) if word in s.title]
    if len(found) != 1:
        raise AssertionError(f"expected one step titled with {word!r}, found {[s.title for s in found]}")
    return found[0]


def _sentences(text: str) -> list[str]:
    """Sentences and list items, with line wraps joined."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n|\n(?=\s*(?:\d+\.|-) )", text):
        joined = " ".join(line.strip() for line in block.splitlines()).strip()
        out.extend(s for s in re.split(r"(?<=[.?!;])\s+", joined) if s)
    return out


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

    def test_finishing_vault_work_chains_to_a_step_that_asks_about_locking(self) -> None:
        text = SKILL.read_text()
        finish = _step_titled(text, "Finish")
        chain = re.search(r"whenever you finish work that used a vault folder, run Step (\d+)", text)
        self.assertIsNotNone(chain)
        assert chain is not None
        self.assertEqual(int(chain[1]), finish.number)
        self.assertIn("as in Step 4", finish.body)
        self.assertIn("Run `finvault status`", finish.body)
        questions = [q for q in re.findall(r'"([^"]*)"', finish.body) if q.rstrip().endswith("?") or "?" in q]
        self.assertTrue(any("locked now?" in q for q in questions), questions)
        for field in ("terminals", "busy_processes", "--force"):
            self.assertIn(field, finish.body)
        self.assertRegex(finish.body, r"the human runs `finvault lock`")

    def test_the_agent_only_ever_runs_finvault_status(self) -> None:
        text = _strip_frontmatter(SKILL.read_text())
        quoted = re.findall(r'"([^"]*)"', text)
        for sentence in _sentences(re.sub(r'"[^"]*"', '""', text)):
            for command in re.findall(r"`finvault ([a-z-]+)[^`]*`", sentence):
                if command == "status":
                    continue
                with self.subTest(sentence=sentence):
                    # Any other command is the human's to run, or named in a never-rule.
                    self.assertRegex(sentence, r"(?i)\bhuman\b|\bthemselves\b|\bnever\b")
                    self.assertNotRegex(sentence, r"^(\d+\.\s*)?Run `finvault (?!status)")
        for speech in quoted:
            with self.subTest(speech=speech):
                self.assertNotRegex(speech, OFFER)

    def test_the_skill_never_offers_to_lock(self) -> None:
        self.assertNotRegex(SKILL.read_text(), OFFER)

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

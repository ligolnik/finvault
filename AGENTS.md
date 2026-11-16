# Working on finvault

Instructions for AI agents (and people) changing this repository. Read the README first: it says what
finvault promises, and every change must keep those promises.

## Safety rules

- Never touch real user data. Tests and experiments use fresh temp folders only, never the real
  `~/.claude`, `~/.finvault`, or any folder a vault config names.
- `hc` is always faked in tests (`FakePlatform` in `tests/helpers.py`). Never run `hc kill`, `hc send`
  or `hc keys` from code under test.
- Real `hdiutil` runs only in `tests/test_hdiutil_integration.py`, on throwaway images in a temp
  folder. `FINVAULT_SKIP_HDIUTIL=1` skips them where disk images can't be created (some sandboxes).
- The passphrase goes to `hdiutil` only on standard input (`-stdinpass`, NUL-terminated). Never put it
  in argv, an environment variable, a file, a log or an exception message.
  `tests/test_platform.py` enforces this for every hdiutil call.
- finvault never deletes user data. `migrate` keeps `<path>.plain`; the sweep moves files into the
  vault and deletes only the configured scratch folders.
- Fail closed. When a check can't be completed, refuse (exit non-zero, a `problems` entry, an
  actionable hint) rather than report a clean lock. A refusal must say what to do next.
- The repo is public. Commit no names, emails, account numbers, real folder paths or other personal
  data, including in tests, commit messages and PR text.

## Code

- Python 3.11+, standard library only at runtime. Dev tools are pinned in `requirements-dev.txt`.
- `src/finvault/`:
  - `cli.py`: argument parsing and prompts.
  - `vault.py`: the commands.
  - `sweep.py`: the history sweep; a pure layer on top and the file-system layer below.
  - `platform.py`: hdiutil, lsof, ps, hc and their parsers.
  - `manifest.py`: checksum lists.
  - `config.py`, `pathutil.py`, `errors.py`.
- Decode `hdiutil -plist` output with `plutil -convert json`. Don't import `plistlib` or `xml` at
  module level: some Homebrew Pythons can't load `pyexpat`.
- Every command prints JSON on stdout and diagnostics on stderr. Exit codes are in `errors.py`.
- State file formats are in `docs/state.md`. Changing one means updating that file.

## Changes

1. Branch from a fresh `main` (`<type>/<description>`).
2. Every fix gets a regression test that fails on the old code. Check it does before committing.
3. Run `scripts/check.sh` (ruff, ruff format, pyright, shellcheck, tests). It must pass with zero
   findings: the same gates CI runs.
4. For any change to behaviour or output, add a `## <version> — <date>` entry at the top of
   `CHANGELOG.md` (no `Unreleased` section), and bump the version in `pyproject.toml` and
   `src/finvault/__init__.py`.
5. Keep commits focused, subject in the imperative, body saying why.
6. Open a PR. Merge only when CI is green **and** the Codex review has completed for the PR's head
   commit with no unresolved findings. Its issue comment carries `codex-review-state:v1` with
   `commit_sha` and `status: completed`; a clean review shows as a 👍 reaction, not a GitHub review.
   Read and address every inline finding first.
7. After merging, watch CI on `main` until it passes.

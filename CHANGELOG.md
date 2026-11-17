# Changelog

## 0.1.14 — 2026-11-17

### Fixed

Findings from a third adversarial Codex review, of 0.1.11 plus the text output. Each has a regression test that fails on the old code. Its two text-output findings (control characters, mixed `--json`/`--text`) shipped in 0.1.13.

- A line for another project written during the history rename was lost when only the main disk was full: the fallback covered vault lines only. Such lines are now parked inside the vault in `history-overflow.jsonl` (a half-written line there is cut before the next append), and `restore-history` merges them back.
- A vault paste put back in the paste cache while the images closed went unnoticed. The after-close check now also looks for the vault's pastes, from the final plan and from vault lines still in the shared history, and for leftover temp files. A paste cache or history folder it can't read is reported under `unreadable`, never taken as empty. A paste another project's lines now use is shared, not a vault trace.
- `lock` on a vault that was already closed reported success without looking at anything. It now checks the shared files and reports traces it finds under `traces_outside` (exit 1, "unlock, then lock to sweep them"), unless `--skip-sweep`. `not_mode_000` now covers every closed folder whose path holds anything but a mode-000 directory (a plain file, a symlink, another mode), as well as every folder closed in that run that wasn't set to 000, even while `hdiutil info` still lists its image. `lock` no longer follows a symlink at a vault path when setting mode 000, which could have changed the mode of the folder it points to.
- With the whole Claude Code projects folder in the vault, no session was found, so every session's file-history stayed outside. Every project folder in it is now read for sessions; one that can't be read, or is a symlink, makes the sweep incomplete.
- A failure while preparing a mountpoint during `unlock` (a permission error, Ctrl-C) left mountpoints already prepared writable, and showed a traceback. Preparation now runs inside the same cleanup as the prompt and attach: a filesystem error is reported under `failed`, and every folder it touched that did not unlock goes back to mode 000.

## 0.1.13 — 2026-11-16

### Added

- Plain-text output for people. Every command prints readable text when stdout is a terminal (`src/finvault/render.py`): folders and their state, busy processes and terminals, what was swept into the vault, which scratch folders were deleted (they are not kept), what failed and what to do next, with paths under your home shown as `~`. When stdout is piped or captured, the JSON is unchanged, so scripts and agents keep working. `--json` and `--text` pick one explicitly, before or after the command.
- Text output escapes control and bidirectional characters from paths, process names, terminal labels and messages (shown as `\x1b`, `\u202e` and so on), so a hostile name can't clear the screen or forge a line. Warnings and errors on stderr are escaped the same way. `--json` and `--text` given on opposite sides of the command are refused. Both found by the third adversarial review.
- The agent skill now always runs `finvault status --json`, since an agent in a terminal would otherwise get text; `tests/test_skill.py` checks every `status` it names asks for JSON.

## 0.1.12 — 2026-11-16

### Fixed

- The real-image integration test failed now and then with "Resource busy" on detach, and longer retry budgets (0.1.8, 0.1.9) never helped. The diagnostics added in 0.1.9 showed why: a root `lsof` found nothing holding the volume when `lock` gave up, so the busy state was brief. The test built its dependencies with the fakes' no-op `sleep`, so all of `lock`'s retries ran in the same instant. The test now uses real sleeping, as `lock` does on a real Mac. No change to `finvault` itself.

## 0.1.11 — 2026-11-16

### Added

- Agent skill Step 7, "Finish work that used the vault". When an agent finishes work that read or wrote a vault folder, it leaves the vault and closes what it owns (as in Step 4), runs `finvault status`, and ends its reply by asking the human whether the vault should be locked now. It names any busy terminals, which would make `lock` refuse or be ended by `--force`. The human runs `finvault lock`; the agent never does, and never phrases the question as an offer. The router preamble names this as the one required chain. `tests/test_skill.py` now checks the chain points at that step, that the step asks rather than offers, and that every `finvault` command other than `status` appears only as the human's to run or in a never-rule.

## 0.1.10 — 2026-11-16

### Added

- An agent skill, `skills/finvault/SKILL.md`, for AI agents working on a Mac with a vault: recognise a vault folder, leave a locked one alone and ask the human to unlock it (never touching the passphrase or permissions), explain `finvault status` in plain words, leave the vault before the human locks, and relay a failed `lock` without retrying with `--force` or `--skip-sweep`. The agent runs only `finvault status` itself. `tests/test_skill.py` checks every command and flag it names against the real parser, and every JSON key it names against the code. The README says how to install it for Claude Code and Codex (issue #6).

## 0.1.9 — 2026-11-16

### Fixed

- A busy detach still failed on CI after 0.1.8's five 1 s retries (run 43905069899): something not visible to a non-root `lsof` held the freshly migrated volume for longer, most likely Spotlight indexing it (unconfirmed). `lock` now keeps trying for up to 30 s (15 tries, 2 s apart), warns once that it is waiting, and its final hint says the volume may still be being indexed. On a `lock` failure the integration test now also prints the loop cycle and the running Spotlight processes, to confirm or rule out the cause.

## 0.1.8 — 2026-11-16

### Fixed

- `lock` failed with "Resource busy" when macOS still held a volume for a moment after the last process using it had exited, even though `lsof` showed nothing open. A busy detach is now tried up to 5 times, 1 s apart, looking the device up again each time; it is never forced. This was also the cause of the mode-700 folder seen on CI after 0.1.6: that detach failed as busy, the volume unmounted moments later, and the folder was skipped. The integration test's new output (0.1.7) showed it (run 43901948034).

## 0.1.7 — 2026-11-16

### Fixed

- `lock` could leave a closed folder at mode 700 instead of 000. It set mode 000 only on folders `hdiutil info` no longer listed, and skipped the rest silently. Seen once on CI right after the 0.1.6 merge (`test_full_lifecycle`, run 43897752487) and not reproduced locally in 25 attach/detach cycles. `lock` now polls `hdiutil info` for up to 5 s until the closed images leave it, and a folder that still can't be set to 000 is listed under `not_mode_000` with exit 1. The integration test now checks `lock`'s exit code and prints its output on failure.

## 0.1.6 — 2026-11-16

### Fixed

Findings from a second adversarial Codex review, of 0.1.4. Each has a regression test.

- Lines appended during the history rename were lost when the archive was full. They now go back into `history.jsonl` for the next sweep, and the error is still reported. The history and archive writes are each tried even when the other fails, so a full main disk can't keep the vault's late lines from the archive.
- Ctrl-C while `restore-history` wrote its temp file left vault lines in `.history.jsonl.*.tmp` outside the vault. The temp is now removed on any interruption, and the sweep lists a leftover one under `problems` (a killed process can't clean up). The same goes for a paste restore's `paste-cache/.finvault-*.tmp`.
- A paste or backup with a second hard link kept its contents under the other name after the sweep. Such files are now refused, like a hard-linked `history.jsonl`.
- `lock` reported success when a backup for a vault session, or a matching scratch folder, was written while the images closed. The check after closing now covers history, backups and scratch, and reports them under `written_while_locking`; a folder it can no longer read counts as `unreadable`, not empty. `history_lines_written_while_locking` is kept.
- When an identical copy was already in the archive, the source was deleted even if that copy changed right after the comparison. The copy must now be unchanged after its folder is synced.
- A failed paste restore left a partial file that later runs skipped forever. Pastes are now restored through a temp file and a link, and a paste already there with different content is listed under `paste_files_differing`.

### Not fixed

- A process that can write inside the unlocked vault can swap an archive folder for a symlink mid-move. That needs every file operation rewritten around open folder handles; the README lists it as out of scope.

## 0.1.5 — 2026-11-16

### Fixed

- A process that kept `history.jsonl` open across a sweep went on appending to the replaced file, and those lines were lost. Before each rewrite (and before `restore-history`), `lsof` now checks the file; a holder still there after three looks 0.2 s apart makes the sweep refuse with exit 3 and the holders under `busy`. `status` lists them as `history_holders`. A sweep that doesn't rewrite history (only scratch folders, say) is not blocked. The README states the remaining window: a process that opens the file after the check and holds it across the rename.

## 0.1.4 — 2026-11-16

### Fixed

Findings from an adversarial Codex review of 0.1.3, each with a regression test.

- `lock` closed the vault after a sweep that could not read a projects, `file-history`, `paste-cache` or scratch folder, leaving those traces outside. The sweep now lists such sources under `problems`, and `lock` refuses to close anything until they are fixed (or `--skip-sweep` is used).
- A `file-history` session that was a symlink was followed, moving files from elsewhere into the vault. It is now refused, never followed.
- A `history.jsonl` with a second hard link kept its old lines under the other name after a sweep. It is now refused.
- A full disk part way through an archive append left half a line that the retry glued onto the next one. The partial line is now cut before every append and before `restore-history`.
- A failed folder sync right after the history rename lost lines appended during the rename. They are now read back and saved before the error is raised.
- Moving a paste or backup copied through a symlink placed at the destination, and deleted a source written to during the copy. Files are now copied into a new private file and linked in without replacing or following anything, and the source is deleted only if it is unchanged.
- `lock` could report success when a session wrote vault history while the images closed. It now checks again after closing and exits `1` with `history_lines_written_while_locking`.
- `init` and `archive` accepted a different passphrase when every image was unlocked. The passphrase is now checked against an unlocked image too: `hdiutil imageinfo` answers "Authentication error" for a wrong passphrase and "Resource temporarily unavailable" for the right one on an image in use.
- The checksum list skipped folders it could not read, so `verify` could pass on an incomplete copy. Unreadable folders now fail `migrate`, `archive` and `verify`.
- `lock --force` protected only the nearest terminal `finvault` runs under. Every terminal on its process ancestry is now `self`.

## 0.1.3 — 2026-11-16

### Changed

- Licensed under the Apache License 2.0 (`LICENSE`, `license = "Apache-2.0"` in `pyproject.toml`).

## 0.1.2 — 2026-11-16

### Fixed

- `lock` listed no terminal when a shell had `cd`'d into the vault after it started, so `--force` could not end it. `hc ls` reports the folder a terminal started in, not where it is now. Each busy pid is now walked up its parent chain (`ps -A -o pid=,ppid=`) to a terminal's program or foreground pid (`hc get <t> --json`). A terminal that only started in the vault and holds nothing there is no longer ended.
- `lock --force` never ends the terminal `finvault` itself runs in. It is reported with `"self": true`, and the hint says to `cd` out of the vault.
- `lock` and `status` ask `hc` nothing when no process is busy.

## 0.1.1 — 2026-11-15

### Fixed

Findings from the Codex review of PR #1, which arrived after it merged.

- Sweep no longer loses a history line another session appends while the archive is written. The stamp is checked before and after the archive write, and a line that lands on the old file during the rename is read back and sorted into the archive or the new history.
- A paste whose move failed (for example, a full disk) after its history line was archived is found again from the archive's lines. `lock` plans again after each pass and closes nothing until the plan is empty.
- Sweep refuses an archive folder, or an archive entry, that is a symlink, so swept history and pastes can't land outside the image.
- `unlock` sets every prepared mountpoint back to mode 000 when the passphrase prompt is cancelled or empty, not only on a wrong passphrase.
- `migrate` refuses a folder that already holds the sweep `archive_subdir`. `verify` leaves that folder out of the saved checksum list as well as the folder it checks, so a changed `archive_subdir` can't report false differences.

## 0.1.0 — 2026-11-15

### Added

- `finvault` CLI with `init`, `migrate`, `archive`, `unlock`, `lock`, `status`, `verify`, `sweep` and `restore-history`.
- Encrypted AES-256 APFS sparse bundles, one per folder, unlocked at the folder's own path. The passphrase goes only through `-stdinpass`.
- Several vaults per Mac. Each has its own `--config`, `state_dir` and passphrase, and a state marker that refuses a shared `state_dir`.
- Checksum lists stored inside each image, so file names never sit on disk in plain text while the vault is locked.
- Claude Code sweep of `history.jsonl`, `paste-cache`, `file-history` and scratchpad folders, with an atomic history rewrite that aborts if the file changes mid-run.
- `scripts/install.sh` builds a single-file zipapp. `scripts/check.sh` runs the CI gates locally.
- CI on a macOS runner: ruff, pyright (strict), shellcheck, unit tests and real-`hdiutil` integration tests.

### Decisions

- Images are identified by their image path from `hdiutil info -plist`, never by mountpoint or `/dev/diskN`, because device names are reused after detach.
- `hdiutil` output is decoded with `plutil -convert json`. `plistlib` is only a fallback, because it loads `pyexpat`, which fails on some Homebrew Pythons.
- The passphrase is always typed by hand. There is no Keychain option, and nothing schedules lock or unlock.

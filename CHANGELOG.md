# Changelog

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

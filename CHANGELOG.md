# Changelog

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

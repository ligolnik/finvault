# finvault

A macOS command-line tool that keeps folders inside encrypted APFS sparse bundles.
Each folder is unlocked at its original path, so programs using it see no change.
When the vault is locked, the data on disk can't be read.

- **Encryption:** macOS's built-in `hdiutil`, AES-256, APFS sparse bundles. No extra software, no kernel extensions.
- **One image per folder,** unlocked with `hdiutil attach -mountpoint <path> -nobrowse` at the folder's own path.
- **One passphrase per vault,** typed by hand at each unlock. It goes to `hdiutil` only through standard input (`-stdinpass`). It never appears on a command line, in an environment variable, in a file or in a log.
- **Locked mountpoints** are empty folders set to mode 000, so nothing can quietly write plain text into them.
- **Claude Code sweep:** at each lock, the vault's lines in Claude Code's shared history, paste cache and edit backups move into the vault, and the matching scratchpad folders are deleted.
- **Several vaults** can live on one Mac. Each has its own config, state folder and passphrase.

Lock and unlock are always run by hand. finvault installs no launchd jobs, timers or login items.

## Requirements

- macOS with `hdiutil`, `plutil`, `rsync` and `lsof` (all built in).
- Python 3.11 or newer. No third-party packages.
- Optional: `hc`, used to list and end terminals working in the vault.

## Install

```sh
git clone <this repo> ~/src/finvault        # anywhere outside the vault
mkdir -p ~/bin
~/src/finvault/scripts/install.sh ~/bin/finvault /opt/homebrew/bin/python3
finvault --version
```

`install.sh` builds a single-file zipapp and writes it to the path you give, with the
given interpreter's absolute path as its shebang. Run it again to update. Keep the
installed tool outside the vault so it works while the vault is locked.

## Configure

```sh
mkdir -p ~/.finvault && chmod 700 ~/.finvault
cp config.example.toml ~/.finvault/config.toml
$EDITOR ~/.finvault/config.toml
```

Every command reads `~/.finvault/config.toml` unless you pass `--config <path>`.
`config.example.toml` documents every key. The essentials:

- `state_dir` (required): this vault's images, archives and temporary mountpoints.
- `[[folders]]`: `name` (the image name) and `path` (unlocked at exactly that path). Optional `size` caps the image (`"8g"`). Otherwise the cap is `size_factor` × the folder's current size, never below `min_size_gb`.
- `[sweep]`: where swept Claude Code files go and which scratchpad folders to delete.

To vault a project's Claude Code transcripts and memory too, add its folder under
`~/.claude/projects/` (the project path with `/` turned into `-`) as another `[[folders]]` entry.

### More than one vault

Give each vault its own config and its own `state_dir`:

```sh
finvault --config ~/.finvault-work/config.toml unlock
```

finvault refuses a `state_dir` that another config already claimed (see `docs/state.md`).
Vaults never share images, checksum lists, archives or passphrases. Locking one leaves the others alone.

## Commands

Every command prints JSON on stdout and diagnostics on stderr.

| Command | What it does |
|---|---|
| `finvault init <name>` | Create the encrypted image for a folder in `<state_dir>/images/` (mode 700). Asks for the passphrase. On the first image it asks twice (12 characters minimum). Later images must use the same one. |
| `finvault migrate <name>` | Hash every file, copy the folder into its image with `rsync -aE` at a temporary path, check the copy, rename the original to `<path>.plain`, unlock the image at the real path and check again. **Never deletes anything.** |
| `finvault archive` | Build a read-only, encrypted, compressed image of every `.plain` folder in `<state_dir>/archive/`, then check it. |
| `finvault unlock` | Ask for the passphrase once, set each mountpoint from 000 back to 700, and unlock every image at its path. Refuses a mountpoint that holds files. |
| `finvault lock [--force] [--skip-sweep]` | Refuse while any process has files open in the vault (`lsof`), listing the `hc` terminals those processes run under. A terminal is matched by process ancestry (each busy pid's parents up to a terminal's pid or foreground pid from `hc get`), never by the folder it started in. `--force` ends those terminals with `hc kill`, except the one `finvault` itself runs in (`"self": true`). Then sweep, close every image, and set each mountpoint to mode 000. |
| `finvault status` | Which images are unlocked, busy processes, the `hc` terminals they run under, and how much is waiting to be swept. |
| `finvault verify` | Check each unlocked image against the checksum list saved inside it at migration. |
| `finvault sweep [--dry-run]` | Run the sweep by itself (needs the sweep archive folder unlocked unless `--dry-run`). |
| `finvault restore-history` | Merge the swept history lines back into `history.jsonl` in timestamp order, and copy back missing paste files. |

Exit codes: `0` success, `1` failure, `2` usage error, `3` vault busy, `4` checksum mismatch,
`5` a shared file changed during the run (nothing was changed; run it again).

`verify` compares against the list saved at migration, so files you edited since then
show as changed. That's expected after normal use.

## Moving a folder into the vault

1. Quit every program using the folder.
2. `finvault init <name>`
3. `finvault migrate <name>`. The original stays at `<path>.plain`.
4. Repeat for each folder, then run `finvault archive` for a one-time encrypted copy of all `.plain` folders.
5. Once you've checked everything works, delete the `.plain` folders by hand. finvault never does.

## Daily routine

```sh
finvault unlock      # one passphrase prompt
# ... work ...
finvault status      # anything still busy?
finvault lock        # refuses while busy; --force ends hc terminals in the vault
```

## The sweep

Claude Code writes some files shared by every session, so they can't live in an image.
At each lock, finvault moves the vault's part of them into `<archive folder>/<archive_subdir>/`
inside the vault:

- `history.jsonl`: lines whose `project` is a vault folder or sits under one. The remaining lines are written back atomically (temp file, fsync, rename). If the file's size or modification time changes during the run, the sweep stops with no changes, and `lock` retries. A line another session appends in the instant of the rename is read back from the old file and sorted into the archive or the new history.
- `paste-cache/<hash>.txt`: files referenced only by the moved lines, or by lines already in the archive (so a paste left behind by an interrupted sweep is found again).
- `file-history/<sessionId>/`: backups for sessions with a `<sessionId>.jsonl` in a vaulted `~/.claude/projects` folder.
- Scratchpad folders under `scratch_root` that match `scratch_globs` are deleted.

`lock` plans the sweep again after each pass and closes the images only once nothing is left to move.
The archive folder must be a real folder inside the unlocked image: the sweep refuses a symlink anywhere on the way.
`migrate` refuses a folder that already holds an entry named `archive_subdir`, which is reserved for the sweep.

## Rollback

- **Before you delete `.plain`:** run `finvault lock`, remove the empty mountpoint folder (`rmdir <path>`), and rename `<path>.plain` back to `<path>`.
- **After deleting `.plain`:** attach the archive (`hdiutil attach -readonly -stdinpass <state_dir>/archive/plain-….dmg`), copy the folder back, and check it.
  - With one `.plain` folder, the archive holds its contents at the root.
  - With several, each folder sits at the top level under its `.plain` name.
- **Swept history:** `finvault restore-history`.

**If the passphrase is lost, the data is gone.** Keep it in a password manager.

## What it protects against

- **Protected:** while locked, the images hold no plain text, and every mountpoint is an empty mode-000 folder. Other programs and agents running as you can't read the vault.
- **Not protected:** while unlocked, anything running as you can read the vault. Copies already elsewhere (cloud services, git remotes, email) are out of scope.

## Development

```sh
python3 -m pip install -r requirements-dev.txt
scripts/check.sh     # ruff, ruff format, pyright, shellcheck, tests: the same gates as CI
```

- The tests build all their data in temp folders and never touch `~/.claude` or `~/.finvault`.
- `hc` is always faked in tests.
- `tests/test_hdiutil_integration.py` creates, attaches and detaches real throwaway encrypted images in a temp folder. It runs on macOS, including CI's macOS runner. Set `FINVAULT_SKIP_HDIUTIL=1` to skip it.
- State file formats are in `docs/state.md`.
- For secret scanning before commits: `pip install pre-commit && pre-commit install`.

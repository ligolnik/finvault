# finvault state files

finvault writes two kinds of state. Both are owned by `src/finvault/vault.py`
(the state marker) and `src/finvault/manifest.py` (checksum lists); no other
module writes them.

## State folder layout

Each vault's config sets `state_dir`. Everything the vault owns lives there:

| Path | Mode | What |
|---|---|---|
| `<state_dir>/vault.json` | 600 | State marker (below) |
| `<state_dir>/images/<name>.sparsebundle` | 700 | One encrypted image per folder |
| `<state_dir>/archive/plain-<YYYYMMDD-HHMMSS>.dmg` | 600 | Read-only encrypted archive of the `.plain` folders |
| `<state_dir>/mnt/` | 700 | Temporary mountpoints during `migrate` and `archive` |

Two vaults on one Mac use two configs with two different `state_dir`s. They
share no images, checksum lists, archives or passphrases.

## State marker: `<state_dir>/vault.json`

Claims the state folder for one config file, so a second config pointing at the
same folder is refused.

```json
{"schema_version": 1, "config_path": "/Users/example/.finvault/config.toml"}
```

- Writer: `init`, `migrate`, `archive` (created once, never rewritten).
- Readers: every command. A different `config_path` stops the command. A
  missing marker means "not claimed yet". An unknown `schema_version` stops the
  command with "update finvault".
- If you move the config file, edit `config_path` by hand.

## Checksum list: `<volume>/.finvault/manifest.json`

Written inside each image by `migrate`, so file names are encrypted with the
data. It is never stored outside the image.

```json
{
  "schema_version": 1,
  "files": {"sub/report.txt": {"sha256": "<hex>", "size": 123, "mode": 384}},
  "symlinks": {"link": "sub/report.txt"},
  "dirs": ["sub"],
  "special": []
}
```

- Paths are relative to the folder root. Volume entries (`.fseventsd`,
  `.Spotlight-V100`, `.Trashes`, `.TemporaryItems`, `.DocumentRevisions-V100`,
  `.finvault`) and the sweep archive subfolder are not listed.
- Writer: `migrate`. Readers: `verify`, `unlock` (checks it exists).
- An unknown `schema_version` stops `verify` with "update finvault".

## Sweep archive: `<archive folder>/<archive_subdir>/`

Inside the configured `sweep.archive_folder`, so it is encrypted with that image.

- `history.jsonl`: swept lines, byte-for-byte, appended without duplicates.
- `paste-cache/<hash>.txt`: paste files only vaulted lines referenced.
- `file-history/<sessionId>/`: edit backups for the vault's sessions.

A file that already exists with different content is kept, and the new one is
written beside it as `<name>.1`, `<name>.2` and so on.

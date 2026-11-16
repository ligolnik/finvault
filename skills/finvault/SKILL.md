---
name: finvault
description: Use on a Mac with a finvault vault when the user mentions a vault, locked folder, finvault, mode 000, folder is empty, or permission denied in a vault folder, and whenever you finish work that used a vault folder. Protect locked mountpoints, interpret status for the human, and ask whether to lock when vault work is done.
---

# Working beside a finvault vault

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

One chain is required: whenever you finish work that used a vault folder, run Step 7 before your final reply, even if no step was picked for that work.

## Step 1 — Recognise a vault folder

Run `finvault status` and match the folder against `folders[].path`.
A configured vault folder can be an empty mode-000 directory when locked,
or a mountpoint when unlocked. An empty folder or permission denial alone
is not proof of a vault. Do not probe protected contents or repair permissions.
If status fails, relay its diagnostics and ask the human to resolve them.
The only finvault command this skill executes is `finvault status`;
every other command below is for the human to run manually.

## Step 2 — Respond to a locked folder

Ask the human to run `finvault unlock` in their own terminal, then wait for them.
Never unlock it yourself, ask for, search for, read, store, or handle the passphrase.
Never chmod a vault mountpoint, recreate it, or write replacement files into it.

## Step 3 — Explain status

Run `finvault status` and explain its JSON in plain words:
- `folders[].unlocked`: whether the image is mounted at its configured path;
  false does not guarantee the image is closed everywhere. Relay warnings.
- `folders[].busy_processes`: count of processes with open files in that image;
  an absent count is not a measured zero.
- `terminals`: matched hc terminals for busy processes; `terminals[].self` true
  marks a terminal on finvault's own process ancestry, protected from force.
  Null means terminal information is unavailable; an empty list means none matched.
- `sweep_pending`: counts of traces waiting outside the vault to be swept;
  null means sweeping is disabled. Relay any error or problems rather than
  treating an incomplete check as clean.
- `history_holders`: processes keeping shared history open; they can block its
  safe rewrite. Null means sweeping is disabled, not a measured empty list.
- `warnings`: conditions needing human attention; relay them without fixing them.

## Step 4 — Prepare for the human to lock

Leave the vault: move your shell's working directory outside every vault folder
and close files, handles, and tools you own that use it. Ask the human to close
other programs using it, then run `finvault lock` themselves.
Explain that human-run `finvault lock --force` ends matched busy hc terminals
and their work; it does not end terminals marked `self: true`, arbitrary busy
processes, or force a detach. It may end this agent's terminal and work.
Do not run lock or end other processes yourself.

## Step 5 — Explain a failed lock

For a human-provided nonzero lock result, relay the exit code, diagnostics,
and any `problems`, `busy`, `terminals`, `failed`, `written_while_locking`,
`history_lines_written_while_locking`, and `not_mode_000` fields present.
Relay the supplied hint (or stderr guidance); do not claim a clean lock.
Traces written while locking remain outside: the human should close the writer,
then run `finvault unlock` and `finvault lock` themselves.
For closed folders listed in `not_mode_000`, tell the human the guidance is to
run `finvault lock` again; never chmod them yourself.
Never retry lock yourself or escalate to `finvault lock --force` or
`finvault lock --skip-sweep` on your own. Skipping the sweep leaves traces outside.
Exit codes: 0 success; 1 failure; 2 usage error; 3 vault busy;
4 checksum mismatch; 5 a shared file changed during the run (nothing changed).
Any suggested retry is for the human, including code 5.

## Step 6 — Leave vault-managed copies alone

Leave `<path>.plain` original copies, the configured state folder (`<state_dir>`),
and the sweep archive inside the vault alone. Do not read, edit, move, delete,
clean up, or use them to bypass a locked folder. Ask the human to manage them.

## Step 7 — Finish work that used the vault

Run this before your final reply whenever the work you just did read or wrote a vault folder.
1. Leave every vault folder and close what you own there, as in Step 4: move your shell's working
   directory outside every vault folder and close files, handles and tools you started that use it.
2. Run `finvault status`.
3. End your reply by asking the human whether the vault should be locked now, for example:
   "I'm done with the vault folders. Should the vault be locked now? If so, run `finvault lock`."
   Name every terminal in `terminals` and any `busy_processes` count above zero: they would make
   `finvault lock` refuse, and a human-run `finvault lock --force` would end those terminals.
   If `history_holders` or `warnings` are not empty, relay them too.
4. If the human says yes, the human runs `finvault lock`. You still never run lock yourself, with or
   without `--force` or `--skip-sweep`. Ask the question; never phrase it as an offer to lock for them.

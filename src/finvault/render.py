"""Plain-text versions of the JSON each command prints, for a person at a terminal.

Every function here is pure: it takes the same payload the JSON output carries
and returns lines of text. Paths under ``home`` are shown with ``~``.
"""

from __future__ import annotations

from collections.abc import Callable

from finvault.jsonutil import as_dict, as_list

Lines = list[str]
COUNT_NAMES = (
    ("history_lines", "history line"),
    ("paste_files", "paste file"),
    ("file_history_folders", "file-history folder"),
    ("scratch_folders", "scratch folder"),
)


def render(payload: dict[str, object], home: str) -> str:
    """The text for one command's payload (the JSON object the command prints)."""
    command = str(payload.get("command", ""))
    view = _View(home)
    if payload.get("ok") is False and "error" in payload:
        lines = _error(payload, view)
    else:
        renderer = RENDERERS.get(command)
        lines = renderer(payload, view) if renderer else [f"{k}: {v}" for k, v in sorted(payload.items())]
    return "\n".join(lines) + "\n" if lines else ""


class _View:
    def __init__(self, home: str) -> None:
        self.home = home.rstrip("/")

    def path(self, value: object) -> str:
        text = str(value)
        if self.home and (text == self.home or text.startswith(self.home + "/")):
            return "~" + text[len(self.home) :]
        return text


def _strs(value: object) -> list[str]:
    return [str(v) for v in as_list(value) or []]


def _dicts(value: object) -> list[dict[str, object]]:
    return [d for d in (as_dict(v) for v in as_list(value) or []) if d is not None]


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word}es" if word.endswith("s") else f"{n} {word}s"


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _counts(counts: dict[str, object]) -> str:
    """'2 history lines, 1 paste file', or '' when every count is zero."""
    return ", ".join(_plural(_int(counts.get(key)), word) for key, word in COUNT_NAMES if _int(counts.get(key)))


def _reasons(title: str, items: list[dict[str, object]]) -> Lines:
    if not items:
        return []
    return [title] + [f"  {item.get('name')}: {item.get('reason')}" for item in items]


def _problems(value: object) -> Lines:
    problems = _strs(value)
    if not problems:
        return []
    return ["The sweep can't check everything it must move:"] + [f"  - {p}" for p in problems]


def _busy(value: object, view: _View) -> Lines:
    procs = _dicts(value)
    if not procs:
        return []
    out = ["Processes with files open:"]
    for p in procs:
        files = ", ".join(view.path(f) for f in _strs(p.get("files")))
        out.append(f"  pid {p.get('pid')} {p.get('command')}" + (f": {files}" if files else ""))
    return out


def _terminals(value: object, view: _View) -> Lines:
    if value is None:
        return []
    terms = _dicts(value)
    if not terms:
        return []
    out = ["Terminals working in the vault:"]
    for t in terms:
        pids = ", ".join(str(p) for p in as_list(t.get("busy_pids")) or [])
        where = f" ({t.get('where')})" if t.get("where") else ""
        note = "  <- this terminal; lock --force never ends it" if t.get("self") is True else ""
        out.append(f"  {t.get('address')}{where}, busy pids {pids}{note}")
    return out


def _error(payload: dict[str, object], view: _View) -> Lines:
    # The message and hint already went to stderr; show only the details here.
    out = _busy(payload.get("busy"), view) + _terminals(payload.get("terminals"), view)
    killed = _strs(payload.get("killed"))
    if killed:
        out.append(f"Ended terminals: {', '.join(killed)}")
    out += _problems(payload.get("problems"))
    diff = as_dict(payload.get("diff"))
    if diff is not None:
        out += _diff(diff, view)
    return out


def _diff(diff: dict[str, object], view: _View) -> Lines:
    out = [f"{_int(diff.get('missing'))} missing, {_int(diff.get('changed'))} changed, {_int(diff.get('added'))} added"]
    out += [f"  {view.path(e)}" for e in _strs(diff.get("examples"))]
    return out


# ------------------------------------------------------------------ commands


def _status(payload: dict[str, object], view: _View) -> Lines:
    folders = _dicts(payload.get("folders"))
    out = [f"Vault: {_plural(len(folders), 'folder')}"]
    width = max((len(str(f.get("name"))) for f in folders), default=0)
    for f in folders:
        name = str(f.get("name")).ljust(width)
        if f.get("image") is not True:
            state = "no image yet (run finvault init)"
        elif f.get("unlocked") is True:
            busy = _int(f.get("busy_processes"))
            state = "UNLOCKED" + (f", {_plural(busy, 'process')} busy" if busy else "")
        else:
            state = "locked" + (" (mode 000)" if f.get("mountpoint_mode") == "0o0" else "")
        out.append(f"  {name}  {state}  {view.path(f.get('path'))}")
        if f.get("plain_copy") is True:
            out.append(f"  {' ' * width}  plain copy still at {view.path(f.get('path'))}.plain")
    out += _terminals(payload.get("terminals"), view)
    holders = _dicts(payload.get("history_holders"))
    if holders:
        out.append("Holding the shared history open (a sweep would refuse):")
        out += [f"  pid {h.get('pid')} {h.get('command')}" for h in holders]
    pending = as_dict(payload.get("sweep_pending"))
    if pending is not None:
        if "error" in pending:
            out.append(f"Sweep: {pending['error']}")
        else:
            waiting = _counts(pending)
            out.append(f"Waiting to be swept: {waiting}" if waiting else "Sweep: nothing waiting")
            out += _problems(pending.get("problems"))
    out += [f"Warning: {w}" for w in _strs(payload.get("warnings"))]
    return out


def _lock(payload: dict[str, object], view: _View) -> Lines:
    out: Lines = []
    locked = _strs(payload.get("locked"))
    already = _strs(payload.get("already_locked"))
    if locked:
        out.append(f"Locked: {', '.join(locked)}")
    if already:
        out.append(f"Already locked: {', '.join(already)}")
    if not locked and not already:
        out.append("Nothing to lock")
    sweep = as_dict(payload.get("sweep"))
    if sweep is not None:
        moved = _counts(sweep)
        out.append(f"Swept into the vault: {moved}" if moved else "Sweep: nothing to move")
    killed = _strs(payload.get("killed"))
    if killed:
        out.append(f"Ended terminals: {', '.join(killed)}")
    out += _reasons("Could not close:", _dicts(payload.get("failed")))
    late = as_dict(payload.get("written_while_locking"))
    if late:
        out.append("Written while locking, still outside the vault (unlock and lock again):")
        out += [f"  {k}: {v}" for k, v in sorted(late.items())]
    not_000 = _strs(payload.get("not_mode_000"))
    if not_000:
        out.append(f"Closed but not set to mode 000: {', '.join(not_000)} (run finvault lock again)")
    return out


def _unlock(payload: dict[str, object], view: _View) -> Lines:
    out: Lines = []
    unlocked = _strs(payload.get("unlocked"))
    already = _strs(payload.get("already_unlocked"))
    if unlocked:
        out.append(f"Unlocked: {', '.join(unlocked)}")
    if already:
        out.append(f"Already unlocked: {', '.join(already)}")
    out += _reasons("Skipped:", _dicts(payload.get("skipped")))
    out += _reasons("Failed:", _dicts(payload.get("failed")))
    return out or ["Nothing to unlock"]


def _init(payload: dict[str, object], view: _View) -> Lines:
    if payload.get("created") is True:
        return [f"Created the encrypted image for {payload.get('name')} ({payload.get('size_mb')} MB)",
                f"  {view.path(payload.get('image'))}"]  # fmt: skip
    return [f"{payload.get('name')}: {payload.get('reason')}", f"  {view.path(payload.get('image'))}"]


def _migrate(payload: dict[str, object], view: _View) -> Lines:
    return [
        f"Moved {payload.get('name')} into its image: {_plural(_int(payload.get('files')), 'file')}",
        f"  unlocked at {view.path(payload.get('path'))}",
        f"  original kept at {view.path(payload.get('plain_copy'))}",
        f"Next: {view.path(payload.get('next'))}",
    ]


def _archive(payload: dict[str, object], view: _View) -> Lines:
    return [
        f"Archived {_plural(_int(payload.get('files')), 'file')} from {', '.join(_strs(payload.get('folders')))}",
        f"  {view.path(payload.get('archive'))}",
        f"Next: {payload.get('next')}",
    ]


def _verify(payload: dict[str, object], view: _View) -> Lines:
    out: Lines = []
    for image in _dicts(payload.get("images")):
        result = str(image.get("result"))
        if result == "differs":
            out.append(f"{image.get('name')}: differs from its checksum list:")
            out += ["  " + line for line in _diff(image, view)]
        else:
            out.append(f"{image.get('name')}: {result}")
    return out


def _sweep(payload: dict[str, object], view: _View) -> Lines:
    moved = _counts(payload)
    if payload.get("dry_run") is True:
        out = [f"Would move into the vault: {moved}" if moved else "Nothing to sweep"]
    else:
        out = [f"Moved into the vault: {moved}" if moved else "Nothing to sweep"]
    return out + _problems(payload.get("problems"))


def _restore(payload: dict[str, object], view: _View) -> Lines:
    out = [
        f"Restored {_plural(_int(payload.get('history_lines_restored')), 'history line')} and "
        f"{_plural(_int(payload.get('paste_files_restored')), 'paste file')}"
    ]
    differing = _strs(payload.get("paste_files_differing"))
    if differing:
        out.append("Left alone, content differs from the vault's copy:")
        out += [f"  {view.path(p)}" for p in differing]
    return out


RENDERERS: dict[str, Callable[[dict[str, object], _View], Lines]] = {
    "status": _status,
    "lock": _lock,
    "unlock": _unlock,
    "init": _init,
    "migrate": _migrate,
    "archive": _archive,
    "verify": _verify,
    "sweep": _sweep,
    "restore-history": _restore,
}

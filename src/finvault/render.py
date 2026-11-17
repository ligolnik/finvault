"""Plain-text versions of the JSON each command prints, for a person at a terminal.

Every function here is pure: it takes the same payload the JSON output carries
and returns lines of text. Paths under ``home`` are shown with ``~``.
"""

from __future__ import annotations

from collections.abc import Callable

from finvault.jsonutil import as_dict, as_list

Lines = list[str]
# What the sweep moves into the vault. Scratch folders are deleted, never moved,
# so they are reported on a line of their own.
COUNT_NAMES = (
    ("history_lines", "history line"),
    ("paste_files", "paste file"),
    ("file_history_folders", "file-history folder"),
)


# Bidirectional formatting characters can make text display in a different order.
BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean(text: str) -> str:
    """``text`` with control and bidi characters shown as escapes, safe to print on a terminal.

    Paths, process names and terminal labels come from outside finvault; a
    raw ESC, CR or newline in one could clear the screen or forge a line.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif ch in BIDI:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def render(payload: dict[str, object], home: str) -> str:
    """The text for one command's payload (the JSON object the command prints).

    Every line is passed through ``clean``; the renderers never put a newline
    inside a line, so the line breaks are all finvault's own.
    """
    command = str(payload.get("command", ""))
    view = _View(home)
    if payload.get("ok") is False and "error" in payload:
        lines = _error(payload, view)
    else:
        renderer = RENDERERS.get(command)
        lines = renderer(payload, view) if renderer else [f"{k}: {v}" for k, v in sorted(payload.items())]
    return "\n".join(clean(line) for line in lines) + "\n" if lines else ""


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


def _sweep_lines(counts: dict[str, object], moved: str, deleted: str, nothing: str) -> Lines:
    """What a sweep moves and what it deletes, each on its own line."""
    out: Lines = []
    archived = _counts(counts)
    if archived:
        out.append(f"{moved}: {archived}")
    scratch = _int(counts.get("scratch_folders"))
    if scratch:
        out.append(f"{deleted}: {_plural(scratch, 'scratch folder')} (not kept)")
    return out or [nothing]


TRACE_NAMES = {
    "history_lines": "history lines",
    "paste_files": "paste files",
    "file_history_folders": "file-history folders",
    "scratch_folders": "scratch folders",
    "leftover_temps": "leftover temp files",
    "unreadable": "couldn't check",
}


def _traces(traces: dict[str, object], view: _View) -> Lines:
    """One line per kind of trace: a count, or the names it lists."""
    out: Lines = []
    for key, value in sorted(traces.items()):
        label = TRACE_NAMES.get(key, key)
        items = as_list(value)
        if items is None:
            out.append(f"  {label}: {value}")
        else:
            out.append(f"  {label}: {', '.join(view.path(i) for i in items)}")
    return out


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
            out += _sweep_lines(pending, "Waiting to be swept", "Waiting to be deleted", "Sweep: nothing waiting")
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
        out += _sweep_lines(sweep, "Swept into the vault", "Deleted", "Sweep: nothing to move")
    killed = _strs(payload.get("killed"))
    if killed:
        out.append(f"Ended terminals: {', '.join(killed)}")
    out += _reasons("Could not close:", _dicts(payload.get("failed")))
    late = as_dict(payload.get("written_while_locking"))
    if late:
        out.append("Written while locking, still outside the vault (unlock and lock again):")
        out += _traces(late, view)
    outside = as_dict(payload.get("traces_outside"))
    if outside:
        out.append("Vault traces outside the vault (unlock, then lock to sweep them):")
        out += _traces(outside, view)
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
    if payload.get("dry_run") is True:
        out = _sweep_lines(payload, "Would move into the vault", "Would delete", "Nothing to sweep")
    else:
        out = _sweep_lines(payload, "Moved into the vault", "Deleted", "Nothing to sweep")
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

"""Checksum lists: the sha256 of every file under a folder.

A manifest is stored inside its own image at ``.finvault/manifest.json``, so file
names never sit on disk in plain text while the vault is locked. Its shape is
documented in docs/state.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field

from finvault.errors import FinvaultError
from finvault.jsonutil import as_dict, as_list

SCHEMA_VERSION = 1
MANIFEST_DIR = ".finvault"
MANIFEST_FILE = "manifest.json"
# Names macOS creates at a volume root; never part of the user's data.
VOLUME_ENTRIES = frozenset(
    {MANIFEST_DIR, ".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems", ".DocumentRevisions-V100"}
)
CHUNK = 1 << 20


@dataclass
class Manifest:
    files: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    symlinks: dict[str, str] = field(default_factory=dict[str, str])
    dirs: list[str] = field(default_factory=list[str])
    special: list[str] = field(default_factory=list[str])

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "files": self.files,
            "symlinks": self.symlinks,
            "dirs": self.dirs,
            "special": self.special,
        }

    @classmethod
    def from_json(cls, data: object, where: str) -> Manifest:
        table = as_dict(data)
        if table is None or table.get("schema_version") != SCHEMA_VERSION:
            raise FinvaultError(
                f"checksum list at {where} has an unknown format",
                f"this finvault reads schema_version {SCHEMA_VERSION}; update finvault",
            )
        files_raw = as_dict(table.get("files"))
        links_raw = as_dict(table.get("symlinks"))
        dirs_raw = as_list(table.get("dirs"))
        special_raw = as_list(table.get("special", [])) or []
        if files_raw is None or links_raw is None or dirs_raw is None:
            raise FinvaultError(
                f"checksum list at {where} is damaged", "run finvault migrate again from the .plain copy"
            )
        files: dict[str, dict[str, object]] = {}
        for rel, entry in files_raw.items():
            info = as_dict(entry)
            if info is None:
                raise FinvaultError(
                    f"checksum list at {where} is damaged", "run finvault migrate again from the .plain copy"
                )
            files[rel] = info
        return cls(
            files=files,
            symlinks={rel: str(target) for rel, target in links_raw.items()},
            dirs=[str(d) for d in dirs_raw],
            special=[str(s) for s in special_raw],
        )

    def file_count(self) -> int:
        return len(self.files) + len(self.symlinks)

    def without(self, top_names: frozenset[str]) -> Manifest:
        """A copy without entries under the given top-level names."""
        if not top_names:
            return self

        def keep(rel: str) -> bool:
            return rel.split(os.sep, 1)[0] not in top_names

        return Manifest(
            files={r: v for r, v in self.files.items() if keep(r)},
            symlinks={r: v for r, v in self.symlinks.items() if keep(r)},
            dirs=[d for d in self.dirs if keep(d)],
            special=[x for x in self.special if keep(x)],
        )


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _walk_error(err: OSError) -> None:
    """Fail the walk: a folder that can't be listed must never look empty."""
    raise FinvaultError(
        f"can't read {err.filename}: {err.strerror}",
        "make every folder in it readable (chmod u+rx) and run the command again",
    ) from err


def build_manifest(root: str, *, volume_root: bool = False, exclude: frozenset[str] = frozenset()) -> Manifest:
    """Hash every file under ``root``.

    ``volume_root`` skips macOS volume entries at the top level; ``exclude`` skips
    further top-level names (the sweep archive folder).
    """
    m = Manifest()
    skip: frozenset[str] = (VOLUME_ENTRIES if volume_root else frozenset[str]()) | exclude
    for dirpath, dirnames, all_files in os.walk(root, topdown=True, onerror=_walk_error, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        filenames = all_files
        if rel_dir == "." and skip:
            dirnames[:] = [d for d in dirnames if d not in skip]
            filenames = [f for f in all_files if f not in skip]
        dirnames.sort()
        for name in list(dirnames):
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                m.symlinks[rel] = os.readlink(full)
                dirnames.remove(name)
            else:
                m.dirs.append(rel)
        for name in sorted(filenames):
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                m.symlinks[rel] = os.readlink(full)
            elif stat.S_ISREG(st.st_mode):
                m.files[rel] = {"sha256": sha256_file(full), "size": st.st_size, "mode": stat.S_IMODE(st.st_mode)}
            else:
                m.special.append(rel)
    m.dirs.sort()
    m.special.sort()
    return m


@dataclass
class ManifestDiff:
    missing: list[str] = field(default_factory=list[str])
    changed: list[str] = field(default_factory=list[str])
    added: list[str] = field(default_factory=list[str])

    def ok(self) -> bool:
        return not (self.missing or self.changed or self.added)

    def summary(self, limit: int = 20) -> dict[str, object]:
        return {
            "missing": len(self.missing),
            "changed": len(self.changed),
            "added": len(self.added),
            "examples": (self.missing + self.changed + self.added)[:limit],
        }


def compare(expected: Manifest, actual: Manifest, *, check_modes: bool = True) -> ManifestDiff:
    """Paths missing from, changed in, or added to ``actual`` relative to ``expected``."""
    diff = ManifestDiff()
    for kind_exp, kind_act in (
        (expected.files, actual.files),
        (expected.symlinks, actual.symlinks),
    ):
        for rel, want in kind_exp.items():
            got = kind_act.get(rel)
            if got is None:
                diff.missing.append(rel)
            elif isinstance(want, dict) and isinstance(got, dict):
                if want.get("sha256") != got.get("sha256") or (check_modes and want.get("mode") != got.get("mode")):
                    diff.changed.append(rel)
            elif want != got:
                diff.changed.append(rel)
        diff.added.extend(rel for rel in kind_act if rel not in kind_exp)
    exp_dirs = set(expected.dirs) | set(expected.special)
    act_dirs = set(actual.dirs) | set(actual.special)
    diff.missing.extend(sorted(exp_dirs - act_dirs))
    diff.added.extend(sorted(act_dirs - exp_dirs))
    diff.missing.sort()
    diff.changed.sort()
    diff.added.sort()
    return diff


def manifest_path(volume: str) -> str:
    return os.path.join(volume, MANIFEST_DIR, MANIFEST_FILE)


def save_manifest(volume: str, manifest: Manifest) -> None:
    folder = os.path.join(volume, MANIFEST_DIR)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = manifest_path(volume)
    data = json.dumps(manifest.to_json(), indent=1, sort_keys=True).encode()
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(path + ".tmp", path)


def load_manifest(volume: str) -> Manifest:
    path = manifest_path(volume)
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise FinvaultError(
            f"no checksum list at {path}",
            "this image was not filled by finvault migrate; nothing to verify against",
        ) from None
    except json.JSONDecodeError:
        raise FinvaultError(
            f"checksum list at {path} is not valid JSON", "run finvault migrate again from the .plain copy"
        ) from None
    return Manifest.from_json(data, path)

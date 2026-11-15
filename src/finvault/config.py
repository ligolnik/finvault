"""Load and validate a vault's config file.

``parse_config`` is pure: it takes the decoded TOML table plus the home folder and
uid used for expansion, and returns a ``Config`` or raises ``FinvaultError``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from finvault.errors import FinvaultError
from finvault.jsonutil import as_dict, as_list
from finvault.pathutil import is_under

DEFAULT_CONFIG = "~/.finvault/config.toml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SIZE_RE = re.compile(r"^[1-9][0-9]*[mgt]$")
MIN_GLOB_LITERALS = 3

_TOP_KEYS = {"state_dir", "image", "folders", "sweep"}
_IMAGE_KEYS = {"min_size_gb", "size_factor"}
_FOLDER_KEYS = {"name", "path", "size"}
_SWEEP_KEYS = {
    "enabled",
    "archive_folder",
    "archive_subdir",
    "claude_dir",
    "history",
    "file_history",
    "paste_cache",
    "projects_dir",
    "scratch_root",
    "scratch_globs",
}


@dataclass(frozen=True)
class Folder:
    name: str
    path: str
    size: str | None = None


@dataclass(frozen=True)
class ImageSettings:
    min_size_gb: int = 1
    size_factor: int = 4


@dataclass(frozen=True)
class SweepSettings:
    archive_folder: str
    archive_subdir: str
    history: str
    file_history: str
    paste_cache: str
    projects_dir: str
    scratch_root: str
    scratch_globs: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    config_path: str
    state_dir: str
    folders: tuple[Folder, ...]
    image: ImageSettings
    sweep: SweepSettings | None

    @property
    def images_dir(self) -> str:
        return os.path.join(self.state_dir, "images")

    @property
    def archive_dir(self) -> str:
        return os.path.join(self.state_dir, "archive")

    @property
    def mnt_dir(self) -> str:
        return os.path.join(self.state_dir, "mnt")

    def image_path(self, folder: Folder) -> str:
        return os.path.join(self.images_dir, f"{folder.name}.sparsebundle")

    def folder(self, name: str) -> Folder:
        for f in self.folders:
            if f.name == name:
                return f
        known = ", ".join(f.name for f in self.folders)
        raise FinvaultError(
            f"no folder named {name!r} in {self.config_path}",
            f"use one of: {known}",
        )

    def vault_paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.folders)

    def sweep_archive_dir(self) -> str | None:
        if self.sweep is None:
            return None
        base = self.folder(self.sweep.archive_folder).path
        return os.path.join(base, self.sweep.archive_subdir)


def _expand(value: object, key: str, home: str) -> str:
    if not isinstance(value, str) or not value:
        raise FinvaultError(f"{key} must be a non-empty string", "fix it in the config file")
    if value == "~" or value.startswith("~/"):
        value = home + value[1:]
    if not os.path.isabs(value):
        raise FinvaultError(
            f"{key} = {value!r} is not an absolute path",
            "use an absolute path or one starting with ~/",
        )
    return os.path.normpath(value)


def _check_keys(table: dict[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise FinvaultError(
            f"unknown key(s) in {where}: {', '.join(unknown)}",
            f"allowed keys are: {', '.join(sorted(allowed))}",
        )


def _positive_int(value: object, key: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FinvaultError(f"{key} must be a positive integer", "fix it in the config file")
    return value


def _check_glob(pattern: object) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise FinvaultError("sweep.scratch_globs entries must be non-empty strings")
    if "/" in pattern or ".." in pattern:
        raise FinvaultError(
            f"scratch glob {pattern!r} may not contain '/' or '..'",
            "give a pattern for names directly under scratch_root",
        )
    literals = re.sub(r"\[[^\]]*\]|[*?]", "", pattern)
    if len(literals) < MIN_GLOB_LITERALS:
        raise FinvaultError(
            f"scratch glob {pattern!r} is too broad",
            f"include at least {MIN_GLOB_LITERALS} literal characters",
        )
    return pattern


def _parse_folders(raw: object, home: str) -> tuple[Folder, ...]:
    entries = as_list(raw)
    if not entries:
        raise FinvaultError(
            "the config has no [[folders]] entries",
            "add at least one [[folders]] table with name and path",
        )
    folders: list[Folder] = []
    for i, item in enumerate(entries):
        entry = as_dict(item)
        if entry is None:
            raise FinvaultError(f"folders[{i}] must be a table")
        _check_keys(entry, _FOLDER_KEYS, f"folders[{i}]")
        name = entry.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise FinvaultError(
                f"folders[{i}].name = {name!r} is not a valid name",
                "use lowercase letters, digits, '.', '_' or '-' (max 64)",
            )
        path = _expand(entry.get("path"), f"folders[{i}].path", home)
        size = entry.get("size")
        if size is not None and (not isinstance(size, str) or not SIZE_RE.match(size)):
            raise FinvaultError(
                f"folders[{i}].size = {size!r} is not a size",
                "write it like '500m', '8g' or '1t'",
            )
        folders.append(Folder(name=name, path=path, size=size))
    return tuple(folders)


def _check_layout(folders: tuple[Folder, ...], state_dir: str, home: str) -> None:
    names = [f.name for f in folders]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise FinvaultError(f"duplicate folder name(s): {', '.join(dupes)}", "give each folder its own name")
    for f in folders:
        if f.path in ("/", home) or is_under(home, f.path):
            raise FinvaultError(f"folder {f.name} path {f.path} is too broad", "pick a folder below your home")
        if is_under(f.path, state_dir) or is_under(state_dir, f.path):
            raise FinvaultError(
                f"folder {f.name} and state_dir {state_dir} overlap",
                "keep state_dir outside every vault folder",
            )
    for a in folders:
        for b in folders:
            if a is not b and is_under(a.path, b.path):
                raise FinvaultError(
                    f"folder {a.name} ({a.path}) is inside folder {b.name} ({b.path})",
                    "vault folders may not be nested",
                )


def _parse_sweep(table: object, folders: tuple[Folder, ...], home: str, uid: int) -> SweepSettings | None:
    if table is None:
        return None
    raw = as_dict(table)
    if raw is None:
        raise FinvaultError("[sweep] must be a table")
    _check_keys(raw, _SWEEP_KEYS, "[sweep]")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise FinvaultError("sweep.enabled must be true or false")
    if not enabled:
        return None
    archive_folder = raw.get("archive_folder", folders[0].name)
    if not isinstance(archive_folder, str) or archive_folder not in {f.name for f in folders}:
        raise FinvaultError(
            f"sweep.archive_folder = {archive_folder!r} is not a configured folder",
            "name one of the [[folders]] entries",
        )
    subdir = raw.get("archive_subdir", ".finvault-sweep")
    if not isinstance(subdir, str) or not subdir or "/" in subdir or subdir in (".", ".."):
        raise FinvaultError("sweep.archive_subdir must be a single folder name")
    claude_dir = _expand(raw.get("claude_dir", "~/.claude"), "sweep.claude_dir", home)

    def sub(key: str, default: str) -> str:
        return _expand(raw.get(key, os.path.join(claude_dir, default)), f"sweep.{key}", home)

    scratch_root = _expand(raw.get("scratch_root", f"/private/tmp/claude-{uid}"), "sweep.scratch_root", home)
    if scratch_root == "/" or is_under(home, scratch_root):
        raise FinvaultError(f"sweep.scratch_root {scratch_root} is too broad", "point it at Claude's tmp folder")
    globs_raw = as_list(raw.get("scratch_globs", []))
    if globs_raw is None:
        raise FinvaultError("sweep.scratch_globs must be a list of strings")
    globs = tuple(_check_glob(g) for g in globs_raw)
    return SweepSettings(
        archive_folder=archive_folder,
        archive_subdir=subdir,
        history=sub("history", "history.jsonl"),
        file_history=sub("file_history", "file-history"),
        paste_cache=sub("paste_cache", "paste-cache"),
        projects_dir=sub("projects_dir", "projects"),
        scratch_root=scratch_root,
        scratch_globs=globs,
    )


def parse_config(data: dict[str, object], *, config_path: str, home: str, uid: int) -> Config:
    """Validate a decoded config table and return a ``Config``."""
    _check_keys(data, _TOP_KEYS, "the top level")
    if "state_dir" not in data:
        raise FinvaultError(
            f"{config_path} does not set state_dir",
            'add state_dir = "~/.finvault" (one state folder per vault)',
        )
    state_dir = _expand(data["state_dir"], "state_dir", home)
    if state_dir in ("/", home):
        raise FinvaultError(f"state_dir {state_dir} is too broad", "use a dedicated folder like ~/.finvault")
    image_raw = as_dict(data.get("image", {}))
    if image_raw is None:
        raise FinvaultError("[image] must be a table")
    _check_keys(image_raw, _IMAGE_KEYS, "[image]")
    image = ImageSettings(
        min_size_gb=_positive_int(image_raw.get("min_size_gb"), "image.min_size_gb", 1),
        size_factor=_positive_int(image_raw.get("size_factor"), "image.size_factor", 4),
    )
    folders = _parse_folders(data.get("folders"), home)
    _check_layout(folders, state_dir, home)
    sweep = _parse_sweep(data.get("sweep"), folders, home, uid)
    return Config(
        config_path=config_path,
        state_dir=state_dir,
        folders=folders,
        image=image,
        sweep=sweep,
    )


def load_config(path: str | None) -> Config:
    """Read and validate the config file at ``path`` (default ~/.finvault/config.toml)."""
    home = str(Path.home())
    raw_path = path or DEFAULT_CONFIG
    config_path = os.path.normpath(home + raw_path[1:] if raw_path.startswith("~") else os.path.abspath(raw_path))
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise FinvaultError(
            f"config file not found at {config_path}",
            "copy config.example.toml there and edit it, or pass --config <path>",
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise FinvaultError(f"{config_path} is not valid TOML: {exc}", "fix the syntax error it names") from None
    return parse_config(data, config_path=config_path, home=home, uid=os.getuid())

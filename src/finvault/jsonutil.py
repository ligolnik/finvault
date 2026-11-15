"""Typed views of decoded JSON/TOML/plist values."""

from __future__ import annotations

from typing import cast


def as_dict(value: object) -> dict[str, object] | None:
    """``value`` as a str-keyed dict, or None when it is not a dict."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def as_list(value: object) -> list[object] | None:
    """``value`` as a list, or None when it is not a list."""
    if isinstance(value, list):
        return cast("list[object]", value)
    return None

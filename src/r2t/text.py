"""Helpers for putting untrusted strings into terminals and Markdown safely."""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(value: object, limit: int = 300) -> str:
    """Replace control characters (including ESC) so repo-derived text cannot drive a terminal."""
    text = _CONTROL.sub("?", str(value))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def code_span(value: object) -> str:
    """Markdown inline code that survives backticks inside the value."""
    text = clean(value)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"

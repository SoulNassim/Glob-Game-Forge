"""Keep generation prompts free of injected palette values."""

from __future__ import annotations

import re

_PALETTE_CLAUSE = re.compile(
    r"(?i)\b(?:color\s+palette\s+anchor|dominant\s+palette|master\s+palette|palette\s+anchor)\b\s*:?\s*"
    r"(?:#[0-9a-f]{3,8}(?:\s*,\s*)?)+"
)
_LOOSE_PUNCTUATION = re.compile(r"\s+([,.;])")
_REPEATED_COMMAS = re.compile(r"(?:,\s*){2,}")
_REPEATED_PERIODS = re.compile(r"\.\s+\.")
_EXTRA_SPACE = re.compile(r"[ \t]{2,}")


def strip_palette_values(text: str) -> str:
    """Remove injected palette-hex clauses without changing the surrounding art direction."""
    cleaned = _PALETTE_CLAUSE.sub(" ", str(text or ""))
    cleaned = _EXTRA_SPACE.sub(" ", cleaned)
    cleaned = _LOOSE_PUNCTUATION.sub(r"\1", cleaned)
    cleaned = _REPEATED_COMMAS.sub(", ", cleaned)
    cleaned = _REPEATED_PERIODS.sub(".", cleaned)
    return cleaned.strip(" ,")


def _append_clause(text: str, clause: str) -> str:
    base = text.rstrip()
    if base.endswith("."):
        base = base[:-1].rstrip()
    return f"{base}. {clause}."


def finalize_generation_prompt(prompt: str, *, pose_mode: str = "", aspect_ratio: str = "1:1") -> str:
    """Return the exact positive prompt a provider would send.

    Pose and aspect clauses are included once. Calling this again with the same
    constraints does not change the text.
    """
    text = strip_palette_values(prompt)
    pose = str(pose_mode or "").strip().lower()
    if pose in {"a-pose", "t-pose"} and pose not in text.lower():
        text = _append_clause(text, f"Character pose: {pose.upper()}")
    ratio = str(aspect_ratio or "1:1").strip() or "1:1"
    if ratio != "1:1" and ratio not in text:
        text = _append_clause(text, f"Framing aspect ratio: {ratio}")
    return strip_palette_values(text)

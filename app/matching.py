"""How alike names are: a release's or a file's, and a game's or a dump's as the DATs name it."""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def words(name: str) -> set[str]:
    return set(_WORD.findall(name.lower()))


def similarity(a: str, b: str) -> float:
    """How alike two names are, from 0 to 1, by their words: tags in brackets count, case and punctuation don't."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


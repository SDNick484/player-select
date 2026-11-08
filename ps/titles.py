"""Title normalization (spec 5.4): raw GameEye title -> (work_key, edition)."""
from __future__ import annotations

import re
import unicodedata

_BRACKET = re.compile(r"\[([^\]]*)\]")
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
          "ix": "9", "x": "10", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14",
          "xv": "15", "xvi": "16"}


def normalize(raw: str) -> tuple[str, str | None]:
    m = _BRACKET.search(raw)
    edition = (m.group(1).strip() or None) if m else None
    s = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    s = _BRACKET.sub(" ", s.lower()).replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    words = s.split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(_ROMAN.get(w, w) for w in words), edition


def display_title(raw: str) -> str:
    return " ".join(_BRACKET.sub(" ", raw).split())

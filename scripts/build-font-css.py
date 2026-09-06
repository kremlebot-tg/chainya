#!/usr/bin/env python3
"""Build the self-contained font stylesheet used by standalone previews."""

from __future__ import annotations

import base64
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "fonts"
CYRILLIC_RANGE = "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116"
LATIN_RANGE = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, "
    "U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, "
    "U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
)

FONTS = (
    ("Prata", "prata", "400"),
    ("Onest", "onest", "400 700"),
    ("Rubik", "rubik", "400 700"),
)


def face(family: str, file_stem: str, weight: str, subset: str) -> str:
    payload = base64.b64encode((FONT_DIR / f"{file_stem}-{subset}.woff2").read_bytes()).decode()
    unicode_range = CYRILLIC_RANGE if subset == "cyr" else LATIN_RANGE
    return (
        f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
        f"font-display:swap;src:url(data:font/woff2;base64,{payload}) format('woff2');"
        f"unicode-range:{unicode_range};}}"
    )


css = "\n".join(
    face(family, file_stem, weight, subset)
    for family, file_stem, weight in FONTS
    for subset in ("cyr", "lat")
)
(FONT_DIR / "fonts-inline.css").write_text(css + "\n", encoding="utf-8")

#!/usr/bin/env python3
"""Build the versioned catalog seed from the current static storefront data."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.catalog_store import CATALOG_TYPE_DEFAULTS, DEFAULT_PARTNERS

SOURCE = ROOT / "src.html"
BOT_CATALOG = ROOT / "telegram-bot" / "teas.json"
OUTPUT = ROOT / "backend" / "catalog.seed.json"


def balanced(text: str, start: int) -> str:
    opening = text[start]
    closing = {"[": "]", "{": "}"}[opening]
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("Незакрытый JavaScript-блок")


def block(source: str, marker: str, bracket: str, start: int = 0) -> tuple[str, int]:
    marker_pos = source.index(marker, start)
    opening = source.index(bracket, marker_pos)
    result = balanced(source, opening)
    return result, opening + len(result)


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    meta, _ = block(source, "const TEA_META = ", "[")
    taste, _ = block(source, "const TASTE = ", "{")
    translations = []
    cursor = 0
    for _language in ("ru", "en", "zh"):
        tea_data, cursor = block(source, "teas:{", "{", cursor)
        translations.append(tea_data)
    javascript = "\n".join((
        f"const meta={meta};",
        f"const taste={taste};",
        f"const ru={translations[0]};",
        f"const en={translations[1]};",
        f"const zh={translations[2]};",
        "console.log(JSON.stringify({meta,taste,ru,en,zh}));",
    ))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(javascript)
            temporary = Path(handle.name)
        parsed = json.loads(subprocess.check_output(["node", temporary], text=True))
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)

    bot = json.loads(BOT_CATALOG.read_text(encoding="utf-8"))
    teas = []
    for meta_item in parsed["meta"]:
        item_id = meta_item["id"]
        localized = {}
        for language in ("ru", "en", "zh"):
            text = parsed[language][item_id]
            localized[language] = {
                "name": text["n"], "orig": text["o"], "desc": text["d"],
                "composition": "", "manufacturer": "", "shelf_life": "", "storage": "",
            }
        ru = localized["ru"]
        image = meta_item["img"].removeprefix("{{img:").removesuffix("}}")
        teas.append({
            "id": item_id,
            "type": meta_item["t"],
            "name": ru["name"],
            "orig": ru["orig"],
            "desc": ru["desc"],
            "price": meta_item["p"],
            "unit": meta_item["unit"],
            "stock": meta_item.get("stock", True),
            "published": True,
            "img": image,
            "image": {"kind": "seed", "name": image},
            "taste": parsed["taste"][item_id],
            "translations": localized,
        })
    result = {
        "schema_version": 5,
        "revision": 1,
        "partners": list(DEFAULT_PARTNERS),
        "types": [
            {
                "id": type_id,
                "name": ru,
                "group": group,
                "names": {"ru": ru, "en": en, "zh": zh},
            }
            for type_id, group, ru, en, zh in CATALOG_TYPE_DEFAULTS
        ],
        "axes": bot["axes"],
        "packs": bot["packs"],
        "teas": teas,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if "--check" in sys.argv:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit("backend/catalog.seed.json устарел; запустите scripts/build-catalog-seed.py")
    else:
        OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"{OUTPUT}: {len(teas)} товаров")


if __name__ == "__main__":
    main()

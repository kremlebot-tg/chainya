#!/usr/bin/env python3
"""Refresh the compact Russian city-name index used by CDEK autocomplete."""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.request
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/"
    "epogrebnyak/ru-cities/main/assets/towns.csv"
)


def main() -> None:
    target = Path(
        os.getenv(
            "CDEK_CITIES_PATH",
            Path(os.getenv("CHAINYA_DATA_DIR", "/var/lib/chainya-shop"))
            / "cdek-cities-ru.json",
        )
    )
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "Chainya delivery autocomplete/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        source = response.read().decode("utf-8-sig")
    cities = []
    for row in csv.DictReader(io.StringIO(source)):
        name = str(row.get("city", "")).strip()
        if not name:
            continue
        try:
            population = int(float(row.get("population", 0) or 0) * 1000)
        except ValueError:
            population = 0
        cities.append(
            {
                "city": name,
                "region": str(row.get("region_name", "")).strip(),
                "country": "Россия",
                "population": population,
            }
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(cities, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    print(f"Russian cities: {len(cities)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Создаёт согласованную SQLite-копию заказов и удаляет старые архивы."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE = Path("/var/lib/chainya-shop/orders.sqlite3")
DESTINATION = Path("/var/backups/chainya-shop")
KEEP_DAYS = 30


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    target = DESTINATION / f"orders-{stamp}.sqlite3"
    with sqlite3.connect(SOURCE) as source, sqlite3.connect(target) as backup:
        source.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Проверка резервной копии не пройдена")
    target.chmod(0o600)
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for old in DESTINATION.glob("orders-*.sqlite3"):
        if datetime.fromtimestamp(old.stat().st_mtime, timezone.utc) < cutoff:
            old.unlink()


if __name__ == "__main__":
    main()

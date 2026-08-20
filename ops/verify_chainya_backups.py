#!/usr/bin/env python3
"""Read-only validation for Chainya database and catalog backups."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote


class BackupVerificationError(RuntimeError):
    """Raised when a backup cannot be trusted for recovery."""


def _secure_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BackupVerificationError("backup is not a regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BackupVerificationError("backup permissions are too broad")


def _age_hours(path: Path, *, now: datetime) -> float:
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return (now - modified).total_seconds() / 3600


def _latest(directory: Path, pattern: str) -> Path:
    candidates = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime)
    if not candidates:
        raise BackupVerificationError("required backup is missing")
    return candidates[-1]


def verify_sqlite_backup(path: Path) -> None:
    _secure_regular_file(path)
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise BackupVerificationError("SQLite integrity_check failed")


def _safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise BackupVerificationError("catalog archive contains an unsafe path")
    if member.issym() or member.islnk() or not member.isfile():
        raise BackupVerificationError("catalog archive contains an unsupported member")
    return name


def verify_catalog_archive(path: Path) -> dict[str, int]:
    _secure_regular_file(path)
    catalog_items = None
    media_files = 0
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise BackupVerificationError("catalog archive is empty")
        for member in members:
            name = _safe_member(member)
            if name == PurePosixPath("catalog.json"):
                source = archive.extractfile(member)
                if source is None:
                    raise BackupVerificationError("catalog.json cannot be read")
                try:
                    catalog = json.load(source)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BackupVerificationError("catalog.json is invalid") from exc
                teas = catalog.get("teas") if isinstance(catalog, dict) else None
                if not isinstance(teas, list):
                    raise BackupVerificationError("catalog does not contain a product list")
                catalog_items = len(teas)
            elif len(name.parts) == 2 and name.parts[0] == "catalog-media":
                if name.suffix.lower() != ".webp":
                    raise BackupVerificationError("catalog archive contains unexpected media")
                media_files += 1
            else:
                raise BackupVerificationError("catalog archive contains an unexpected file")
    if catalog_items is None:
        raise BackupVerificationError("catalog.json is missing")
    return {"catalog_items": catalog_items, "catalog_media_files": media_files}


def verify_backup_directory(
    directory: Path,
    *,
    max_age_hours: float = 36,
    now: datetime | None = None,
) -> dict[str, int | float | str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    if directory.is_symlink() or not directory.is_dir():
        raise BackupVerificationError("backup directory is missing")

    database = _latest(directory, "orders-*.sqlite3")
    catalog = _latest(directory, "catalog-*.tar.gz")
    database_age = _age_hours(database, now=current)
    catalog_age = _age_hours(catalog, now=current)
    if database_age < 0 or catalog_age < 0:
        raise BackupVerificationError("backup timestamp is in the future")
    if database_age > max_age_hours or catalog_age > max_age_hours:
        raise BackupVerificationError("latest backup is stale")

    verify_sqlite_backup(database)
    catalog_result = verify_catalog_archive(catalog)
    return {
        "status": "ok",
        "database_age_hours": round(database_age, 2),
        "catalog_age_hours": round(catalog_age, 2),
        **catalog_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", type=Path, default=Path("/var/backups/chainya-shop")
    )
    parser.add_argument("--max-age-hours", type=float, default=36)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = verify_backup_directory(
            args.directory, max_age_hours=args.max_age_hours
        )
    except (BackupVerificationError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        if args.json:
            print(json.dumps({"status": "error", "reason": str(exc)}))
        else:
            print(f"backup verification failed: {exc}")
        raise SystemExit(1) from None
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "backup verification: ok; "
            f"catalog={result['catalog_items']}; media={result['catalog_media_files']}"
        )


if __name__ == "__main__":
    os.umask(0o077)
    main()

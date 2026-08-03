#!/usr/bin/env python3
"""Создаёт согласованную SQLite-копию и применяет сроки хранения данных."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = Path("/var/lib/chainya-shop/orders.sqlite3")
CATALOG_SOURCE = Path("/var/lib/chainya-shop/catalog.json")
CATALOG_MEDIA_SOURCE = Path("/var/lib/chainya-shop/catalog-media")
DESTINATION = Path("/var/backups/chainya-shop")
KEEP_DAYS = 30
ANALYTICS_LIVE_DAYS = 360  # с учётом ежедневного удаления — до 32 дней в копиях
# Контактные заявки и брони обезличиваются в рабочей базе через 333 дня.
# Двухдневный запас учитывает ежедневный график запуска и удаление 30-дневных
# копий на следующем запуске: полный срок остаётся в пределах одного года.
CONTACT_PII_TOTAL_DAYS = 365
RETENTION_JOB_GRACE_DAYS = 2
CONTACT_PII_LIVE_DAYS = (
    CONTACT_PII_TOTAL_DAYS - KEEP_DAYS - RETENTION_JOB_GRACE_DAYS
)
# Контактные и адресные данные заказов нужны дольше из-за требований к учёту и
# претензионной работе. Пять лет считаются в рабочей базе; с учётом ежедневного
# графика копия может прожить ещё не более 32 дней.
ORDER_PII_LIVE_YEARS = 5
ANONYMIZED_CUSTOMER = json.dumps(
    {"anonymized": True}, ensure_ascii=False, separators=(",", ":")
)


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def activity_expression() -> str:
    """Latest ISO timestamp for schemas where updated_at can be blank."""
    return (
        "CASE WHEN COALESCE(NULLIF(updated_at, ''), created_at) > created_at "
        "THEN updated_at ELSE created_at END"
    )


def years_before(value: datetime, years: int) -> datetime:
    """Subtract calendar years; 29 February maps to 28 February."""
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def apply_retention(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, int]:
    """Anonymize expired PII while preserving non-personal operational history."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    counts = {
        "analytics_deleted": 0,
        "business_leads_anonymized": 0,
        "bookings_anonymized": 0,
        "orders_anonymized": 0,
        "customer_sessions_deleted": 0,
    }

    if table_exists(connection, "analytics_events"):
        analytics_cutoff = current - timedelta(days=ANALYTICS_LIVE_DAYS)
        cursor = connection.execute(
            "DELETE FROM analytics_events WHERE created_at < ?",
            (analytics_cutoff.isoformat(),),
        )
        counts["analytics_deleted"] = cursor.rowcount

    if table_exists(connection, "customer_sessions"):
        cursor = connection.execute(
            "DELETE FROM customer_sessions WHERE expires_at <= ?",
            (current.isoformat(),),
        )
        counts["customer_sessions_deleted"] = cursor.rowcount

    contact_cutoff = (
        current - timedelta(days=CONTACT_PII_LIVE_DAYS)
    ).isoformat()
    if table_exists(connection, "business_leads"):
        cursor = connection.execute(
            f"""UPDATE business_leads
                   SET company = '', name = '', contact = '', note = ''
                 WHERE {activity_expression()} < ?
                   AND (company != '' OR name != '' OR contact != '' OR note != '')""",
            (contact_cutoff,),
        )
        counts["business_leads_anonymized"] = cursor.rowcount

    if table_exists(connection, "bookings"):
        cursor = connection.execute(
            f"""UPDATE bookings
                   SET name = '', phone = '', note = '',
                       idempotency_key_hash = NULL, request_hash = NULL
                 WHERE {activity_expression()} < ?
                   AND (
                       name != '' OR phone != '' OR note != ''
                       OR idempotency_key_hash IS NOT NULL OR request_hash IS NOT NULL
                   )""",
            (contact_cutoff,),
        )
        counts["bookings_anonymized"] = cursor.rowcount

    order_cutoff = years_before(current, ORDER_PII_LIVE_YEARS).isoformat()
    if table_exists(connection, "orders"):
        cursor = connection.execute(
            f"""UPDATE orders
                   SET customer_json = ?, payment_token = NULL, payment_url = NULL,
                       idempotency_key_hash = NULL, request_hash = NULL
                 WHERE {activity_expression()} < ?
                   AND (
                       customer_json != ? OR payment_token IS NOT NULL
                       OR payment_url IS NOT NULL OR idempotency_key_hash IS NOT NULL
                       OR request_hash IS NOT NULL
                   )""",
            (ANONYMIZED_CUSTOMER, order_cutoff, ANONYMIZED_CUSTOMER),
        )
        counts["orders_anonymized"] = cursor.rowcount

    connection.commit()
    return counts


def main(
    source_path: Path = SOURCE,
    destination: Path = DESTINATION,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    stamp = current.strftime("%Y-%m-%dT%H%M%SZ")
    target = destination / f"orders-{stamp}.sqlite3"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target) as backup:
        apply_retention(source, now=current)
        source.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Проверка резервной копии не пройдена")
    target.chmod(0o600)
    if CATALOG_SOURCE.is_file():
        # JSON заменяется приложением атомарно, поэтому чтение даёт целую версию.
        catalog_bytes = CATALOG_SOURCE.read_bytes()
        catalog = json.loads(catalog_bytes)
        if not isinstance(catalog.get("teas"), list):
            raise RuntimeError("Каталог не прошёл проверку перед копированием")
        catalog_target = destination / f"catalog-{stamp}.tar.gz"
        temporary = destination / f".catalog-{stamp}.tar.gz"
        with tarfile.open(temporary, "w:gz") as archive:
            info = tarfile.TarInfo("catalog.json")
            info.size = len(catalog_bytes)
            info.mode = 0o600
            info.mtime = int(current.timestamp())
            archive.addfile(info, io.BytesIO(catalog_bytes))
            if CATALOG_MEDIA_SOURCE.is_dir():
                for image in sorted(CATALOG_MEDIA_SOURCE.glob("*.webp")):
                    if image.is_file() and not image.is_symlink():
                        archive.add(image, arcname=f"catalog-media/{image.name}", recursive=False)
        temporary.chmod(0o600)
        temporary.replace(catalog_target)
    cutoff = current - timedelta(days=KEEP_DAYS)
    for old in destination.glob("orders-*.sqlite3"):
        if datetime.fromtimestamp(old.stat().st_mtime, timezone.utc) < cutoff:
            old.unlink()
    for old in destination.glob("catalog-*.tar.gz"):
        if datetime.fromtimestamp(old.stat().st_mtime, timezone.utc) < cutoff:
            old.unlink()


if __name__ == "__main__":
    main()

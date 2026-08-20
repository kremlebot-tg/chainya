from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "ops" / "backup-chainya.py"
SERVICE = Path(__file__).parents[2] / "ops" / "chainya-backup.service"
SPEC = importlib.util.spec_from_file_location("chainya_backup_retention", SCRIPT)
assert SPEC and SPEC.loader
backup_retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_retention)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE analytics_events (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE business_leads (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            company TEXT NOT NULL,
            name TEXT NOT NULL,
            contact TEXT NOT NULL,
            note TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE bookings (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            booking_time TEXT NOT NULL,
            format TEXT NOT NULL,
            guests INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            note TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key_hash TEXT,
            request_hash TEXT
        );
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            total INTEGER NOT NULL,
            items_json TEXT NOT NULL,
            customer_json TEXT NOT NULL,
            payment_token TEXT,
            payment_url TEXT,
            idempotency_key_hash TEXT,
            request_hash TEXT
        );
        CREATE TABLE customer_sessions (
            token_hash TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """
    )


def iso(value: datetime) -> str:
    return value.isoformat()


def test_retention_anonymizes_only_expired_personal_data(tmp_path):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    database = tmp_path / "orders.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        create_schema(connection)
        stale_contact = iso(
            now - timedelta(days=backup_retention.CONTACT_PII_LIVE_DAYS + 1)
        )
        contact_boundary = iso(
            now - timedelta(days=backup_retention.CONTACT_PII_LIVE_DAYS)
        )
        recent = iso(now - timedelta(days=10))
        stale_order = iso(
            backup_retention.years_before(now, backup_retention.ORDER_PII_LIVE_YEARS)
            - timedelta(seconds=1)
        )
        order_boundary = iso(
            backup_retention.years_before(now, backup_retention.ORDER_PII_LIVE_YEARS)
        )

        connection.executemany(
            """INSERT INTO business_leads
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "lead-old",
                    stale_contact,
                    stale_contact,
                    "Компания",
                    "Анна",
                    "@anna",
                    "Позвонить",
                    "closed",
                ),
                (
                    "lead-boundary",
                    contact_boundary,
                    contact_boundary,
                    "Кофейня",
                    "Борис",
                    "+79990000000",
                    "",
                    "new",
                ),
                (
                    "lead-recent-update",
                    stale_contact,
                    recent,
                    "Ресторан",
                    "Вера",
                    "vera@example.test",
                    "",
                    "contacted",
                ),
            ],
        )
        connection.executemany(
            """INSERT INTO bookings
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "booking-old",
                    stale_contact,
                    stale_contact,
                    "2025-01-01",
                    "12:00",
                    "master",
                    2,
                    "Гость",
                    "+79990000001",
                    "У окна",
                    "completed",
                    "key-hash",
                    "request-hash",
                ),
                (
                    "booking-recent",
                    recent,
                    recent,
                    "2026-08-01",
                    "14:00",
                    "self",
                    3,
                    "Новый гость",
                    "+79990000002",
                    "",
                    "new",
                    "recent-key",
                    "recent-request",
                ),
            ],
        )
        order_customer = json.dumps(
            {
                "name": "Покупатель",
                "phone": "+79990000003",
                "city": "Москва",
                "address": "Улица, дом",
            },
            ensure_ascii=False,
        )
        connection.executemany(
            """INSERT INTO orders
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "order-old",
                    stale_order,
                    stale_order,
                    875,
                    '[{"id":"baihao"}]',
                    order_customer,
                    "read-token",
                    "https://bank.example/payment",
                    "order-key",
                    "order-request",
                ),
                (
                    "order-boundary",
                    order_boundary,
                    order_boundary,
                    440,
                    '[{"id":"baimudan"}]',
                    order_customer,
                    "boundary-token",
                    "https://bank.example/boundary",
                    "boundary-key",
                    "boundary-request",
                ),
                (
                    "order-recent-update",
                    stale_order,
                    recent,
                    1200,
                    '[{"id":"dancong"}]',
                    order_customer,
                    "recent-token",
                    "https://bank.example/recent",
                    "recent-key",
                    "recent-request",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO analytics_events(created_at) VALUES (?)",
            [
                (iso(now - timedelta(days=backup_retention.ANALYTICS_LIVE_DAYS + 1)),),
                (iso(now - timedelta(days=backup_retention.ANALYTICS_LIVE_DAYS)),),
            ],
        )
        connection.executemany(
            "INSERT INTO customer_sessions VALUES (?, ?, ?, ?)",
            [
                ("expired-session", "account-1", recent, iso(now - timedelta(seconds=1))),
                ("active-session", "account-1", recent, iso(now + timedelta(days=1))),
            ],
        )

        counts = backup_retention.apply_retention(connection, now=now)

        assert counts == {
            "analytics_deleted": 1,
            "business_leads_anonymized": 1,
            "bookings_anonymized": 1,
            "orders_anonymized": 1,
            "customer_sessions_deleted": 1,
        }
        old_lead = connection.execute(
            "SELECT * FROM business_leads WHERE id = 'lead-old'"
        ).fetchone()
        assert (old_lead["company"], old_lead["name"], old_lead["contact"], old_lead["note"]) == (
            "",
            "",
            "",
            "",
        )
        assert connection.execute(
            "SELECT contact FROM business_leads WHERE id = 'lead-boundary'"
        ).fetchone()[0] == "+79990000000"
        assert connection.execute(
            "SELECT contact FROM business_leads WHERE id = 'lead-recent-update'"
        ).fetchone()[0] == "vera@example.test"

        old_booking = connection.execute(
            "SELECT * FROM bookings WHERE id = 'booking-old'"
        ).fetchone()
        assert (old_booking["name"], old_booking["phone"], old_booking["note"]) == (
            "",
            "",
            "",
        )
        assert old_booking["idempotency_key_hash"] is None
        assert old_booking["request_hash"] is None
        assert old_booking["format"] == "master"
        assert old_booking["guests"] == 2
        assert connection.execute(
            "SELECT phone FROM bookings WHERE id = 'booking-recent'"
        ).fetchone()[0] == "+79990000002"

        old_order = connection.execute(
            "SELECT * FROM orders WHERE id = 'order-old'"
        ).fetchone()
        assert old_order["customer_json"] == backup_retention.ANONYMIZED_CUSTOMER
        assert old_order["payment_token"] is None
        assert old_order["payment_url"] is None
        assert old_order["idempotency_key_hash"] is None
        assert old_order["request_hash"] is None
        assert old_order["total"] == 875
        assert old_order["items_json"] == '[{"id":"baihao"}]'
        assert connection.execute(
            "SELECT payment_token FROM orders WHERE id = 'order-boundary'"
        ).fetchone()[0] == "boundary-token"
        assert connection.execute(
            "SELECT payment_token FROM orders WHERE id = 'order-recent-update'"
        ).fetchone()[0] == "recent-token"
        assert connection.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0] == 1
        assert connection.execute(
            "SELECT token_hash FROM customer_sessions"
        ).fetchone()[0] == "active-session"


def test_main_backs_up_sanitized_database_and_prunes_old_archives(tmp_path):
    now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    source_path = tmp_path / "source.sqlite3"
    destination = tmp_path / "backups"
    destination.mkdir()
    stale = iso(
        now - timedelta(days=backup_retention.CONTACT_PII_LIVE_DAYS + 1)
    )
    with sqlite3.connect(source_path) as connection:
        create_schema(connection)
        connection.execute(
            """INSERT INTO business_leads
               VALUES ('lead-old', ?, ?, 'Компания', 'Имя', '@contact', 'note', 'closed')""",
            (stale, stale),
        )

    expired_archive = destination / "orders-expired.sqlite3"
    expired_archive.write_bytes(b"expired")
    expired_mtime = (now - timedelta(days=backup_retention.KEEP_DAYS + 1)).timestamp()
    os.utime(expired_archive, (expired_mtime, expired_mtime))
    recent_archive = destination / "orders-recent.sqlite3"
    recent_archive.write_bytes(b"recent")
    recent_mtime = (now - timedelta(days=1)).timestamp()
    os.utime(recent_archive, (recent_mtime, recent_mtime))

    backup_retention.main(source_path, destination, now=now)

    target = destination / "orders-2026-07-30T120000Z.sqlite3"
    assert target.exists()
    assert destination.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    assert not expired_archive.exists()
    assert recent_archive.exists()
    with sqlite3.connect(source_path) as source:
        assert source.execute(
            "SELECT contact FROM business_leads WHERE id = 'lead-old'"
        ).fetchone()[0] == ""
    with sqlite3.connect(target) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT contact FROM business_leads WHERE id = 'lead-old'"
        ).fetchone()[0] == ""


def test_retention_is_safe_for_database_without_optional_tables(tmp_path):
    database = tmp_path / "empty.sqlite3"
    with sqlite3.connect(database) as connection:
        assert backup_retention.apply_retention(
            connection, now=datetime(2026, 7, 30, tzinfo=timezone.utc)
        ) == {
            "analytics_deleted": 0,
            "business_leads_anonymized": 0,
            "bookings_anonymized": 0,
            "orders_anonymized": 0,
            "customer_sessions_deleted": 0,
        }


def test_main_backs_up_persistent_catalog_and_uploaded_media(tmp_path, monkeypatch):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    source_path = tmp_path / "orders.sqlite3"
    destination = tmp_path / "backups"
    catalog_path = tmp_path / "catalog.json"
    media_dir = tmp_path / "catalog-media"
    media_dir.mkdir()
    catalog_path.write_text(
        json.dumps({"teas": [{"id": "baihao"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (media_dir / ("a" * 32 + ".webp")).write_bytes(b"webp-image")
    with sqlite3.connect(source_path) as connection:
        create_schema(connection)
    monkeypatch.setattr(backup_retention, "CATALOG_SOURCE", catalog_path)
    monkeypatch.setattr(backup_retention, "CATALOG_MEDIA_SOURCE", media_dir)

    backup_retention.main(source_path, destination, now=now)

    archive_path = destination / "catalog-2026-08-03T120000Z.tar.gz"
    assert archive_path.is_file()
    assert archive_path.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == [
            "catalog.json",
            "catalog-media/" + "a" * 32 + ".webp",
        ]
        archived_catalog = json.load(archive.extractfile("catalog.json"))
        assert archived_catalog["teas"][0]["id"] == "baihao"


def test_retention_rejects_ambiguous_naive_clock(tmp_path):
    database = tmp_path / "empty.sqlite3"
    with sqlite3.connect(database) as connection:
        with pytest.raises(ValueError, match="timezone-aware"):
            backup_retention.apply_retention(
                connection, now=datetime(2026, 7, 30)
            )


def test_calendar_year_cutoff_handles_leap_day():
    leap_day = datetime(2024, 2, 29, 12, tzinfo=timezone.utc)

    assert backup_retention.years_before(leap_day, 5) == datetime(
        2019, 2, 28, 12, tzinfo=timezone.utc
    )


def test_privacy_copy_matches_enforced_retention_periods():
    policy = (SCRIPT.parents[1] / "privacy.html").read_text(encoding="utf-8")

    assert backup_retention.CONTACT_PII_LIVE_DAYS == 333
    assert backup_retention.CONTACT_PII_TOTAL_DAYS == 365
    assert backup_retention.KEEP_DAYS == 30
    assert backup_retention.ANALYTICS_LIVE_DAYS == 360
    assert backup_retention.ORDER_PII_LIVE_YEARS == 5
    assert policy.count("стр. 2, кв. 233") == 4
    for phrase in (
        "рабочей базе не более 360 дней",
        "333 дня",
        "365 дней",
        "пять лет",
        "32 дней",
        "live database for no more than 360 days",
        "333 days",
        "365 days",
        "five years",
        "32 additional days",
        "在线数据库中保存不超过360天",
        "333天",
        "365天",
        "5年",
        "32天",
    ):
        assert phrase in policy


def test_backup_service_repairs_destination_before_dropping_privileges():
    unit = SERVICE.read_text(encoding="utf-8")
    prepare = (
        "ExecStartPre=+/usr/bin/install -d -o chainya-shop -g chainya-shop "
        "-m 0700 /var/backups/chainya-shop"
    )

    assert "User=chainya-shop" in unit
    assert "Group=chainya-shop" in unit
    assert prepare in unit
    assert unit.index(prepare) < unit.index("ExecStart=/opt/chainya-shop/.venv/bin/python")
    assert "ExecStart=+/opt/chainya-shop" not in unit

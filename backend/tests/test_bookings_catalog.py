import json
import sqlite3
from datetime import datetime

from backend.tests.test_orders import app_client


def booking_payload(**changes):
    data = {
        "format": "master",
        "date": "2026-08-01",
        "time": "18:30",
        "guests": 2,
        "name": "Анна",
        "phone": "+7 999 123-45-67",
        "note": "Стол у окна",
        "privacy_accepted": True,
    }
    data.update(changes)
    return data


def test_booking_is_validated_saved_and_notified(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    sent = []
    monkeypatch.setattr(module, "notify_booking", lambda booking: sent.append(booking))

    with client:
        response = client.post("/api/bookings", json=booking_payload())
        with module.db() as con:
            stored = con.execute("SELECT * FROM bookings").fetchone()

    assert response.status_code == 201
    assert response.json()["accepted"] is True
    assert stored["id"] == response.json()["id"]
    assert stored["booking_date"] == "2026-08-01"
    assert stored["booking_time"] == "18:30:00"
    assert stored["format"] == "master"
    assert stored["guests"] == 2
    assert stored["phone"] == "+7 999 123-45-67"
    assert stored["status"] == "new"
    assert sent[0]["id"] == stored["id"]


def test_booking_rejects_invalid_or_past_moscow_slot(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 18, 45, tzinfo=module.MOSCOW_TZ),
    )

    with client:
        assert client.post(
            "/api/bookings",
            json=booking_payload(date="2026-07-30", time="18:30"),
        ).status_code == 422
        assert client.post(
            "/api/bookings",
            json=booking_payload(time="18:15"),
        ).status_code == 422
        assert client.post(
            "/api/bookings",
            json=booking_payload(guests=8),
        ).status_code == 422
        assert client.post(
            "/api/bookings",
            json=booking_payload(privacy_accepted=False),
        ).status_code == 422
        assert client.post(
            "/api/bookings",
            json=booking_payload(phone="123"),
        ).status_code == 422


def test_booking_date_limit_uses_moscow_calendar_boundaries(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 0, 15, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)

    with client:
        boundary = client.post(
            "/api/bookings",
            json=booking_payload(date="2026-08-13", time="20:00"),
        )
        too_far = client.post(
            "/api/bookings",
            json=booking_payload(date="2026-08-14", time="12:00"),
        )
        with module.db() as con:
            stored = con.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]

    assert boundary.status_code == 201
    assert too_far.status_code == 422
    assert stored == 1


def test_booking_rate_limit(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)

    with client:
        responses = [
            client.post(
                "/api/bookings",
                json=booking_payload(date=f"2026-08-{day:02d}"),
            )
            for day in range(1, 7)
        ]

    assert [response.status_code for response in responses] == [
        201, 201, 201, 201, 201, 429,
    ]


def test_booking_creation_is_idempotent(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    sent = []
    monkeypatch.setattr(module, "notify_booking", lambda booking: sent.append(booking))
    headers = {"Idempotency-Key": "booking-018f6f07-7648-safe"}

    with client:
        first = client.post("/api/bookings", json=booking_payload(), headers=headers)
        replays = [
            client.post("/api/bookings", json=booking_payload(), headers=headers)
            for _ in range(7)
        ]
        conflict = client.post(
            "/api/bookings",
            json=booking_payload(note="Другие пожелания"),
            headers=headers,
        )
        invalid = client.post(
            "/api/bookings",
            json=booking_payload(),
            headers={"Idempotency-Key": "contains a space"},
        )
        with module.db() as con:
            rows = con.execute(
                "SELECT idempotency_key_hash, request_hash FROM bookings"
            ).fetchall()

    assert first.status_code == 201
    assert all(response.status_code == 201 for response in replays)
    assert all(first.json() == response.json() for response in replays)
    assert conflict.status_code == 409
    assert invalid.status_code == 422
    assert len(sent) == 1
    assert len(rows) == 1
    assert rows[0]["idempotency_key_hash"] != headers["Idempotency-Key"]
    assert len(rows[0]["idempotency_key_hash"]) == 64
    assert len(rows[0]["request_hash"]) == 64


def test_availability_blocks_every_overlapping_two_hour_start(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)

    with client:
        before = client.get(
            "/api/bookings/availability", params={"date": "2026-08-01"}
        )
        created = client.post(
            "/api/bookings", json=booking_payload(time="13:00")
        )
        after = client.get(
            "/api/bookings/availability", params={"date": "2026-08-01"}
        )
        overlaps = client.post(
            "/api/bookings",
            json=booking_payload(time="14:30", name="Борис"),
        )
        next_free = client.post(
            "/api/bookings",
            json=booking_payload(time="15:00", name="Вера"),
        )

    assert before.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    assert all(slot["available"] for slot in before.json()["slots"])
    assert created.status_code == 201
    states = {slot["time"]: slot["available"] for slot in after.json()["slots"]}
    assert [time for time, available in states.items() if not available] == [
        "12:00", "12:30", "13:00", "13:30", "14:00", "14:30"
    ]
    assert states["15:00"] is True
    assert overlaps.status_code == 409
    assert "уже занято" in overlaps.json()["detail"]
    assert next_free.status_code == 201


def test_cancelled_booking_releases_slot_and_reactivation_checks_conflicts(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    auth = {"Authorization": "Bearer test-admin-token"}

    with client:
        first = client.post(
            "/api/bookings", json=booking_payload(time="13:00")
        ).json()
        cancelled = client.patch(
            f"/api/admin/bookings/{first['id']}",
            json={"status": "cancelled"},
            headers=auth,
        )
        released = client.get(
            "/api/bookings/availability", params={"date": "2026-08-01"}
        )
        second = client.post(
            "/api/bookings",
            json=booking_payload(time="14:30", name="Борис"),
        )
        restore = client.patch(
            f"/api/admin/bookings/{first['id']}",
            json={"status": "confirmed"},
            headers=auth,
        )

    assert cancelled.status_code == 200
    assert all(slot["available"] for slot in released.json()["slots"])
    assert second.status_code == 201
    assert restore.status_code == 409


def test_admin_manual_blocks_close_and_release_public_slots(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    auth = {"Authorization": "Bearer test-admin-token"}

    with client:
        assert client.get("/api/admin/booking-blocks").status_code == 401
        created = client.post(
            "/api/admin/booking-blocks",
            json={
                "date": "2026-08-01",
                "start": "13:00",
                "end": "15:00",
                "note": "Закрытое мероприятие",
            },
            headers=auth,
        )
        page = client.get("/api/admin/booking-blocks", headers=auth)
        availability = client.get(
            "/api/bookings/availability", params={"date": "2026-08-01"}
        )
        conflict = client.post(
            "/api/bookings", json=booking_payload(time="14:30")
        )
        deleted = client.delete(
            f"/api/admin/booking-blocks/{created.json()['id']}", headers=auth
        )
        released = client.get(
            "/api/bookings/availability", params={"date": "2026-08-01"}
        )

    assert created.status_code == 201
    assert created.json()["note"] == "Закрытое мероприятие"
    assert page.json()["blocks"][0]["id"] == created.json()["id"]
    assert any(not slot["available"] for slot in availability.json()["slots"])
    assert conflict.status_code == 409
    assert deleted.status_code == 204
    assert all(slot["available"] for slot in released.json()["slots"])


def test_telegram_booking_requires_shared_secret_and_uses_same_calendar(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "BOOKING_BOT_SECRET", "shared-booking-secret")
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    telegram = booking_payload(
        source="telegram",
        phone="@chainyaguest",
        name="Гость Telegram",
        time="13:00",
    )

    with client:
        unauthorized = client.post("/api/bookings", json=telegram)
        created = client.post(
            "/api/bookings",
            json=telegram,
            headers={
                "X-Booking-Bot-Secret": "shared-booking-secret",
                "Idempotency-Key": "booking-telegram-018f6f07-safe",
            },
        )
        website_without_phone = client.post(
            "/api/bookings", json=booking_payload(phone="@chainyaguest")
        )
        with module.db() as con:
            stored = con.execute(
                "SELECT source, phone FROM bookings WHERE id = ?",
                (created.json()["id"],),
            ).fetchone()

    assert unauthorized.status_code == 403
    assert created.status_code == 201
    assert stored["source"] == "telegram"
    assert stored["phone"] == "@chainyaguest"
    assert website_without_phone.status_code == 422


def test_admin_bookings_support_queue_operations(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    auth = {"Authorization": "Bearer test-admin-token"}

    with client:
        created = []
        for day, name in enumerate(("Анна", "Борис", "Вера"), start=1):
            response = client.post(
                "/api/bookings",
                json=booking_payload(
                    date=f"2026-08-{day:02d}", name=name, note=f"Гость {name}"
                ),
            )
            assert response.status_code == 201
            created.append(response.json()["id"])

        assert client.get("/api/admin/bookings").status_code == 401
        page = client.get(
            "/api/admin/bookings",
            params={"limit": 1, "offset": 1},
            headers=auth,
        )
        search = client.get(
            "/api/admin/bookings", params={"q": "анна"}, headers=auth
        )
        updated = client.patch(
            f"/api/admin/bookings/{created[0]}",
            json={"status": "confirmed"},
            headers=auth,
        )
        filtered = client.get(
            "/api/admin/bookings",
            params={"status": "confirmed"},
            headers=auth,
        )
        invalid_filter = client.get(
            "/api/admin/bookings", params={"status": "secret"}, headers=auth
        )
        invalid_patch = client.patch(
            f"/api/admin/bookings/{created[0]}",
            json={"status": "secret"},
            headers=auth,
        )
        missing = client.patch(
            "/api/admin/bookings/DOESNOTEXIST",
            json={"status": "cancelled"},
            headers=auth,
        )

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["bookings"]) == 1
    assert search.json()["total"] == 1
    booking = search.json()["bookings"][0]
    assert booking["name"] == "Анна"
    assert set(booking) == {
        "id", "created_at", "updated_at", "date", "time", "format",
        "guests", "name", "phone", "note", "status", "source",
    }
    assert updated.status_code == 200
    assert updated.json()["status"] == "confirmed"
    assert updated.json()["updated_at"]
    assert filtered.json()["total"] == 1
    assert filtered.json()["bookings"][0]["id"] == created[0]
    assert invalid_filter.status_code == 422
    assert invalid_patch.status_code == 422
    assert missing.status_code == 404


def test_dashboard_reports_new_bookings_queue(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    auth = {"Authorization": "Bearer test-admin-token"}

    with client:
        booking = client.post("/api/bookings", json=booking_payload()).json()
        initial = client.get(
            "/api/admin/dashboard", params={"days": 30}, headers=auth
        )
        updated = client.patch(
            f"/api/admin/bookings/{booking['id']}",
            json={"status": "confirmed"},
            headers=auth,
        )
        handled = client.get(
            "/api/admin/dashboard", params={"days": 30}, headers=auth
        )

    assert initial.status_code == 200
    assert initial.json()["commerce"]["needs_attention"] == 0
    assert initial.json()["commerce"]["new_bookings"] == 1
    assert updated.status_code == 200
    assert handled.json()["commerce"]["new_bookings"] == 0


def test_existing_bookings_table_is_migrated_safely(tmp_path, monkeypatch):
    database = sqlite3.connect(tmp_path / "orders.sqlite3")
    database.execute(
        """CREATE TABLE bookings (
             id TEXT PRIMARY KEY,
             created_at TEXT NOT NULL,
             booking_date TEXT NOT NULL,
             booking_time TEXT NOT NULL,
             format TEXT NOT NULL,
             guests INTEGER NOT NULL,
             name TEXT NOT NULL,
             phone TEXT NOT NULL,
             note TEXT NOT NULL,
             status TEXT NOT NULL DEFAULT 'new'
           )"""
    )
    database.execute(
        """INSERT INTO bookings
           VALUES ('LEGACY', '2026-07-01T10:00:00+00:00', '2026-08-01',
                   '18:30:00', 'master', 2, 'Анна', '+79991234567', '', 'new')"""
    )
    database.commit()
    database.close()

    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        response = client.get("/api/admin/bookings", headers=auth)
        with module.db() as con:
            columns = {
                row["name"] for row in con.execute("PRAGMA table_info(bookings)")
            }

    assert response.status_code == 200
    assert response.json()["bookings"][0]["id"] == "LEGACY"
    assert {"source", "updated_at", "idempotency_key_hash", "request_hash"} <= columns
    assert response.json()["bookings"][0]["source"] == "website"


def test_management_page_contains_booking_queue(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)

    with client:
        login = client.post(
            "/api/admin/session", json={"token": "test-admin-token"}
        )
        panel = client.get("/manage")

    assert login.status_code == 204
    assert panel.status_code == 200
    assert 'id="tab-bookings"' in panel.text
    assert 'id="booking-search"' in panel.text
    assert 'id="booking-status"' in panel.text
    assert 'id="booking-block-form"' in panel.text
    assert "/api/admin/booking-blocks" in panel.text
    assert "/api/admin/bookings" in panel.text
    assert "Новые брони" in panel.text
    assert "c.new_bookings" in panel.text


def test_catalog_exposes_complete_public_fields_without_internal_data(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    catalog_path = tmp_path / "teas.json"
    catalog_path.write_text(json.dumps({
        "revision": 4,
        "types": [{"id": "white", "name": "Белый чай"}],
        "axes": [],
        "packs": [10, 25, 50, 100],
        "teas": [
            {
                "id": "public-tea",
                "type": "white",
                "price": 175,
                "unit": "g",
                "stock": True,
                "name": "Публичное название",
                "orig": "Фуцзянь",
                "desc": "Описание",
                "img": "tea-baihao",
                "taste": {"floral": 5},
                "saby_id": "secret-id",
            },
        ]
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "CATALOG_PATH", catalog_path)

    with client:
        response = client.get("/api/catalog")

    assert response.status_code == 200
    data = response.json()
    assert data["revision"] == 4
    assert data["types"][0]["id"] == "white"
    assert data["types"][0]["group"] == "tea"
    assert any(row["id"] == "teaware-teapots" and row["group"] == "teaware" for row in data["types"])
    assert data["teas"][0]["id"] == "public-tea"
    assert data["teas"][0]["image_url"] == "/img/tea-baihao.webp"
    assert data["teas"][0]["image_urls"] == ["/img/tea-baihao.webp"]
    assert data["teas"][0]["translations"]["ru"]["name"] == "Публичное название"
    assert data["teas"][0]["taste"]["floral"] == 5
    assert "saby_id" not in data["teas"][0]

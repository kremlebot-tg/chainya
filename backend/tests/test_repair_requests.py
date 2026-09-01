from io import BytesIO
import sqlite3

from PIL import Image

from backend.tests.test_orders import app_client


AUTH = {"Authorization": "Bearer test-admin-token"}


def repair_payload(**changes):
    payload = {
        "name": "Анна",
        "phone": "+7 999 123-45-67",
        "description": "Трещина на пиале",
        "has_image": False,
        "upload_token": "",
        "privacy_accepted": True,
    }
    payload.update(changes)
    return payload


def image_bytes(fmt="PNG"):
    stream = BytesIO()
    Image.new("RGB", (48, 32), (180, 140, 90)).save(stream, format=fmt)
    return stream.getvalue()


def test_repair_request_is_idempotent_and_visible_to_owner(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    headers = {"Idempotency-Key": "repair-test-1"}
    with client:
        first = client.post("/api/repair-requests", json=repair_payload(), headers=headers)
        repeated = client.post("/api/repair-requests", json=repair_payload(), headers=headers)
        listing = client.get("/api/admin/repair-requests", headers=AUTH)

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json()["id"] == repeated.json()["id"]
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["requests"][0]["description"] == "Трещина на пиале"
    assert listing.json()["requests"][0]["source"] == "website"


def test_repair_photo_is_reencoded_private_and_retryable(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    token = "A" * 43
    with client:
        created = client.post(
            "/api/repair-requests",
            json=repair_payload(has_image=True, upload_token=token),
            headers={"Idempotency-Key": "repair-photo-1"},
        )
        request_id = created.json()["id"]
        upload = client.post(
            f"/api/repair-requests/{request_id}/image",
            content=image_bytes(),
            headers={"Content-Type": "image/png", "X-Repair-Upload-Token": token},
        )
        retry = client.post(
            f"/api/repair-requests/{request_id}/image",
            content=image_bytes(),
            headers={"Content-Type": "image/png", "X-Repair-Upload-Token": token},
        )
        anonymous = client.get(f"/api/admin/repair-requests/{request_id}/image")
        owner = client.get(
            f"/api/admin/repair-requests/{request_id}/image", headers=AUTH
        )

    assert created.status_code == 202
    assert upload.status_code == 202
    assert retry.status_code == 202
    assert anonymous.status_code == 401
    assert owner.status_code == 200
    assert owner.headers["content-type"].startswith("image/webp")
    assert owner.headers["cache-control"] == "private, no-store"


def test_repair_request_rejects_bad_phone_and_owner_can_update_status(
    tmp_path, monkeypatch
):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        bad = client.post(
            "/api/repair-requests", json=repair_payload(phone="123")
        )
        created = client.post(
            "/api/repair-requests",
            json=repair_payload(),
            headers={"Idempotency-Key": "repair-status-1"},
        )
        updated = client.patch(
            f"/api/admin/repair-requests/{created.json()['id']}",
            json={"status": "contacted"},
            headers=AUTH,
        )

    assert bad.status_code == 422
    assert updated.status_code == 200
    assert updated.json()["status"] == "contacted"


def test_repair_request_from_authenticated_bot_records_telegram_consent_source(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "BOOKING_BOT_SECRET", "shared-secret")
    with client:
        created = client.post(
            "/api/repair-requests",
            json=repair_payload(),
            headers={
                "Idempotency-Key": "repair-telegram-1",
                "X-Booking-Bot-Secret": "shared-secret",
                "X-Telegram-User-ID": "123456789",
            },
        )
        with module.db() as con:
            consent = con.execute(
                "SELECT * FROM personal_data_consents WHERE record_id = ?",
                (created.json()["id"],),
            ).fetchone()

    assert created.status_code == 202
    assert consent["record_type"] == "repair_request"
    assert consent["purpose"] == "teaware_repair_enquiry"
    assert consent["source"] == "telegram"


def test_telegram_repair_rate_limit_is_per_user(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "BOOKING_BOT_SECRET", "shared-secret")
    module._rate_buckets.clear()
    with client:
        responses = [
            client.post(
                "/api/repair-requests",
                json=repair_payload(name=f"Гость {index}"),
                headers={
                    "Idempotency-Key": f"repair-telegram-user-{index}",
                    "X-Booking-Bot-Secret": "shared-secret",
                    "X-Telegram-User-ID": str(1000 + index),
                },
            )
            for index in range(6)
        ]
        listing = client.get("/api/admin/repair-requests", headers=AUTH)

    assert [response.status_code for response in responses] == [202] * 6
    assert {item["source"] for item in listing.json()["requests"]} == {"telegram"}


def test_authenticated_telegram_repair_requires_user_id(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "BOOKING_BOT_SECRET", "shared-secret")
    with client:
        response = client.post(
            "/api/repair-requests",
            json=repair_payload(),
            headers={
                "Idempotency-Key": "repair-telegram-without-user",
                "X-Booking-Bot-Secret": "shared-secret",
            },
        )

    assert response.status_code == 400


def test_existing_repair_table_is_migrated_with_website_source(tmp_path, monkeypatch):
    database = tmp_path / "orders.sqlite3"
    with sqlite3.connect(database) as con:
        con.execute(
            """CREATE TABLE repair_requests (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL,
                phone TEXT NOT NULL, description TEXT NOT NULL,
                has_image INTEGER NOT NULL DEFAULT 0, image_name TEXT,
                upload_token_hash TEXT, notification_sent INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new', updated_at TEXT NOT NULL,
                idempotency_key_hash TEXT, request_hash TEXT
            )"""
        )
        con.execute(
            """INSERT INTO repair_requests
               VALUES ('OLD1','2026-08-01T10:00:00+03:00','Анна','+79991234567',
                       'Скол',0,NULL,NULL,0,'new','2026-08-01T10:00:00+03:00',NULL,NULL)"""
        )

    client, module = app_client(tmp_path, monkeypatch)
    with client, module.db() as con:
        migrated = con.execute(
            "SELECT source FROM repair_requests WHERE id = 'OLD1'"
        ).fetchone()

    assert migrated["source"] == "website"

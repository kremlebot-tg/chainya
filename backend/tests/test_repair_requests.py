from io import BytesIO

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

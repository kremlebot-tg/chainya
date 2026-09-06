from datetime import datetime, timedelta

from backend.tests.test_bookings_catalog import booking_payload
from backend.tests.test_customer_account import register
from backend.tests.test_orders import app_client, payload

ADMIN = {"Authorization": "Bearer test-admin-token", "X-Chainya-Admin": "promos"}


def create_promo(client, **changes):
    data = {
        "code": "WELCOME10",
        "discount_percent": 10,
        "min_subtotal": 0,
        "usage_limit": 0,
        "per_customer_limit": 0,
        "expires_at": None,
        "active": True,
        "note": "Запуск",
    }
    data.update(changes)
    return client.post("/api/admin/promos", json=data, headers=ADMIN)


def test_owner_session_can_create_promo_when_mobile_browser_rewrites_origin(
    tmp_path, monkeypatch
):
    client, _ = app_client(tmp_path, monkeypatch)
    promo = {
        "code": "MOBILE15",
        "discount_percent": 15,
        "min_subtotal": 0,
        "expires_at": None,
        "active": True,
        "note": "Мобильная админка",
    }
    with client:
        assert client.post(
            "/api/admin/session", json={"token": "test-admin-token"}
        ).status_code == 204
        allowed = client.post(
            "/api/admin/promos",
            json=promo,
            headers={
                "Origin": "https://mobile-browser.invalid",
                "Sec-Fetch-Site": "same-origin",
                "X-Chainya-Admin": "promos",
            },
        )
        missing_marker = client.post(
            "/api/admin/promos",
            json={**promo, "code": "MISSING-MARKER"},
            headers={"Origin": "https://mobile-browser.invalid"},
        )

    assert allowed.status_code == 201
    assert missing_marker.status_code == 403
    assert missing_marker.json() == {
        "detail": "Требуется подтверждение запроса админ-панели"
    }


def test_promo_preview_and_order_use_the_same_discounted_item_totals(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        assert create_promo(client).status_code == 201
        preview = client.post(
            "/api/promos/preview",
            json={"promo_code": "welcome10", "items": payload()["items"]},
        )
        order = client.post("/api/orders", json=payload(promo_code="welcome10"))
        assert preview.status_code == 200
        assert order.status_code == 201
        result = order.json()["order"]
        with module.db() as con:
            row = con.execute("SELECT * FROM orders WHERE id = ?", (result["id"],)).fetchone()
        lines = module.public_order(row)["items"]

    assert result["promo_code"] == "WELCOME10"
    assert result["discount_percent"] == 10
    assert result["subtotal"] == sum(line["total"] for line in lines)
    assert result["subtotal"] == preview.json()["subtotal"]
    assert result["original_subtotal"] - result["subtotal"] == result["discount_amount"]
    assert all(line["unit_price"] * line["qty"] == line["total"] for line in lines)


def test_promo_total_limit_is_reserved_during_checkout(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert create_promo(client, code="ONLYONE", usage_limit=1).status_code == 201
        first = client.post("/api/orders", json=payload(promo_code="ONLYONE"))
        second = client.post("/api/orders", json=payload(promo_code="ONLYONE"))

    assert first.status_code == 201
    assert second.status_code == 422
    assert second.json()["detail"] == "Лимит применений промокода исчерпан"


def test_promo_per_customer_limit_uses_normalized_phone(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert create_promo(
            client, code="PERSONAL", per_customer_limit=1
        ).status_code == 201
        first_payload = payload(promo_code="PERSONAL")
        first_payload["phone"] = "+7 999 123-45-67"
        second_payload = payload(promo_code="PERSONAL")
        second_payload["phone"] = "8 (999) 123-45-67"
        first = client.post("/api/orders", json=first_payload)
        second = client.post("/api/orders", json=second_payload)

    assert first.status_code == 201
    assert second.status_code == 422
    assert "максимальное число раз" in second.json()["detail"]


def test_abandoned_checkout_releases_promo_limit_after_payment_window(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        assert create_promo(client, code="RELEASED", usage_limit=1).status_code == 201
        first = client.post("/api/orders", json=payload(promo_code="RELEASED"))
        with module.db() as con:
            con.execute(
                "UPDATE orders SET created_at = ? WHERE id = ?",
                (
                    (
                        datetime.now(module.timezone.utc)
                        - timedelta(minutes=module.STOCK_RESERVATION_MINUTES + 1)
                    ).isoformat(),
                    first.json()["order"]["id"],
                ),
            )
        second = client.post("/api/orders", json=payload(promo_code="RELEASED"))

    assert first.status_code == second.status_code == 201


def test_inactive_expired_and_minimum_promos_fail_closed(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert create_promo(client, code="OFF10", active=False).status_code == 201
        assert create_promo(
            client, code="OLD10", expires_at="2020-01-01T00:00:00+03:00"
        ).status_code == 201
        assert create_promo(client, code="BIG10", min_subtotal=999999).status_code == 201
        for code in ("OFF10", "OLD10", "BIG10", "MISSING"):
            response = client.post("/api/orders", json=payload(promo_code=code))
            assert response.status_code == 422


def test_guest_cancel_token_is_idempotent_and_releases_slot(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    monkeypatch.setattr(module, "notify_booking_cancelled", lambda booking, source: None)
    with client:
        created = client.post("/api/bookings", json=booking_payload(time="13:00"))
        body = created.json()
        wrong = client.post(
            f"/api/bookings/{body['id']}/cancel", json={"token": "0" * 24}
        )
        cancelled = client.post(
            f"/api/bookings/{body['id']}/cancel", json={"token": body["cancel_token"]}
        )
        replay = client.post(
            f"/api/bookings/{body['id']}/cancel", json={"token": body["cancel_token"]}
        )
        slots = client.get("/api/bookings/availability", params={"date": "2026-08-01"})
    assert wrong.status_code == 404
    assert cancelled.status_code == replay.status_code == 200
    assert all(slot["available"] for slot in slots.json()["slots"])


def test_account_can_cancel_only_its_own_future_booking(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    now = datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ)
    monkeypatch.setattr(module, "moscow_now", lambda: now)
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    monkeypatch.setattr(module, "notify_booking_cancelled", lambda booking, source: None)
    booking_date = (now.date() + timedelta(days=2)).isoformat()
    with client:
        assert register(client).status_code == 201
        created = client.post("/api/bookings", json=booking_payload(date=booking_date)).json()
        listed = client.get("/api/account/bookings").json()["bookings"][0]
        cancelled = client.post(f"/api/account/bookings/{created['id']}/cancel")
    assert listed["can_cancel"] is True
    assert cancelled.status_code == 200
    assert cancelled.json()["booking"]["status"] == "cancelled"
    assert cancelled.json()["booking"]["can_cancel"] is False


def test_promo_and_cancellation_controls_are_present_in_owner_and_customer_ui(
    tmp_path, monkeypatch
):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        account = client.get("/account")
        promos_login = client.get("/manage/promos")
        session = client.post(
            "/api/admin/session", json={"token": "test-admin-token"}
        )
        promos = client.get("/manage/promos")
        admin = client.get("/manage")
    assert "/api/account/bookings/" in account.text
    assert "admin-login.html" not in promos_login.text
    assert session.status_code == 204
    assert "Промокоды" in promos.text
    assert "Сейчас работают" in promos.text
    assert "Скидка покупателям" in promos.text
    assert "Сколько раз код можно применить всего" in promos.text
    assert "Сколько раз одному покупателю" in promos.text
    assert "Отменить бронь" in admin.text
    assert "это время сразу станет доступно другим гостям" in admin.text
    assert "confirmBookingCancellation" in admin.text


def test_promo_admin_reports_paid_usage_and_discount_total(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        assert create_promo(client).status_code == 201
        order = client.post("/api/orders", json=payload(promo_code="WELCOME10")).json()[
            "order"
        ]
        with module.db() as con:
            con.execute(
                "UPDATE orders SET paid_at = ?, payment_state = 'paid' WHERE id = ?",
                (module.now_iso(), order["id"]),
            )
        rows = client.get("/api/admin/promos", headers=ADMIN).json()["promos"]

    assert rows[0]["paid_redemptions"] == 1
    assert rows[0]["paid_discount_total"] == order["discount_amount"]


def test_admin_cancellation_records_source_and_releases_slot(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    with client:
        created = client.post("/api/bookings", json=booking_payload(time="13:00")).json()
        cancelled = client.patch(
            f"/api/admin/bookings/{created['id']}",
            json={"status": "cancelled"},
            headers=ADMIN,
        )
        slots = client.get(
            "/api/bookings/availability", params={"date": "2026-08-01"}
        ).json()["slots"]

    assert cancelled.status_code == 200
    result = cancelled.json()
    assert result["status"] == "cancelled"
    assert result["cancellation_source"] == "admin"
    assert result["cancelled_at"]
    assert all(slot["available"] for slot in slots)


def test_admin_restore_clears_cancellation_audit_fields(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    with client:
        created = client.post("/api/bookings", json=booking_payload()).json()
        client.patch(
            f"/api/admin/bookings/{created['id']}",
            json={"status": "cancelled"},
            headers=ADMIN,
        )
        restored = client.patch(
            f"/api/admin/bookings/{created['id']}",
            json={"status": "confirmed"},
            headers=ADMIN,
        )

    assert restored.status_code == 200
    result = restored.json()
    assert result["status"] == "confirmed"
    assert result["cancelled_at"] is None
    assert result["cancellation_source"] == ""


def test_admin_can_cancel_past_no_show_while_customer_cannot(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "moscow_now",
        lambda: datetime(2026, 7, 30, 15, 0, tzinfo=module.MOSCOW_TZ),
    )
    monkeypatch.setattr(module, "notify_booking", lambda booking: None)
    with client:
        created = client.post("/api/bookings", json=booking_payload()).json()
        monkeypatch.setattr(
            module,
            "moscow_now",
            lambda: datetime(2026, 8, 1, 21, 0, tzinfo=module.MOSCOW_TZ),
        )
        customer_cancel = client.post(
            f"/api/bookings/{created['id']}/cancel",
            json={"token": created["cancel_token"]},
        )
        admin_cancel = client.patch(
            f"/api/admin/bookings/{created['id']}",
            json={"status": "cancelled"},
            headers=ADMIN,
        )

    assert customer_cancel.status_code == 409
    assert admin_cancel.status_code == 200
    assert admin_cancel.json()["cancellation_source"] == "admin"

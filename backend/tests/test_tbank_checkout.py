import importlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend.tbank import generate_token


def demo_app(tmp_path, monkeypatch):
    for key in (
        "CDEK_CLIENT_ID", "CDEK_CLIENT_SECRET", "CDEK_INTEGRATION_MODE",
        "SABY_APP_CLIENT_ID", "SABY_APP_SECRET", "SABY_SECRET_KEY",
        "SABY_POINT_ID", "SABY_PRICE_LIST_ID", "SABY_ORDER_SYNC_MODE",
        "SABY_PURCHASE_ROUTE", "SABY_OFD_COMPANY_ID",
        "SABY_OFD_KKT_REG_NUMBER", "SABY_OFD_TAX_SYSTEM",
        "SABY_OFD_PAY_METHOD", "SABY_OFD_ALLOW_NEGATIVE_STOCK",
        "SABY_STOCK_GUARD_MODE",
        "SABY_CATALOG_SHADOW_MODE", "SABY_CATALOG_SHADOW_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CHAINYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINYA_TEST_MODE", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("TBANK_CHECKOUT_MODE", "demo")
    monkeypatch.setenv("TBANK_TERMINAL_KEY", "1234567890DEMO")
    monkeypatch.setenv("TBANK_PASSWORD", "demo-password")
    monkeypatch.setenv(
        "TBANK_NOTIFICATION_URL", "https://chainya.ru/api/payments/tbank/notification"
    )
    monkeypatch.setenv("TBANK_SUCCESS_URL", "https://chainya.ru/payment/success")
    monkeypatch.setenv("TBANK_FAIL_URL", "https://chainya.ru/payment/fail")
    import backend.app as module
    module = importlib.reload(module)
    return TestClient(module.app), module


def order_payload(**changes):
    result = {
        "items": [{"id": "baihao", "pack": 25, "qty": 2}],
        "delivery": "pickup",
        "payment_method": "bank_card",
        "name": "Тест",
        "phone": "+7 999 123-45-67",
        "city": "",
        "address": "",
        "pvz_code": "",
        "note": "",
        "privacy_accepted": True,
        "language": "ru",
    }
    result.update(changes)
    return result


def bank_response(payment_id="123456", status="NEW"):
    return {
        "Success": True,
        "PaymentId": payment_id,
        "Status": status,
        "PaymentURL": f"https://securepayments.tinkoff.ru/{payment_id}",
    }


def signed_notification(module, order_id, payment_id, amount, status, *, success=True):
    result = {
        "TerminalKey": module.tbank_client.settings.terminal_key,
        "OrderId": order_id,
        "Success": success,
        "Status": status,
        "PaymentId": payment_id,
        "ErrorCode": "0",
        "Amount": amount,
    }
    result["Token"] = generate_token(result, module.tbank_client.settings.password)
    return result


def test_demo_init_is_once_idempotent_and_uses_server_amount(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    calls = []

    def create_payment(*args, **kwargs):
        calls.append((args, kwargs))
        return bank_response()

    monkeypatch.setattr(module.tbank_client, "create_payment", create_payment)
    headers = {"Idempotency-Key": "demo-checkout-one"}
    with client:
        first = client.post("/api/orders", json=order_payload(), headers=headers)
        second = client.post("/api/orders", json=order_payload(), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["payment"] == {
        "mode": "tbank_demo",
        "url": "https://securepayments.tinkoff.ru/123456",
        "reused": False,
    }
    assert second.json()["payment"]["url"] == first.json()["payment"]["url"]
    assert second.json()["payment"]["reused"] is True
    assert len(calls) == 1
    assert calls[0][0][0] == first.json()["order"]["id"]
    assert calls[0][0][1] == 88_000
    assert calls[0][1]["notification_url"].endswith("/notification")
    success = urlparse(calls[0][1]["success_url"])
    assert success.path == "/payment/success"
    assert parse_qs(success.query)["order_id"] == [first.json()["order"]["id"]]
    assert parse_qs(success.query)["token"]


def test_live_stock_guard_uses_one_stage_short_lived_payment(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "TEST_MODE", False)
    monkeypatch.setenv("SABY_STOCK_GUARD_MODE", "auto")
    monkeypatch.setattr(module, "tbank_checkout_ready", lambda: True)
    calls = []
    monkeypatch.setattr(
        module.saby_client,
        "base_catalog_all",
        lambda with_balance=False: [{
            "externalId": module.SABY_NOMENCLATURE_BY_SITE_ID["baihao"].external_id,
            "unit": "г",
            "balance": 200,
        }],
    )
    monkeypatch.setattr(
        module.tbank_client,
        "create_payment",
        lambda *_args, **kwargs: calls.append(kwargs) or bank_response(),
    )
    with client:
        response = client.post("/api/orders", json=order_payload())
    assert response.status_code == 201
    assert calls[0]["pay_type"] == "O"
    assert calls[0]["redirect_due_date"]
    with module.db() as con:
        reservation = con.execute(
            "SELECT quantity, state FROM stock_reservations"
        ).fetchone()
    assert tuple(reservation) == ("50", "held")


def test_live_stock_guard_rejects_concurrent_oversell(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "TEST_MODE", False)
    monkeypatch.setenv("SABY_STOCK_GUARD_MODE", "auto")
    monkeypatch.setattr(module, "tbank_checkout_ready", lambda: True)
    monkeypatch.setattr(
        module.saby_client,
        "base_catalog_all",
        lambda with_balance=False: [{
            "externalId": module.SABY_NOMENCLATURE_BY_SITE_ID["baihao"].external_id,
            "unit": "г",
            "balance": 60,
        }],
    )
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    with client:
        first = client.post(
            "/api/orders", json=order_payload(),
            headers={"Idempotency-Key": "stock-first"},
        )
        second = client.post(
            "/api/orders", json=order_payload(),
            headers={"Idempotency-Key": "stock-second"},
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"].startswith("Недостаточно товара:")


def test_authorized_callback_does_not_trigger_capture_in_one_stage_mode(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "TEST_MODE", False)
    monkeypatch.setenv("SABY_STOCK_GUARD_MODE", "auto")
    monkeypatch.setattr(module, "tbank_checkout_ready", lambda: True)
    external_id = module.SABY_NOMENCLATURE_BY_SITE_ID["baihao"].external_id
    monkeypatch.setattr(
        module.saby_client,
        "base_catalog_all",
        lambda with_balance=False: [{
            "externalId": external_id, "unit": "г", "balance": 200,
        }],
    )
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    monkeypatch.setattr(
        module.tbank_client,
        "confirm",
        lambda *_args: pytest.fail("one-stage checkout must not call Confirm"),
    )
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        notice = signed_notification(
            module, created["id"], "123456", 88_000, "AUTHORIZED"
        )
        response = client.post("/api/payments/tbank/notification", json=notice)
    assert response.status_code == 200
    assert module.order_row(created["id"])["payment_state"] == "awaiting"
    with module.db() as con:
        reservation = con.execute(
            "SELECT state FROM stock_reservations WHERE order_id = ?",
            (created["id"],),
        ).fetchone()
    assert reservation["state"] == "held"


def test_checkout_status_reports_configured_demo_as_available(tmp_path, monkeypatch):
    client, _ = demo_app(tmp_path, monkeypatch)
    with client:
        response = client.get("/api/checkout/status")
    assert response.status_code == 200
    assert response.json() == {"available": True, "provider": "tbank"}


def test_local_mock_cannot_mark_tbank_order_paid(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    with client:
        response = client.post("/api/orders", json=order_payload())
        created = response.json()
        order_id = created["order"]["id"]
        success_url = urlparse(module.tbank_client.settings.success_url)
        # The private order token is intentionally taken from the bank return
        # URL generated by the server, not exposed by the public order model.
        with module.db() as con:
            token = con.execute(
                "SELECT payment_token FROM orders WHERE id = ?", (order_id,)
            ).fetchone()[0]
        pay = client.post(f"/api/orders/{order_id}/test-pay", params={"token": token})
        page = client.get(f"/test-payment/{order_id}", params={"token": token})
        state = client.get(f"/api/orders/{order_id}", params={"token": token}).json()
        with module.db() as con:
            provider_payment_id = con.execute(
                "SELECT provider_payment_id FROM orders WHERE id = ?", (order_id,)
            ).fetchone()[0]
    assert success_url.path == "/payment/success"
    assert pay.status_code == 404
    assert page.status_code == 404
    assert state["status"] == "pending_payment"
    assert state["payment_state"] == "awaiting"
    assert provider_payment_id == "123456"


def test_demo_mode_rejects_non_demo_credentials_without_network(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.tbank_client,
        "settings",
        module.tbank_client.settings.__class__(
            terminal_key="production-terminal",
            password="secret",
            notification_url="https://chainya.ru/api/payments/tbank/notification",
            success_url="https://chainya.ru/payment/success",
            fail_url="https://chainya.ru/payment/fail",
        ),
    )
    called = False

    def create_payment(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must stay untouched")

    monkeypatch.setattr(module.tbank_client, "create_payment", create_payment)
    with client:
        response = client.post("/api/orders", json=order_payload())
    assert response.status_code == 503
    assert called is False


def test_malformed_init_never_returns_redirect_or_retries(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    init_calls, check_calls = [], []
    monkeypatch.setattr(
        module.tbank_client,
        "create_payment",
        lambda *_args, **_kwargs: init_calls.append(1) or {"Success": True},
    )
    monkeypatch.setattr(
        module.tbank_client,
        "check_order",
        lambda order_id: check_calls.append(order_id) or {
            "Success": True, "OrderId": order_id, "Payments": []
        },
    )
    headers = {"Idempotency-Key": "demo-init-malformed"}
    with client:
        first = client.post("/api/orders", json=order_payload(), headers=headers)
        second = client.post("/api/orders", json=order_payload(), headers=headers)
        third = client.post("/api/orders", json=order_payload(), headers=headers)
    assert first.status_code == 502
    assert first.json()["detail"] == "Тестовая форма Т-Банка временно недоступна"
    assert second.status_code == 503
    assert third.status_code == 503
    assert init_calls == [1]
    assert len(check_calls) == 1
    with module.db() as con:
        stored = con.execute(
            "SELECT payment_state FROM orders"
        ).fetchone()
    assert stored["payment_state"] == "init_ambiguous"


def test_failed_init_replay_recovers_payment_url_via_check_order(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    init_calls, check_calls = [], []

    def failed_init(*_args, **_kwargs):
        init_calls.append(1)
        raise module.TBankError("Т-Банк временно недоступен")

    monkeypatch.setattr(module.tbank_client, "create_payment", failed_init)
    monkeypatch.setattr(
        module.tbank_client,
        "check_order",
        lambda order_id: check_calls.append(order_id) or {
            "Success": True,
            "OrderId": order_id,
            "Payments": [{
                "PaymentId": 7654321,
                "Status": "NEW",
                "Amount": 88_000,
                "PaymentURL": "https://pay.tbank.ru/recovered",
            }],
        },
    )
    headers = {"Idempotency-Key": "recover-through-check-order"}
    with client:
        first = client.post("/api/orders", json=order_payload(), headers=headers)
        second = client.post("/api/orders", json=order_payload(), headers=headers)

    assert first.status_code == 502
    assert second.status_code == 201
    assert second.json()["payment"] == {
        "mode": "tbank_demo",
        "url": "https://pay.tbank.ru/recovered",
        "reused": True,
    }
    assert init_calls == [1]
    assert check_calls == [second.json()["order"]["id"]]
    with module.db() as con:
        stored = con.execute(
            """SELECT provider_payment_id, payment_state, payment_attempts
               FROM orders"""
        ).fetchone()
    assert tuple(stored) == ("7654321", "awaiting", 1)


def test_failed_check_order_never_falls_back_to_second_init(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    init_calls, check_calls = [], []

    def failed_init(*_args, **_kwargs):
        init_calls.append(1)
        raise module.TBankError("Т-Банк временно недоступен")

    def failed_check(order_id):
        check_calls.append(order_id)
        raise module.TBankError("Т-Банк временно недоступен")

    monkeypatch.setattr(module.tbank_client, "create_payment", failed_init)
    monkeypatch.setattr(module.tbank_client, "check_order", failed_check)
    headers = {"Idempotency-Key": "check-order-transport-failure"}
    with client:
        first = client.post("/api/orders", json=order_payload(), headers=headers)
        replay = client.post("/api/orders", json=order_payload(), headers=headers)

    assert first.status_code == 502
    assert replay.status_code == 503
    assert init_calls == [1]
    assert len(check_calls) == 1
    with module.db() as con:
        stored = con.execute(
            "SELECT payment_state, provider_payment_id FROM orders"
        ).fetchone()
    assert tuple(stored) == ("init_ambiguous", None)


def test_stale_init_recovery_keeps_found_payment_ambiguous_without_url(
    tmp_path, monkeypatch
):
    client, module = demo_app(tmp_path, monkeypatch)
    init_calls = []

    def failed_init(*_args, **_kwargs):
        init_calls.append(1)
        raise module.TBankError("Т-Банк временно недоступен")

    monkeypatch.setattr(module.tbank_client, "create_payment", failed_init)
    headers = {"Idempotency-Key": "stale-init-found-no-url"}
    with client:
        first = client.post("/api/orders", json=order_payload(), headers=headers)
        with module.db() as con:
            order_id = con.execute("SELECT id FROM orders").fetchone()[0]
        stale = (
            module.datetime.now(module.timezone.utc)
            - module.timedelta(seconds=module.TBANK_INIT_LEASE_SECONDS + 1)
        ).isoformat()
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET payment_state = 'initializing',
                       payment_updated_at = ?
                   WHERE id = ?""",
                (stale, order_id),
            )
        monkeypatch.setattr(
            module.tbank_client,
            "check_order",
            lambda checked_order_id: {
                "Success": True,
                "OrderId": checked_order_id,
                "Payments": [{
                    "PaymentId": 887766,
                    "Status": "NEW",
                    "Amount": 88_000,
                }],
            },
        )
        replay = client.post("/api/orders", json=order_payload(), headers=headers)

    assert first.status_code == 502
    assert replay.status_code == 503
    assert init_calls == [1]
    stored = module.order_row(order_id)
    assert stored["provider_payment_id"] == "887766"
    assert stored["payment_state"] == "init_ambiguous"
    assert stored["payment_url"] is None


def test_live_init_failure_and_notification_have_no_test_wording(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET payment_provider = 'tbank', provider_payment_id = NULL,
                       payment_url = NULL, payment_state = 'initializing'
                   WHERE id = ?""",
                (created["id"],),
            )
        row = module.order_row(created["id"])
        monkeypatch.setattr(module, "TEST_MODE", False)
        notification = module.paid_notification(row)

        def fail_payment(*_args, **_kwargs):
            raise module.TBankError("provider unavailable")

        monkeypatch.setattr(module.tbank_client, "create_payment", fail_payment)
        with pytest.raises(module.HTTPException) as exc_info:
            module.initialize_tbank_payment(row, "ru")

    assert notification.startswith("💳 Новый заказ оплачен")
    assert "Тестовый заказ" not in notification
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Платёжная форма Т-Банка временно недоступна"


def test_signed_confirmed_callback_is_idempotent_and_redirect_is_not_proof(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    monkeypatch.setattr(module, "notify_owners", lambda row: sent.append(row["id"]))
    monkeypatch.setattr(module, "_telegram_notifications_enabled", lambda: True)
    with client:
        created = client.post("/api/orders", json=order_payload()).json()
        order_id = created["order"]["id"]
        with module.db() as con:
            row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

        # Merely opening the browser return URL cannot mark an order paid.
        returned = client.get(
            "/payment/success",
            params={"order_id": order_id, "token": row["payment_token"], "lang": "ru"},
        )
        assert returned.status_code == 200
        assert client.get(
            f"/api/orders/{order_id}", params={"token": row["payment_token"]}
        ).json()["payment_state"] == "awaiting"

        notice = signed_notification(module, order_id, "123456", 88_000, "CONFIRMED")
        first = client.post("/api/payments/tbank/notification", json=notice)
        second = client.post("/api/payments/tbank/notification", json=notice)
        current = client.get(
            f"/api/orders/{order_id}", params={"token": row["payment_token"]}
        ).json()

    assert first.status_code == 200 and first.text == "OK"
    assert second.status_code == 200 and second.text == "OK"
    assert current["status"] == "paid"
    assert current["payment_state"] == "paid"
    assert sent == [order_id]


def test_late_authorized_callback_does_not_replace_confirmed_status(
    tmp_path, monkeypatch
):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.tbank_client, "create_payment", lambda *_a, **_k: bank_response()
    )
    monkeypatch.setattr(module, "notify_owners", lambda _row: True)
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        confirmed = signed_notification(
            module, created["id"], "123456", 88_000, "CONFIRMED"
        )
        authorized = signed_notification(
            module, created["id"], "123456", 88_000, "AUTHORIZED"
        )

        assert client.post(
            "/api/payments/tbank/notification", json=confirmed
        ).text == "OK"
        assert client.post(
            "/api/payments/tbank/notification", json=authorized
        ).text == "OK"

        with module.db() as con:
            row = con.execute(
                """SELECT status, payment_state, payment_provider_status
                   FROM orders WHERE id = ?""",
                (created["id"],),
            ).fetchone()

    assert tuple(row) == ("paid", "paid", "CONFIRMED")


def test_paid_callback_replay_retries_durable_telegram_effect(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    deliveries = []
    outcomes = iter((False, True))
    monkeypatch.setattr(
        module.tbank_client, "create_payment", lambda *_a, **_k: bank_response()
    )
    monkeypatch.setattr(module, "_telegram_notifications_enabled", lambda: True)

    def notify(row):
        deliveries.append(row["id"])
        return next(outcomes)

    monkeypatch.setattr(module, "notify_owners", notify)
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        notice = signed_notification(
            module, created["id"], "123456", 88_000, "CONFIRMED"
        )
        first = client.post("/api/payments/tbank/notification", json=notice)
        with module.db() as con:
            failed = con.execute(
                """SELECT state, attempts FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'telegram'""",
                (created["id"],),
            ).fetchone()
        second = client.post("/api/payments/tbank/notification", json=notice)
        third = client.post("/api/payments/tbank/notification", json=notice)
        with module.db() as con:
            sent = con.execute(
                """SELECT state, attempts, completed_at
                   FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'telegram'""",
                (created["id"],),
            ).fetchone()

    assert first.status_code == second.status_code == third.status_code == 200
    assert tuple(failed) == ("failed", 1)
    assert sent["state"] == "sent"
    assert sent["attempts"] == 2
    assert sent["completed_at"]
    assert deliveries == [created["id"], created["id"]]


def test_restart_recovery_delivers_committed_pending_effect(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    delivered = []
    monkeypatch.setattr(
        module.tbank_client, "create_payment", lambda *_a, **_k: bank_response()
    )
    monkeypatch.setattr(module, "_telegram_notifications_enabled", lambda: True)
    monkeypatch.setattr(
        module, "notify_owners",
        lambda row: delivered.append(row["id"]) or True,
    )

    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        now = module.now_iso()
        # This is the durable state left if the process dies immediately after
        # committing CONFIRMED and before its BackgroundTask starts.
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'paid', payment_state = 'paid', paid_at = ?
                   WHERE id = ?""",
                (now, created["id"]),
            )
            module.enqueue_paid_order_effects(con, created["id"], now)
        module.recover_paid_order_effects()
        with module.db() as con:
            effect = con.execute(
                """SELECT state, attempts FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'telegram'""",
                (created["id"],),
            ).fetchone()

    assert tuple(effect) == ("sent", 1)
    assert delivered == [created["id"]]


def test_disabled_integrations_keep_outbox_pending_without_fake_success(
    tmp_path, monkeypatch
):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.tbank_client, "create_payment", lambda *_a, **_k: bank_response()
    )
    monkeypatch.setattr(module, "BOT_TOKEN", "")
    monkeypatch.setattr(module, "OWNER_CHAT_IDS", [])
    monkeypatch.setattr(module, "_saby_auto_sync_enabled", lambda: False)

    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        notice = signed_notification(
            module, created["id"], "123456", 88_000, "CONFIRMED"
        )
        response = client.post("/api/payments/tbank/notification", json=notice)
        with module.db() as con:
            effects = con.execute(
                """SELECT effect, state, attempts FROM paid_order_effects
                   WHERE order_id = ? ORDER BY effect""",
                (created["id"],),
            ).fetchall()

    assert response.status_code == 200
    assert [tuple(effect) for effect in effects] == [
        ("saby", "pending", 0),
        ("telegram", "pending", 0),
    ]


def test_paid_effect_recovery_retries_definite_saby_failure(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    saby_calls = []
    monkeypatch.setattr(
        module.tbank_client, "create_payment", lambda *_a, **_k: bank_response()
    )
    monkeypatch.setattr(module, "notify_owners", lambda _row: True)
    monkeypatch.setattr(module, "_saby_auto_sync_enabled", lambda: True)

    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        now = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'paid', payment_state = 'paid', paid_at = ?
                   WHERE id = ?""",
                (now, created["id"]),
            )
            module.enqueue_paid_order_effects(con, created["id"], now)

        def sync(order_id):
            saby_calls.append(order_id)
            next_state = "failed" if len(saby_calls) == 1 else "synced"
            with module.db() as con:
                con.execute(
                    "UPDATE orders SET saby_state = ? WHERE id = ?",
                    (next_state, order_id),
                )

        monkeypatch.setattr(module, "sync_paid_order_to_saby", sync)
        module.process_paid_order_effects(created["id"])
        with module.db() as con:
            failed = con.execute(
                """SELECT state, attempts FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (created["id"],),
            ).fetchone()
        module.recover_paid_order_effects()
        with module.db() as con:
            sent = con.execute(
                """SELECT state, attempts FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (created["id"],),
            ).fetchone()

    assert tuple(failed) == ("failed", 1)
    assert tuple(sent) == ("sent", 2)
    assert saby_calls == [created["id"], created["id"]]


def test_stale_saby_send_is_durable_ambiguous_not_retried(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.tbank_client, "create_payment", lambda *_a, **_k: bank_response()
    )
    monkeypatch.setattr(module, "notify_owners", lambda _row: True)
    monkeypatch.setattr(module, "_saby_auto_sync_enabled", lambda: True)
    monkeypatch.setattr(
        module,
        "sync_paid_order_to_saby",
        lambda _order_id: pytest.fail("ambiguous Saby write must not be replayed"),
    )

    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        now = module.now_iso()
        stale = (
            module.datetime.now(module.timezone.utc)
            - module.timedelta(seconds=module.PAID_EFFECT_LEASE_SECONDS + 1)
        ).isoformat()
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'paid', payment_state = 'paid', paid_at = ?,
                       saby_state = 'sending'
                   WHERE id = ?""",
                (now, created["id"]),
            )
            module.enqueue_paid_order_effects(con, created["id"], now)
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'sending', updated_at = ?
                   WHERE order_id = ? AND effect = 'saby'""",
                (stale, created["id"]),
            )
        module.recover_paid_order_effects()
        with module.db() as con:
            effect = con.execute(
                """SELECT state FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (created["id"],),
            ).fetchone()
        current = module.order_row(created["id"])

    assert effect["state"] == "ambiguous"
    assert current["saby_state"] == "ambiguous"


def test_terminal_fallback_result_urls_render_without_order_query(tmp_path, monkeypatch):
    client, _module = demo_app(tmp_path, monkeypatch)
    with client:
        success = client.get("/payment/success")
        failed = client.get("/payment/fail")
        incomplete = client.get("/payment/success", params={"order_id": "ONLY-ID"})

    assert success.status_code == 200
    assert failed.status_code == 200
    assert incomplete.status_code == 400
    assert "no-store" in success.headers["cache-control"]


def test_callback_rejects_bad_signature_amount_and_nonfinal_status(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        authorized = signed_notification(
            module, created["id"], "123456", 88_000, "AUTHORIZED"
        )
        assert client.post("/api/payments/tbank/notification", json=authorized).text == "OK"
        with module.db() as con:
            assert con.execute(
                "SELECT payment_state FROM orders WHERE id = ?", (created["id"],)
            ).fetchone()[0] == "awaiting"

        wrong_amount = signed_notification(
            module, created["id"], "123456", 1, "CONFIRMED"
        )
        assert client.post("/api/payments/tbank/notification", json=wrong_amount).status_code == 409
        wrong_amount["Token"] = "0" * 64
        assert client.post("/api/payments/tbank/notification", json=wrong_amount).status_code == 403
        with module.db() as con:
            assert con.execute(
                "SELECT paid_at FROM orders WHERE id = ?", (created["id"],)
            ).fetchone()[0] is None


def test_admin_reads_live_tbank_status_without_mutating_order(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    monkeypatch.setattr(module.tbank_client, "get_state", lambda _payment_id: {
        "Success": True,
        "Status": "CONFIRMED",
        "Amount": 88_000,
        "ErrorCode": "0",
    })
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        anonymous = client.get(f"/api/admin/orders/{created['id']}/tbank/status")
        status = client.get(
            f"/api/admin/orders/{created['id']}/tbank/status", headers=auth
        )
        current = module.admin_order(module.order_row(created["id"]))

    assert anonymous.status_code == 401
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json() == {
        "success": True,
        "provider_status": "CONFIRMED",
        "confirmed": True,
        "amount_matches": True,
        "amount_kopeks": 88_000,
        "expected_amount_kopeks": 88_000,
        "local_payment_state": "awaiting",
        "local_provider_status": "NEW",
    }
    assert current["status"] == "pending_payment"
    assert current["integrations"]["payment"]["state"] == "awaiting"


def test_admin_full_refund_calls_cancel_once(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    monkeypatch.setattr(module, "notify_owners", lambda _row: None)
    monkeypatch.setattr(
        module.tbank_client,
        "refund",
        lambda payment_id, **kwargs: calls.append((payment_id, kwargs)) or {
            "Success": True, "Status": "REFUNDED"
        },
    )
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        notice = signed_notification(module, created["id"], "123456", 88_000, "CONFIRMED")
        client.post("/api/payments/tbank/notification", json=notice)
        first = client.post(
            f"/api/admin/orders/{created['id']}/tbank/refund", headers=auth
        )
        second = client.post(
            f"/api/admin/orders/{created['id']}/tbank/refund", headers=auth
        )
        late_confirmation = client.post(
            "/api/payments/tbank/notification", json=notice
        )
        after_late_confirmation = module.admin_order(module.order_row(created["id"]))
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert first.json()["integrations"]["payment"]["state"] == "refunded"
    assert second.status_code == 200
    assert late_confirmation.status_code == 200
    assert after_late_confirmation["status"] == "cancelled"
    assert after_late_confirmation["integrations"]["payment"]["state"] == "refunded"
    assert calls == [("123456", {})]


def test_admin_cannot_fake_or_cancel_tbank_payment_state(tmp_path, monkeypatch):
    client, module = demo_app(tmp_path, monkeypatch)
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: bank_response())
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        fake_paid = client.patch(
            f"/api/admin/orders/{created['id']}", json={"status": "paid"}, headers=auth
        )
        early_cancel = client.patch(
            f"/api/admin/orders/{created['id']}", json={"status": "cancelled"}, headers=auth
        )
        client.post(
            "/api/payments/tbank/notification",
            json=signed_notification(module, created["id"], "123456", 88_000, "CONFIRMED"),
        )
        paid_cancel = client.patch(
            f"/api/admin/orders/{created['id']}", json={"status": "cancelled"}, headers=auth
        )
        current = module.admin_order(module.order_row(created["id"]))
    assert fake_paid.status_code == 409
    assert early_cancel.status_code == 409
    assert paid_cancel.status_code == 409
    assert current["status"] == "paid"
    assert current["integrations"]["payment"]["state"] == "paid"


def test_configured_receipt_is_sent_for_sale_but_not_full_refund(tmp_path, monkeypatch):
    monkeypatch.setenv("TBANK_RECEIPT_ENABLED", "1")
    monkeypatch.setenv("TBANK_RECEIPT_TAXATION", "usn_income")
    monkeypatch.setenv("TBANK_RECEIPT_ITEM_TAX", "none")
    monkeypatch.setenv("TBANK_RECEIPT_DELIVERY_TAX", "none")
    monkeypatch.setenv("TBANK_RECEIPT_FFD_VERSION", "1.2")
    client, module = demo_app(tmp_path, monkeypatch)
    sale_receipts, refund_receipts = [], []

    def create_payment(*_args, **kwargs):
        sale_receipts.append(kwargs["receipt"])
        return bank_response()

    def refund(_payment_id, **kwargs):
        refund_receipts.append(kwargs.get("receipt"))
        return {"Success": True, "Status": "REFUNDED"}

    monkeypatch.setattr(module.tbank_client, "create_payment", create_payment)
    monkeypatch.setattr(module.tbank_client, "refund", refund)
    monkeypatch.setattr(module, "notify_owners", lambda _row: None)
    with client:
        created = client.post("/api/orders", json=order_payload()).json()["order"]
        client.post(
            "/api/payments/tbank/notification",
            json=signed_notification(module, created["id"], "123456", 88_000, "CONFIRMED"),
        )
        response = client.post(
            f"/api/admin/orders/{created['id']}/tbank/refund",
            headers={"Authorization": "Bearer test-admin-token"},
        )
    assert response.status_code == 200
    assert len(sale_receipts) == 1
    assert refund_receipts == [None]
    assert sale_receipts[0]["Items"][0]["Amount"] == 88_000
    assert sale_receipts[0]["Items"][0]["Name"].endswith(" · 25 г")
    assert sale_receipts[0]["Items"][0]["MeasurementUnit"] == "шт"

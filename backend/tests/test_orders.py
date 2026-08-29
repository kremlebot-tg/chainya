import importlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend.cdek import CdekSettings
from backend.saby import SabySettings
from backend.saby_purchase import SabyFiscalSettings
from backend.tbank import TBankSettings
from backend.tbank_receipt import TBankReceiptSettings


def app_client(tmp_path, monkeypatch, *, test_mode="1"):
    for key in (
        "TBANK_TERMINAL_KEY", "TBANK_PASSWORD", "TBANK_NOTIFICATION_URL",
        "TBANK_SUCCESS_URL", "TBANK_FAIL_URL", "TBANK_CHECKOUT_MODE",
        "CDEK_CLIENT_ID", "CDEK_CLIENT_SECRET", "CDEK_INTEGRATION_MODE",
        "SABY_APP_CLIENT_ID", "SABY_APP_SECRET", "SABY_SECRET_KEY",
        "SABY_POINT_ID", "SABY_PRICE_LIST_ID", "SABY_ORDER_SYNC_MODE",
        "SABY_ORDER_SYNC_STARTED_AT", "SABY_PURCHASE_ROUTE",
        "SABY_OFD_COMPANY_ID", "SABY_OFD_KKT_REG_NUMBER",
        "SABY_OFD_TAX_SYSTEM", "SABY_OFD_PAY_METHOD",
        "SABY_OFD_ALLOW_NEGATIVE_STOCK", "TBANK_RECEIPT_ENABLED",
        "SABY_STOCK_GUARD_MODE", "SABY_AMBIGUOUS_CHECKOUT_POLICY",
        "SABY_CATALOG_SHADOW_MODE", "SABY_CATALOG_SHADOW_INTERVAL_SECONDS",
        "BOOKING_BOT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CHAINYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINYA_TEST_MODE", test_mode)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    import backend.app as module
    module = importlib.reload(module)
    return TestClient(module.app, base_url="https://chainya.ru"), module


def payload(**changes):
    data = {
        "items": [{"id": "baihao", "pack": 25, "qty": 2}],
        "delivery": "pickup",
        "payment_method": "sbp",
        "name": "Тест",
        "phone": "+7 999 123-45-67",
        "city": "", "address": "", "pvz_code": "", "note": "",
        "privacy_accepted": True,
    }
    data.update(changes)
    return data


def test_test_mode_parser_fails_closed(monkeypatch, tmp_path):
    _, module = app_client(tmp_path, monkeypatch)
    assert module.test_mode_from_value(None) is True
    assert module.test_mode_from_value("1") is True
    assert module.test_mode_from_value(" true ") is True
    assert module.test_mode_from_value("typo") is True
    assert module.test_mode_from_value("0") is False


def test_health_exposes_safe_release_version(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["version"] in {"development", "unknown"} or len(response.json()["version"]) == 12


def test_every_sellable_catalog_option_builds_an_exact_saby_fiscal_line(
    tmp_path, monkeypatch
):
    """Exercise the real checkout pricing rules against the complete live seed.

    This is deliberately an offline contract test: it proves that every product
    and pack currently offered by the storefront can be serialized without a
    rounding mismatch, but it never calls Saby or any other external service.
    """

    _client, module = app_client(tmp_path, monkeypatch)
    settings = SabyFiscalSettings(
        point_id="274",
        company_id="274",
        kkt_reg_number="0001234567890123",
        tax_system=2,
        pay_method=4,
    )
    catalog = module.load_catalog()
    packs = module.get_catalog_store().get()["packs"]
    expected_options = sum(
        1 if tea["unit"] == "pc" else len(packs)
        for tea in catalog.values()
        if tea.get("published") is not False and tea.get("stock") is not False
    )
    checked_options = 0

    for tea in catalog.values():
        if tea.get("published") is False or tea.get("stock") is False:
            continue
        options = ["pc"] if tea["unit"] == "pc" else packs
        for pack in options:
            request = module.CreateOrder(
                items=[{"id": tea["id"], "pack": pack, "qty": 1}],
                delivery="pickup",
                payment_method="sbp",
                name="Тест",
                phone="+7 999 000-00-00",
                privacy_accepted=True,
            )
            lines, subtotal = module.price_order(request)
            fiscal = module.build_fiscal_sale(
                {
                    "id": f"CATALOG{checked_options:04d}",
                    "total": subtotal,
                    "delivery_price": 0,
                    "customer": {
                        "name": request.name,
                        "phone": request.phone,
                    },
                    "items": lines,
                },
                settings=settings,
            )
            fiscal_line = fiscal["nomenclatures"][0]
            serialized_total = (
                Decimal(fiscal_line["priceNomenclature"])
                * Decimal(fiscal_line["quantityNomenclature"])
            ).quantize(Decimal("0.01"))
            assert serialized_total == Decimal(subtotal).quantize(Decimal("0.01"))
            assert Decimal(fiscal["internetSum"]) == Decimal(subtotal)
            assert Decimal(fiscal["vatNone"]) == Decimal(subtotal)
            checked_options += 1

    assert checked_options == expected_options
    assert checked_options > 0


def test_database_enforces_durable_effect_foreign_keys(tmp_path, monkeypatch):
    _client, module = app_client(tmp_path, monkeypatch)
    module.init_db()

    with module.db() as con:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """INSERT INTO paid_order_effects
                   (order_id, effect, state, attempts, last_error, updated_at)
                   VALUES ('missing-order', 'telegram', 'pending', 0, '', ?)""",
                (module.now_iso(),),
            )


def test_checkout_status_fails_closed_without_live_payment_mode(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch, test_mode="0")
    with client:
        response = client.get("/api/checkout/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"available": False, "provider": "tbank"}


def test_live_checkout_accepts_exactly_one_ready_fiscal_provider(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch, test_mode="0")
    module.init_db()
    monkeypatch.setenv("TBANK_CHECKOUT_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    monkeypatch.setenv("SABY_STOCK_GUARD_MODE", "auto")
    monkeypatch.setattr(module.tbank_client, "settings", TBankSettings(
        terminal_key="live-terminal", password="safe-password",
        notification_url="https://chainya.ru/api/payments/tbank/callback",
        success_url="https://chainya.ru/payment/success",
        fail_url="https://chainya.ru/payment/fail",
    ))
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
    ))
    monkeypatch.setattr(module, "tbank_receipt_settings", TBankReceiptSettings(enabled=False))
    monkeypatch.setattr(module, "saby_fiscal_settings", SabyFiscalSettings(
        point_id="274", company_id="274", kkt_reg_number="0001234567890123",
        tax_system=2, pay_method=4,
    ))
    assert module.tbank_checkout_ready() is True

    monkeypatch.setattr(module, "tbank_receipt_settings", TBankReceiptSettings(
        enabled=True, taxation="usn_income", item_tax="none",
        delivery_tax="none", ffd_version="1.05",
    ))
    assert module.tbank_checkout_ready() is False


def test_live_checkout_closes_while_saby_fiscal_result_is_ambiguous(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
    module.TEST_MODE = False
    monkeypatch.setenv("TBANK_CHECKOUT_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    monkeypatch.setenv("SABY_STOCK_GUARD_MODE", "auto")
    monkeypatch.setattr(module.tbank_client, "settings", TBankSettings(
        terminal_key="live-terminal", password="safe-password",
        notification_url="https://chainya.ru/api/payments/tbank/callback",
        success_url="https://chainya.ru/payment/success",
        fail_url="https://chainya.ru/payment/fail",
    ))
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
    ))
    monkeypatch.setattr(module, "tbank_receipt_settings", TBankReceiptSettings(enabled=False))
    monkeypatch.setattr(module, "saby_fiscal_settings", SabyFiscalSettings(
        point_id="274", company_id="274", kkt_reg_number="0001234567890123",
        tax_system=2, pay_method=4,
    ))
    with module.db() as con:
        con.execute(
            "UPDATE orders SET saby_receipt_state='ambiguous' WHERE id=?",
            (order["id"],),
        )
    assert module.tbank_checkout_ready() is False


def test_live_checkout_can_continue_with_explicit_manual_saby_reconciliation(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
    module.TEST_MODE = False
    monkeypatch.setenv("TBANK_CHECKOUT_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    monkeypatch.setenv("SABY_STOCK_GUARD_MODE", "auto")
    monkeypatch.setenv("SABY_AMBIGUOUS_CHECKOUT_POLICY", "manual")
    monkeypatch.setattr(module.tbank_client, "settings", TBankSettings(
        terminal_key="live-terminal", password="safe-password",
        notification_url="https://chainya.ru/api/payments/tbank/callback",
        success_url="https://chainya.ru/payment/success",
        fail_url="https://chainya.ru/payment/fail",
    ))
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
    ))
    monkeypatch.setattr(module, "tbank_receipt_settings", TBankReceiptSettings(enabled=False))
    monkeypatch.setattr(module, "saby_fiscal_settings", SabyFiscalSettings(
        point_id="274", company_id="274", kkt_reg_number="0001234567890123",
        tax_system=2, pay_method=4,
    ))
    with module.db() as con:
        con.execute(
            "UPDATE orders SET saby_receipt_state='ambiguous' WHERE id=?",
            (order["id"],),
        )
    assert module.tbank_checkout_ready() is True


def test_order_requires_nonempty_customer_name(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert client.post("/api/orders", json=payload(name="")).status_code == 422
        assert client.post("/api/orders", json=payload(name="   ")).status_code == 422


def test_invalid_ambiguous_checkout_policy_fails_closed(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch, test_mode="0")
    monkeypatch.setenv("TBANK_CHECKOUT_MODE", "auto")
    monkeypatch.setenv("SABY_AMBIGUOUS_CHECKOUT_POLICY", "unexpected")
    assert module.tbank_checkout_ready() is False


def test_service_pages_support_head_without_exposing_content(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        for path in ("/manage", "/manage/", "/manage/catalog", "/manage/site", "/manage/guides", "/manage/promos", "/admin/orders", "/account", "/account/", "/payment/success", "/payment/fail"):
            response = client.head(path)
            assert response.status_code == 200
            assert response.content == b""


def test_server_prices_order_and_mock_payment(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(module, "notify_owners", lambda row: sent.append(row["id"]))
    with client:
        response = client.post("/api/orders", json=payload())
        assert response.status_code == 201
        body = response.json()
        order = body["order"]
        assert order["subtotal"] == 2 * 440  # 175 ₽ / 10 г → 440 ₽ / 25 г
        assert order["total"] == 880
        assert order["status"] == "pending_payment"
        assert order["payment_state"] == "awaiting"
        payment_token = parse_qs(urlparse(body["payment"]["url"]).query)["token"][0]
        assert client.get(f"/api/orders/{order['id']}").status_code == 422
        assert client.get(f"/api/orders/{order['id']}", params={"token": "wrong"}).status_code == 403
        assert client.get(f"/api/orders/{order['id']}", params={"token": payment_token}).status_code == 200
        assert client.post(f"/api/orders/{order['id']}/test-pay", params={"token": "wrong"}).status_code == 403
        paid = client.post(f"/api/orders/{order['id']}/test-pay", params={"token": payment_token})
        assert paid.status_code == 200
        assert paid.json()["status"] == "paid"
        assert paid.json()["payment_state"] == "paid"
        assert sent == [order["id"]]
        assert client.post(f"/api/orders/{order['id']}/test-pay", params={"token": payment_token}).status_code == 200
        assert sent == [order["id"]]


def test_order_accepts_10_gram_pack_at_base_price(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        response = client.post(
            "/api/orders",
            json=payload(items=[{"id": "baihao", "pack": 10, "qty": 1}]),
        )
    assert response.status_code == 201
    assert response.json()["order"]["subtotal"] == 175


def test_order_creation_is_idempotent_for_network_retries(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    headers = {"Idempotency-Key": "checkout-018f6f07-7648-7d6b-a0b1-safe"}
    with client:
        first = client.post("/api/orders", json=payload(), headers=headers)
        replays = [
            client.post("/api/orders", json=payload(), headers=headers)
            for _ in range(15)
        ]
        conflict = client.post("/api/orders", json=payload(name="Другой"), headers=headers)
        invalid = client.post(
            "/api/orders", json=payload(), headers={"Idempotency-Key": "contains a space"}
        )
        with module.db() as con:
            rows = con.execute(
                "SELECT idempotency_key_hash, request_hash FROM orders"
            ).fetchall()
    assert first.status_code == 201
    assert all(response.status_code == 201 for response in replays)
    assert all(
        first.json()["order"]["id"] == response.json()["order"]["id"]
        for response in replays
    )
    assert all(
        first.json()["payment"]["url"] == response.json()["payment"]["url"]
        for response in replays
    )
    assert first.json()["payment"]["reused"] is False
    assert all(response.json()["payment"]["reused"] is True for response in replays)
    assert conflict.status_code == 409
    assert invalid.status_code == 422
    assert len(rows) == 1
    assert rows[0]["idempotency_key_hash"] != headers["Idempotency-Key"]
    assert len(rows[0]["idempotency_key_hash"]) == 64
    assert len(rows[0]["request_hash"]) == 64


def test_rejects_client_pack_for_piece_item(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        response = client.post("/api/orders", json=payload(items=[{"id": "mandarin", "pack": 25, "qty": 1}]))
        assert response.status_code == 422


def test_unconfigured_checkout_does_not_create_order(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "TEST_MODE", False)
    with client:
        response = client.post("/api/orders", json=payload())
        assert response.status_code == 503
        with module.db() as con:
            assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_requires_pvz_details(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        response = client.post("/api/orders", json=payload(delivery="cdek_pvz", city="Москва"))
        assert response.status_code == 422


def test_city_search_falls_back_to_local_prefix_index(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    module.CDEK_CITIES_PATH.write_text(json.dumps([
        {
            "city": "Санкт-Петербург",
            "region": "Санкт-Петербург",
            "country": "Россия",
            "population": 5_600_000,
        },
        {
            "city": "Москва",
            "region": "Москва",
            "country": "Россия",
            "population": 13_000_000,
        },
    ], ensure_ascii=False))
    def fake_cities(**params):
        if params.get("city") == "Санкт-Петербург":
            return [{
                "code": 137,
                "city": "Санкт-Петербург",
                "region": "Санкт-Петербург",
                "country": "Россия",
            }]
        return []
    monkeypatch.setattr(module.cdek_client, "cities", fake_cities)
    with client:
        response = client.get("/api/delivery/cities", params={"q": "санкт"})
    assert response.status_code == 200
    assert response.json()["cities"] == [{
        "code": 137,
        "city": "Санкт-Петербург",
        "region": "Санкт-Петербург",
        "country": "Россия",
    }]


def test_cdek_quote_and_order_use_server_price_and_selected_point(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.cdek_client,
        "settings",
        CdekSettings(client_id="client", client_secret="secret"),
    )
    monkeypatch.setattr(module.cdek_client, "calculate_tariff", lambda request: {
        "delivery_sum": 321.2,
        "period_min": 2,
        "period_max": 3,
        "tariff_name": "Посылка склад-склад",
    })
    monkeypatch.setattr(module.cdek_client, "delivery_points", lambda **params: [{
        "code": "SPB1",
        "name": "SPB1, Санкт-Петербург",
        "status": "ACTIVE",
        "is_handout": True,
        "work_time": "10:00–20:00",
        "location": {
            "city_code": 137,
            "address": "Невский проспект, 1",
            "longitude": 30.315868,
            "latitude": 59.939095,
        },
    }])
    request = {
        "items": [{"id": "baihao", "pack": 25, "qty": 2}],
        "method": "cdek_pvz",
        "city_code": 137,
    }
    with client:
        points = client.get("/api/delivery/points", params={"city_code": 137})
        quote = client.post("/api/delivery/quote", json=request)
        order = client.post("/api/orders", json=payload(
            delivery="cdek_pvz",
            city="Санкт-Петербург",
            city_code=137,
            pvz_code="SPB1",
        ))
    assert points.status_code == 200
    assert points.json()["points"][0]["longitude"] == 30.315868
    assert points.json()["points"][0]["latitude"] == 59.939095
    assert quote.status_code == 200
    assert quote.json()["price"] == 322
    assert quote.json()["tariff_code"] == 136
    assert order.status_code == 201
    assert order.json()["order"]["delivery_price"] == 322
    assert order.json()["order"]["total"] == 1202
    assert order.json()["order"]["delivery_quote"]["period_max"] == 3


def test_requires_privacy_consent(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert client.post("/api/orders", json=payload(privacy_accepted=False)).status_code == 422
        assert client.post("/api/business-leads", json={"contact": "@guest", "privacy_accepted": False}).status_code == 422


def test_business_lead_is_saved_and_notified(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(module, "notify_business_lead", lambda lead: sent.append(lead))
    with client:
        response = client.post("/api/business-leads", json={
            "company": "Кофейня Утро", "name": "Анна",
            "contact": "@anna", "note": "Нужно 2 кг в месяц", "privacy_accepted": True,
        })
        assert response.status_code == 202
        assert response.json()["accepted"] is True
        assert sent[0]["contact"] == "@anna"
        with module.db() as con:
            stored = con.execute("SELECT * FROM business_leads").fetchone()
        assert stored["company"] == "Кофейня Утро"


def test_business_lead_is_idempotent_before_rate_limit(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module, "notify_business_lead", lambda lead: sent.append(lead["id"])
    )
    lead = {
        "company": "Кофейня Утро",
        "name": "Анна",
        "contact": "@anna",
        "note": "Нужно 2 кг в месяц",
        "privacy_accepted": True,
    }
    headers = {"Idempotency-Key": "b2b-network-replay-1"}
    with client:
        first = client.post("/api/business-leads", json=lead, headers=headers)
        replays = [
            client.post("/api/business-leads", json=lead, headers=headers)
            for _ in range(12)
        ]
        conflict = client.post(
            "/api/business-leads",
            json={**lead, "note": "Другая заявка"},
            headers=headers,
        )
        with module.db() as con:
            rows = con.execute(
                """SELECT id, idempotency_key_hash, request_hash
                   FROM business_leads"""
            ).fetchall()

    assert first.status_code == 202
    assert all(response.status_code == 202 for response in replays)
    assert all(
        response.json()["id"] == first.json()["id"] for response in replays
    )
    assert conflict.status_code == 409
    assert len(rows) == 1
    assert len(rows[0]["idempotency_key_hash"]) == 64
    assert len(rows[0]["request_hash"]) == 64
    assert sent == [first.json()["id"]]


def test_admin_lists_and_updates_orders(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/orders", json=payload()).json()["order"]
        single_reservation_summary = module.admin_stock_reservation
        monkeypatch.setattr(
            module,
            "admin_stock_reservation",
            lambda *_args, **_kwargs: pytest.fail(
                "the order list must batch stock-reservation summaries"
            ),
        )
        assert client.get("/api/admin/orders").status_code == 401
        listing = client.get("/api/admin/orders", headers=auth)
        monkeypatch.setattr(
            module, "admin_stock_reservation", single_reservation_summary
        )
        assert listing.status_code == 200
        assert listing.json()["orders"][0]["customer"]["phone"] == "+7 999 123-45-67"
        integrations = listing.json()["orders"][0]["integrations"]
        assert integrations["payment"]["provider"] == "test"
        assert integrations["payment"]["state"] == "awaiting"
        assert integrations["saby"]["state"] == "not_queued"
        assert integrations["cdek"]["state"] == "not_requested"
        assert listing.json()["total"] == 1
        blocked = client.patch(
            f"/api/admin/orders/{created['id']}", headers=auth, json={"status": "confirmed"}
        )
        assert blocked.status_code == 409
        paid = client.patch(f"/api/admin/orders/{created['id']}", headers=auth, json={"status": "paid"})
        assert paid.status_code == 200
        confirmed = client.patch(
            f"/api/admin/orders/{created['id']}", headers=auth, json={"status": "confirmed"}
        )
        assert confirmed.json()["status"] == "confirmed"


def test_legacy_orders_receive_safe_integration_states(tmp_path, monkeypatch):
    database = sqlite3.connect(tmp_path / "orders.sqlite3")
    database.execute(
        """CREATE TABLE orders (
             id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL, subtotal INTEGER NOT NULL,
             delivery_price INTEGER NOT NULL, total INTEGER NOT NULL,
             payment_method TEXT NOT NULL, delivery TEXT NOT NULL,
             customer_json TEXT NOT NULL, items_json TEXT NOT NULL,
             provider_payment_id TEXT, payment_token TEXT, paid_at TEXT
           )"""
    )
    common = (
        "2026-07-20T10:00:00+00:00", "2026-07-20T10:05:00+00:00",
        100, 0, 100, "sbp", "pickup", json.dumps({"phone": "+79990000000"}), "[]",
    )
    database.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("MOCKPAID", "paid", *common, "mock_123", "token-1", common[1]),
    )
    database.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("MANUALPAID", "confirmed", *common, None, "token-2", common[1]),
    )
    database.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("PENDING", "pending_payment", *common, None, "token-3", None),
    )
    database.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("CANCELLEDPAID", "cancelled", *common, "mock_456", "token-4", common[1]),
    )
    database.commit()
    database.close()

    client, module = app_client(tmp_path, monkeypatch)
    with client:
        with module.db() as con:
            rows = {
                row["id"]: row
                for row in con.execute(
                    """SELECT id, payment_provider, payment_state,
                              paid_effects_enqueued, saby_state,
                              saby_receipt_state, saby_settlement_state,
                              saby_refund_state, cdek_state
                       FROM orders"""
                ).fetchall()
            }
            effects = con.execute(
                "SELECT order_id, effect FROM paid_order_effects"
            ).fetchall()
            indexes = {
                row["name"] for row in con.execute("PRAGMA index_list(orders)")
            }
    assert (rows["MOCKPAID"]["payment_provider"], rows["MOCKPAID"]["payment_state"]) == ("test", "paid")
    assert (rows["MANUALPAID"]["payment_provider"], rows["MANUALPAID"]["payment_state"]) == ("manual", "paid")
    assert (rows["PENDING"]["payment_provider"], rows["PENDING"]["payment_state"]) == ("test", "awaiting")
    assert (rows["CANCELLEDPAID"]["payment_provider"], rows["CANCELLEDPAID"]["payment_state"]) == ("test", "paid")
    assert rows["PENDING"]["paid_effects_enqueued"] == 0
    assert all(rows[order_id]["paid_effects_enqueued"] == 1 for order_id in (
        "MOCKPAID", "MANUALPAID", "CANCELLEDPAID",
    ))
    assert all(rows[order_id]["saby_state"] == "not_queued" for order_id in rows)
    assert all(rows[order_id]["saby_receipt_state"] == "not_requested" for order_id in rows)
    assert all(rows[order_id]["saby_settlement_state"] == "not_requested" for order_id in rows)
    assert all(rows[order_id]["saby_refund_state"] == "not_requested" for order_id in rows)
    assert all(rows[order_id]["cdek_state"] == "not_requested" for order_id in rows)
    assert effects == []
    assert {
        "idx_orders_saby_receipt",
        "idx_orders_saby_settlement_receipt",
        "idx_orders_saby_refund_receipt",
    }.issubset(indexes)


def test_admin_lists_and_updates_business_leads(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "notify_business_lead", lambda lead: None)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/business-leads", json={
            "company": "Ресторан", "name": "Илья", "contact": "@ilya",
            "note": "Нужна дегустация", "privacy_accepted": True,
        }).json()
        listing = client.get("/api/admin/business-leads", headers=auth)
        assert listing.status_code == 200
        assert listing.json()["leads"][0]["status"] == "new"
        assert listing.json()["total"] == 1
        updated = client.patch(
            f"/api/admin/business-leads/{created['id']}", headers=auth, json={"status": "contacted"}
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "contacted"


def test_admin_records_support_search_filters_and_pagination(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "notify_business_lead", lambda lead: None)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        for name in ("Анна", "Борис", "Вера"):
            assert client.post("/api/orders", json=payload(name=name)).status_code == 201
        page = client.get(
            "/api/admin/orders", params={"limit": 1, "offset": 1}, headers=auth
        ).json()
        assert page["total"] == 3
        assert len(page["orders"]) == 1
        search = client.get("/api/admin/orders", params={"q": "анна"}, headers=auth).json()
        assert search["total"] == 1
        assert search["orders"][0]["customer"]["name"] == "Анна"

        for company in ("Чайный дом", "Ресторан"):
            client.post("/api/business-leads", json={
                "company": company, "name": "Илья", "contact": "@ilya",
                "note": "Запрос", "privacy_accepted": True,
            })
        leads = client.get(
            "/api/admin/business-leads", params={"q": "чайный"}, headers=auth
        ).json()
        assert leads["total"] == 1
        assert leads["leads"][0]["company"] == "Чайный дом"


def test_admin_reports_saby_configuration(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        assert client.get("/api/admin/saby/status").status_code == 401
        response = client.get("/api/admin/saby/status", headers=auth)
        assert response.status_code == 200
        assert response.json()["configured"] is False


def test_admin_reports_secret_free_integration_readiness(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        assert client.get("/api/admin/integrations/status").status_code == 401
        response = client.get("/api/admin/integrations/status", headers=auth)
    assert response.status_code == 200
    result = response.json()
    assert result["guard"] == {
        "test_mode": True,
        "external_writes_locked": True,
        "demo_writes_enabled": False,
        "workflow_exposed": True,
        "exposed_providers": ["cdek", "saby", "tbank"],
    }
    assert result["tbank"]["adapter_ready"] is True
    assert result["tbank"]["mode"] == "off"
    assert result["tbank"]["writes_enabled"] is False
    assert result["saby"]["mapping_valid"] is True
    assert result["saby"]["mapping_items"] == 29
    assert result["saby"]["writes_enabled"] is False
    assert result["saby"]["purchase_route"] == {
        "route": "delivery", "valid": True, "implemented": True,
        "writes_enabled": True, "blockers": [],
    }
    assert result["cdek"]["adapter_ready"] is True
    assert result["cdek"]["writes_enabled"] is False
    serialized = json.dumps(result)
    assert "app_secret" not in serialized
    assert "client_secret" not in serialized
    assert "password" not in serialized


def test_integration_preview_is_network_free_and_builds_pickup_saby_payload(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
        point_id=274, price_list_id=7,
    ))
    monkeypatch.setattr(module.tbank_client, "create_payment", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("preview must not call T-Bank")
    ))
    monkeypatch.setattr(module.saby_client, "create_delivery_order", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("preview must not call Saby")
    ))
    monkeypatch.setattr(module.cdek_client, "create_order", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("preview must not call CDEK")
    ))
    ready_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        assert client.patch(
            f"/api/admin/orders/{order['id']}", headers=auth, json={"status": "paid"}
        ).status_code == 200
        response = client.get(
            f"/api/admin/orders/{order['id']}/integration-preview",
            params={"ready_at": ready_at}, headers=auth,
        )
    assert response.status_code == 200
    result = response.json()
    assert result["external_writes_locked"] is True
    assert result["payment"]["amount_kopeks"] == 88_000
    assert result["payment"]["network_called"] is False
    assert result["saby"]["ready"] is True
    assert result["saby"]["payload"]["pointId"] == 274
    assert result["saby"]["payload"]["priceListId"] == 7
    assert result["saby"]["payload"]["nomenclatures"][0]["id"] == 39
    assert len(result["saby"]["payload_sha256"]) == 64
    assert result["cdek"]["required"] is False


def test_saby_preview_rejects_unpaid_order_and_past_ready_time(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
        point_id=274, price_list_id=7,
    ))
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        unpaid = client.get(
            f"/api/admin/orders/{order['id']}/integration-preview",
            params={"ready_at": "2020-01-01 12:00:00"}, headers=auth,
        ).json()
    assert unpaid["saby"]["ready"] is False
    assert "Заказ ещё не оплачен" in unpaid["saby"]["blockers"]
    assert "Плановое время готовности должно быть в будущем" in unpaid["saby"]["blockers"]
    assert "payload" not in unpaid["saby"]


def test_admin_saby_test_does_not_require_delivery_for_cdek_flow(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-readiness"}
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })
    monkeypatch.setattr(module.saby_client, "sales_points", lambda product="retail": {
        "salesPoints": [{"id": 274, "name": "Чайня"}] if product == "retail" else {},
    })
    monkeypatch.setattr(module.saby_client, "delivery_calendar", lambda point_id=None: {})
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: [
        {"id": 7, "name": "Сайт chainya.ru"},
    ])
    monkeypatch.setattr(module.saby_client, "catalog_all", lambda with_balance=False: [
        {**item, "balance": 100}
        for item in matching_saby_catalog(module)
    ] + [{
        "id": 59, "name": "Чон Ши Ча", "cost": 350, "balance": 0,
        "externalId": "9003e2a3-bbd8-4353-85f7-b2e901781ec8",
    }])
    with client:
        response = client.post("/api/admin/saby/test", headers=action)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["state"] == "ready"
    assert result["connected"] is True
    assert result["point_found"] is True
    assert result["price_list_found"] is True
    assert result["catalog_items"] == 30
    assert result["priced_items"] == 30
    assert result["in_stock_items"] == 28
    assert result["catalog_mapping_valid"] is True
    assert result["zero_balance_items"] == []
    assert result["warnings"] == [
        "В Saby есть скрытые на сайте позиции: 1",
        "Кассовая регистрация не проверяется этим read-only тестом",
    ]
    assert result["delivery_configured"] is False
    assert result["delivery_confirmation"] == "not_required_cdek"
    assert result["retail_catalog_ready"] is True
    assert result["write_probe_performed"] is False
    assert result["fiscal_registration_confirmed"] is False
    assert result["ready_for_orders"] is False
    assert result["blockers"] == []


def test_admin_saby_readiness_reports_separate_ofd_auth_without_writing(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    action = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-readiness",
    }
    monkeypatch.setattr(module, "saby_fiscal_settings", SabyFiscalSettings(
        point_id="274", company_id="274",
        kkt_reg_number="0001234567890123", pay_method=4,
    ))
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })
    monkeypatch.setattr(module.saby_client, "sales_points", lambda product="retail": {
        "salesPoints": [{"id": 274, "name": "Чайня"}],
    })
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: {
        "priceLists": [{"id": 7, "name": "Сайт chainya.ru"}],
    })
    monkeypatch.setattr(
        module.saby_client, "catalog_all",
        lambda with_balance=False: matching_saby_catalog(module),
    )
    monkeypatch.setattr(module.saby_client, "companies", lambda: {
        "companies": [{"id": 274, "inn": "1234567890"}],
    })

    with client:
        response = client.post("/api/admin/saby/test", headers=action)

    assert response.status_code == 200
    result = response.json()
    assert result["fiscal_probe"] == {
        "state": "separate_auth_required",
        "company_found": True,
        "kkt_found": False,
        "read_only": True,
        "detail": (
            "Retail API видит компанию, но список ККТ требует отдельную "
            "OFD-авторизацию; эта проверка не подтверждает кассу"
        ),
    }
    assert result["write_probe_performed"] is False
    assert result["fiscal_registration_confirmed"] is False
    assert "отдельную OFD-авторизацию" in " ".join(result["warnings"])


def test_admin_saby_readiness_does_not_expose_company_identifier(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    action = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-readiness",
    }
    monkeypatch.setattr(module, "saby_fiscal_settings", SabyFiscalSettings(
        point_id="274", company_id="274",
        kkt_reg_number="0001234567890123", pay_method=4,
    ))
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })
    monkeypatch.setattr(module.saby_client, "sales_points", lambda product="retail": {
        "salesPoints": [{"id": 274, "name": "Чайня"}],
    })
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: {
        "priceLists": [{"id": 7, "name": "Сайт chainya.ru"}],
    })
    monkeypatch.setattr(
        module.saby_client, "catalog_all",
        lambda with_balance=False: matching_saby_catalog(module),
    )
    monkeypatch.setattr(module.saby_client, "companies", lambda: {
        "companies": [{"id": 274, "inn": "1234567890"}],
    })

    with client:
        response = client.post("/api/admin/saby/test", headers=action)

    assert response.status_code == 200
    result = response.json()
    assert result["fiscal_probe"] == {
        "state": "separate_auth_required",
        "company_found": True,
        "kkt_found": False,
        "read_only": True,
        "detail": (
            "Retail API видит компанию, но список ККТ требует отдельную "
            "OFD-авторизацию; эта проверка не подтверждает кассу"
        ),
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "1234567890" not in serialized
    assert "0001234567890123" not in serialized
    assert result["write_probe_performed"] is False
    assert result["fiscal_registration_confirmed"] is False


def test_saby_readiness_rejects_unknown_balance_and_external_id_mismatch(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-readiness"}
    refs = list(module.SABY_NOMENCLATURE_BY_SITE_ID.values())
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })
    monkeypatch.setattr(module.saby_client, "sales_points", lambda product="retail": {
        "salesPoints": [{"id": 274, "name": "Чайня"}],
    })
    monkeypatch.setattr(module.saby_client, "delivery_calendar", lambda point_id=None: {})
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: {
        "priceLists": [{"id": 7, "name": "Сайт chainya.ru"}],
    })
    catalog = [
        {"id": index, "name": f"Чай {index}", "cost": 10,
         "balance": 100, "externalId": ref.external_id}
        for index, ref in enumerate(refs)
    ]
    catalog[0]["externalId"] = "00000000-0000-0000-0000-000000000000"
    catalog[1]["balance"] = "100"
    monkeypatch.setattr(module.saby_client, "catalog_all", lambda with_balance=False: catalog)
    with client:
        result = client.post("/api/admin/saby/test", headers=action).json()
    assert result["state"] == "blocked"
    assert result["ready_for_orders"] is False
    assert result["catalog_mapping_valid"] is False
    assert result["in_stock_items"] == 26
    assert result["unknown_balance_items"] == [{"id": 1, "name": "Бай Му Дань"}]
    assert "Каталог сайта не совпадает" in " ".join(result["blockers"])
    assert "не вернул числовой остаток" in " ".join(result["blockers"])


def test_admin_saby_readiness_requires_intent_and_is_rate_limited(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-readiness"}
    monkeypatch.setattr(
        module.saby_client,
        "sales_points",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing local configuration must not call Saby")
        ),
    )

    with client:
        assert client.post(
            "/api/admin/saby/test",
            headers={"X-Chainya-Admin": "saby-readiness"},
        ).status_code == 401
        assert client.post("/api/admin/saby/test", headers=auth).status_code == 403
        assert client.post(
            "/api/admin/saby/test",
            headers={**action, "Origin": "https://attacker.invalid"},
        ).status_code == 403
        responses = [
            client.post("/api/admin/saby/test", headers=action)
            for _ in range(module.SABY_READINESS_LIMIT)
        ]
        limited = client.post("/api/admin/saby/test", headers=action)

    assert all(response.status_code == 200 for response in responses)
    assert all(response.headers["cache-control"] == "no-store" for response in responses)
    assert responses[0].json()["state"] == "blocked"
    assert limited.status_code == 429


def test_admin_saby_readiness_rejects_a_concurrent_probe(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-readiness",
    }
    monkeypatch.setattr(module, "rate_limit", lambda *_args, **_kwargs: None)

    with module._saby_readiness_lock, client:
        response = client.post("/api/admin/saby/test", headers=headers)

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == "Проверка Saby уже выполняется"


def matching_saby_catalog(module):
    site = module.load_catalog()
    return [
        {
            "id": ref.id,
            "externalId": ref.external_id,
            "name": site[site_id]["name"],
            "unit": "шт" if site[site_id]["unit"] == "pc" else "г",
            "cost": (
                site[site_id]["price"]
                if site[site_id]["unit"] == "pc"
                else site[site_id]["price"] / 10
            ),
            "balance": 1000 if site[site_id].get("stock", True) else 0,
            "published": True,
        }
        for site_id, ref in module.SABY_NOMENCLATURE_BY_SITE_ID.items()
    ]


def test_saby_readiness_ignores_delivery_product_for_fiscal_sale(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-readiness"}
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })
    monkeypatch.setattr(module.saby_client, "sales_points", lambda product="retail": {
        "salesPoints": [{"id": 274, "name": "Чайня"}],
    })
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: {
        "priceLists": [{"id": 7, "name": "Сайт chainya.ru"}],
    })
    monkeypatch.setattr(
        module.saby_client,
        "catalog_all",
        lambda with_balance=False: matching_saby_catalog(module),
    )

    with client:
        result = client.post("/api/admin/saby/test", headers=action).json()

    assert result["state"] == "ready"
    assert result["delivery_configured"] is False
    assert result["delivery_confirmation"] == "not_required_cdek"
    assert result["retail_catalog_ready"] is True
    assert result["ready_for_orders"] is False
    assert result["blockers"] == []
    assert result["errors"] == {}


def test_saby_readiness_does_not_call_irrelevant_delivery_api(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-readiness"}
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })

    def sales_points(product="retail"):
        assert product == "retail"
        return {"salesPoints": [{"id": 274, "name": "Чайня"}]}

    monkeypatch.setattr(module.saby_client, "sales_points", sales_points)
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: {
        "priceLists": [{"id": 7, "name": "Сайт chainya.ru"}],
    })
    monkeypatch.setattr(
        module.saby_client,
        "catalog_all",
        lambda with_balance=False: matching_saby_catalog(module),
    )

    with client:
        result = client.post("/api/admin/saby/test", headers=action).json()

    assert result["state"] == "ready"
    assert result["delivery_configured"] is False
    assert result["retail_catalog_ready"] is True
    assert result["ready_for_orders"] is False
    assert result["blockers"] == []
    assert result["errors"] == {}


def test_saby_readiness_never_calls_delivery_calendar(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-readiness"}
    monkeypatch.setattr(module.saby_client, "configuration", lambda: {
        "configured": True, "point_id": 274, "price_list_id": 7, "missing": [],
    })
    monkeypatch.setattr(module.saby_client, "sales_points", lambda product="retail": {
        "salesPoints": [{"id": 274, "name": "Чайня"}] if product == "retail" else {},
    })
    monkeypatch.setattr(
        module.saby_client,
        "delivery_calendar",
        lambda point_id=None: (_ for _ in ()).throw(
            AssertionError("calendar is not proof of Delivery registration")
        ),
    )
    monkeypatch.setattr(module.saby_client, "price_lists", lambda: {
        "priceLists": [{"id": 7, "name": "Сайт chainya.ru"}],
    })
    monkeypatch.setattr(
        module.saby_client, "catalog_all",
        lambda with_balance=False: matching_saby_catalog(module),
    )

    with client:
        result = client.post("/api/admin/saby/test", headers=action).json()

    assert result["state"] == "ready"
    assert result["delivery_configured"] is False
    assert result["delivery_confirmation"] == "not_required_cdek"
    assert result["retail_catalog_ready"] is True
    assert result["ready_for_orders"] is False
    assert result["blockers"] == []


def test_admin_saby_shadow_is_read_only_persistent_and_protected(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    action = {**auth, "X-Chainya-Admin": "saby-shadow", "Origin": "https://chainya.ru"}
    with client:
        assert client.get("/api/admin/saby/catalog-shadow").status_code == 401
        empty = client.get("/api/admin/saby/catalog-shadow", headers=auth)
        assert empty.status_code == 200
        assert empty.headers["cache-control"] == "no-store"
        assert empty.json()["latest"] is None
        assert empty.json()["read_only"] is True
        assert empty.json()["writes_enabled"] is False
        assert client.post(
            "/api/admin/saby/catalog-shadow/run", headers=auth
        ).status_code == 403
        assert client.post(
            "/api/admin/saby/catalog-shadow/run",
            headers={**action, "Origin": "https://attacker.invalid"},
        ).status_code == 403

        monkeypatch.setattr(
            module.saby_client, "catalog_all",
            lambda with_balance=False: matching_saby_catalog(module),
        )
        monkeypatch.setattr(
            module.saby_client, "base_catalog_all",
            lambda with_balance=False: matching_saby_catalog(module),
        )
        monkeypatch.setattr(
            module.saby_client, "configuration",
            lambda: {"configured": True, "point_id": 274, "price_list_id": 7},
        )
        monkeypatch.setattr(
            module.saby_client, "create_delivery_order",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Saby write called")),
        )
        before = module.CATALOG_PATH.read_bytes()
        revision = module.get_catalog_store().get()["revision"]
        response = client.post("/api/admin/saby/catalog-shadow/run", headers=action)
        after = module.CATALOG_PATH.read_bytes()

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["status"] == "ok"  # three intentionally inactive site items are informational
        assert result["report"]["counts"]["actionable_differences"] == 0
        assert result["report"]["counts"]["info"] == 3
        assert result["report"]["catalog_changed"] is False
        assert result["report"]["source"]["site_revision"] == revision
        assert len(result["report"]["source"]["site_sha256"]) == 64
        assert before == after
        assert module.get_catalog_store().get()["revision"] == revision

        stored = client.get("/api/admin/saby/catalog-shadow", headers=auth).json()
        assert stored["latest"]["id"] == result["id"]
        assert stored["history"][0]["report"]["read_only"] is True


def test_admin_can_acknowledge_only_the_current_exact_saby_difference(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    run_headers = {
        **auth,
        "X-Chainya-Admin": "saby-shadow",
        "Origin": "https://chainya.ru",
    }
    ack_headers = {
        **auth,
        "X-Chainya-Admin": "saby-shadow-ack",
        "Origin": "https://chainya.ru",
    }
    catalog = matching_saby_catalog(module)
    catalog[0]["cost"] = float(catalog[0]["cost"]) + 10
    monkeypatch.setattr(
        module.saby_client, "catalog_all", lambda with_balance=False: catalog
    )
    monkeypatch.setattr(
        module.saby_client,
        "base_catalog_all",
        lambda with_balance=False: matching_saby_catalog(module),
    )
    monkeypatch.setattr(
        module.saby_client,
        "configuration",
        lambda: {"configured": True, "point_id": 274, "price_list_id": 7},
    )

    with client:
        assert client.post(
            "/api/admin/saby/catalog-shadow/run", headers=run_headers
        ).status_code == 200
        report = client.get(
            "/api/admin/saby/catalog-shadow", headers=auth
        ).json()["latest"]["report"]
        difference = next(
            item for item in report["differences"] if item["severity"] != "info"
        )
        assert difference["acknowledged"] is False
        assert report["counts"]["unacknowledged_actionable_differences"] == 1

        payload = {"fingerprint": difference["fingerprint"], "acknowledged": True}
        assert client.post(
            "/api/admin/saby/catalog-shadow/acknowledge",
            headers=auth,
            json=payload,
        ).status_code == 403
        acknowledged = client.post(
            "/api/admin/saby/catalog-shadow/acknowledge",
            headers=ack_headers,
            json=payload,
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json() == {"ok": True, "acknowledged": True}

        report = client.get(
            "/api/admin/saby/catalog-shadow", headers=auth
        ).json()["latest"]["report"]
        assert report["counts"]["unacknowledged_actionable_differences"] == 0
        assert report["counts"]["acknowledged_actionable_differences"] == 1
        assert next(
            item for item in report["differences"] if item["fingerprint"] == payload["fingerprint"]
        )["acknowledged"] is True

        assert client.post(
            "/api/admin/saby/catalog-shadow/acknowledge",
            headers=ack_headers,
            json={"fingerprint": "0" * 64, "acknowledged": True},
        ).status_code == 409
        restored = client.post(
            "/api/admin/saby/catalog-shadow/acknowledge",
            headers=ack_headers,
            json={**payload, "acknowledged": False},
        )
        assert restored.status_code == 200
        assert restored.json() == {"ok": True, "acknowledged": False}
        report = client.get(
            "/api/admin/saby/catalog-shadow", headers=auth
        ).json()["latest"]["report"]
        assert report["counts"]["unacknowledged_actionable_differences"] == 1


def test_admin_shadow_ui_states_read_only_guarantee_without_apply_action(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch)
    html = (module.ROOT / "backend" / "admin.html").read_text(encoding="utf-8")
    assert "Теневая сверка каталога" in html
    assert "Только чтение. Сайт, касса, цены, остатки и заказы не изменяются." in html
    assert "Сравнить сейчас" in html
    assert 'id="saby-attention"' in html
    assert "Каталог требует проверки" in html
    assert "сайт и СБИС по-разному оценивают одно и то же количество товара" in html
    assert "Сверка подтвердила одинаковую цену" in html
    assert "saby_sale_quantum_inferred:'Фасовка'" in html
    assert "проверьте цену вместе с единицей продажи: грамм или упаковка" in html
    assert "Сверьте фактический остаток в СБИС" in html
    assert "Информационные отличия" in html
    assert "document.visibilityState==='visible'" in html
    assert "Скрыть как проверенное" in html
    assert "Вернуть в активные" in html
    assert "Эта кнопка не исправляет и не синхронизирует данные." in html
    assert "saby-shadow-ack" in html
    assert "Применить изменения Saby" not in html
    assert 'id="saby-shadow-status"' in html
    assert 'id="saby-shadow-content" aria-live=' not in html


def test_admin_uses_readable_typography_scale(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch)
    html = (module.ROOT / "backend" / "admin.html").read_text(encoding="utf-8")
    assert '<body class="admin-readable">' in html
    assert ".admin-readable{font-size:16px;line-height:1.55}" in html
    assert ".admin-readable .saby-alert__title{font-size:20px" in html


def test_admin_shows_every_saby_fiscal_receipt_and_its_incident(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch)
    html = (module.ROOT / "backend" / "admin.html").read_text(encoding="utf-8")
    assert "fiscalLabel('Чек Saby',saby.receipt)" in html
    assert "fiscalLabel('Окончательный чек',saby.settlement_receipt)" in html
    assert "fiscalLabel('Чек возврата',saby.refund_receipt)" in html
    assert "sabyReceiptWarning('Чек продажи Saby',sabyIntegration.receipt)" in html
    assert "sabyReceiptWarning('Окончательный чек Saby',sabyIntegration.settlement_receipt)" in html
    assert "sabyReceiptWarning('Чек возврата Saby',sabyIntegration.refund_receipt)" in html
    assert "Автоповтор запрещён" in html
    assert "Запрос в Saby ещё не отправлялся" in html
    assert "Saby мог принять операцию" in html
    assert "Saby не вернул идентификатор чека" in html
    assert "проверить его через API по кнопке нельзя" in html
    assert "Идентификатор чека Saby сохранён" in html
    assert "Состояние зафиксировано" in html
    assert ".admin-readable .saby-alert__item{padding:5px 8px;font-size:12px" in html
    assert "@media(max-width:760px){.admin-readable{font-size:16px}" in html


def test_admin_uses_refined_responsive_header(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch)
    html = (module.ROOT / "backend" / "admin.html").read_text(encoding="utf-8")
    assert '<header class="topbar topbar--refined">' in html
    assert "grid-template-columns:repeat(6,minmax(76px,1fr))" in html
    assert 'href="/manage/guides">Гайды</a>' in html
    assert ".topbar--refined .nav__count{display:none}" in html
    assert ".topbar--refined .nav__button[aria-selected=true]" in html


def test_admin_does_not_claim_saby_fiscal_success_without_confirmation(
    tmp_path, monkeypatch
):
    _, module = app_client(tmp_path, monkeypatch)
    html = (module.ROOT / "backend" / "admin.html").read_text(encoding="utf-8")
    guides = (module.ROOT / "backend" / "admin-guides.html").read_text(
        encoding="utf-8"
    )
    assert "saby_fiscal_incidents" in html
    assert "saby_fiscal_prewrite_blocked" in html
    assert "saby_fiscal_retrying" in html
    assert "Оплаты получены, чеки Saby требуют сверки" in html
    assert "Автоповтор запрещён" in html
    assert "Остановлено до настройки" in html
    assert "Безопасный повтор" in html
    assert "Кассовая продажа · готова" not in html
    assert "Saby готов принимать заказы" not in html
    assert "Кассовая продажа · маршрут включён" in html
    assert "Отправка после оплаты · включена" in html
    assert "Успех подтверждается только настоящим фискальным признаком" in guides
    assert "контур нельзя считать полностью проверенным" in guides


def test_saby_shadow_persists_safe_errors_recovers_stale_run_and_limits_history(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    from backend.saby import SabyError

    action = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-shadow",
        "Origin": "https://chainya.ru",
    }
    monkeypatch.setattr(module, "rate_limit", lambda *_args, **_kwargs: None)
    with client:
        with module.db() as con:
            con.execute(
                """INSERT INTO saby_shadow_runs
                   (started_at, trigger, status, report_json, error)
                   VALUES (?, 'scheduler', 'running', '{}', '')""",
                (module.now_iso(),),
            )
        monkeypatch.setattr(
            module.saby_client, "catalog_all",
            lambda with_balance=False: (_ for _ in ()).throw(SabyError("Saby временно недоступен")),
        )
        failed = client.post("/api/admin/saby/catalog-shadow/run", headers=action)
        assert failed.status_code == 200
        assert failed.json()["status"] == "error"
        assert failed.json()["error"] == "Saby временно недоступен"
        with module.db() as con:
            interrupted = con.execute(
                "SELECT status, error FROM saby_shadow_runs ORDER BY id ASC LIMIT 1"
            ).fetchone()
        assert interrupted["status"] == "error"
        assert "перезапуском" in interrupted["error"]

        monkeypatch.setattr(module, "SABY_SHADOW_RETENTION_RUNS", 3)
        monkeypatch.setattr(
            module.saby_client, "catalog_all",
            lambda with_balance=False: matching_saby_catalog(module),
        )
        monkeypatch.setattr(
            module.saby_client, "base_catalog_all",
            lambda with_balance=False: matching_saby_catalog(module),
        )
        for _ in range(4):
            assert client.post(
                "/api/admin/saby/catalog-shadow/run", headers=action
            ).status_code == 200
        with module.db() as con:
            assert con.execute("SELECT COUNT(*) FROM saby_shadow_runs").fetchone()[0] == 3


def test_saby_shadow_manual_run_returns_conflict_while_busy(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)

    class BusyLock:
        def acquire(self, blocking=False):
            assert blocking is False
            return False

        def release(self):
            raise AssertionError("unacquired lock must not be released")

    monkeypatch.setattr(module, "_saby_shadow_lock", BusyLock())
    with client:
        response = client.post(
            "/api/admin/saby/catalog-shadow/run",
            headers={
                "Authorization": "Bearer test-admin-token",
                "X-Chainya-Admin": "saby-shadow",
                "Origin": "https://chainya.ru",
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "Сравнение Saby уже выполняется"


def test_saby_shadow_manual_run_is_rate_limited(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "run_saby_shadow_check",
        lambda trigger: {"trigger": trigger, "status": "ok", "report": {}},
    )
    action = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-shadow",
        "Origin": "https://chainya.ru",
    }
    with client:
        for _ in range(module.SABY_SHADOW_MANUAL_LIMIT):
            assert client.post(
                "/api/admin/saby/catalog-shadow/run", headers=action
            ).status_code == 200
        limited = client.post(
            "/api/admin/saby/catalog-shadow/run", headers=action
        )
    assert limited.status_code == 429


def test_saby_shadow_worker_runs_immediately_and_stops_cleanly(tmp_path, monkeypatch):
    _, module = app_client(tmp_path, monkeypatch)
    stop = module.threading.Event()
    triggers = []

    def run_once(trigger):
        triggers.append(trigger)
        stop.set()
        return {"status": "ok"}

    monkeypatch.setattr(module, "run_saby_shadow_check", run_once)

    module.saby_shadow_worker(stop, 300)

    assert triggers == ["scheduler"]


def test_paid_order_is_sent_to_saby_once_in_auto_mode(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
        point_id=274, price_list_id=7,
    ))
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_delivery_order",
        lambda data: sent.append(data) or {"externalId": "saby-order-123"},
    )
    monkeypatch.setattr(module.saby_client, "sales_point_enabled", lambda product: True)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        monkeypatch.setattr(module, "TEST_MODE", False)
        monkeypatch.setattr(
            module,
            "integration_writer",
            module.IntegrationWriter(
                test_mode=False, exposed_providers=frozenset({"tbank", "saby", "cdek"})
            ),
        )
        with module.db() as con:
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (module.now_iso(), module.now_iso(), order["id"]),
            )
        module.sync_paid_order_to_saby(order["id"])
        module.sync_paid_order_to_saby(order["id"])
        current = module.admin_order(module.order_row(order["id"]))

    assert len(sent) == 1
    assert sent[0]["delivery"] == {
        "isPickup": True,
        "paymentType": "online",
        "shopURL": "https://chainya.ru",
        "successURL": "https://chainya.ru/payment/success",
        "errorURL": "https://chainya.ru/payment/fail",
    }
    assert current["integrations"]["saby"]["state"] == "synced"
    assert current["integrations"]["saby"]["external_id"] == "saby-order-123"
    assert current["integrations"]["saby"]["attempts"] == 1


def test_paid_order_is_blocked_when_point_has_no_saby_delivery(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
        point_id=274, price_list_id=7,
    ))
    monkeypatch.setattr(module.saby_client, "sales_point_enabled", lambda product: False)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_delivery_order",
        lambda data: sent.append(data) or {"externalId": "must-not-exist"},
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        monkeypatch.setattr(module, "TEST_MODE", False)
        monkeypatch.setattr(
            module,
            "integration_writer",
            module.IntegrationWriter(
                test_mode=False, exposed_providers=frozenset({"tbank", "saby", "cdek"})
            ),
        )
        paid_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
            module.enqueue_paid_order_effects(con, order["id"], paid_at)

        module.process_paid_order_effects(order["id"])
        module.recover_paid_order_effects()
        current = module.admin_order(module.order_row(order["id"]))
        with module.db() as con:
            effect = con.execute(
                """SELECT state, attempts FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            ).fetchone()

    assert sent == []
    assert current["integrations"]["saby"]["state"] == "blocked"
    assert "Delivery" in current["integrations"]["saby"]["last_error"]
    assert effect["state"] == "blocked"
    assert effect["attempts"] == 1


def test_non_delivery_purchase_route_never_falls_back_to_delivery(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_delivery_order",
        lambda data: sent.append(data) or {"externalId": "must-not-exist"},
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        monkeypatch.setattr(module, "TEST_MODE", False)
        monkeypatch.setattr(
            module,
            "integration_writer",
            module.IntegrationWriter(
                test_mode=False, exposed_providers=frozenset({"tbank", "saby", "cdek"})
            ),
        )
        with module.db() as con:
            paid_at = module.now_iso()
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
        module.sync_paid_order_to_saby(order["id"])

    assert sent == []
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "blocked"
    assert "сервисная авторизация" in current["saby_receipt_last_error"]


def test_paid_effect_preserves_actual_purchase_route_blocker(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        monkeypatch.setattr(module, "TEST_MODE", False)
        paid_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
            module.enqueue_paid_order_effects(con, order["id"], paid_at)
        module.process_paid_order_effects(order["id"])
        with module.db() as con:
            effect = con.execute(
                """SELECT state, last_error FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            ).fetchone()

    assert effect["state"] == "blocked"
    assert "сервисная авторизация" in effect["last_error"]
    assert "Delivery" not in effect["last_error"]


def test_worker_resumes_prewrite_fiscal_block_after_configuration_is_fixed(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "resumed-safe-receipt"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])

        module.process_paid_order_effects(order["id"])
        blocked = module.order_row(order["id"])
        assert blocked["saby_receipt_state"] == "blocked"
        assert blocked["saby_receipt_id"] is None
        assert blocked["saby_receipt_payload_hash"] is None

        configure_saby_fiscal_flow(module, monkeypatch)
        module.recover_paid_order_effects()

    current = module.order_row(order["id"])
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert len(sent) == 1
    assert current["saby_receipt_state"] == "registered"
    assert current["saby_receipt_id"] == "resumed-safe-receipt"
    assert effect["state"] == "sent"
    assert effect["attempts"] == 2


def test_worker_never_requeues_blocked_fiscal_sale_with_persisted_payload(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'blocked',
                       saby_receipt_payload_hash = 'provider-bound-payload'
                   WHERE id = ?""",
                (order["id"],),
            )
            con.execute(
                """UPDATE paid_order_effects SET state = 'blocked'
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            )
        module.recover_paid_order_effects()

    assert sent == []
    assert module.order_row(order["id"])["saby_receipt_state"] == "blocked"


def test_safe_fiscal_requeue_updates_only_exact_eligible_outbox_row(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)

    with client:
        eligible = client.post("/api/orders", json=payload()).json()["order"]
        unrelated = client.post(
            "/api/orders",
            json={**payload(), "phone": "+7 999 111-22-34"},
        ).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, eligible["id"])
        mark_order_paid_and_enqueue(module, unrelated["id"])
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'blocked'
                   WHERE id = ?""",
                (eligible["id"],),
            )
            con.execute(
                """UPDATE paid_order_effects SET state = 'blocked'
                   WHERE order_id = ? AND effect = 'saby'""",
                (eligible["id"],),
            )
            con.execute(
                """UPDATE orders
                   SET payment_state = 'failed', saby_receipt_state = 'failed'
                   WHERE id = ?""",
                (unrelated["id"],),
            )
            con.execute(
                """UPDATE paid_order_effects SET state = 'blocked'
                   WHERE order_id = ? AND effect = 'saby'""",
                (unrelated["id"],),
            )
            resumed = module.requeue_safe_blocked_saby_sales(con)
            effects = {
                row["order_id"]: row["state"]
                for row in con.execute(
                    """SELECT order_id, state FROM paid_order_effects
                       WHERE effect = 'saby' AND order_id IN (?, ?)""",
                    (eligible["id"], unrelated["id"]),
                ).fetchall()
            }

    assert resumed == 1
    assert effects[eligible["id"]] == "failed"
    assert effects[unrelated["id"]] == "blocked"


def test_bound_fiscal_blocker_survives_later_route_change(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "delivery")
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        paid_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'paid', payment_state = 'paid', paid_at = ?,
                       saby_state = 'failed', saby_last_error = 'ошибка Delivery',
                       saby_receipt_state = 'blocked',
                       saby_receipt_last_error = 'проверить фискальную кассу',
                       updated_at = ?
                   WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
            module.enqueue_paid_order_effects(con, order["id"], paid_at)

        module.process_paid_order_effects(order["id"])
        with module.db() as con:
            effect = con.execute(
                """SELECT state, attempts, last_error FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            ).fetchone()

    assert effect["state"] == "blocked"
    assert effect["attempts"] == 1
    assert effect["last_error"] == "проверить фискальную кассу"


def configure_saby_fiscal_flow(module, monkeypatch):
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_PURCHASE_ROUTE", "fiscal_sale")
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
    ))
    monkeypatch.setattr(module, "tbank_receipt_settings", TBankReceiptSettings(enabled=False))
    monkeypatch.setattr(module, "saby_fiscal_settings", SabyFiscalSettings(
        point_id="274", company_id="274", kkt_reg_number="0001234567890123",
        tax_system=2, pay_method=4,
    ))
    monkeypatch.setattr(
        module.saby_client, "fiscal_receipt",
        lambda receipt_id: [{"id": receipt_id, "fiscalSign": "1234567890"}],
    )
    monkeypatch.setattr(
        module.saby_client, "base_catalog_all",
        lambda *, with_balance: matching_saby_catalog(module),
    )


def expose_live_saby_writer(module, monkeypatch):
    monkeypatch.setattr(module, "TEST_MODE", False)
    monkeypatch.setattr(
        module,
        "integration_writer",
        module.IntegrationWriter(
            test_mode=False, exposed_providers=frozenset({"tbank", "saby", "cdek"})
        ),
    )


def mark_order_paid_and_enqueue(module, order_id):
    paid_at = module.now_iso()
    with module.db() as con:
        con.execute(
            """UPDATE orders SET status = 'paid', payment_state = 'paid',
                   paid_at = ?, updated_at = ? WHERE id = ?""",
            (paid_at, paid_at, order_id),
        )
        module.enqueue_paid_order_effects(con, order_id, paid_at)


def test_fiscal_sale_is_sent_once_for_repeated_paid_effect_processing(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "safe-receipt-1"},
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        module.process_paid_order_effects(order["id"])

    assert len(sent) == 1
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "registered"
    assert current["saby_receipt_id"] == "safe-receipt-1"
    with module.db() as con:
        effect = con.execute(
            "SELECT state FROM paid_order_effects WHERE order_id = ? AND effect = 'saby'",
            (order["id"],),
        ).fetchone()
    assert effect["state"] == "sent"


def test_fiscal_transport_error_is_ambiguous_and_never_blindly_retried(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    attempts = []

    def lose_response(data):
        attempts.append(data)
        raise module.SabyError("Saby временно недоступен")

    monkeypatch.setattr(module.saby_client, "create_fiscal_sale", lose_response)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        module.process_paid_order_effects(order["id"])

    assert len(attempts) == 1
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "ambiguous"


def test_restart_preserves_provider_bound_ambiguous_sale_without_retry(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'ambiguous',
                       saby_receipt_id = NULL,
                       saby_receipt_payload_hash = 'provider-bound-payload'
                   WHERE id = ?""",
                (order["id"],),
            )
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'ambiguous', attempts = 1
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            )

        module.recover_paid_order_effects()
        module.process_paid_order_effects(order["id"])

    assert sent == []
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "ambiguous"
    assert current["saby_receipt_payload_hash"] == "provider-bound-payload"
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert tuple(effect) == ("ambiguous", 1)


def test_fiscal_http_401_after_post_is_ambiguous_and_never_retried(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    attempts = []

    def unauthorized_after_post(data):
        attempts.append(data)
        raise module.SabyError("Saby вернул HTTP 401")

    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale", unauthorized_after_post
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        module.process_paid_order_effects(order["id"])

    assert len(attempts) == 1
    assert module.order_row(order["id"])["saby_receipt_state"] == "ambiguous"


def test_fiscal_oauth_error_is_retryable_without_marking_sale_ambiguous(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    attempts = []

    def reject_before_retail_request(data):
        attempts.append(data)
        raise module.SabyAuthenticationError("Saby OAuth временно недоступен")

    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale", reject_before_retail_request
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])

    assert len(attempts) == 1
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "failed"
    assert "OAuth" in current["saby_receipt_last_error"]


def test_fiscal_sale_uses_canonical_saby_name_when_public_name_differs(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    catalog = matching_saby_catalog(module)
    catalog[0] = {**catalog[0], "name": "Несовпадающее название"}
    monkeypatch.setattr(
        module.saby_client, "base_catalog_all",
        lambda *, with_balance: catalog,
    )
    sent = []
    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])

    assert len(sent) == 1
    assert sent[0]["nomenclatures"][0]["nameNomenclature"] == "Несовпадающее название"
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "registered"


def test_paid_delivery_blocks_before_post_without_unique_saby_service(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        with module.db() as con:
            con.execute(
                "UPDATE orders SET delivery_price = 100, total = total + 100 "
                "WHERE id = ?",
                (order["id"],),
            )
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])

    assert sent == []
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "failed"
    assert "Доставка" in current["saby_receipt_last_error"]


def test_owner_cannot_retry_ambiguous_sale_even_after_claimed_absence(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    attempts = []

    def send(data):
        attempts.append(data)
        raise module.SabyError("Saby вернул HTTP 500")

    monkeypatch.setattr(module.saby_client, "create_fiscal_sale", send)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        auth = {"Authorization": f"Bearer {module.ADMIN_TOKEN}"}
        refused = client.post(
            f"/api/admin/orders/{order['id']}/saby/retry-sale", headers=auth
        )
        assert refused.status_code == 409
        retried = client.post(
            f"/api/admin/orders/{order['id']}/saby/retry-sale",
            headers={**auth, "X-Chainya-Saby-Receipt-Absence": "confirmed"},
        )
        assert retried.status_code == 409
        assert "Повторная отправка" in retried.json()["detail"]

    assert len(attempts) == 1
    assert module.order_row(order["id"])["saby_receipt_state"] == "ambiguous"


def test_owner_cannot_retry_registered_or_unpaid_saby_receipt(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    auth = {
        "Authorization": f"Bearer {module.ADMIN_TOKEN}",
        "X-Chainya-Saby-Receipt-Absence": "confirmed",
    }
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        assert client.post(
            f"/api/admin/orders/{order['id']}/saby/retry-sale", headers=auth
        ).status_code == 409
        mark_order_paid_and_enqueue(module, order["id"])
        with module.db() as con:
            con.execute(
                "UPDATE orders SET saby_receipt_state='registered', saby_receipt_id='existing' WHERE id=?",
                (order["id"],),
            )
        assert client.post(
            f"/api/admin/orders/{order['id']}/saby/retry-sale", headers=auth
        ).status_code == 409


def test_owner_cannot_repeat_saby_receipt_at_all(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)

    def fail(_data):
        raise module.SabyError("Saby вернул HTTP 500")

    monkeypatch.setattr(module.saby_client, "create_fiscal_sale", fail)
    auth = {
        "Authorization": f"Bearer {module.ADMIN_TOKEN}",
        "X-Chainya-Saby-Receipt-Absence": "confirmed",
    }
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        first_retry = client.post(
            f"/api/admin/orders/{order['id']}/saby/retry-sale", headers=auth
        )
        assert first_retry.status_code == 409
        second_retry = client.post(
            f"/api/admin/orders/{order['id']}/saby/retry-sale", headers=auth
        )
        assert second_retry.status_code == 409
        assert "Повторная отправка" in second_retry.json()["detail"]

    with module.db() as con:
        effect = con.execute(
            "SELECT attempts FROM paid_order_effects WHERE order_id=? AND effect='saby'",
            (order["id"],),
        ).fetchone()
    assert effect["attempts"] == 1


def test_accepted_sale_waits_for_ofd_and_polls_without_second_sale(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    checks = iter((
        [{"id": "safe-receipt-pending", "fiscalSign": "none", "state": "новая"}],
        [{"id": "safe-receipt-pending", "fiscalSign": "1234567890", "state": "готова"}],
    ))
    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "safe-receipt-pending"},
    )
    delivery_sent = []
    monkeypatch.setattr(
        module.saby_client, "create_delivery_order",
        lambda data: delivery_sent.append(data) or {"externalId": "wrong-route"},
    )
    monkeypatch.setattr(module.saby_client, "fiscal_receipt", lambda _id: next(checks))
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        assert module.order_row(order["id"])["saby_receipt_state"] == "pending_ofd"
        # A later deploy may change the route for new orders.  This accepted
        # payId must remain bound to fiscal GET polling and never become a
        # Delivery write.
        monkeypatch.setenv("SABY_PURCHASE_ROUTE", "delivery")
        module.process_paid_order_effects(order["id"])

    assert len(sent) == 1
    assert delivery_sent == []
    assert module.order_row(order["id"])["saby_receipt_state"] == "registered"


def test_accepted_sale_keeps_polling_after_rollout_cutoff_changes(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    checks = iter((
        [{"id": "accepted-before-cutoff", "fiscalSign": "none", "state": "новая"}],
        [{
            "id": "accepted-before-cutoff",
            "fiscalSign": "1234567890",
            "state": "готова",
        }],
    ))
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "accepted-before-cutoff"},
    )
    monkeypatch.setattr(module.saby_client, "fiscal_receipt", lambda _id: next(checks))

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        assert module.order_row(order["id"])["saby_receipt_state"] == "pending_ofd"

        monkeypatch.setenv(
            "SABY_ORDER_SYNC_STARTED_AT", "2099-01-01T00:00:00+00:00"
        )
        module.process_paid_order_effects(order["id"])

    assert len(sent) == 1
    assert module.order_row(order["id"])["saby_receipt_state"] == "registered"
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert tuple(effect) == ("sent", 2)


def test_concurrent_paid_effect_workers_create_only_one_fiscal_sale(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    post_started = threading.Event()
    release_post = threading.Event()
    sent = []

    def create_once(data):
        sent.append(data)
        post_started.set()
        assert release_post.wait(timeout=3)
        return {"id": "safe-concurrent-receipt"}

    monkeypatch.setattr(module.saby_client, "create_fiscal_sale", create_once)
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: [
            {"id": receipt_id, "fiscalSign": "1234567890", "state": "готова"}
        ],
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        first = threading.Thread(
            target=module.process_paid_order_effects, args=(order["id"],)
        )
        second = threading.Thread(
            target=module.process_paid_order_effects, args=(order["id"],)
        )
        first.start()
        assert post_started.wait(timeout=3)
        second.start()
        second.join(timeout=3)
        release_post.set()
        first.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(sent) == 1
    assert module.order_row(order["id"])["saby_receipt_state"] == "registered"


def test_pending_ofd_stops_after_finite_deadline_without_second_sale(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    monkeypatch.setattr(module, "SABY_OFD_PENDING_MAX_SECONDS", 0)
    sent = []
    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "safe-receipt-timeout"},
    )
    monkeypatch.setattr(
        module.saby_client, "fiscal_receipt",
        lambda receipt_id: [{"id": receipt_id, "fiscalSign": "none", "state": "новая"}],
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        assert module.order_row(order["id"])["saby_receipt_state"] == "pending_ofd"
        module.process_paid_order_effects(order["id"])

    current = module.order_row(order["id"])
    assert len(sent) == 1
    assert current["saby_receipt_state"] == "blocked"
    assert "без повторной продажи" in current["saby_receipt_last_error"]


def test_pending_ofd_get_errors_stop_after_deadline_without_second_sale(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )

    def unavailable(_receipt_id):
        raise module.SabyError("временная ошибка чтения")

    monkeypatch.setattr(module.saby_client, "fiscal_receipt", unavailable)

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        expired = "2000-01-01T00:00:00+00:00"
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'pending_ofd',
                       saby_receipt_id = 'accepted-before-get-errors',
                       saby_receipt_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (expired, expired, order["id"]),
            )

        module.process_paid_order_effects(order["id"])
        module.recover_paid_order_effects()

    current = module.order_row(order["id"])
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert sent == []
    assert current["saby_receipt_state"] == "blocked"
    assert "без повторной продажи" in current["saby_receipt_last_error"]
    assert effect["state"] == "blocked"
    assert effect["attempts"] == 1


def test_worker_restart_marks_interrupted_fiscal_post_ambiguous_without_retry(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )
    delivery_sent = []
    monkeypatch.setattr(
        module.saby_client, "create_delivery_order",
        lambda data: delivery_sent.append(data) or {"externalId": "wrong-route"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        stale = "2000-01-01T00:00:00+00:00"
        with module.db() as con:
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'sending', attempts = 1, updated_at = ?
                   WHERE order_id = ? AND effect = 'saby'""",
                (stale, order["id"]),
            )
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'sending',
                       saby_receipt_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (stale, stale, order["id"]),
            )

        monkeypatch.setenv("SABY_PURCHASE_ROUTE", "delivery")
        module.recover_paid_order_effects()

    current = module.order_row(order["id"])
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert sent == []
    assert delivery_sent == []
    assert current["saby_receipt_state"] == "ambiguous"
    assert effect["state"] == "ambiguous"
    assert effect["attempts"] == 1


def test_worker_restart_resumes_pending_receipt_by_get_without_second_sale(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )
    checked = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: checked.append(receipt_id) or [
            {"id": receipt_id, "fiscalSign": "1234567890", "state": "готова"}
        ],
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        stale = "2000-01-01T00:00:00+00:00"
        with module.db() as con:
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'sending', attempts = 1, updated_at = ?
                   WHERE order_id = ? AND effect = 'saby'""",
                (stale, order["id"]),
            )
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'pending_ofd',
                       saby_receipt_id = 'accepted-before-restart',
                       saby_receipt_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (stale, stale, order["id"]),
            )

        module.recover_paid_order_effects()

    current = module.order_row(order["id"])
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert sent == []
    assert checked == ["accepted-before-restart"]
    assert current["saby_receipt_state"] == "registered"
    assert effect["state"] == "sent"
    assert effect["attempts"] == 2


def test_saby_pending_fiscal_sign_markers_are_not_registered(tmp_path, monkeypatch):
    _client, module = app_client(tmp_path, monkeypatch)
    for marker in (None, "", "   ", "none", "NONE", "null", "Null", False, 0):
        assert not module._saby_receipt_is_fiscalized([
            {"fiscalSign": marker, "state": "новая"}
        ])
    assert module._saby_receipt_is_fiscalized([
        {"fiscalSign": "1234567890", "state": "готова"}
    ])


def test_saby_receipt_status_must_match_requested_id(tmp_path, monkeypatch):
    _client, module = app_client(tmp_path, monkeypatch)
    response = [
        {"id": "other-receipt", "fiscalSign": "1234567890", "state": "готова"},
        {"id": "expected-receipt", "fiscalSign": "none", "state": "новая"},
    ]

    assert not module._saby_receipt_is_fiscalized_for_id(
        response, "expected-receipt"
    )
    assert module._saby_receipt_is_fiscalized_for_id(response, "other-receipt")


def test_owner_check_reads_known_pending_receipt_without_second_sale(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    checked = []
    created = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: checked.append(receipt_id) or [
            {"id": receipt_id, "fiscalSign": "none", "state": "новая"}
        ],
    )
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: created.append(data) or {"id": "unexpected"},
    )
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        mark_order_paid_and_enqueue(module, order["id"])
        pending_since = "2026-08-01T10:00:00+00:00"
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'pending_ofd',
                       saby_receipt_id = 'known-pending-receipt',
                       saby_receipt_updated_at = ?
                   WHERE id = ?""",
                (pending_since, order["id"]),
            )
            con.execute(
                """UPDATE paid_order_effects SET state = 'blocked'
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            )
        response = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["found"] is True
    assert response.json()["confirmed"] is False
    assert response.json()["state"] == "pending_ofd"
    assert checked == ["known-pending-receipt"]
    assert created == []
    current = module.order_row(order["id"])
    assert current["saby_receipt_updated_at"] == pending_since
    with module.db() as con:
        effect = con.execute(
            """SELECT state FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert effect["state"] == "failed"


def test_owner_check_confirms_exact_blocked_receipt_without_second_sale(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    created = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: [
            {"id": "other", "fiscalSign": "1234567890", "state": "готова"},
            {"id": receipt_id, "fiscalSign": "9876543210", "state": "готова"},
        ],
    )
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: created.append(data) or {"id": "unexpected"},
    )
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        mark_order_paid_and_enqueue(module, order["id"])
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'blocked',
                       saby_receipt_id = 'known-blocked-receipt',
                       saby_receipt_updated_at = '2026-08-01T10:00:00+00:00'
                   WHERE id = ?""",
                (order["id"],),
            )
            con.execute(
                """UPDATE paid_order_effects SET state = 'blocked'
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            )
        response = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["confirmed"] is True
    assert response.json()["state"] == "registered"
    assert created == []
    with module.db() as con:
        effect = con.execute(
            """SELECT state FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert effect["state"] == "sent"


def test_owner_check_queues_one_return_when_refunded_sale_is_confirmed(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    created = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: [
            {"id": receipt_id, "fiscalSign": "9876543210", "state": "готова"}
        ],
    )
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: created.append(data) or {"id": "unexpected"},
    )
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        mark_order_paid_and_enqueue(module, order["id"])
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'cancelled', payment_state = 'refunded',
                       saby_receipt_state = 'blocked',
                       saby_receipt_id = 'known-refunded-sale'
                   WHERE id = ?""",
                (order["id"],),
            )
            con.execute(
                """UPDATE paid_order_effects SET state = 'blocked'
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            )

        first = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )
        # Simulate a historical deployment that recorded the confirmed sale
        # but crashed before persisting the matching refund effect.
        with module.db() as con:
            con.execute(
                """DELETE FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby_refund'""",
                (order["id"],),
            )
        second = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )

    assert first.status_code == 200
    assert first.json()["state"] == "registered"
    assert second.status_code == 200
    assert created == []
    with module.db() as con:
        effects = con.execute(
            """SELECT effect, state FROM paid_order_effects
               WHERE order_id = ? AND effect IN ('saby', 'saby_refund')
               ORDER BY effect""",
            (order["id"],),
        ).fetchall()
    assert [(row["effect"], row["state"]) for row in effects] == [
        ("saby", "sent"),
        ("saby_refund", "pending"),
    ]


def test_owner_check_fails_closed_without_receipt_id_or_action_header(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    checked = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: checked.append(receipt_id) or [],
    )
    auth = {"Authorization": "Bearer test-admin-token"}

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        with module.db() as con:
            con.execute(
                "UPDATE orders SET saby_receipt_state = 'ambiguous' WHERE id = ?",
                (order["id"],),
            )
        missing_header = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=auth,
        )
        no_identifier = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers={**auth, "X-Chainya-Admin": "saby-receipt-check"},
        )

    assert missing_header.status_code == 403
    assert no_identifier.status_code == 409
    assert "Повторная продажа запрещена" in no_identifier.json()["detail"]
    assert checked == []


def test_owner_check_does_not_accept_a_different_receipt(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda _receipt_id: [
            {"id": "different", "fiscalSign": "1234567890", "state": "готова"}
        ],
    )
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'ambiguous',
                       saby_receipt_id = 'expected'
                   WHERE id = ?""",
                (order["id"],),
            )
        response = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["found"] is False
    assert response.json()["confirmed"] is False
    assert response.json()["state"] == "ambiguous"
    assert module.order_row(order["id"])["saby_receipt_state"] == "ambiguous"


def test_owner_check_keeps_expired_receipt_blocked_when_sign_is_still_missing(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: [
            {"id": receipt_id, "fiscalSign": "none", "state": "новая"}
        ],
    )
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        blocked_since = "2026-08-01T10:00:00+00:00"
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'blocked',
                       saby_receipt_id = 'expired-receipt',
                       saby_receipt_updated_at = ?
                   WHERE id = ?""",
                (blocked_since, order["id"]),
            )
        response = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["found"] is True
    assert response.json()["confirmed"] is False
    assert response.json()["state"] == "blocked"
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "blocked"
    assert current["saby_receipt_updated_at"] == blocked_since


def test_owner_check_does_not_downgrade_concurrently_blocked_receipt(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET saby_receipt_state = 'pending_ofd',
                       saby_receipt_id = 'race-receipt',
                       saby_receipt_updated_at = '2026-08-01T10:00:00+00:00'
                   WHERE id = ?""",
                (order["id"],),
            )
            con.execute(
                """INSERT INTO paid_order_effects
                   (order_id, effect, state, attempts, last_error,
                    updated_at, completed_at)
                   VALUES (?, 'saby', 'blocked', 1, '', ?, NULL)""",
                (order["id"], module.now_iso()),
            )

        def block_while_checking(receipt_id):
            with module.db() as con:
                con.execute(
                    """UPDATE orders SET saby_receipt_state = 'blocked'
                       WHERE id = ?""",
                    (order["id"],),
                )
            return [{"id": receipt_id, "fiscalSign": "none", "state": "новая"}]

        monkeypatch.setattr(module.saby_client, "fiscal_receipt", block_while_checking)
        response = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/sale/check",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["state"] == "blocked"
    assert module.order_row(order["id"])["saby_receipt_state"] == "blocked"
    with module.db() as con:
        effect = con.execute(
            """SELECT state FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert effect["state"] == "blocked"


@pytest.mark.parametrize(
    ("kind", "state_col", "id_col"),
    (
        ("settlement", "saby_settlement_state", "saby_settlement_receipt_id"),
        ("refund", "saby_refund_state", "saby_refund_receipt_id"),
    ),
)
def test_owner_check_uses_the_selected_receipt_columns(
    tmp_path, monkeypatch, kind, state_col, id_col
):
    client, module = app_client(tmp_path, monkeypatch)
    checked = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: checked.append(receipt_id) or [
            {"id": receipt_id, "fiscalSign": "1234567890", "state": "готова"}
        ],
    )
    headers = {
        "Authorization": "Bearer test-admin-token",
        "X-Chainya-Admin": "saby-receipt-check",
    }

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        receipt_id = f"known-{kind}-receipt"
        with module.db() as con:
            con.execute(
                f"""UPDATE orders SET {state_col} = 'pending_ofd',
                       {id_col} = ? WHERE id = ?""",
                (receipt_id, order["id"]),
            )
        response = client.post(
            f"/api/admin/orders/{order['id']}/saby/receipts/{kind}/check",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["state"] == "registered"
    assert checked == [receipt_id]
    assert module.order_row(order["id"])[state_col] == "registered"


def test_confirmed_refund_creates_one_saby_return_after_original_receipt(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []

    def register(data):
        sent.append(data)
        return {"id": f"safe-receipt-{len(sent)}"}

    monkeypatch.setattr(module.saby_client, "create_fiscal_sale", register)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        refunded_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders SET status = 'refunded', payment_state = 'refunded',
                       updated_at = ? WHERE id = ?""",
                (refunded_at, order["id"]),
            )
            module.enqueue_saby_refund_effect(con, order["id"], refunded_at)
        module.process_paid_order_effects(order["id"])
        module.process_paid_order_effects(order["id"])

    assert [item["operationType"] for item in sent] == ["1", "2"]
    current = module.order_row(order["id"])
    assert current["saby_refund_state"] == "registered"


def test_refund_waits_for_pending_sale_receipt_then_creates_one_return(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "safe-return-after-pending"},
    )
    checked = []
    monkeypatch.setattr(
        module.saby_client,
        "fiscal_receipt",
        lambda receipt_id: checked.append(receipt_id) or [
            {"id": receipt_id, "fiscalSign": "1234567890", "state": "готова"}
        ],
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        refunded_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'cancelled', payment_state = 'refunded',
                       saby_receipt_state = 'pending_ofd',
                       saby_receipt_id = 'accepted-sale-before-refund',
                       saby_receipt_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (refunded_at, refunded_at, order["id"]),
            )
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'failed', last_error = 'ожидаем ОФД', updated_at = ?
                   WHERE order_id = ? AND effect = 'saby'""",
                (refunded_at, order["id"]),
            )

        module.process_paid_order_effects(order["id"])
        module.process_paid_order_effects(order["id"])

    current = module.order_row(order["id"])
    assert checked == ["accepted-sale-before-refund", "safe-return-after-pending"]
    assert [item["operationType"] for item in sent] == ["2"]
    assert current["saby_receipt_state"] == "registered"
    assert current["saby_refund_state"] == "registered"
    with module.db() as con:
        effects = {
            row["effect"]: row["state"]
            for row in con.execute(
                """SELECT effect, state FROM paid_order_effects
                   WHERE order_id = ? AND effect IN ('saby', 'saby_refund')""",
                (order["id"],),
            ).fetchall()
        }
    assert effects == {"saby": "sent", "saby_refund": "sent"}


def test_refund_after_safe_preflight_failure_never_creates_sale_or_return(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": "must-not-be-created"},
    )

    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        refunded_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders
                   SET status = 'cancelled', payment_state = 'refunded',
                       saby_receipt_state = 'failed', updated_at = ?
                   WHERE id = ?""",
                (refunded_at, order["id"]),
            )
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'failed', last_error = 'ошибка до POST', updated_at = ?
                   WHERE order_id = ? AND effect = 'saby'""",
                (refunded_at, order["id"]),
            )

        module.process_paid_order_effects(order["id"])
        module.recover_paid_order_effects()

    assert sent == []
    current = module.order_row(order["id"])
    assert current["saby_receipt_state"] == "failed"
    assert current["saby_refund_state"] == "not_requested"
    with module.db() as con:
        effect = con.execute(
            """SELECT state, attempts, last_error FROM paid_order_effects
               WHERE order_id = ? AND effect = 'saby'""",
            (order["id"],),
        ).fetchone()
    assert effect["state"] == "skipped"
    assert effect["attempts"] == 1
    assert "возвращена" in effect["last_error"]


def test_admin_settlement_is_rejected_for_one_stage_checkout(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module.saby_client, "create_fiscal_sale",
        lambda data: sent.append(data) or {"id": f"safe-receipt-{len(sent)}"},
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        auth = {"Authorization": f"Bearer {module.ADMIN_TOKEN}"}

        too_early = client.post(
            f"/api/admin/orders/{order['id']}/saby/settle", headers=auth
        )
        assert too_early.status_code == 409

        client.patch(
            f"/api/admin/orders/{order['id']}",
            headers=auth,
            json={"status": "completed"},
        )
        rejected = client.post(
            f"/api/admin/orders/{order['id']}/saby/settle", headers=auth
        )

    assert rejected.status_code == 409
    assert "Окончательный чек не нужен" in rejected.json()["detail"]
    assert [item["payMethod"] for item in sent] == ["4"]
    assert module.order_row(order["id"])["saby_settlement_state"] == "not_requested"


def test_legacy_settlement_endpoint_never_calls_saby(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    configure_saby_fiscal_flow(module, monkeypatch)
    calls = []

    def register(data):
        calls.append(data)
        return {"id": "safe-sale"}

    monkeypatch.setattr(module.saby_client, "create_fiscal_sale", register)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        expose_live_saby_writer(module, monkeypatch)
        mark_order_paid_and_enqueue(module, order["id"])
        module.process_paid_order_effects(order["id"])
        auth = {"Authorization": f"Bearer {module.ADMIN_TOKEN}"}
        client.patch(
            f"/api/admin/orders/{order['id']}", headers=auth,
            json={"status": "completed"},
        )
        first = client.post(
            f"/api/admin/orders/{order['id']}/saby/settle", headers=auth
        )
        second = client.post(
            f"/api/admin/orders/{order['id']}/saby/settle", headers=auth
        )

    assert first.status_code == 409
    assert second.status_code == 409
    assert len(calls) == 1
    assert module.order_row(order["id"])["saby_settlement_state"] == "not_requested"


def test_saby_preflight_failure_is_retryable_without_creating_order(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
        point_id=274, price_list_id=7,
    ))

    def fail_preflight(product):
        assert product == "delivery"
        raise module.SabyError("Saby временно недоступен")

    monkeypatch.setattr(module.saby_client, "sales_point_enabled", fail_preflight)
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_delivery_order",
        lambda data: sent.append(data) or {"externalId": "must-not-exist"},
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        monkeypatch.setattr(module, "TEST_MODE", False)
        monkeypatch.setattr(
            module,
            "integration_writer",
            module.IntegrationWriter(
                test_mode=False, exposed_providers=frozenset({"tbank", "saby", "cdek"})
            ),
        )
        paid_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
            module.enqueue_paid_order_effects(con, order["id"], paid_at)

        module.process_paid_order_effects(order["id"])
        current = module.admin_order(module.order_row(order["id"]))
        with module.db() as con:
            effect = con.execute(
                """SELECT state, attempts FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            ).fetchone()

    assert sent == []
    assert current["integrations"]["saby"]["state"] == "failed"
    assert "временно недоступен" in current["integrations"]["saby"]["last_error"]
    assert effect["state"] == "failed"
    assert effect["attempts"] == 1


def test_saby_rollout_cutoff_never_backfills_older_paid_order(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_ORDER_SYNC_STARTED_AT", "2999-01-01T00:00:00+00:00")
    monkeypatch.setattr(module.saby_client, "settings", SabySettings(
        app_client_id="configured", app_secret="configured", secret_key="configured",
        point_id=274, price_list_id=7,
    ))
    sent = []
    monkeypatch.setattr(
        module.saby_client,
        "create_delivery_order",
        lambda data: sent.append(data) or {"externalId": "must-not-exist"},
    )
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        monkeypatch.setattr(module, "TEST_MODE", False)
        monkeypatch.setattr(
            module,
            "integration_writer",
            module.IntegrationWriter(
                test_mode=False, exposed_providers=frozenset({"tbank", "saby", "cdek"})
            ),
        )
        with module.db() as con:
            paid_at = module.now_iso()
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
        module.sync_paid_order_to_saby(order["id"])
        current = module.admin_order(module.order_row(order["id"]))

    assert sent == []
    assert current["integrations"]["saby"]["state"] == "not_queued"
    assert current["integrations"]["saby"]["attempts"] == 0


def test_saby_rollout_cutoff_marks_paid_effect_skipped_once(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    monkeypatch.setenv("SABY_ORDER_SYNC_MODE", "auto")
    monkeypatch.setenv("SABY_ORDER_SYNC_STARTED_AT", "2999-01-01T00:00:00+00:00")
    monkeypatch.setattr(module, "_saby_auto_sync_enabled", lambda: True)
    with client:
        order = client.post("/api/orders", json=payload()).json()["order"]
        paid_at = module.now_iso()
        with module.db() as con:
            con.execute(
                """UPDATE orders SET status = 'paid', payment_state = 'paid',
                       paid_at = ?, updated_at = ? WHERE id = ?""",
                (paid_at, paid_at, order["id"]),
            )
            module.enqueue_paid_order_effects(con, order["id"], paid_at)

        module.process_paid_order_effects(order["id"])
        module.recover_paid_order_effects()
        with module.db() as con:
            effect = con.execute(
                """SELECT state, attempts, last_error FROM paid_order_effects
                   WHERE order_id = ? AND effect = 'saby'""",
                (order["id"],),
            ).fetchone()

    assert effect["state"] == "skipped"
    assert effect["attempts"] == 1
    assert "до включения" in effect["last_error"]


def test_anonymous_analytics_feed_dashboard(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    session = "session_0123456789abcdef"
    with client:
        for event, section in (
            ("page_view", "home"),
            ("section_view", "shop"),
            ("cart_open", "cart"),
            ("checkout_start", "cart"),
        ):
            response = client.post("/api/analytics/events", json={
                "session_id": session,
                "event": event,
                "section": section,
                "language": "ru",
                "device": "mobile",
                "referrer": "direct",
                "campaign": "yandex / cpc / maps",
            })
            assert response.status_code == 204
        # A later-stage event without a page view is outside the funnel cohort.
        assert client.post("/api/analytics/events", json={
            "session_id": "unrelated_0123456789abcdef",
            "event": "cart_open", "section": "cart", "language": "ru",
            "device": "desktop", "referrer": "direct",
        }).status_code == 204

        created = client.post("/api/orders", json=payload(analytics_session=session)).json()["order"]
        assert client.patch(
            f"/api/admin/orders/{created['id']}", headers=auth, json={"status": "paid"}
        ).status_code == 200

        assert client.get("/api/admin/dashboard").status_code == 401
        dashboard = client.get("/api/admin/dashboard", headers=auth).json()
        assert dashboard["traffic"]["visitors"] == 1
        assert dashboard["traffic"]["shop_visitors"] == 1
        assert dashboard["traffic"]["cart_visitors"] == 1
        assert dashboard["traffic"]["order_conversion"] == 100
        assert dashboard["commerce"]["paid_orders"] == 1
        assert dashboard["commerce"]["revenue"] == 880
        assert dashboard["breakdown"]["device"] == [{"name": "mobile", "value": 1}]
        assert dashboard["breakdown"]["campaign"] == [
            {"name": "yandex / cpc / maps", "value": 1}
        ]
        assert dashboard["system"]["catalog_items"] > 0
        assert dashboard["system"]["catalog_active_items"] == 28
        assert len(dashboard["daily"]) == 30

        with module.db() as con:
            stored = con.execute("SELECT * FROM analytics_events LIMIT 1").fetchone()
            order_event = con.execute(
                "SELECT * FROM analytics_events WHERE event = 'order_created'"
            ).fetchone()
            stored_order = con.execute("SELECT * FROM orders WHERE id = ?", (created["id"],)).fetchone()
            analytics_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(analytics_events)")
            }
            order_columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        assert stored["session_hash"] != session
        assert order_event["session_hash"] == stored["session_hash"]
        assert stored_order["paid_at"] is not None
        assert "analytics_session_hash" not in order_columns
        assert "ip" not in analytics_columns
        assert "user_agent" not in analytics_columns
        assert "campaign" in analytics_columns


def test_production_dashboard_uses_actual_tbank_write_readiness(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    integration = module.integrations_status()
    integration["guard"]["external_writes_locked"] = False
    integration["tbank"].update(
        {
            "configured": True,
            "mode": "auto",
            "writes_enabled": True,
            "callback_ready": True,
            "receipt_configured": True,
        }
    )
    monkeypatch.setattr(module, "TEST_MODE", False)
    monkeypatch.setattr(module, "integrations_status", lambda: integration)

    with client:
        dashboard = client.get("/api/admin/dashboard", headers=auth).json()

    assert dashboard["system"]["checkout"] == "live"
    assert dashboard["system"]["tbank_writes_enabled"] is True
    assert dashboard["system"]["tbank_callback_ready"] is True
    assert dashboard["system"]["tbank_receipt_configured"] is True


def test_dashboard_counts_unresolved_saby_fiscal_incidents(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        first = client.post("/api/orders", json=payload()).json()["order"]
        second = client.post("/api/orders", json=payload()).json()["order"]
        third = client.post("/api/orders", json=payload()).json()["order"]
        fourth = client.post("/api/orders", json=payload()).json()["order"]
        fifth = client.post("/api/orders", json=payload()).json()["order"]
        with module.db() as con:
            con.execute(
                "UPDATE orders SET saby_receipt_state = 'ambiguous' WHERE id = ?",
                (first["id"],),
            )
            con.execute(
                "UPDATE orders SET saby_refund_state = 'failed' WHERE id = ?",
                (second["id"],),
            )
            con.execute(
                "UPDATE orders SET saby_receipt_state = 'pending_ofd' WHERE id = ?",
                (third["id"],),
            )
            con.execute(
                "UPDATE orders SET saby_receipt_state = 'registered' WHERE id = ?",
                (fourth["id"],),
            )
            con.execute(
                "UPDATE orders SET saby_receipt_state = 'blocked' WHERE id = ?",
                (fifth["id"],),
            )
        dashboard = client.get("/api/admin/dashboard", headers=auth).json()

    assert dashboard["system"]["saby_fiscal_incidents"] == 1
    assert dashboard["system"]["saby_fiscal_retrying"] == 1
    assert dashboard["system"]["saby_fiscal_prewrite_blocked"] == 1
    assert dashboard["system"]["saby_fiscal_pending"] == 1
    assert dashboard["system"]["saby_fiscal_registered"] == 1


def test_dashboard_counts_payment_states_requiring_reconciliation(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    states = (
        "init_ambiguous",
        "capture_ambiguous",
        "refund_ambiguous",
        "partially_refunded",
        "paid",
    )
    with client:
        orders = [
            client.post("/api/orders", json=payload()).json()["order"]
            for _ in states
        ]
        with module.db() as con:
            for order, state in zip(orders, states, strict=True):
                con.execute(
                    "UPDATE orders SET payment_state = ? WHERE id = ?",
                    (state, order["id"]),
                )
        dashboard = client.get("/api/admin/dashboard", headers=auth).json()

    assert dashboard["system"]["payment_reconciliation_incidents"] == 4


def test_analytics_validates_public_payload_and_dashboard_range(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        invalid = client.post("/api/analytics/events", json={
            "session_id": "too-short", "event": "made_up", "section": "home",
        })
        assert invalid.status_code == 422
        forged_order = client.post("/api/analytics/events", json={
            "session_id": "session_0123456789abcdef", "event": "order_created", "section": "payment",
        })
        assert forged_order.status_code == 422
        assert client.get("/api/admin/dashboard", params={"days": 365}, headers=auth).status_code == 422


def test_revenue_uses_payment_time_and_queue_is_not_period_limited(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/orders", json=payload()).json()["order"]
        with module.db() as con:
            con.execute(
                "UPDATE orders SET created_at = ?, updated_at = ? WHERE id = ?",
                ("2024-01-01T10:00:00+00:00", "2024-01-01T10:00:00+00:00", created["id"]),
            )
        before_payment = client.get("/api/admin/dashboard", params={"days": 7}, headers=auth).json()
        assert before_payment["commerce"]["orders_created"] == 0
        assert before_payment["commerce"]["awaiting_payment"] == 1
        client.patch(f"/api/admin/orders/{created['id']}", headers=auth, json={"status": "paid"})
        after_payment = client.get("/api/admin/dashboard", params={"days": 7}, headers=auth).json()
        assert after_payment["commerce"]["paid_orders"] == 1
        assert after_payment["commerce"]["revenue"] == 880
        assert after_payment["commerce"]["needs_attention"] == 1


def test_management_pages_are_served_without_exposing_token(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        for path in ("/manage", "/manage/", "/manage/catalog", "/manage/site", "/manage/guides", "/manage/promos", "/admin/orders"):
            response = client.get(path)
            assert response.status_code == 200
            assert "Вход владельца" in response.text
            assert "Пульс бизнеса" not in response.text
            assert "ADMIN_TOKEN" not in response.text

        bad = client.post("/api/admin/session", json={"token": "not-the-owner-token"})
        assert bad.status_code == 401
        login = client.post("/api/admin/session", json={"token": "test-admin-token"})
        assert login.status_code == 204
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "Secure" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]

        panel = client.get("/manage")
        assert panel.status_code == 200
        assert "Пульс бизнеса" in panel.text
        assert "chainya-admin-token" not in panel.text
        assert client.get("/api/admin/dashboard").status_code == 200

        assert client.delete("/api/admin/session").status_code == 204
        assert "Вход владельца" in client.get("/manage").text
        assert client.get("/api/admin/dashboard").status_code == 401


def test_production_does_not_register_test_payment_routes(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch, test_mode="0")
    with client:
        assert client.get("/test-payment/nonexistent", params={"token": "x"}).status_code == 404
        assert client.post(
            "/api/orders/nonexistent/test-pay", params={"token": "x"}
        ).status_code == 404

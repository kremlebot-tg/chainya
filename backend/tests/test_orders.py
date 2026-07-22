import importlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAINYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINYA_TEST_MODE", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    import backend.app as module
    module = importlib.reload(module)
    return TestClient(module.app), module


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
        payment_token = parse_qs(urlparse(body["payment"]["url"]).query)["token"][0]
        assert client.get(f"/api/orders/{order['id']}").status_code == 422
        assert client.get(f"/api/orders/{order['id']}", params={"token": "wrong"}).status_code == 403
        assert client.get(f"/api/orders/{order['id']}", params={"token": payment_token}).status_code == 200
        assert client.post(f"/api/orders/{order['id']}/test-pay", params={"token": "wrong"}).status_code == 403
        paid = client.post(f"/api/orders/{order['id']}/test-pay", params={"token": payment_token})
        assert paid.status_code == 200
        assert paid.json()["status"] == "paid"
        assert sent == [order["id"]]
        assert client.post(f"/api/orders/{order['id']}/test-pay", params={"token": payment_token}).status_code == 200
        assert sent == [order["id"]]


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


def test_admin_lists_and_updates_orders(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer test-admin-token"}
    with client:
        created = client.post("/api/orders", json=payload()).json()["order"]
        assert client.get("/api/admin/orders").status_code == 401
        listing = client.get("/api/admin/orders", headers=auth)
        assert listing.status_code == 200
        assert listing.json()["orders"][0]["customer"]["phone"] == "+7 999 123-45-67"
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
        assert dashboard["system"]["catalog_items"] > 0
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
        for path in ("/manage", "/manage/", "/admin/orders"):
            response = client.get(path)
            assert response.status_code == 200
            assert "Чайня" in response.text
            assert "ADMIN_TOKEN" not in response.text

import importlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAINYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINYA_TEST_MODE", "1")
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

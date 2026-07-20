import importlib

from fastapi.testclient import TestClient


def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAINYA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHAINYA_TEST_MODE", "1")
    import backend.app as module
    module = importlib.reload(module)
    return TestClient(module.app)


def payload(**changes):
    data = {
        "items": [{"id": "baihao", "pack": 25, "qty": 2}],
        "delivery": "pickup",
        "payment_method": "sbp",
        "name": "Тест",
        "phone": "+7 999 123-45-67",
        "city": "", "address": "", "pvz_code": "", "note": "",
    }
    data.update(changes)
    return data


def test_server_prices_order_and_mock_payment(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/orders", json=payload())
        assert response.status_code == 201
        body = response.json()
        order = body["order"]
        assert order["subtotal"] == 2 * 440  # 175 ₽ / 10 г → 440 ₽ / 25 г
        assert order["total"] == 880
        assert order["status"] == "pending_payment"
        paid = client.post(f"/api/orders/{order['id']}/test-pay")
        assert paid.status_code == 200
        assert paid.json()["status"] == "paid"


def test_rejects_client_pack_for_piece_item(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/orders", json=payload(items=[{"id": "mandarin", "pack": 25, "qty": 1}]))
        assert response.status_code == 422


def test_requires_pvz_details(tmp_path, monkeypatch):
    with app_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/orders", json=payload(delivery="cdek_pvz", city="Москва"))
        assert response.status_code == 422

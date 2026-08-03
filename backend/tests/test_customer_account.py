import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from backend.tests.test_orders import app_client, payload


def register(client, *, phone="+7 999 111-22-33", name="Анна", password="tea-password-2026"):
    return client.post(
        "/api/account/register",
        json={
            "name": name,
            "phone": phone,
            "password": password,
            "privacy_accepted": True,
        },
    )


def payment_access(response):
    url = response.json()["payment"]["url"]
    query = parse_qs(urlparse(url).query)
    return response.json()["order"]["id"], query["token"][0]


def test_customer_registration_uses_hashed_credentials_and_http_only_session(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        response = register(client)
        assert response.status_code == 201
        assert response.json()["account"]["phone"] == "+79991112233"
        cookie = response.headers["set-cookie"].lower()
        assert "chainya_customer_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        profile = client.get("/api/account")
        assert profile.status_code == 200
        with module.db() as con:
            account = con.execute("SELECT * FROM customer_accounts").fetchone()
            session = con.execute("SELECT * FROM customer_sessions").fetchone()
        assert account["password_hash"].startswith("pbkdf2_sha256$")
        assert "tea-password-2026" not in account["password_hash"]
        assert len(session["token_hash"]) == 64
        assert session["token_hash"] not in response.headers["set-cookie"]


def test_login_profile_password_rotation_and_logout(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert register(client).status_code == 201
        assert client.patch("/api/account", json={"name": "Анна Чайная"}).status_code == 200
        assert client.post(
            "/api/account/password",
            json={"current_password": "wrong", "new_password": "new-password-2026"},
        ).status_code == 403
        assert client.post(
            "/api/account/password",
            json={
                "current_password": "tea-password-2026",
                "new_password": "new-password-2026",
            },
        ).status_code == 204
        assert client.delete("/api/account/session").status_code == 204
        assert client.get("/api/account").status_code == 401
        assert client.post(
            "/api/account/login",
            json={"phone": "+7 999 111-22-33", "password": "tea-password-2026"},
        ).status_code == 401
        logged_in = client.post(
            "/api/account/login",
            json={"phone": "89991112233", "password": "new-password-2026"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["account"]["name"] == "Анна Чайная"


def test_authenticated_checkout_appears_only_in_own_account(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        assert register(client).status_code == 201
        created = client.post("/api/orders", json=payload())
        assert created.status_code == 201
        order_id, token = payment_access(created)
        listing = client.get("/api/account/orders")
        assert listing.status_code == 200
        assert [order["id"] for order in listing.json()["orders"]] == [order_id]
        assert listing.json()["orders"][0]["customer"]["phone"] == "+7 999 123-45-67"
        with module.db() as con:
            con.execute(
                "UPDATE orders SET payment_url = ? WHERE id = ?",
                ("javascript:alert(1)", order_id),
            )
        assert client.get("/api/account/orders").json()["orders"][0]["payment_url"] is None
        booking_date = (module.moscow_now().date() + timedelta(days=2)).isoformat()
        booked = client.post(
            "/api/bookings",
            json={
                "format": "master", "date": booking_date, "time": "13:00",
                "guests": 2, "name": "Анна", "phone": "+7 999 111-22-33",
                "note": "Тихий стол", "privacy_accepted": True,
            },
        )
        assert booked.status_code == 201
        bookings = client.get("/api/account/bookings")
        assert [row["id"] for row in bookings.json()["bookings"]] == [booked.json()["id"]]

        assert client.delete("/api/account/session").status_code == 204
        assert register(
            client, phone="+7 999 555-66-77", name="Борис", password="boris-password-2026"
        ).status_code == 201
        assert client.get("/api/account/orders").json()["orders"] == []
        assert client.get("/api/account/bookings").json()["bookings"] == []
        conflict = client.post(
            "/api/account/orders/claim", json={"order_id": order_id, "token": token}
        )
        assert conflict.status_code == 409


def test_guest_order_can_only_be_claimed_with_its_private_token(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        guest = client.post("/api/orders", json=payload())
        assert guest.status_code == 201
        order_id, token = payment_access(guest)
        assert register(client).status_code == 201
        assert client.post(
            "/api/account/orders/claim",
            json={"order_id": order_id, "token": "0" * 32},
        ).status_code == 403
        claimed = client.post(
            "/api/account/orders/claim", json={"order_id": order_id, "token": token}
        )
        assert claimed.status_code == 200
        assert claimed.json()["order"]["id"] == order_id
        assert client.post(
            "/api/account/orders/claim", json={"order_id": order_id, "token": token}
        ).status_code == 200


def test_account_deletion_detaches_orders_but_preserves_legal_order_record(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        assert register(client).status_code == 201
        created = client.post("/api/orders", json=payload())
        order_id = created.json()["order"]["id"]
        booking_date = (module.moscow_now().date() + timedelta(days=2)).isoformat()
        booking_id = client.post(
            "/api/bookings",
            json={
                "format": "self", "date": booking_date, "time": "16:00",
                "guests": 1, "name": "Анна", "phone": "+7 999 111-22-33",
                "note": "", "privacy_accepted": True,
            },
        ).json()["id"]
        assert client.request(
            "DELETE", "/api/account", json={"password": "wrong"}
        ).status_code == 403
        deleted = client.request(
            "DELETE", "/api/account", json={"password": "tea-password-2026"}
        )
        assert deleted.status_code == 204
        assert client.get("/api/account").status_code == 401
        with module.db() as con:
            assert con.execute("SELECT COUNT(*) FROM customer_accounts").fetchone()[0] == 0
            order = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            booking = con.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
        assert order is not None
        assert order["customer_account_id"] is None
        assert booking["customer_account_id"] is None
        assert json.loads(order["customer_json"])["phone"] == "+7 999 123-45-67"


def test_production_customer_cookie_is_secure(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch, test_mode="0")
    with client:
        response = register(client)
    assert response.status_code == 201
    assert "secure" in response.headers["set-cookie"].lower()


def test_account_page_is_no_store_and_not_indexed(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        page = client.get("/account")
        head = client.head("/account/")
    assert page.status_code == 200
    assert "Личный кабинет" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-robots-tag"] == "noindex, nofollow"
    assert head.status_code == 200
    assert head.content == b""

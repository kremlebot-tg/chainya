from backend.tests.test_customer_account import register
from backend.tests.test_orders import app_client, payload


def test_application_emits_baseline_security_headers(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        response = client.get("/api/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["x-permitted-cross-domain-policies"] == "none"
    assert response.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_production_https_response_emits_hsts(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch, test_mode="0")
    with client:
        response = client.get("/api/health")

    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_cross_site_mutation_is_rejected_for_customer_cookie(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert register(client).status_code == 201
        blocked_origin = client.patch(
            "/api/account",
            json={"name": "Подменённое имя"},
            headers={"Origin": "https://attacker.example"},
        )
        blocked_fetch_metadata = client.patch(
            "/api/account",
            json={"name": "Подменённое имя"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        allowed = client.patch(
            "/api/account",
            json={"name": "Анна Чайная"},
            headers={"Origin": "https://chainya.ru", "Sec-Fetch-Site": "same-origin"},
        )

    assert blocked_origin.status_code == 403
    assert blocked_origin.json() == {"detail": "Недопустимый источник запроса"}
    assert blocked_origin.headers["cache-control"] == "no-store"
    assert blocked_fetch_metadata.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["account"]["name"] == "Анна Чайная"
    assert allowed.headers["cache-control"] == "no-store"


def test_mobile_origin_rewrite_cannot_block_customer_checkout(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert register(client).status_code == 201
        checkout = client.post(
            "/api/orders",
            json=payload(),
            headers={
                "Origin": "https://mobile-browser.invalid",
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert checkout.status_code == 201
    assert checkout.json()["order"]["id"]


def test_admin_cookie_does_not_depend_on_mobile_origin_headers(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        login = client.post(
            "/api/admin/session", json={"token": "test-admin-token"}
        )
        rewritten_origin = client.patch(
            "/api/admin/orders/UNKNOWN",
            json={"status": "cancelled"},
            headers={"Origin": "https://attacker.example"},
        )
        allowed = client.patch(
            "/api/admin/orders/UNKNOWN",
            json={"status": "cancelled"},
            headers={"Origin": "https://chainya.ru"},
        )

    assert login.status_code == 204
    assert rewritten_origin.status_code == 404
    assert allowed.status_code == 404


def test_admin_login_works_when_customer_cookie_has_rewritten_origin(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        assert register(client).status_code == 201
        login = client.post(
            "/api/admin/session",
            json={"token": "test-admin-token"},
            headers={
                "Origin": "https://mobile-browser.invalid",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        dashboard = client.get("/api/admin/dashboard")

    assert login.status_code == 204
    assert dashboard.status_code == 200


def test_explicit_bearer_client_without_browser_cookie_keeps_api_contract(
    tmp_path, monkeypatch
):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        response = client.patch(
            "/api/admin/orders/UNKNOWN",
            json={"status": "cancelled"},
            headers={
                "Authorization": "Bearer test-admin-token",
                "Origin": "https://operations.example",
            },
        )

    # The request reaches normal endpoint validation instead of the browser
    # session CSRF guard. External bearer integrations do not use cookies.
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"

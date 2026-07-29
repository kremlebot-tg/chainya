import json
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from backend.cdek import CdekClient, CdekError, CdekSettings


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def settings(**changes):
    values = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "api_root": "https://api.cdek.ru/v2",
    }
    values.update(changes)
    return CdekSettings(**values)


def test_cdek_reads_environment_without_network(monkeypatch):
    monkeypatch.setenv("CDEK_CLIENT_ID", " env-client ")
    monkeypatch.setenv("CDEK_CLIENT_SECRET", " env-secret ")
    monkeypatch.setenv("CDEK_API_ROOT", "https://api.edu.cdek.ru/v2/")

    client = CdekClient()

    assert client.settings == CdekSettings(
        client_id="env-client",
        client_secret="env-secret",
        api_root="https://api.edu.cdek.ru/v2",
    )
    assert client.configuration() == {
        "configured": True,
        "api_root": "https://api.edu.cdek.ru/v2",
        "missing": [],
    }


@pytest.mark.parametrize("root", [
    "http://api.cdek.ru/v2",
    "https://user:secret@api.cdek.ru/v2",
    "https://example.com/v2",
    "https://api.cdek.ru/v2?token=secret",
])
def test_cdek_rejects_non_official_or_unsafe_api_roots(monkeypatch, root):
    monkeypatch.setenv("CDEK_API_ROOT", root)
    with pytest.raises(CdekError, match="Некорректный адрес"):
        CdekSettings.from_env()


def test_cdek_caches_token_and_calls_both_calculator_methods():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if "/oauth/token?" in request.full_url:
            assert request.method == "POST"
            query = parse_qs(urlparse(request.full_url).query)
            assert query == {
                "grant_type": ["client_credentials"],
                "client_id": ["client-id"],
                "client_secret": ["client-secret"],
            }
            return Response({"access_token": "access-one", "expires_in": 3600})

        assert request.method == "POST"
        assert request.get_header("Authorization") == "Bearer access-one"
        payload = json.loads(request.data)
        if request.full_url.endswith("/calculator/tariff"):
            assert payload["tariff_code"] == 136
            return Response({"tariff_code": 136, "delivery_sum": 490})
        assert request.full_url.endswith("/calculator/tarifflist")
        return Response({"tariff_codes": [{"tariff_code": 136}]})

    client = CdekClient(settings(), opener=opener)
    common = {
        "from_location": {"code": 44},
        "to_location": {"code": 270},
        "packages": [{"weight": 300, "length": 20, "width": 15, "height": 10}],
    }

    assert client.calculate_tariff({**common, "tariff_code": 136})["delivery_sum"] == 490
    assert client.quote(common)["tariff_codes"][0]["tariff_code"] == 136
    assert sum("/oauth/token?" in request.full_url for request in calls) == 1


def test_cdek_refreshes_token_before_expiration():
    now = [1000.0]
    issued = []

    def opener(request, timeout):
        if "/oauth/token?" in request.full_url:
            token = f"access-{len(issued) + 1}"
            issued.append(token)
            return Response({"access_token": token, "expires_in": 100})
        return Response({"ok": True})

    client = CdekClient(settings(), opener=opener, clock=lambda: now[0])
    assert client.access_token() == "access-1"
    now[0] = 1089.0
    assert client.access_token() == "access-1"
    now[0] = 1090.0
    assert client.access_token() == "access-2"


def test_cdek_gets_cities_and_delivery_points_with_encoded_query():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if "/oauth/token?" in request.full_url:
            return Response({"access_token": "access", "expires_in": 3600})
        assert request.method == "GET"
        assert request.get_header("Authorization") == "Bearer access"
        if "/location/cities?" in request.full_url:
            assert parse_qs(urlparse(request.full_url).query) == {
                "city": ["Санкт-Петербург"],
                "country_codes": ["RU"],
            }
            return Response([{"code": 137, "city": "Санкт-Петербург"}])
        assert request.full_url.endswith(
            "/deliverypoints?city_code=137&is_handout=true"
        )
        return Response([{"code": "SPB1"}])

    client = CdekClient(settings(), opener=opener)
    assert client.cities(city="Санкт-Петербург", country_codes="RU")[0]["code"] == 137
    assert client.delivery_points(city_code=137, is_handout="true")[0]["code"] == "SPB1"
    assert len(calls) == 3


def test_cdek_create_order_keeps_payload_and_uses_orders_endpoint():
    calls = []
    payload = {
        "type": 1,
        "number": "site-42",
        "tariff_code": 136,
        "recipient": {"name": "Покупатель", "phones": [{"number": "+79990000000"}]},
        "packages": [{
            "number": "site-42-1",
            "weight": 300,
            "items": [{"name": "Чай", "ware_key": "tea", "cost": 1000, "amount": 1, "weight": 300}],
        }],
    }

    def opener(request, timeout):
        calls.append(request)
        if "/oauth/token?" in request.full_url:
            return Response({"access_token": "access", "expires_in": 3600})
        assert request.method == "POST"
        assert request.full_url == "https://api.cdek.ru/v2/orders"
        assert request.get_header("Authorization") == "Bearer access"
        assert json.loads(request.data) == payload
        return Response({"entity": {"uuid": "order-uuid"}, "requests": [{"state": "ACCEPTED"}]})

    result = CdekClient(settings(), opener=opener).create_order(payload)

    assert result["entity"]["uuid"] == "order-uuid"
    assert result["requests"][0]["state"] == "ACCEPTED"
    assert len(calls) == 2


def test_cdek_retries_one_api_request_after_unauthorized():
    auth_count = 0
    api_count = 0

    def opener(request, timeout):
        nonlocal auth_count, api_count
        if "/oauth/token?" in request.full_url:
            auth_count += 1
            return Response({"access_token": f"access-{auth_count}", "expires_in": 3600})
        api_count += 1
        if api_count == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)
        assert request.get_header("Authorization") == "Bearer access-2"
        return Response({"tariff_codes": []})

    result = CdekClient(settings(), opener=opener).quote({"from_location": {}, "to_location": {}, "packages": []})

    assert result == {"tariff_codes": []}
    assert auth_count == 2
    assert api_count == 2


def test_cdek_errors_never_expose_credentials():
    secret_settings = settings(client_id="private-client", client_secret="private-secret")
    assert "private-client" not in repr(secret_settings)
    assert "private-secret" not in repr(secret_settings)

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "private-secret", None, None)

    with pytest.raises(CdekError) as captured:
        CdekClient(secret_settings, opener=opener).access_token()

    message = str(captured.value)
    assert message == "CDEK вернул HTTP 400"
    assert "private-client" not in message
    assert "private-secret" not in message


def test_cdek_missing_configuration_fails_before_network():
    client = CdekClient(CdekSettings())

    assert client.configuration()["configured"] is False
    assert client.configuration()["missing"] == ["CDEK_CLIENT_ID", "CDEK_CLIENT_SECRET"]
    with pytest.raises(CdekError, match="Не заданы параметры"):
        client.quote({})

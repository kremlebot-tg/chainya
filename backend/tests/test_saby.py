import io
import json
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from backend.saby import SabyClient, SabyError, SabySettings


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
        "app_client_id": "client", "app_secret": "secret", "secret_key": "service",
        "point_id": 10, "price_list_id": 20,
    }
    values.update(changes)
    return SabySettings(**values)


def test_saby_authenticates_once_and_requests_catalog():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            assert json.loads(request.data) == {
                "app_client_id": "client", "app_secret": "secret", "secret_key": "service",
            }
            return Response({"token": "access"})
        assert request.headers["X-sbisaccesstoken"] == "access"
        return Response({"nomenclatures": [{"id": 1, "name": "Чай"}]})

    client = SabyClient(settings(), opener=opener)
    assert client.catalog()["nomenclatures"][0]["name"] == "Чай"
    assert client.catalog()["nomenclatures"][0]["id"] == 1
    assert sum(call.full_url.endswith("/oauth/service/") for call in calls) == 1
    assert "pointId=10" in calls[1].full_url
    assert "priceListId=20" in calls[1].full_url


def test_saby_catalog_all_reads_every_page_and_deduplicates():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        page = int(parse_qs(urlparse(request.full_url).query)["page"][0])
        if page == 0:
            return Response({
                "nomenclatures": [{"id": 1, "name": "Первый"}],
                "outcome": {"hasMore": True},
            })
        return Response({
            "nomenclatures": [{"id": 1, "name": "Первый"}, {"id": 2, "name": "Второй"}],
            "outcome": {"hasMore": False},
        })

    client = SabyClient(settings(), opener=opener)
    assert [item["id"] for item in client.catalog_all()] == [1, 2]
    assert len(calls) == 3
    assert "noStopList=false" in calls[1].full_url
    assert "withBalance=false" in calls[1].full_url


def test_saby_base_catalog_omits_price_list_and_remains_read_only():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        assert request.method == "GET"
        return Response({
            "nomenclatures": [{"id": 1, "name": "Чай", "cost": 17.5}],
            "outcome": {"hasMore": False},
        })

    client = SabyClient(settings(), opener=opener)
    assert client.base_catalog_all(with_balance=True)[0]["cost"] == 17.5
    query = parse_qs(urlparse(calls[1].full_url).query)
    assert query["pointId"] == ["10"]
    assert query["withBalance"] == ["true"]
    assert "priceListId" not in query


def test_saby_delivery_calendar_is_read_only_and_uses_selected_point():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        assert request.method == "GET"
        assert "/retail/delivery/calendar?" in request.full_url
        assert parse_qs(urlparse(request.full_url).query)["pointId"] == ["10"]
        return Response({"dates": [{"date": "2026-08-08"}]})

    client = SabyClient(settings(), opener=opener)
    assert client.delivery_calendar()["dates"] == [{"date": "2026-08-08"}]
    assert len(calls) == 2


def test_saby_checks_whether_point_is_enabled_for_delivery():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        assert request.method == "GET"
        query = parse_qs(urlparse(request.full_url).query)
        assert query["product"] == ["delivery"]
        return Response({"salesPoints": [{"id": "10"}, {"id": 11}]})

    client = SabyClient(settings(), opener=opener)
    assert client.sales_point_enabled("delivery") is True
    assert client.sales_point_enabled("delivery", 99) is False


def test_saby_accepts_empty_delivery_point_mapping_as_disabled():
    def opener(request, timeout):
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        return Response({"salesPoints": {}})

    client = SabyClient(settings(), opener=opener)
    assert client.sales_point_enabled("delivery") is False


def test_saby_reports_missing_configuration_without_network():
    client = SabyClient(SabySettings())
    assert client.configuration()["configured"] is False
    assert "SABY_SECRET_KEY" in client.configuration()["missing"]
    with pytest.raises(SabyError, match="Не заданы параметры"):
        client.sales_points()


def test_saby_settings_repr_and_vendor_errors_never_expose_secrets():
    secret_settings = settings(
        app_client_id="private-client", app_secret="private-app-secret",
        secret_key="private-service-secret",
    )
    rendered = repr(secret_settings)
    assert "private-client" not in rendered
    assert "private-app-secret" not in rendered
    assert "private-service-secret" not in rendered

    client = SabyClient(
        secret_settings,
        opener=lambda *_args, **_kwargs: Response({
            "error": {
                "code": "AUTH_7",
                "message": "bad private-app-secret private-service-secret",
            }
        }),
    )
    with pytest.raises(SabyError) as captured:
        client.access_token()
    assert str(captured.value) == "Saby отклонил запрос (код AUTH_7)"
    assert "private" not in str(captured.value)


def test_saby_http_error_exposes_only_sanitized_vendor_explanation():
    secret_settings = settings(
        app_client_id="private-client", app_secret="private-app-secret",
        secret_key="private-service-secret",
    )
    body = json.dumps({
        "error": {
            "message": (
                "ККТ недоступна; private-service-secret; +7 999 123-45-67; "
                "owner@example.test"
            )
        }
    }).encode()

    def opener(request, timeout):
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access-token-value"})
        raise urllib.error.HTTPError(
            request.full_url, 500, "Internal Server Error", {}, io.BytesIO(body)
        )

    client = SabyClient(secret_settings, opener=opener)
    with pytest.raises(SabyError) as captured:
        client.create_fiscal_sale({"externalId": "safe-order"})
    message = str(captured.value)
    assert message.startswith("Saby вернул HTTP 500: ККТ недоступна")
    assert "private-service-secret" not in message
    assert "+7 999 123-45-67" not in message
    assert "owner@example.test" not in message


def test_saby_delivery_order_keeps_payload_and_uses_create_endpoint():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        assert request.method == "POST"
        assert request.full_url.endswith("/retail/order/create")
        assert json.loads(request.data) == {"pointId": 10, "amount": 1230, "items": [{"id": 7, "quantity": 1}]}
        return Response({"id": 99, "status": "created"})

    client = SabyClient(settings(), opener=opener)
    result = client.create_delivery_order({"pointId": 10, "amount": 1230, "items": [{"id": 7, "quantity": 1}]})
    assert result == {"id": 99, "status": "created"}
    assert len(calls) == 2


def test_saby_fiscal_sale_and_receipt_status_use_documented_endpoints():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/oauth/service/"):
            return Response({"token": "access"})
        if request.method == "POST":
            assert request.full_url.endswith("/retail/sale/create")
            assert json.loads(request.data) == {"externalId": "chainya-order-sale"}
            return Response({"id": "receipt-safe-id"})
        query = parse_qs(urlparse(request.full_url).query)
        assert request.full_url.startswith("https://api.sbis.ru/retail/pay/list?")
        assert query["ids[]"] == ["receipt-safe-id"]
        return Response({"payments": [{"id": "receipt-safe-id"}]})

    client = SabyClient(settings(), opener=opener)
    assert client.create_fiscal_sale({"externalId": "chainya-order-sale"})["id"] == "receipt-safe-id"
    assert client.fiscal_receipt("receipt-safe-id")["payments"][0]["id"] == "receipt-safe-id"

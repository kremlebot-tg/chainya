import json
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

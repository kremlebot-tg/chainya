import json

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


def test_saby_reports_missing_configuration_without_network():
    client = SabyClient(SabySettings())
    assert client.configuration()["configured"] is False
    assert "SABY_SECRET_KEY" in client.configuration()["missing"]
    with pytest.raises(SabyError, match="Не заданы параметры"):
        client.sales_points()

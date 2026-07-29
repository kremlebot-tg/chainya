import pytest

from backend.cdek_delivery import (
    CdekDeliverySettings,
    build_order_payload,
    normalized_quote,
    package_spec,
    tariff_payload,
)


def settings(**changes):
    values = {
        "from_city_code": 44,
        "sender_city": "Москва",
        "sender_address": "ул. Острякова, 3",
        "sender_name": "Чайня",
        "sender_phone": "+79055908801",
        "shipment_point": "MSK1",
        "package_length": 20,
        "package_width": 15,
        "package_height": 10,
        "packaging_weight": 150,
        "piece_weight": 100,
    }
    values.update(changes)
    return CdekDeliverySettings(**values)


def test_package_and_quote_are_server_derived():
    package = package_spec([
        {"pack": 25, "qty": 2},
        {"pack": 100, "qty": 1},
        {"pack": "pc", "qty": 1},
    ], settings())
    assert package == {
        "weight": 400,
        "length": 20,
        "width": 15,
        "height": 12,
    }
    payload = tariff_payload("cdek_pvz", 137, package, settings())
    assert payload["tariff_code"] == 136
    assert payload["from_location"]["code"] == 44
    assert payload["to_location"]["code"] == 137
    quote = normalized_quote("cdek_pvz", 137, package, {
        "delivery_sum": 250.01,
        "period_min": 1,
        "period_max": 2,
        "tariff_name": "Посылка склад-склад",
    })
    assert quote["price"] == 251
    assert quote["tariff_code"] == 136


def test_build_prepaid_pvz_order_requires_shipment_point():
    order = {
        "id": "ORDER1",
        "delivery": "cdek_pvz",
        "customer": {
            "name": "Гость",
            "phone": "+7 999 000-00-00",
            "city": "Санкт-Петербург",
            "pvz_code": "SPB1",
            "address": "",
            "note": "",
        },
        "items": [{
            "id": "tea",
            "name": "Чай",
            "pack": 25,
            "qty": 2,
            "unit_price": 400,
            "total": 800,
        }],
    }
    quote = {
        "tariff_code": 136,
        "city_code": 137,
        "weight": 200,
        "length": 20,
        "width": 15,
        "height": 10,
    }
    payload = build_order_payload(order, quote, settings())
    assert payload["shipment_point"] == "MSK1"
    assert payload["delivery_point"] == "SPB1"
    assert payload["packages"][0]["items"][0]["payment"]["value"] == 0
    assert payload["recipient"]["phones"][0]["number"] == "+79990000000"

    with pytest.raises(ValueError, match="пункт CDEK"):
        build_order_payload(order, quote, settings(shipment_point=""))

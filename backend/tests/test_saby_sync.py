import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from backend.saby import SabySettings
from backend.saby_sync import (
    SABY_NOMENCLATURE_BY_SITE_ID,
    SabySyncError,
    SabySyncMode,
    SabySyncPolicyError,
    build_nomenclatures,
    build_saby_order,
    require_write_allowed,
    sync_mode_from_env,
    validate_mapping_against_catalog,
    validate_mapping_file,
    write_allowed,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = ROOT / "backend" / "catalog.seed.json"
CATALOG_PATH = Path(os.environ.get("CHAINYA_CATALOG_PATH", DEFAULT_CATALOG_PATH))


def settings(**changes):
    values = {
        "app_client_id": "client",
        "app_secret": "secret",
        "secret_key": "service",
        "point_id": 274,
        "price_list_id": 7,
    }
    values.update(changes)
    return SabySettings(**values)


def gram_line(**changes):
    values = {
        "id": "baihao",
        "name": "Бай Хао Инь Чжень",
        "pack": 25,
        "qty": 2,
        "unit_price": 440,
        "total": 880,
    }
    values.update(changes)
    return values


def piece_line(**changes):
    values = {
        "id": "mandarin",
        "name": "Шу Пуэр в мандарине",
        "pack": "pc",
        "qty": 3,
        "unit_price": 320,
        "total": 960,
    }
    values.update(changes)
    return values


def pickup_order(**changes):
    values = {
        "id": "ORDER123",
        "delivery": "pickup",
        "payment_method": "sbp",
        "customer": {"name": "Даниил", "phone": "+7 900 000-00-00", "note": "Позвонить"},
        "items": [gram_line(), piece_line(qty=1, total=320)],
    }
    values.update(changes)
    return values


def test_mapping_is_exactly_the_active_catalog_items():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    active = {tea["id"] for tea in catalog["teas"] if tea.get("stock") is True}

    assert len(active) == 29
    assert set(SABY_NOMENCLATURE_BY_SITE_ID) == active
    assert "molimaojian" not in SABY_NOMENCLATURE_BY_SITE_ID
    assert "ginseng1s" not in SABY_NOMENCLATURE_BY_SITE_ID
    assert "chongshicha" not in SABY_NOMENCLATURE_BY_SITE_ID
    validate_mapping_against_catalog(catalog)
    validate_mapping_file(CATALOG_PATH)


def test_mapping_validator_reports_missing_and_extra_ids():
    catalog = {"teas": [
        {"id": "baihao", "stock": True},
        {"id": "disabled", "stock": False},
    ]}
    mapping = {
        "different": next(iter(SABY_NOMENCLATURE_BY_SITE_ID.values())),
    }
    with pytest.raises(SabySyncError, match="нет соответствий: baihao") as error:
        validate_mapping_against_catalog(catalog, mapping)
    assert "лишние соответствия: different" in str(error.value)


def test_gram_and_piece_lines_use_the_checkout_amounts():
    gram, piece = build_nomenclatures([gram_line(), piece_line()])

    assert gram == {
        "id": 39,
        "externalId": "b4bc9267-241d-4bfe-9fe8-8af23409f4f0",
        "count": 50,
        "cost": 17.6,
        "name": "Бай Хао Инь Чжень",
    }
    assert piece["id"] == 44
    assert piece["count"] == 3
    assert piece["cost"] == 320


def test_nomenclature_builder_rejects_unknown_or_inconsistent_lines():
    with pytest.raises(SabySyncError, match="Нет соответствия"):
        build_nomenclatures([gram_line(id="unknown")])
    with pytest.raises(SabySyncError, match="не совпадает"):
        build_nomenclatures([piece_line(total=1)])


def test_full_builder_uses_configured_point_and_price_list():
    payload = build_saby_order(
        pickup_order(),
        settings=settings(),
        ready_at=datetime(2026, 7, 24, 18, 30),
    )

    assert payload["product"] == "delivery"
    assert payload["pointId"] == 274
    assert payload["priceListId"] == 7
    assert payload["datetime"] == "2026-07-24 18:30:00"
    assert payload["delivery"] == {"isPickup": True, "paymentType": "online"}
    assert payload["customer"] == {"name": "Даниил", "phone": "+7 900 000-00-00"}
    assert payload["comment"] == "Заказ сайта №ORDER123. Позвонить"
    assert len(payload["nomenclatures"]) == 2


def test_full_builder_supports_cdek_pvz_and_courier_addresses():
    pvz = build_saby_order(
        pickup_order(
            delivery="cdek_pvz",
            customer={
                "name": "Даниил", "phone": "+7 900 000-00-00",
                "city": "Москва", "pvz_code": "MSK2631",
            },
            delivery_quote={"point": {"address": "ул. Примерная, 1"}},
        ),
        settings=settings(),
        ready_at="2026-07-24 18:30:00",
    )
    courier = build_saby_order(
        pickup_order(
            delivery="cdek_courier",
            customer={
                "name": "Даниил", "phone": "+7 900 000-00-00",
                "city": "Москва", "address": "ул. Чайная, 3",
            },
        ),
        settings=settings(),
        ready_at="2026-07-24 18:30:00",
    )

    assert pvz["delivery"] == {
        "isPickup": False,
        "paymentType": "online",
        "addressFull": "Москва, ул. Примерная, 1",
    }
    assert courier["delivery"] == {
        "isPickup": False,
        "paymentType": "online",
        "addressFull": "Москва, ул. Чайная, 3",
    }


def test_full_builder_rejects_unknown_delivery_or_missing_address():
    with pytest.raises(SabySyncError, match="Неподдерживаемый"):
        build_saby_order(
            pickup_order(delivery="courier"),
            settings=settings(),
            ready_at="2026-07-24 18:30:00",
        )
    with pytest.raises(SabySyncError, match="требуется адрес"):
        build_saby_order(
            pickup_order(
                delivery="cdek_courier",
                customer={"name": "Даниил", "phone": "+7 900 000-00-00"},
            ),
            settings=settings(),
            ready_at="2026-07-24 18:30:00",
        )


def test_full_builder_requires_ready_datetime_and_selected_saby_ids():
    with pytest.raises(SabySyncError, match="ready_at"):
        build_saby_order(pickup_order(), settings=settings(), ready_at="")
    with pytest.raises(SabySyncError, match="SABY_POINT_ID"):
        build_saby_order(
            pickup_order(), settings=settings(point_id=None), ready_at="2026-07-24 18:30:00"
        )
    with pytest.raises(SabySyncError, match="SABY_PRICE_LIST_ID"):
        build_saby_order(
            pickup_order(), settings=settings(price_list_id=None), ready_at="2026-07-24 18:30:00"
        )


def test_sync_mode_defaults_to_off_and_rejects_unknown_values():
    assert sync_mode_from_env({}) is SabySyncMode.OFF
    assert sync_mode_from_env({"SABY_ORDER_SYNC_MODE": " ShAdOw "}) is SabySyncMode.SHADOW
    with pytest.raises(SabySyncError, match="Неизвестный режим"):
        sync_mode_from_env({"SABY_ORDER_SYNC_MODE": "live"})


@pytest.mark.parametrize("mode", list(SabySyncMode))
def test_test_mode_is_an_unconditional_write_barrier(mode):
    assert write_allowed(mode, test_mode=True, manual_approved=True) is False
    with pytest.raises(SabySyncPolicyError):
        require_write_allowed(mode, test_mode=True, manual_approved=True)


def test_non_test_write_policy_requires_an_explicit_rollout_mode():
    assert write_allowed(SabySyncMode.OFF, test_mode=False) is False
    assert write_allowed(SabySyncMode.SHADOW, test_mode=False) is False
    assert write_allowed(SabySyncMode.MANUAL, test_mode=False) is False
    assert write_allowed(SabySyncMode.MANUAL, test_mode=False, manual_approved=True) is True
    assert write_allowed(SabySyncMode.AUTO, test_mode=False) is True

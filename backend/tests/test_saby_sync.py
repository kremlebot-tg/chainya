import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from backend.saby import SabySettings
from backend.saby_sync import (
    SABY_NOMENCLATURE_BY_SITE_ID,
    SabyNomenclatureRef,
    SabySyncError,
    SabySyncMode,
    SabySyncPolicyError,
    build_nomenclatures,
    build_saby_order,
    require_write_allowed,
    sync_mode_from_env,
    validate_mapping_against_catalog,
    validate_mapping_file,
    mapping_for_catalog,
    write_allowed,
)


def test_reviewed_catalog_link_extends_order_mapping():
    external_id = "11111111-1111-4111-8111-111111111111"
    catalog = {"teas": [{
        "id": "owner-added", "stock": True,
        "saby": {"id": 777, "external_id": external_id, "image_pending": False},
    }]}
    mapping = mapping_for_catalog(catalog, {})
    assert mapping["owner-added"] == SabyNomenclatureRef(777, external_id)

    rows = build_nomenclatures([{
        "id": "owner-added", "name": "Owner tea", "pack": 10, "qty": 1,
        "unit_price": 175, "total": 175,
        "saby": {"id": 777, "external_id": external_id},
    }])
    assert rows[0]["id"] == 777
    assert rows[0]["externalId"] == external_id


def test_reviewed_catalog_link_cannot_duplicate_or_replace_a_verified_link():
    verified = {"legacy": SabyNomenclatureRef(
        5, "55555555-5555-4555-8555-555555555555"
    )}
    with pytest.raises(SabySyncError, match="несколькими товарами"):
        mapping_for_catalog({"teas": [{
            "id": "new", "saby": {
                "id": 5, "external_id": "66666666-6666-4666-8666-666666666666",
            },
        }]}, verified)
    with pytest.raises(SabySyncError, match="менять нельзя"):
        mapping_for_catalog({"teas": [{
            "id": "legacy", "saby": {
                "id": 6, "external_id": "66666666-6666-4666-8666-666666666666",
            },
        }]}, verified)


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


def test_mapping_covers_active_items_and_keeps_stable_out_of_stock_links():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    active = {tea["id"] for tea in catalog["teas"] if tea.get("stock") is True}

    assert len(active) == 28
    assert active < set(SABY_NOMENCLATURE_BY_SITE_ID)
    assert set(SABY_NOMENCLATURE_BY_SITE_ID) - active == {"gabamaocha"}
    assert "molimaojian" not in SABY_NOMENCLATURE_BY_SITE_ID
    assert "ginseng1s" not in SABY_NOMENCLATURE_BY_SITE_ID
    assert "chongshicha" not in SABY_NOMENCLATURE_BY_SITE_ID
    validate_mapping_against_catalog(catalog)
    validate_mapping_file(CATALOG_PATH)


def test_mapping_validator_reports_missing_but_allows_stable_inactive_ids():
    catalog = {"teas": [
        {"id": "baihao", "stock": True},
        {"id": "disabled", "stock": False},
    ]}
    mapping = {
        "different": next(iter(SABY_NOMENCLATURE_BY_SITE_ID.values())),
    }
    with pytest.raises(SabySyncError, match="нет соответствий: baihao") as error:
        validate_mapping_against_catalog(catalog, mapping)
    assert "different" not in str(error.value)

    validate_mapping_against_catalog(
        {"teas": [{"id": "baihao", "stock": False}]},
        {"baihao": SABY_NOMENCLATURE_BY_SITE_ID["baihao"]},
    )


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


def test_saby_rows_preserve_the_server_checkout_total():
    lines = [gram_line(), piece_line(qty=1, total=320)]
    rows = build_nomenclatures(lines)

    checkout_total = sum(line["total"] for line in lines)
    saby_total = sum(row["count"] * row["cost"] for row in rows)
    assert saby_total == pytest.approx(checkout_total)


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
    assert payload["delivery"] == {
        "isPickup": True,
        "paymentType": "online",
        "shopURL": "https://chainya.ru",
        "successURL": "https://chainya.ru/payment/success",
        "errorURL": "https://chainya.ru/payment/fail",
    }
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
        "shopURL": "https://chainya.ru",
        "successURL": "https://chainya.ru/payment/success",
        "errorURL": "https://chainya.ru/payment/fail",
        "addressFull": "Москва, ул. Примерная, 1",
    }
    assert courier["delivery"] == {
        "isPickup": False,
        "paymentType": "online",
        "shopURL": "https://chainya.ru",
        "successURL": "https://chainya.ru/payment/success",
        "errorURL": "https://chainya.ru/payment/fail",
        "addressFull": "Москва, ул. Чайная, 3",
    }


def test_full_builder_rejects_non_https_saby_redirects():
    with pytest.raises(SabySyncError, match="SABY_SHOP_URL"):
        build_saby_order(
            pickup_order(),
            settings=settings(shop_url="http://chainya.ru"),
            ready_at="2026-07-24 18:30:00",
        )


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

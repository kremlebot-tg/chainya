from backend.saby_shadow import SabyShadowSettings, compare_catalogs
from backend.saby_sync import SabyNomenclatureRef


MAPPING = {
    "loose": SabyNomenclatureRef(1, "11111111-1111-4111-8111-111111111111"),
    "piece": SabyNomenclatureRef(2, "22222222-2222-4222-8222-222222222222"),
}


def site_catalog():
    return {
        "loose": {
            "id": "loose", "name": "Рассыпной чай", "unit": "g",
            "price": 175, "stock": True, "published": True,
        },
        "piece": {
            "id": "piece", "name": "Чай в мандарине", "unit": "pc",
            "price": 320, "stock": True, "published": True,
        },
    }


def saby_catalog():
    return [
        {
            "id": 1, "externalId": MAPPING["loose"].external_id,
            "name": "Рассыпной чай", "unit": "г", "cost": 17.5,
            "balance": 500, "published": True,
        },
        {
            "id": 2, "externalId": MAPPING["piece"].external_id,
            "name": "Чай в мандарине", "unit": "шт", "cost": 320,
            "balance": 10, "published": True,
        },
    ]


def test_shadow_settings_are_fail_closed_and_interval_is_bounded():
    assert SabyShadowSettings.from_env({}).enabled is False
    assert SabyShadowSettings.from_env({"SABY_CATALOG_SHADOW_MODE": "true"}).enabled is False
    settings = SabyShadowSettings.from_env({
        "SABY_CATALOG_SHADOW_MODE": " ON ",
        "SABY_CATALOG_SHADOW_INTERVAL_SECONDS": "5",
    })
    assert settings.enabled is True
    assert settings.interval_seconds == 300
    assert SabyShadowSettings.from_env({
        "SABY_CATALOG_SHADOW_INTERVAL_SECONDS": "not-a-number"
    }).interval_seconds == 300


def test_shadow_compares_price_units_and_stock_without_mutating_inputs():
    site, saby = site_catalog(), saby_catalog()
    original_site = {key: dict(value) for key, value in site.items()}
    original_saby = [dict(value) for value in saby]

    result = compare_catalogs(site, saby, MAPPING)

    assert result["state"] == "ok"
    assert result["read_only"] is True
    assert result["catalog_changed"] is False
    assert result["counts"] == {
        "site_items": 2,
        "site_active_items": 2,
        "mapped_items": 2,
        "saby_items": 2,
        "compared_items": 2,
        "price_matches": 2,
        "stock_matches": 2,
        "errors": 0,
        "warnings": 0,
        "info": 0,
        "actionable_differences": 0,
    }
    assert result["differences"] == []
    assert site == original_site
    assert saby == original_saby


def test_shadow_reports_price_stock_mapping_and_saby_only_differences():
    site, saby = site_catalog(), saby_catalog()
    site["unmapped"] = {
        "id": "unmapped", "name": "Новый чай", "unit": "g",
        "price": 100, "stock": True, "published": True,
    }
    saby[0]["cost"] = 175
    saby[1]["balance"] = 0
    saby.append({
        "id": 3,
        "externalId": "33333333-3333-4333-8333-333333333333",
        "name": "Только в Saby", "unit": "г", "cost": 10,
        "balance": 1, "published": True,
    })

    result = compare_catalogs(site, saby, MAPPING)
    kinds = {item["kind"] for item in result["differences"]}

    assert result["state"] == "differences"
    assert kinds == {
        "unmapped_site_item", "price_mismatch", "stock_mismatch", "saby_only_item"
    }
    assert result["counts"]["errors"] == 1
    assert result["counts"]["warnings"] == 2
    assert result["counts"]["info"] == 1
    price = next(item for item in result["differences"] if item["kind"] == "price_mismatch")
    assert price["site_value"] == {
        "price": 175, "unit": "10 г", "expected_saby_cost": 17.5,
    }
    assert price["saby_value"] == {"cost": 175, "unit": "г"}


def test_shadow_reports_unknown_units_and_missing_saby_items_instead_of_guessing():
    site, saby = site_catalog(), saby_catalog()
    site["piece"]["unit"] = "box"
    saby = saby[:1]

    result = compare_catalogs(site, saby, MAPPING)

    kinds = [item["kind"] for item in result["differences"]]
    assert kinds == ["missing_in_saby"]

    saby.append({
        "id": 2, "externalId": MAPPING["piece"].external_id,
        "name": "Чай в мандарине", "unit": "уп", "cost": None,
        "balance": "unknown", "published": False,
    })
    result = compare_catalogs(site, saby, MAPPING)
    kinds = {item["kind"] for item in result["differences"]}
    assert kinds == {"price_not_comparable", "stock_not_comparable"}


def test_shadow_requires_enough_balance_for_the_smallest_sellable_pack():
    saby = saby_catalog()
    saby[0]["balance"] = "9.9"

    result = compare_catalogs(site_catalog(), saby, MAPPING)

    stock = next(item for item in result["differences"] if item["kind"] == "stock_mismatch")
    assert stock["saby_value"] == {
        "in_stock": False, "balance": 9.9, "minimum_to_sell": 10, "unit": "г",
    }


def test_shadow_accepts_exact_balance_thresholds_and_decimal_strings():
    saby = saby_catalog()
    saby[0].update(cost="17.500", balance="10.0")
    saby[1].update(cost="320.00", balance="1")

    result = compare_catalogs(site_catalog(), saby, MAPPING)

    assert result["state"] == "ok"
    assert result["counts"]["price_matches"] == 2
    assert result["counts"]["stock_matches"] == 2


def test_shadow_price_tolerance_has_an_explicit_boundary():
    saby = saby_catalog()
    saby[0]["cost"] = "17.505"
    assert compare_catalogs(site_catalog(), saby, MAPPING)["counts"]["price_matches"] == 2

    saby[0]["cost"] = "17.5051"
    result = compare_catalogs(site_catalog(), saby, MAPPING)
    assert result["counts"]["price_matches"] == 1
    assert any(item["kind"] == "price_mismatch" for item in result["differences"])


def test_shadow_lists_unmapped_inactive_site_items_as_information_only():
    site = site_catalog()
    site["old"] = {
        "id": "old", "name": "Снятый с продажи чай", "unit": "g",
        "price": 100, "stock": False, "published": True,
    }

    result = compare_catalogs(site, saby_catalog(), MAPPING)

    assert result["state"] == "ok"
    assert result["counts"]["info"] == 1
    assert result["differences"][0]["kind"] == "unmapped_inactive_site_item"


def test_shadow_reports_duplicate_external_ids():
    saby = saby_catalog()
    saby.append(dict(saby[0], id=99))

    result = compare_catalogs(site_catalog(), saby, MAPPING)

    duplicate = [
        item for item in result["differences"]
        if item["kind"] == "duplicate_saby_external_id"
    ]
    assert len(duplicate) == 1
    assert duplicate[0]["severity"] == "error"


def test_shadow_reports_saby_items_without_external_id():
    saby = saby_catalog()
    saby.append({
        "id": 77, "externalId": "", "name": "Повреждённая позиция",
        "unit": "г", "cost": "12", "balance": "100",
    })

    result = compare_catalogs(site_catalog(), saby, MAPPING)

    malformed = [
        item for item in result["differences"]
        if item["kind"] == "saby_item_without_external_id"
    ]
    assert len(malformed) == 1
    assert malformed[0]["severity"] == "warning"
    assert malformed[0]["saby_value"] == {"id": 77}


def test_shadow_rejects_changed_numeric_saby_id_even_when_external_id_matches():
    saby = saby_catalog()
    saby[0]["id"] = 999

    result = compare_catalogs(site_catalog(), saby, MAPPING)

    mismatch = [item for item in result["differences"] if item["kind"] == "saby_id_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["site_id"] == "loose"
    assert mismatch[0]["severity"] == "error"

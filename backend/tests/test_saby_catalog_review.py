from backend.saby_catalog_review import build_catalog_review


def test_review_does_not_guess_unknown_units_or_fractional_storefront_price():
    document = {"teas": []}
    rows = [
        {"id": 1, "externalId": "11111111-1111-4111-8111-111111111111", "name": "Литр", "unit": "л", "cost": 20, "balance": 3},
        {"id": 2, "externalId": "22222222-2222-4222-8222-222222222222", "name": "Вес", "unit": "г", "cost": 17.55, "balance": 100},
    ]

    result = build_catalog_review(document, rows, [])

    assert result["catalog_changed"] is False
    assert result["counts"]["new"] == 2
    assert result["counts"]["ready_for_draft"] == 0
    assert all(item["can_create_draft"] is False for item in result["items"])


def test_review_keeps_missing_balance_unknown_instead_of_marking_out_of_stock():
    rows = [{
        "id": 3,
        "externalId": "33333333-3333-4333-8333-333333333333",
        "name": "Чай без переданного остатка",
        "unit": "г",
        "cost": 18,
    }]

    result = build_catalog_review({"teas": []}, rows, [])

    assert result["items"][0]["suggested_price"] == 180
    assert result["items"][0]["suggested_stock"] is None
    assert result["items"][0]["can_create_draft"] is True


def test_review_does_not_guess_ten_gram_price_when_balance_evidence_is_missing():
    external_id = "44444444-4444-4444-8444-444444444444"
    selected = [{
        "id": 4,
        "externalId": external_id,
        "name": "Неоднозначная фасовка",
        "unit": "г",
        "cost": 175,
    }]
    base = [{
        "id": 4,
        "externalId": external_id,
        "name": "Неоднозначная фасовка",
        "unit": "г",
        "cost": 17.5,
    }]

    result = build_catalog_review({"teas": []}, selected, base)

    assert result["items"][0]["suggested_price"] is None
    assert result["items"][0]["suggested_stock"] is None
    assert result["items"][0]["can_create_draft"] is False
    assert "нельзя подтвердить" in result["items"][0]["note"]


def test_review_surfaces_base_catalog_items_missing_from_selected_price_list():
    selected_external = "55555555-5555-4555-8555-555555555555"
    base_only_external = "66666666-6666-4666-8666-666666666666"
    selected = [{
        "id": 5,
        "externalId": selected_external,
        "name": "Товар прайс-листа",
        "unit": "г",
        "cost": 20,
        "balance": 100,
    }]
    base = [
        selected[0],
        {
            "id": 6,
            "externalId": base_only_external,
            "name": "Товар только основного каталога",
            "unit": "г",
            "cost": 30,
            "balance": 100,
        },
    ]

    result = build_catalog_review({"teas": []}, selected, base)

    assert result["counts"] == {
        "saby_items": 1,
        "base_items": 2,
        "linked": 0,
        "new": 1,
        "not_in_price_list": 1,
        "ready_for_draft": 1,
    }
    base_only = next(item for item in result["items"] if item["status"] == "not_in_price_list")
    assert base_only["name"] == "Товар только основного каталога"
    assert base_only["can_create_draft"] is False
    assert "не добавлено в прайс-лист" in base_only["note"]

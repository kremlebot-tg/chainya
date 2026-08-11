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

from decimal import Decimal

import pytest

from backend.saby_purchase import (
    SabyFiscalSettings,
    SabyPurchaseError,
    build_fiscal_nomenclatures,
    build_fiscal_sale,
    purchase_route_status,
)


def fiscal_settings(**changes):
    values = {
        "point_id": "274",
        "company_id": "274",
        "kkt_reg_number": "0001234567890123",
        "tax_system": 2,
        "pay_method": 4,
    }
    values.update(changes)
    return SabyFiscalSettings(**values)


def order(**changes):
    values = {
        "id": "7E41D55A13E7",
        "total": 350,
        "delivery_price": 0,
        "customer": {"name": "Тест", "phone": "+7 999 000-00-00"},
        "items": [{
            "name": "Бай Хао Инь Чжень", "pack": 10, "qty": 2,
            "unit_price": 175, "total": 350,
        }],
    }
    values.update(changes)
    return values


def test_delivery_route_preserves_existing_implemented_path():
    status = purchase_route_status({}, tbank_receipt_enabled=True)
    assert status.route == "delivery"
    assert status.implemented is True
    assert status.writes_enabled is True


def test_fiscal_sale_is_ready_only_with_one_receipt_source_and_kkt():
    ready = purchase_route_status(
        {"SABY_PURCHASE_ROUTE": "fiscal_sale"},
        tbank_receipt_enabled=False,
        fiscal_settings=fiscal_settings(),
    )
    duplicate = purchase_route_status(
        {"SABY_PURCHASE_ROUTE": "fiscal_sale"},
        tbank_receipt_enabled=True,
        fiscal_settings=fiscal_settings(),
    )
    missing = purchase_route_status(
        {"SABY_PURCHASE_ROUTE": "fiscal_sale"},
        tbank_receipt_enabled=False,
        fiscal_settings=SabyFiscalSettings(),
    )
    assert ready.implemented is True and ready.writes_enabled is True
    assert duplicate.writes_enabled is False
    assert any("второй фискальный чек" in item for item in duplicate.blockers)
    assert missing.writes_enabled is False
    assert any("параметры ККТ" in item for item in missing.blockers)


def test_external_ofd_and_unknown_route_remain_fail_closed():
    external = purchase_route_status(
        {"SABY_PURCHASE_ROUTE": "external_ofd"}, tbank_receipt_enabled=True
    )
    unknown = purchase_route_status(
        {"SABY_PURCHASE_ROUTE": "anything"}, tbank_receipt_enabled=False
    )
    assert external.writes_enabled is False
    assert "настраивается в Saby" in external.blockers[0]
    assert unknown.route == "invalid" and unknown.valid is False


def test_fiscal_sale_converts_grams_to_kg_and_keeps_checkout_total():
    payload = build_fiscal_sale(order(), settings=fiscal_settings())
    line = payload["nomenclatures"][0]
    assert payload["companyID"] == "274"
    assert payload["kktRegNumber"] == "0001234567890123"
    assert payload["operationType"] == "1"
    assert payload["internetSum"] == "350.00"
    assert payload["cashSum"] is None
    assert payload["bankSum"] is None
    assert payload["prepaySum"] is None
    assert payload["vatNone"] == "350.00"
    assert payload["vatSum0"] is None
    assert payload["vatSum20"] is None
    assert payload["allowRetailPayed"] == "0"
    assert payload["taxSystem"] == "2"
    assert payload["payMethod"] == "4"
    assert line["measureNomenclature"] == "кг"
    assert line["quantityNomenclature"] == "0.020"
    assert line["priceNomenclature"] == "17500.00"
    assert line["totalPriceNomenclature"] == "350.00"
    # The concrete official JSON request uses ``propVal`` even though the
    # parameter table currently truncates the label to ``propVa``.
    assert payload["propVal"] == "7E41D55A13E7"
    assert "propVa" not in payload
    assert payload["customerEmail"] is None
    assert payload["customerINN"] is None
    assert payload["externalId"] == "chainya-7e41d55a13e7-sale"


def test_fiscal_sale_matches_documented_legacy_wire_types():
    payload = build_fiscal_sale(
        order(),
        settings=fiscal_settings(allow_negative_stock=True),
    )
    line = payload["nomenclatures"][0]

    # Saby's current concrete request example uses strings for identifiers,
    # operation flags and monetary scalars, with JSON null for unused buckets.
    assert isinstance(payload["companyID"], str)
    assert isinstance(payload["kktRegNumber"], str)
    assert isinstance(payload["operationType"], str)
    assert isinstance(payload["allowRetailPayed"], str)
    assert payload["allowRetailPayed"] == "1"
    assert isinstance(payload["internetSum"], str)
    assert payload["cashSum"] is None
    assert payload["bankSum"] is None
    assert payload["customerEmail"] is None
    assert payload["customerINN"] is None
    assert isinstance(payload["taxSystem"], str)
    assert isinstance(payload["payMethod"], str)
    assert isinstance(line["priceNomenclature"], str)
    assert isinstance(line["quantityNomenclature"], str)
    assert isinstance(line["totalPriceNomenclature"], str)


@pytest.mark.parametrize(
    ("pack", "qty", "unit_price", "expected_quantity"),
    [
        (10, 1, 175, "0.010"),
        (25, 1, 440, "0.025"),
        (50, 1, 880, "0.050"),
        (100, 1, 1760, "0.100"),
        (25, 3, 440, "0.075"),
    ],
)
def test_fiscal_nomenclatures_preserve_gram_precision_after_serialization(
    pack, qty, unit_price, expected_quantity
):
    total = unit_price * qty
    line = build_fiscal_nomenclatures([{
        "name": "Тестовый чай",
        "pack": pack,
        "qty": qty,
        "unit_price": unit_price,
        "total": total,
    }])[0]
    assert line["quantityNomenclature"] == expected_quantity
    serialized_total = (
        Decimal(line["priceNomenclature"])
        * Decimal(line["quantityNomenclature"])
    ).quantize(Decimal("0.01"))
    assert serialized_total == Decimal(total).quantize(Decimal("0.01"))


def test_fiscal_nomenclatures_fail_closed_on_serialized_rounding_mismatch():
    with pytest.raises(SabyPurchaseError, match="расходятся после округления"):
        build_fiscal_nomenclatures([{
            "name": "Некорректная дробная позиция",
            "pack": 25,
            "qty": 43,
            "unit_price": 1,
            "total": 21,
        }])


def test_fiscal_sale_supports_piece_item_delivery_and_full_return():
    source = order(
        total=665,
        delivery_price=490,
        items=[{
            "name": "Пуэр в мандарине", "pack": "pc", "qty": 1,
            "unit_price": 175, "total": 175,
        }],
    )
    sale = build_fiscal_sale(source, settings=fiscal_settings())
    refund = build_fiscal_sale(source, settings=fiscal_settings(), refund=True)
    assert sale["nomenclatures"][0]["measureNomenclature"] == "шт"
    assert sale["nomenclatures"][0]["quantityNomenclature"] == "1.00"
    assert sale["nomenclatures"][1]["kindNomenclature"] == "у"
    assert sale["nomenclatures"][1]["quantityNomenclature"] == "1.00"
    assert refund["operationType"] == "2"
    assert refund["externalId"].endswith("-refund")


def test_one_stage_sale_rejects_a_second_settlement_receipt():
    with pytest.raises(SabyPurchaseError, match="полный расчёт уже создаётся"):
        build_fiscal_sale(order(), settings=fiscal_settings(), settlement=True)


def test_fiscal_sale_rejects_invalid_phone_and_sum_mismatch():
    with pytest.raises(SabyPurchaseError, match="российский номер"):
        build_fiscal_sale(
            order(customer={"name": "Тест", "phone": "123"}),
            settings=fiscal_settings(),
        )
    with pytest.raises(SabyPurchaseError, match="не совпадает"):
        build_fiscal_sale(order(total=351), settings=fiscal_settings())


def test_fiscal_sale_requires_customer_name_for_receipt():
    with pytest.raises(SabyPurchaseError, match="имя покупателя"):
        build_fiscal_sale(
            order(customer={"name": "   ", "phone": "+7 999 000-00-00"}),
            settings=fiscal_settings(),
        )


@pytest.mark.parametrize("registration_number", ["12345678", "1" * 15, "1" * 21])
def test_fiscal_settings_require_16_to_20_digit_registration_number(
    registration_number,
):
    settings = fiscal_settings(kkt_reg_number=registration_number)
    assert settings.configured is False
    assert "SABY_OFD_KKT_REG_NUMBER" in settings.missing


@pytest.mark.parametrize("registration_number", ["1" * 16, "1" * 20])
def test_fiscal_settings_accept_fns_registration_number_lengths(
    registration_number,
):
    assert fiscal_settings(kkt_reg_number=registration_number).configured is True


def test_fiscal_settings_reject_all_zero_registration_number():
    settings = fiscal_settings(kkt_reg_number="0" * 16)
    assert settings.configured is False
    assert "SABY_OFD_KKT_REG_NUMBER" in settings.missing


def test_fiscal_settings_report_names_not_secret_values():
    settings = SabyFiscalSettings.from_env({
        "SABY_POINT_ID": "",
        "SABY_OFD_KKT_REG_NUMBER": "bad",
        "SABY_OFD_TAX_SYSTEM": "typo",
    })
    public = settings.public_dict()
    assert public["configured"] is False
    assert "SABY_POINT_ID" in public["missing"]
    assert "SABY_OFD_COMPANY_ID" in public["missing"]
    assert public["point_binding_confirmed"] is False
    assert "bad" not in repr(public)


def test_fiscal_settings_never_infer_company_id_from_sales_point():
    settings = SabyFiscalSettings.from_env({
        "SABY_POINT_ID": "274",
        "SABY_OFD_KKT_REG_NUMBER": "0001234567890123",
        "SABY_OFD_PAY_METHOD": "4",
    })
    assert settings.company_id == ""
    assert settings.point_id == "274"
    assert settings.configured is False
    assert "SABY_OFD_COMPANY_ID" in settings.missing


def test_fiscal_company_id_must_match_the_selected_sales_point():
    settings = SabyFiscalSettings.from_env({
        "SABY_POINT_ID": "274",
        "SABY_OFD_COMPANY_ID": "275",
        "SABY_OFD_KKT_REG_NUMBER": "0001234567890123",
        "SABY_OFD_PAY_METHOD": "4",
    })
    assert settings.company_id == "275"
    assert settings.configured is False
    assert "SABY_OFD_COMPANY_ID_POINT_MISMATCH" in settings.missing
    assert settings.public_dict()["point_binding_confirmed"] is False


def test_explicit_matching_fiscal_company_id_confirms_point_binding():
    settings = SabyFiscalSettings.from_env({
        "SABY_POINT_ID": "274",
        "SABY_OFD_COMPANY_ID": "274",
        "SABY_OFD_KKT_REG_NUMBER": "0001234567890123",
        "SABY_OFD_PAY_METHOD": "4",
    })
    assert settings.configured is True
    assert settings.public_dict()["point_binding_confirmed"] is True


def test_fiscal_route_never_assumes_payment_method():
    settings = SabyFiscalSettings.from_env({
        "SABY_POINT_ID": "274",
        "SABY_OFD_COMPANY_ID": "274",
        "SABY_OFD_KKT_REG_NUMBER": "0001234567890123",
    })
    assert settings.configured is False
    assert "SABY_OFD_PAY_METHOD" in settings.missing


def test_prepayment_setting_cannot_replace_required_full_payment_flow():
    settings = SabyFiscalSettings.from_env({
        "SABY_POINT_ID": "274",
        "SABY_OFD_COMPANY_ID": "274",
        "SABY_OFD_KKT_REG_NUMBER": "0001234567890123",
        "SABY_OFD_PAY_METHOD": "1",
    })
    assert settings.configured is False
    assert "SABY_OFD_PAY_METHOD" in settings.missing

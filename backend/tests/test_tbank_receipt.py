import pytest

from backend.tbank_receipt import (
    TBankReceiptError,
    TBankReceiptSettings,
    build_receipt,
)


def settings(**changes):
    values = {
        "enabled": True,
        "taxation": "usn_income",
        "item_tax": "none",
        "delivery_tax": "none",
        "ffd_version": "1.2",
    }
    values.update(changes)
    return TBankReceiptSettings(**values)


def test_builds_exact_sale_or_refund_receipt_with_delivery():
    result = build_receipt(
        phone="+79991234567",
        items=[
            {"name": "Бай Хао", "pack": 25, "qty": 2, "unit_price": 440, "total": 880}
        ],
        delivery_price=490,
        settings=settings(),
    )
    assert result["Phone"] == "+79991234567"
    assert result["Taxation"] == "usn_income"
    assert result["Items"] == [
        {
            "Name": "Бай Хао · 25 г", "Price": 44_000, "Quantity": 2, "Amount": 88_000,
            "Tax": "none", "PaymentMethod": "full_prepayment",
            "PaymentObject": "commodity", "MeasurementUnit": "шт",
        },
        {
            "Name": "Доставка", "Price": 49_000, "Quantity": 1, "Amount": 49_000,
            "Tax": "none", "PaymentMethod": "full_prepayment",
            "PaymentObject": "service", "MeasurementUnit": "шт",
        },
    ]


def test_ffd_105_omits_measurement_unit():
    result = build_receipt(
        phone="+79991234567",
        items=[{"name": "Чай", "qty": 1, "unit_price": 100, "total": 100}],
        delivery_price=0,
        settings=settings(ffd_version="1.05"),
    )
    assert "MeasurementUnit" not in result["Items"][0]


def test_pack_suffix_is_kept_inside_tbank_name_limit():
    result = build_receipt(
        phone="+79991234567",
        items=[
            {"name": "Ч" * 128, "pack": 100, "qty": 1, "unit_price": 100, "total": 100}
        ],
        delivery_price=0,
        settings=settings(),
    )
    name = result["Items"][0]["Name"]
    assert len(name) == 128
    assert name.endswith(" · 100 г")


@pytest.mark.parametrize("pack", [0, -25, True, "25.5", "invalid"])
def test_rejects_invalid_pack_values(pack):
    with pytest.raises(TBankReceiptError, match="фасовка"):
        build_receipt(
            phone="+79991234567",
            items=[
                {"name": "Чай", "pack": pack, "qty": 1, "unit_price": 100, "total": 100}
            ],
            delivery_price=0,
            settings=settings(),
        )


@pytest.mark.parametrize(
    "receipt_settings",
    [
        settings(enabled=False),
        settings(taxation=""),
        settings(item_tax="vat20"),
        settings(delivery_tax="vat20"),
        settings(ffd_version="1.1"),
    ],
)
def test_unknown_or_disabled_tax_configuration_fails_closed(receipt_settings):
    assert receipt_settings.configured is False
    with pytest.raises(TBankReceiptError):
        build_receipt(
            phone="+79991234567",
            items=[{"name": "Чай", "qty": 1, "unit_price": 100, "total": 100}],
            delivery_price=0,
            settings=receipt_settings,
        )


def test_rejects_mismatched_amount_and_bad_phone():
    with pytest.raises(TBankReceiptError):
        build_receipt(
            phone="79991234567",
            items=[{"name": "Чай", "qty": 2, "unit_price": 100, "total": 100}],
            delivery_price=0,
            settings=settings(),
        )

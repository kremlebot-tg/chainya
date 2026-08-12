from decimal import Decimal

import pytest

from backend.stock_guard import StockGuardError, requirements_for_lines


def line(*, site_id="tea", external_id="ext", pack=25, qty=2, name="Чай"):
    return {
        "id": site_id,
        "name": name,
        "pack": pack,
        "qty": qty,
        "saby": {"external_id": external_id},
    }


def test_gram_requirements_use_physical_base_balance():
    result = requirements_for_lines(
        [line(pack=25, qty=2), line(pack=100, qty=1)],
        [{"externalId": "ext", "unit": "г", "balance": 175}],
    )
    assert result[0].quantity == Decimal(150)
    assert result[0].available == Decimal(175)
    assert result[0].unit == "g"


def test_piece_requirements_use_pieces():
    result = requirements_for_lines(
        [line(pack="pc", qty=3)],
        [{"externalId": "ext", "unit": "шт.", "balance": 4}],
    )
    assert result[0].quantity == Decimal(3)
    assert result[0].available == Decimal(4)
    assert result[0].unit == "pc"


@pytest.mark.parametrize(
    "catalog",
    [
        [],
        [{"externalId": "ext", "unit": "г", "balance": None}],
        [{"externalId": "ext", "unit": "г", "balance": -1}],
        [{"externalId": "ext", "unit": "шт", "balance": 10}],
    ],
)
def test_unknown_or_unsafe_stock_fails_closed(catalog):
    with pytest.raises(StockGuardError):
        requirements_for_lines([line()], catalog)


def test_duplicate_external_id_fails_closed():
    with pytest.raises(StockGuardError):
        requirements_for_lines(
            [line()],
            [
                {"externalId": "ext", "unit": "г", "balance": 50},
                {"externalId": "ext", "unit": "г", "balance": 50},
            ],
        )

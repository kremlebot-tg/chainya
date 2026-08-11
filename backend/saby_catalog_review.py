"""Build owner-reviewed catalog suggestions from read-only Saby responses."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .saby_sync import mapping_for_catalog


GRAM_UNITS = {"г", "g", "грамм", "gram"}
PIECE_UNITS = {"шт", "pc", "штука", "piece"}


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _unit(value: Any) -> str:
    return str(value or "").strip().lower().replace(".", "")


def _integer_price(value: Decimal | None) -> int | None:
    if value is None or value <= 0:
        return None
    rounded = value.to_integral_value()
    return int(rounded) if abs(value - rounded) <= Decimal("0.005") else None


def _proposal(
    item: Mapping[str, Any], base: Mapping[str, Any] | None
) -> tuple[str | None, int | None, bool | None, str]:
    """Infer a storefront unit and price only when Saby values prove it."""

    unit = _unit(item.get("unit"))
    cost = _decimal(item.get("cost"))
    balance = _decimal(item.get("balance"))
    if unit in PIECE_UNITS:
        stock = None if balance is None else balance >= 1
        return "pc", _integer_price(cost), stock, "Цена за штуку"
    if unit not in GRAM_UNITS:
        return None, None, None, "Единица продажи Saby не распознана"

    sale_quantum = Decimal(1)
    if base is not None and _unit(base.get("unit")) in GRAM_UNITS:
        base_cost = _decimal(base.get("cost"))
        base_balance = _decimal(base.get("balance"))
        if base_cost and base_cost > 0 and cost and cost > 0:
            ratio = cost / base_cost
            rounded = ratio.to_integral_value()
            looks_like_ten_gram_price = (
                rounded == Decimal(10)
                and abs(ratio - rounded) <= Decimal("0.005")
            )
            balances_agree = (
                balance is not None
                and base_balance is not None
                and abs(balance * rounded - base_balance) <= Decimal("0.005")
            )
            if looks_like_ten_gram_price and (balance is None or base_balance is None):
                return (
                    "g",
                    None,
                    None,
                    "СБИС не вернул остаток: фасовку 10 г нельзя подтвердить автоматически",
                )
            if rounded in {Decimal(1), Decimal(10)} and abs(ratio - rounded) <= Decimal("0.005") and balances_agree:
                sale_quantum = rounded
    site_price = _integer_price(cost * (Decimal(10) / sale_quantum) if cost is not None else None)
    minimum = Decimal(10) / sale_quantum
    stock = None if balance is None else balance >= minimum
    note = "Прайс-лист Saby: порция 10 г" if sale_quantum == 10 else "Цена Saby пересчитана на 10 г"
    return "g", site_price, stock, note


def build_catalog_review(
    document: Mapping[str, Any],
    saby_catalog: Sequence[Mapping[str, Any]],
    base_catalog: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return secret-free suggestions. This function never mutates either catalog."""

    teas = [item for item in document.get("teas", []) if isinstance(item, Mapping)]
    mapping = mapping_for_catalog(teas)
    linked_external = {ref.external_id: site_id for site_id, ref in mapping.items()}
    base_by_external = {
        str(item.get("externalId") or "").strip(): item
        for item in base_catalog
        if isinstance(item, Mapping) and not item.get("isParent") and item.get("externalId")
    }
    rows: list[dict[str, Any]] = []
    for item in saby_catalog:
        if not isinstance(item, Mapping) or item.get("isParent"):
            continue
        external_id = str(item.get("externalId") or "").strip()
        name = str(item.get("name") or "").strip()
        try:
            saby_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not external_id or not name or saby_id <= 0:
            continue
        site_id = linked_external.get(external_id, "")
        unit, price, stock, note = _proposal(item, base_by_external.get(external_id))
        rows.append({
            "status": "linked" if site_id else "new",
            "site_id": site_id,
            "saby_id": saby_id,
            "external_id": external_id,
            "name": name,
            "unit": unit,
            "suggested_price": price,
            "suggested_stock": stock,
            "note": note,
            "can_create_draft": not site_id and unit is not None and price is not None,
        })
    rows.sort(key=lambda row: (row["status"] != "new", row["name"].casefold()))
    return {
        "read_only_source": True,
        "catalog_changed": False,
        "counts": {
            "saby_items": len(rows),
            "linked": sum(row["status"] == "linked" for row in rows),
            "new": sum(row["status"] == "new" for row in rows),
            "ready_for_draft": sum(row["can_create_draft"] for row in rows),
        },
        "items": rows,
    }


__all__ = ["build_catalog_review"]

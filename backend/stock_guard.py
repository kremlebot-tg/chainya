"""Read-only Saby stock validation for the public checkout.

Saby remains the inventory source of truth.  This module converts its base
catalogue balances to the units used by the website and deliberately refuses
to guess when a mapped item, unit, or balance is missing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class StockGuardError(ValueError):
    """Stock could not be verified safely."""


@dataclass(frozen=True)
class StockRequirement:
    site_id: str
    name: str
    unit: str
    quantity: Decimal
    available: Decimal


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _unit(value: Any) -> str:
    return str(value or "").strip().casefold().replace(".", "")


def _catalog_by_external_id(
    base_catalog: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    by_external: dict[str, Mapping[str, Any]] = {}
    for item in base_catalog:
        external_id = str(item.get("externalId") or "").strip()
        if not external_id:
            continue
        if external_id in by_external:
            raise StockGuardError("Saby вернул повторяющуюся товарную позицию")
        by_external[external_id] = item
    return by_external


def verify_line_names(
    lines: Sequence[Mapping[str, Any]],
    base_catalog: Sequence[Mapping[str, Any]],
) -> None:
    """Require exact Saby names for every mapped checkout line.

    Saby creates a new nomenclature item when an external sale contains a
    different name. This preflight is mandatory even when the optional
    balance guard is disabled.
    """

    by_external = _catalog_by_external_id(base_catalog)
    for line in lines:
        site_id = str(line.get("id") or "").strip()
        name = str(line.get("name") or site_id or "Товар")
        saby = line.get("saby")
        external_id = (
            str(saby.get("external_id") or "").strip()
            if isinstance(saby, Mapping)
            else ""
        )
        item = by_external.get(external_id)
        if not site_id or not external_id or item is None:
            raise StockGuardError(f"Не удалось проверить товар в Saby: {name}")
        if name != str(item.get("name") or "").strip():
            raise StockGuardError(f"Название товара не совпадает с Saby: {name}")


def verify_unique_catalog_name(
    name: str,
    base_catalog: Sequence[Mapping[str, Any]],
) -> None:
    """Require exactly one Saby item for a generated fiscal line."""

    expected = str(name or "").strip()
    matches = [
        item for item in base_catalog
        if str(item.get("name") or "").strip() == expected
    ]
    if len(matches) != 1:
        raise StockGuardError(
            f"В Saby должна быть ровно одна позиция с названием: {expected}"
        )


def requirements_for_lines(
    lines: Sequence[Mapping[str, Any]],
    base_catalog: Sequence[Mapping[str, Any]],
) -> list[StockRequirement]:
    """Return aggregated requirements using base Saby balances.

    The selected website price list may expose a ten-gram sale quantum and a
    correspondingly divided balance.  The base catalogue is therefore used as
    the canonical physical balance: grams for weighed tea and pieces for
    piece goods.
    """

    by_external = _catalog_by_external_id(base_catalog)

    required: dict[str, Decimal] = defaultdict(Decimal)
    metadata: dict[str, tuple[str, str, Decimal]] = {}
    for line in lines:
        site_id = str(line.get("id") or "").strip()
        name = str(line.get("name") or site_id or "Товар")
        saby = line.get("saby")
        external_id = (
            str(saby.get("external_id") or "").strip()
            if isinstance(saby, Mapping)
            else ""
        )
        item = by_external.get(external_id)
        if not site_id or not external_id or item is None:
            raise StockGuardError(f"Не удалось проверить остаток: {name}")
        saby_name = str(item.get("name") or "").strip()
        if name != saby_name:
            raise StockGuardError(f"Название товара не совпадает с Saby: {name}")
        balance = _decimal(item.get("balance"))
        if balance is None or balance < 0:
            raise StockGuardError(f"Saby не подтвердил остаток: {name}")

        qty = _decimal(line.get("qty"))
        pack = line.get("pack")
        saby_unit = _unit(item.get("unit"))
        if qty is None or qty <= 0:
            raise StockGuardError(f"Некорректное количество: {name}")
        if pack == "pc":
            if saby_unit not in {"шт", "pc", "штука", "piece"}:
                raise StockGuardError(f"Не удалось проверить единицу товара: {name}")
            website_unit = "pc"
            amount = qty
        else:
            grams = _decimal(pack)
            if grams is None or grams <= 0 or saby_unit not in {
                "г", "g", "грамм", "gram",
            }:
                raise StockGuardError(f"Не удалось проверить единицу товара: {name}")
            website_unit = "g"
            amount = grams * qty

        previous = metadata.get(site_id)
        if previous and previous[:2] != (name, website_unit):
            raise StockGuardError(f"Противоречивые строки товара: {name}")
        required[site_id] += amount
        metadata[site_id] = (name, website_unit, balance)

    return [
        StockRequirement(site_id, metadata[site_id][0], metadata[site_id][1], amount,
                         metadata[site_id][2])
        for site_id, amount in sorted(required.items())
    ]


__all__ = [
    "StockGuardError", "StockRequirement", "requirements_for_lines",
    "verify_line_names", "verify_unique_catalog_name",
]

"""Read-only comparison between the Chainya storefront and Saby Retail.

This module deliberately has no transport or persistence code.  It accepts two
already loaded catalogs and returns a serialisable report; it cannot update the
storefront, Saby, orders, prices, or balances.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .saby_sync import SABY_NOMENCLATURE_BY_SITE_ID, SabyNomenclatureRef


DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class SabyShadowSettings:
    """Fail-closed settings for the read-only background check."""

    enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SabyShadowSettings":
        source = os.environ if environ is None else environ
        enabled = source.get("SABY_CATALOG_SHADOW_MODE", "off").strip().lower() == "on"
        raw_interval = source.get(
            "SABY_CATALOG_SHADOW_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)
        ).strip()
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_SECONDS
        interval = min(max(interval, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)
        return cls(enabled=enabled, interval_seconds=interval)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _normalised_unit(value: Any) -> str:
    return str(value or "").strip().lower().replace(".", "")


def _difference(
    kind: str,
    severity: str,
    *,
    site_id: str = "",
    name: str = "",
    message: str,
    site_value: Any = None,
    saby_value: Any = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "site_id": site_id,
        "name": name,
        "message": message,
        "site_value": site_value,
        "saby_value": saby_value,
    }


def compare_catalogs(
    site_catalog: Mapping[str, Mapping[str, Any]],
    saby_catalog: Sequence[Mapping[str, Any]],
    mapping: Mapping[str, SabyNomenclatureRef] = SABY_NOMENCLATURE_BY_SITE_ID,
    *,
    saby_base_catalog: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, read-only price and availability comparison.

    Chainya stores loose-tea prices per 10 grams.  Saby normally returns a
    price per gram, but a selected price list may expose a 10-gram sale portion
    while still labelling it ``г``.  When the optional base catalog confirms
    both the price and balance ratio, that sale quantum is normalised before
    comparison.  Piece products are compared one-to-one.  Unknown units are
    reported instead of guessed.
    """

    products = [item for item in saby_catalog if isinstance(item, Mapping) and not item.get("isParent")]
    by_external: dict[str, Mapping[str, Any]] = {}
    base_by_external: dict[str, Mapping[str, Any]] = {}
    duplicate_external_ids: set[str] = set()
    missing_external_id_items: list[Mapping[str, Any]] = []
    for item in products:
        external_id = str(item.get("externalId") or "").strip()
        if not external_id:
            missing_external_id_items.append(item)
            continue
        if external_id in by_external:
            duplicate_external_ids.add(external_id)
        else:
            by_external[external_id] = item
    for item in saby_base_catalog or ():
        if not isinstance(item, Mapping) or item.get("isParent"):
            continue
        external_id = str(item.get("externalId") or "").strip()
        if external_id and external_id not in base_by_external:
            base_by_external[external_id] = item

    differences: list[dict[str, Any]] = []
    compared = price_matches = stock_matches = 0

    for item in missing_external_id_items:
        differences.append(_difference(
            "saby_item_without_external_id", "warning",
            name=str(item.get("name") or item.get("id") or "Позиция Saby"),
            message="Saby вернул позицию без externalId; её нельзя безопасно сопоставить.",
            saby_value={"id": item.get("id")},
        ))

    active_site_ids = {
        str(site_id)
        for site_id, item in site_catalog.items()
        if item.get("stock") is True and item.get("published", True) is not False
    }
    for site_id in sorted(active_site_ids - set(mapping)):
        item = site_catalog.get(site_id, {})
        differences.append(_difference(
            "unmapped_site_item", "error", site_id=site_id,
            name=str(item.get("name") or site_id),
            message="Активная позиция сайта не связана с Saby.",
        ))
    inactive_unmapped_ids = {
        str(site_id)
        for site_id, item in site_catalog.items()
        if item.get("published", True) is not False
        and item.get("stock") is not True
        and str(site_id) not in mapping
    }
    for site_id in sorted(inactive_unmapped_ids):
        item = site_catalog.get(site_id, {})
        differences.append(_difference(
            "unmapped_inactive_site_item", "info", site_id=site_id,
            name=str(item.get("name") or site_id),
            message="Недоступная позиция сайта пока не связана с Saby.",
        ))

    for site_id, ref in mapping.items():
        site_item = site_catalog.get(site_id)
        if site_item is None:
            differences.append(_difference(
                "missing_on_site", "error", site_id=site_id,
                message="Связанная позиция отсутствует в каталоге сайта.",
                saby_value=ref.external_id,
            ))
            continue
        name = str(site_item.get("name") or site_id)
        saby_item = by_external.get(ref.external_id)
        if saby_item is None:
            differences.append(_difference(
                "missing_in_saby", "error", site_id=site_id, name=name,
                message="Позиция сайта не найдена в каталоге Saby.",
                site_value=ref.external_id,
            ))
            continue
        compared += 1
        if str(saby_item.get("id")) != str(ref.id):
            differences.append(_difference(
                "saby_id_mismatch", "error", site_id=site_id, name=name,
                message="Числовой id Saby не совпадает с проверенной таблицей соответствий.",
                site_value={"mapped_saby_id": ref.id},
                saby_value={"id": saby_item.get("id"), "external_id": ref.external_id},
            ))

        site_unit = _normalised_unit(site_item.get("unit"))
        saby_unit = _normalised_unit(saby_item.get("unit"))
        site_price = _decimal(site_item.get("price"))
        saby_cost = _decimal(saby_item.get("cost"))
        balance = _decimal(saby_item.get("balance"))
        sale_quantum = Decimal(1)
        base_item = base_by_external.get(ref.external_id)
        if (
            site_unit == "g"
            and saby_unit in {"г", "g", "грамм", "gram"}
            and base_item is not None
            and _normalised_unit(base_item.get("unit")) in {"г", "g", "грамм", "gram"}
        ):
            base_cost = _decimal(base_item.get("cost"))
            base_balance = _decimal(base_item.get("balance"))
            if base_cost and base_cost > 0 and saby_cost and saby_cost > 0:
                candidate = saby_cost / base_cost
                rounded = candidate.to_integral_value()
                balances_agree = (
                    balance is not None
                    and base_balance is not None
                    and abs(balance * rounded - base_balance) <= Decimal("0.005")
                )
                supported_quantum = rounded in {Decimal(1), Decimal(10)}
                if (
                    supported_quantum
                    and abs(candidate - rounded) <= Decimal("0.005")
                    and balances_agree
                ):
                    sale_quantum = rounded
                    if sale_quantum > 1:
                        differences.append(_difference(
                            "saby_sale_quantum_inferred", "info", site_id=site_id,
                            name=name,
                            message=(
                                "Базовый каталог Saby подтверждает цену за грамм, "
                                "а прайс-лист сайта возвращает продажную порцию 10 г."
                            ),
                            site_value={
                                "price": _number(site_price), "unit": "10 г",
                            },
                            saby_value={
                                "base_cost": _number(base_cost),
                                "price_list_cost": _number(saby_cost),
                                "sale_quantum_g": _number(sale_quantum),
                            },
                        ))
        expected_cost: Decimal | None = None
        if site_unit == "g" and saby_unit in {"г", "g", "грамм", "gram"}:
            expected_cost = (
                site_price / Decimal(10) * sale_quantum
                if site_price is not None else None
            )
        elif site_unit == "pc" and saby_unit in {"шт", "pc", "штука", "piece"}:
            expected_cost = site_price

        if expected_cost is None or saby_cost is None:
            differences.append(_difference(
                "price_not_comparable", "warning", site_id=site_id, name=name,
                message="Цена не сравнена: Saby не вернул число или единицы не совпадают.",
                site_value={"price": _number(site_price), "unit": site_item.get("unit")},
                saby_value={"cost": _number(saby_cost), "unit": saby_item.get("unit")},
            ))
        elif abs(expected_cost - saby_cost) <= Decimal("0.005"):
            price_matches += 1
        else:
            differences.append(_difference(
                "price_mismatch", "warning", site_id=site_id, name=name,
                message="Цена сайта и цена за продажную единицу Saby различаются.",
                site_value={
                    "price": _number(site_price),
                    "unit": "10 г" if site_unit == "g" else "шт",
                    "expected_saby_cost": _number(expected_cost),
                },
                saby_value={"cost": _number(saby_cost), "unit": saby_item.get("unit")},
            ))

        site_in_stock = site_item.get("stock") is True
        minimum_balance: Decimal | None = None
        if site_unit == "g" and saby_unit in {"г", "g", "грамм", "gram"}:
            minimum_balance = Decimal(10) / sale_quantum
        elif site_unit == "pc" and saby_unit in {"шт", "pc", "штука", "piece"}:
            minimum_balance = Decimal(1)
        if balance is None or minimum_balance is None:
            differences.append(_difference(
                "stock_not_comparable", "warning", site_id=site_id, name=name,
                message="Остаток не сравнён: Saby не вернул число или единицы не совпадают.",
                site_value={"in_stock": site_in_stock},
                saby_value={"balance": saby_item.get("balance"), "unit": saby_item.get("unit")},
            ))
        else:
            saby_in_stock = balance >= minimum_balance
            if site_in_stock == saby_in_stock:
                stock_matches += 1
            else:
                saby_stock_value = {
                    "in_stock": saby_in_stock,
                    "balance": _number(balance),
                    "minimum_to_sell": _number(minimum_balance),
                    "unit": saby_item.get("unit"),
                }
                if sale_quantum > 1:
                    saby_stock_value["sale_quantum_g"] = _number(sale_quantum)
                differences.append(_difference(
                    "stock_mismatch", "warning", site_id=site_id, name=name,
                    message="Доступность на сайте не совпадает с остатком Saby.",
                    site_value={"in_stock": site_in_stock},
                    saby_value=saby_stock_value,
                ))

    for external_id in sorted(duplicate_external_ids):
        differences.append(_difference(
            "duplicate_saby_external_id", "error",
            message="Saby вернул повторяющийся externalId.", saby_value=external_id,
        ))

    expected_external_ids = {ref.external_id for ref in mapping.values()}
    for item in products:
        external_id = str(item.get("externalId") or "").strip()
        if external_id and external_id not in expected_external_ids:
            differences.append(_difference(
                "saby_only_item", "info", name=str(item.get("name") or item.get("id") or "Позиция"),
                message="Позиция есть в Saby, но не связана с активным товаром сайта.",
                saby_value={"id": item.get("id"), "external_id": external_id},
            ))

    actionable = [item for item in differences if item["severity"] in {"warning", "error"}]
    errors = [item for item in differences if item["severity"] == "error"]
    warnings = [item for item in differences if item["severity"] == "warning"]
    info = [item for item in differences if item["severity"] == "info"]
    return {
        "state": "ok" if not actionable else "differences",
        "read_only": True,
        "catalog_changed": False,
        "counts": {
            "site_items": len(site_catalog),
            "site_active_items": len(active_site_ids),
            "mapped_items": len(mapping),
            "saby_items": len(products),
            "compared_items": compared,
            "price_matches": price_matches,
            "stock_matches": stock_matches,
            "errors": len(errors),
            "warnings": len(warnings),
            "info": len(info),
            "actionable_differences": len(actionable),
        },
        "differences": differences,
    }


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "SabyShadowSettings",
    "compare_catalogs",
]

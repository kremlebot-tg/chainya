"""Доменная логика доставки CDEK для интернет-магазина Чайни.

Публичный сайт передаёт только корзину и выбранный город/ПВЗ. Вес, габариты,
тариф и цена повторно определяются на сервере, поэтому клиент не может
подменить стоимость доставки перед созданием платежа.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable


DELIVERY_TARIFFS = {
    "cdek_pvz": 136,       # Посылка склад-склад
    "cdek_courier": 137,   # Посылка склад-дверь
}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} вне допустимого диапазона")
    return value


@dataclass(frozen=True)
class CdekDeliverySettings:
    from_city_code: int
    sender_city: str
    sender_address: str
    sender_name: str
    sender_phone: str
    shipment_point: str
    package_length: int
    package_width: int
    package_height: int
    packaging_weight: int
    piece_weight: int

    @classmethod
    def from_env(cls) -> "CdekDeliverySettings":
        return cls(
            from_city_code=_env_int("CDEK_FROM_CITY_CODE", 44, 1, 9_999_999),
            sender_city=os.getenv("CDEK_SENDER_CITY", "Москва").strip() or "Москва",
            sender_address=os.getenv(
                "CDEK_SENDER_ADDRESS", "ул. Острякова, 3"
            ).strip() or "ул. Острякова, 3",
            sender_name=os.getenv("CDEK_SENDER_NAME", "Чайня").strip() or "Чайня",
            sender_phone=os.getenv("CDEK_SENDER_PHONE", "+79055908801").strip(),
            shipment_point=os.getenv("CDEK_SHIPMENT_POINT", "").strip().upper(),
            package_length=_env_int("CDEK_PACKAGE_LENGTH_CM", 20, 1, 150),
            package_width=_env_int("CDEK_PACKAGE_WIDTH_CM", 15, 1, 150),
            package_height=_env_int("CDEK_PACKAGE_HEIGHT_CM", 10, 1, 150),
            packaging_weight=_env_int("CDEK_PACKAGING_WEIGHT_G", 150, 1, 10_000),
            piece_weight=_env_int("CDEK_PIECE_WEIGHT_G", 100, 1, 10_000),
        )


def package_spec(
    items: Iterable[dict[str, Any]], settings: CdekDeliverySettings
) -> dict[str, int]:
    """Estimate one compact tea parcel from authoritative order lines."""
    product_weight = 0
    units = 0
    for item in items:
        qty = int(item["qty"])
        units += qty
        pack = item["pack"]
        product_weight += (
            settings.piece_weight if pack == "pc" else int(pack)
        ) * qty
    # Tea bags are light; the box height grows modestly for multi-pack orders.
    height = min(30, settings.package_height + max(0, units - 3) * 2)
    return {
        "weight": settings.packaging_weight + product_weight,
        "length": settings.package_length,
        "width": settings.package_width,
        "height": height,
    }


def tariff_payload(
    method: str,
    city_code: int,
    package: dict[str, int],
    settings: CdekDeliverySettings,
) -> dict[str, Any]:
    if method not in DELIVERY_TARIFFS:
        raise ValueError("Неизвестный способ доставки CDEK")
    return {
        "type": 1,
        "currency": 1,
        "lang": "rus",
        "tariff_code": DELIVERY_TARIFFS[method],
        "from_location": {"code": settings.from_city_code},
        "to_location": {"code": city_code},
        "packages": [package],
    }


def normalized_quote(
    method: str,
    city_code: int,
    package: dict[str, int],
    result: dict[str, Any],
) -> dict[str, Any]:
    try:
        price = int(math.ceil(float(result["delivery_sum"])))
        period_min = int(result["period_min"])
        period_max = int(result["period_max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("CDEK не вернул стоимость или срок доставки") from exc
    if price < 0 or period_min < 0 or period_max < period_min:
        raise ValueError("CDEK вернул некорректный расчёт доставки")
    return {
        "provider": "cdek",
        "method": method,
        "city_code": city_code,
        "tariff_code": DELIVERY_TARIFFS[method],
        "tariff_name": str(result.get("tariff_name", "Посылка CDEK"))[:160],
        "price": price,
        "period_min": period_min,
        "period_max": period_max,
        "weight": package["weight"],
        "length": package["length"],
        "width": package["width"],
        "height": package["height"],
    }


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def build_order_payload(
    order: dict[str, Any],
    quote: dict[str, Any],
    settings: CdekDeliverySettings,
) -> dict[str, Any]:
    """Build a prepaid CDEK order. Calling the provider remains a separate step."""
    customer = order["customer"]
    items = order["items"]
    if order["delivery"] not in DELIVERY_TARIFFS:
        raise ValueError("Для самовывоза отправление CDEK не требуется")
    if not settings.shipment_point:
        raise ValueError("Не выбран пункт CDEK, куда Чайня сдаёт отправления")
    if int(quote.get("tariff_code", 0)) != DELIVERY_TARIFFS[order["delivery"]]:
        raise ValueError("Тариф заказа не соответствует способу доставки")

    package_weight = int(quote["weight"])
    product_weights = []
    remaining = max(1, package_weight - settings.packaging_weight)
    raw_weights = [
        (settings.piece_weight if item["pack"] == "pc" else int(item["pack"]))
        for item in items
    ]
    raw_total = max(1, sum(weight * int(item["qty"]) for weight, item in zip(raw_weights, items)))
    for weight, item in zip(raw_weights, items):
        product_weights.append(max(1, round(remaining * weight / raw_total)))

    package_items = []
    for item, weight in zip(items, product_weights):
        package_items.append({
            "name": str(item["name"])[:255],
            "ware_key": str(item["id"])[:50],
            "payment": {"value": 0},
            "cost": float(item["unit_price"]),
            "weight": weight,
            "amount": int(item["qty"]),
        })

    payload: dict[str, Any] = {
        "number": order["id"],
        "tariff_code": int(quote["tariff_code"]),
        "comment": str(customer.get("note", ""))[:255],
        "shipment_point": settings.shipment_point,
        "sender": {
            "name": settings.sender_name,
            "phones": [{"number": normalize_phone(settings.sender_phone)}],
        },
        "recipient": {
            "name": str(customer.get("name") or "Получатель")[:255],
            "phones": [{"number": normalize_phone(customer["phone"])}],
        },
        "from_location": {
            "code": settings.from_city_code,
            "city": settings.sender_city,
            "address": settings.sender_address,
        },
        "to_location": {
            "code": int(quote["city_code"]),
            "city": str(customer.get("city", ""))[:255],
        },
        "packages": [{
            "number": "1",
            "weight": package_weight,
            "length": int(quote["length"]),
            "width": int(quote["width"]),
            "height": int(quote["height"]),
            "items": package_items,
        }],
    }
    if order["delivery"] == "cdek_pvz":
        payload["delivery_point"] = str(customer["pvz_code"]).upper()
    else:
        payload["to_location"]["address"] = str(customer["address"])[:255]
    return payload

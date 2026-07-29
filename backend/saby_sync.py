"""Pure, network-free preparation of Chainya orders for Saby Retail.

The module deliberately has no transport code.  It can validate the catalog and
build a payload for inspection, but it cannot send that payload to Saby.  A
separate integration step must explicitly pass the write policy before any
future caller is allowed to use a write API.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID

from .saby import SabySettings


class SabySyncError(ValueError):
    """A dry-run payload cannot be built safely."""


class SabySyncPolicyError(PermissionError):
    """The configured mode does not permit a Saby write."""


class SabySyncMode(str, Enum):
    """Rollout modes for the future order sender.

    ``off`` and ``shadow`` never permit writes.  ``manual`` additionally needs
    an explicit approval for a particular order.  ``auto`` may permit writes
    only outside Chainya's test mode.
    """

    OFF = "off"
    SHADOW = "shadow"
    MANUAL = "manual"
    AUTO = "auto"


@dataclass(frozen=True)
class SabyNomenclatureRef:
    id: int
    external_id: str


# Explicit production mapping verified against Saby point 274 / price list 7.
# The two unavailable website entries (molimaojian and ginseng1s) are omitted.
SABY_NOMENCLATURE_BY_SITE_ID: Mapping[str, SabyNomenclatureRef] = {
    "baihao": SabyNomenclatureRef(39, "b4bc9267-241d-4bfe-9fe8-8af23409f4f0"),
    "baimudan": SabyNomenclatureRef(41, "4e638b70-2958-4082-b8fc-fa86cb32aac4"),
    "longjing": SabyNomenclatureRef(57, "edc31bc7-98cd-4f21-bc30-7f09c781ed6f"),
    "biluogold": SabyNomenclatureRef(55, "d5bfb080-3b58-4509-917f-231070f07a06"),
    "xiaozhong": SabyNomenclatureRef(47, "4eb33549-97a1-4e13-9e3f-2ec031b470ac"),
    "longjinghong": SabyNomenclatureRef(38, "f55ef853-3d70-4485-b3aa-4e0750debe1f"),
    "dianhong": SabyNomenclatureRef(54, "2ed9aa13-51f5-458e-a0f2-b41a54149880"),
    "dancong": SabyNomenclatureRef(42, "3f507bce-7040-41ce-ae66-110f6e4fb6fb"),
    "gabahoney": SabyNomenclatureRef(43, "341738e2-d084-4b9c-9239-a7ea5225b64e"),
    "gabaruby": SabyNomenclatureRef(37, "d40e5fbd-9024-4319-8b48-856209693162"),
    "huangjingui": SabyNomenclatureRef(48, "e80b5e40-7355-4bbe-a32a-ef1f860851ed"),
    "dahongpao": SabyNomenclatureRef(58, "c96b8b34-2bda-432b-904f-5d906b36917b"),
    "maoxie": SabyNomenclatureRef(46, "8fc609ab-83c1-400d-a895-c96eb7116ecb"),
    "gabamaocha": SabyNomenclatureRef(52, "174411c3-9ad3-49fd-b363-ffc78b146198"),
    "ginseng": SabyNomenclatureRef(96, "e46bc94c-6a1c-46f3-9f8f-ae6b30bb3186"),
    "mandarin": SabyNomenclatureRef(44, "17474aa7-77a5-41c0-bcc1-deb76454e9d3"),
    "laochatou": SabyNomenclatureRef(40, "5584d4db-c993-4afe-8c0c-cc4a68cf2784"),
    "peacock": SabyNomenclatureRef(49, "3004d06b-4cb9-4c13-976b-da209c2ab769"),
    "bingdao": SabyNomenclatureRef(50, "de28f7d7-16cf-4072-864d-05d6094288b8"),
    "nuomixiang": SabyNomenclatureRef(33, "5882a4b0-d7ed-4c0f-8e98-4f4151ce85b0"),
    "jinhuawang": SabyNomenclatureRef(51, "f5552ac3-8301-4ce8-a755-267d41f13513"),
    "herbal": SabyNomenclatureRef(56, "18bd2c1a-c0d9-4212-826c-2fe48d33a359"),
    "molisiaobaiya": SabyNomenclatureRef(81, "c6c855a4-6763-4c9d-b2fe-300c2ace2cef"),
    "biluochun": SabyNomenclatureRef(97, "5a1466f8-540e-41bf-a625-896ab385ab0b"),
    "dancongmilan": SabyNomenclatureRef(90, "35a19c95-c025-459c-baf9-151f40a6af2e"),
    "dancongtongtian": SabyNomenclatureRef(91, "6c254911-88cc-4635-9040-390758641cc7"),
    "yeshenghong": SabyNomenclatureRef(93, "aedaadab-9eac-4c80-8bf5-0b8a159226bd"),
    "shengchenxiang": SabyNomenclatureRef(88, "3788455f-923f-454e-8e81-42ddeebb092f"),
    "osmanthus": SabyNomenclatureRef(98, "679ecbd6-76ef-41de-91c7-4d4f17e5fcde"),
    "vitamin": SabyNomenclatureRef(94, "4ed23c0a-1511-45f6-9aa2-491d6e71a57e"),
}

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "telegram-bot" / "teas.json"
READY_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def sync_mode_from_env(environ: Mapping[str, str] | None = None) -> SabySyncMode:
    """Read rollout mode, defaulting to the non-writing ``off`` mode."""

    source = os.environ if environ is None else environ
    raw = source.get("SABY_ORDER_SYNC_MODE", SabySyncMode.OFF.value).strip().lower()
    try:
        return SabySyncMode(raw)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in SabySyncMode)
        raise SabySyncError(f"Неизвестный режим Saby: {raw!r}; допустимы {allowed}") from exc


def write_allowed(
    mode: SabySyncMode,
    *,
    test_mode: bool,
    manual_approved: bool = False,
) -> bool:
    """Return whether a future transport layer may perform a write.

    Test mode is an unconditional safety barrier, including for ``auto``.
    """

    if test_mode or mode in (SabySyncMode.OFF, SabySyncMode.SHADOW):
        return False
    if mode is SabySyncMode.MANUAL:
        return manual_approved
    return mode is SabySyncMode.AUTO


def require_write_allowed(
    mode: SabySyncMode,
    *,
    test_mode: bool,
    manual_approved: bool = False,
) -> None:
    """Raise unless the future transport layer is explicitly allowed to write."""

    if not write_allowed(mode, test_mode=test_mode, manual_approved=manual_approved):
        raise SabySyncPolicyError(
            f"Запись в Saby запрещена: mode={mode.value}, test_mode={test_mode}"
        )


def _catalog_teas(catalog: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    if isinstance(catalog, Mapping):
        teas = catalog.get("teas")
    else:
        teas = catalog
    if not isinstance(teas, Sequence) or isinstance(teas, (str, bytes)):
        raise SabySyncError("teas.json не содержит массив teas")
    if any(not isinstance(tea, Mapping) for tea in teas):
        raise SabySyncError("teas.json содержит некорректную позицию")
    return teas


def validate_mapping_against_catalog(
    catalog: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    mapping: Mapping[str, SabyNomenclatureRef] = SABY_NOMENCLATURE_BY_SITE_ID,
) -> None:
    """Require the mapping to equal exactly the set of active catalog IDs."""

    teas = _catalog_teas(catalog)
    active_ids = {str(tea.get("id", "")) for tea in teas if tea.get("stock") is True}
    mapped_ids = set(mapping)
    missing = sorted(active_ids - mapped_ids)
    extra = sorted(mapped_ids - active_ids)
    if missing or extra:
        parts = []
        if missing:
            parts.append("нет соответствий: " + ", ".join(missing))
        if extra:
            parts.append("лишние соответствия: " + ", ".join(extra))
        raise SabySyncError("Таблица Saby не совпадает с активным каталогом; " + "; ".join(parts))

    refs = list(mapping.values())
    if len({ref.id for ref in refs}) != len(refs):
        raise SabySyncError("В таблице Saby повторяется id номенклатуры")
    if len({ref.external_id for ref in refs}) != len(refs):
        raise SabySyncError("В таблице Saby повторяется externalId номенклатуры")
    for site_id, ref in mapping.items():
        if ref.id <= 0:
            raise SabySyncError(f"Некорректный Saby id для {site_id}")
        try:
            UUID(ref.external_id)
        except (ValueError, AttributeError) as exc:
            raise SabySyncError(f"Некорректный Saby externalId для {site_id}") from exc


def validate_mapping_file(path: str | Path | None = None) -> None:
    """Load a teas.json file and validate the production mapping against it."""

    catalog_path = Path(path) if path is not None else Path(
        os.getenv("CHAINYA_CATALOG_PATH", str(DEFAULT_CATALOG_PATH))
    )
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SabySyncError(f"Не удалось прочитать каталог {catalog_path}") from exc
    validate_mapping_against_catalog(catalog)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SabySyncError(f"{field} должен быть положительным целым числом")
    return value


def _number(value: Any, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SabySyncError(f"{field} должен быть положительным числом")
    return value


def build_nomenclatures(lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert server-priced order lines into Saby nomenclature rows.

    Gram products are sent in grams.  Their per-gram cost is derived from the
    already validated line total, so rounding in the website checkout is kept.
    Piece products are sent in pieces at the checkout's unit price.
    """

    if not lines:
        raise SabySyncError("В заказе нет позиций")
    result: list[dict[str, Any]] = []
    for line in lines:
        site_id = str(line.get("id", ""))
        ref = SABY_NOMENCLATURE_BY_SITE_ID.get(site_id)
        if ref is None:
            raise SabySyncError(f"Нет соответствия Saby для позиции {site_id or '<без id>'}")
        qty = _positive_int(line.get("qty"), f"qty позиции {site_id}")
        pack = line.get("pack")
        line_total = _number(line.get("total"), f"total позиции {site_id}")
        if pack == "pc":
            count = qty
            cost = _number(line.get("unit_price"), f"unit_price позиции {site_id}")
            if cost * count != line_total:
                raise SabySyncError(f"Сумма штучной позиции {site_id} не совпадает с ценой")
        else:
            grams = _positive_int(pack, f"pack позиции {site_id}")
            count = grams * qty
            cost = line_total / count
        result.append({
            "id": ref.id,
            "externalId": ref.external_id,
            "count": count,
            "cost": cost,
            "name": str(line.get("name", "")).strip() or site_id,
        })
    return result


def _ready_at_value(ready_at: datetime | str) -> str:
    if isinstance(ready_at, datetime):
        return ready_at.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(ready_at, str) and READY_AT_PATTERN.fullmatch(ready_at):
        try:
            datetime.strptime(ready_at, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise SabySyncError("Некорректные дата и время готовности") from exc
        return ready_at
    raise SabySyncError("Нужно явно указать ready_at в формате YYYY-MM-DD HH:MM:SS")


def build_saby_order(
    order: Mapping[str, Any],
    *,
    settings: SabySettings,
    ready_at: datetime | str,
) -> dict[str, Any]:
    """Build a Saby delivery-order payload without sending it anywhere."""

    delivery_method = str(order.get("delivery", ""))
    if delivery_method not in {"pickup", "cdek_pvz", "cdek_courier"}:
        raise SabySyncError(f"Неподдерживаемый способ доставки: {delivery_method!r}")
    if not settings.point_id:
        raise SabySyncError("Не задан SABY_POINT_ID")
    if not settings.price_list_id:
        raise SabySyncError("Не задан SABY_PRICE_LIST_ID")

    customer = order.get("customer")
    if not isinstance(customer, Mapping):
        raise SabySyncError("В заказе нет данных покупателя")
    name = str(customer.get("name", "")).strip()
    phone = str(customer.get("phone", "")).strip()
    if not name:
        raise SabySyncError("Для Saby требуется имя покупателя")
    if not phone:
        raise SabySyncError("Для Saby требуется телефон покупателя")

    payment_method = order.get("payment_method")
    if payment_method not in ("bank_card", "sbp"):
        raise SabySyncError(f"Неподдерживаемый способ оплаты: {payment_method!r}")

    delivery: dict[str, Any] = {
        "isPickup": delivery_method == "pickup",
        "paymentType": "online",
    }
    if delivery_method != "pickup":
        city = str(customer.get("city", "")).strip()
        address = str(customer.get("address", "")).strip()
        if delivery_method == "cdek_pvz":
            quote = order.get("delivery_quote")
            point = quote.get("point") if isinstance(quote, Mapping) else None
            point_address = str(point.get("address", "")).strip() if isinstance(point, Mapping) else ""
            pvz_code = str(customer.get("pvz_code", "")).strip()
            address = point_address or (f"ПВЗ СДЭК {pvz_code}" if pvz_code else "")
        address_full = ", ".join(part for part in (city, address) if part)
        if not address_full:
            raise SabySyncError("Для доставки Saby требуется адрес")
        delivery["addressFull"] = address_full

    payload: dict[str, Any] = {
        "product": "delivery",
        "pointId": settings.point_id,
        "customer": {"name": name, "phone": phone},
        "datetime": _ready_at_value(ready_at),
        "nomenclatures": build_nomenclatures(order.get("items") or []),
        "priceListId": settings.price_list_id,
        "delivery": delivery,
    }
    order_id = str(order.get("id", "")).strip()
    note = str(customer.get("note", "")).strip()
    comment_parts = [part for part in (f"Заказ сайта №{order_id}" if order_id else "", note) if part]
    if comment_parts:
        payload["comment"] = ". ".join(comment_parts)
    return payload


__all__ = [
    "SABY_NOMENCLATURE_BY_SITE_ID",
    "SabyNomenclatureRef",
    "SabySyncError",
    "SabySyncMode",
    "SabySyncPolicyError",
    "build_nomenclatures",
    "build_saby_order",
    "require_write_allowed",
    "sync_mode_from_env",
    "validate_mapping_against_catalog",
    "validate_mapping_file",
    "write_allowed",
]

"""Fail-closed Saby purchase routes and fiscal-sale payloads.

The fiscal route is deliberately independent from Saby Delivery.  T-Bank
continues to acquire the payment, while Saby is the only system allowed to
register the fiscal receipt and write off stock.  No network calls live here;
builders are deterministic and covered by contract tests.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


class SabyPurchaseError(ValueError):
    """The purchase cannot be represented safely for Saby."""


class SabyPurchaseRoute(str, Enum):
    DELIVERY = "delivery"
    FISCAL_SALE = "fiscal_sale"
    EXTERNAL_OFD = "external_ofd"


@dataclass(frozen=True)
class SabyFiscalSettings:
    company_id: str = ""
    kkt_reg_number: str = ""
    tax_system: int = 2
    pay_method: int = -1
    allow_negative_stock: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SabyFiscalSettings:
        source = os.environ if environ is None else environ

        def integer(name: str, default: int) -> int:
            raw = str(source.get(name, default)).strip()
            try:
                return int(raw)
            except ValueError:
                return -1

        return cls(
            # The fiscal endpoint calls this ``companyID``, while Saby's
            # current Retail docs define it as the sales point identifier.
            company_id=str(
                source.get("SABY_OFD_COMPANY_ID")
                or source.get("SABY_POINT_ID", "")
            ).strip(),
            kkt_reg_number=str(source.get("SABY_OFD_KKT_REG_NUMBER", "")).strip(),
            tax_system=integer("SABY_OFD_TAX_SYSTEM", 2),
            pay_method=integer("SABY_OFD_PAY_METHOD", -1),
            allow_negative_stock=(
                str(source.get("SABY_OFD_ALLOW_NEGATIVE_STOCK", "0")).strip() == "1"
            ),
        )

    @property
    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not re.fullmatch(r"\d+", self.company_id):
            missing.append("SABY_POINT_ID")
        if not re.fullmatch(r"\d{8,32}", self.kkt_reg_number):
            missing.append("SABY_OFD_KKT_REG_NUMBER")
        if self.tax_system not in {1, 2, 4, 16, 32}:
            missing.append("SABY_OFD_TAX_SYSTEM")
        # Chainya uses one-stage card/SBP acquisition.  Saby registers the
        # paid purchase as full settlement and writes stock off after shift
        # closure; a second handover receipt is neither needed nor allowed.
        if self.pay_method != 4:
            missing.append("SABY_OFD_PAY_METHOD")
        return tuple(missing)

    @property
    def configured(self) -> bool:
        return not self.missing

    def public_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "missing": list(self.missing),
            "tax_system": self.tax_system if self.tax_system in {1, 2, 4, 16, 32} else None,
            "pay_method": self.pay_method if self.pay_method in {1, 2, 3, 4, 5, 6, 7} else None,
            "allow_negative_stock": self.allow_negative_stock,
        }


@dataclass(frozen=True)
class SabyPurchaseRouteStatus:
    route: str
    valid: bool
    implemented: bool
    writes_enabled: bool
    blockers: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "valid": self.valid,
            "implemented": self.implemented,
            "writes_enabled": self.writes_enabled,
            "blockers": list(self.blockers),
        }


def purchase_route_status(
    environ: Mapping[str, str] | None = None,
    *,
    tbank_receipt_enabled: bool,
    saby_configured: bool = True,
    fiscal_settings: SabyFiscalSettings | None = None,
) -> SabyPurchaseRouteStatus:
    """Return a secret-free status without performing network calls."""

    source = os.environ if environ is None else environ
    raw = str(source.get("SABY_PURCHASE_ROUTE", SabyPurchaseRoute.DELIVERY.value))
    raw = raw.strip().lower()
    try:
        route = SabyPurchaseRoute(raw)
    except ValueError:
        return SabyPurchaseRouteStatus(
            route="invalid", valid=False, implemented=False, writes_enabled=False,
            blockers=("Неизвестный маршрут передачи покупки в Saby",),
        )

    if route is SabyPurchaseRoute.DELIVERY:
        return SabyPurchaseRouteStatus(
            route=route.value, valid=True, implemented=True,
            writes_enabled=True, blockers=(),
        )

    if route is SabyPurchaseRoute.FISCAL_SALE:
        settings = fiscal_settings or SabyFiscalSettings.from_env(source)
        blockers: list[str] = []
        if tbank_receipt_enabled:
            blockers.append(
                "Онлайн-чек уже формируется через Т-Банк: второй фискальный чек запрещён"
            )
        if not saby_configured:
            blockers.append("Не настроена сервисная авторизация Saby")
        if not settings.configured:
            blockers.append(
                "Не заполнены параметры ККТ для регистрации чека через Saby"
            )
        return SabyPurchaseRouteStatus(
            route=route.value, valid=True, implemented=True,
            writes_enabled=not blockers, blockers=tuple(blockers),
        )

    return SabyPurchaseRouteStatus(
        route=route.value, valid=True, implemented=False, writes_enabled=False,
        blockers=(
            "Импорт из внешнего ОФД настраивается в Saby и требует проверки провайдера и лицензии",
        ),
    )


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise SabyPurchaseError(f"Некорректное значение {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SabyPurchaseError(f"Некорректное значение {field}") from None
    if result <= 0:
        raise SabyPurchaseError(f"{field} должно быть больше нуля")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _phone(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) != 11 or not digits.startswith("7"):
        raise SabyPurchaseError("Для электронного чека нужен российский номер телефона")
    return "+" + digits


def build_fiscal_nomenclatures(
    lines: Sequence[Mapping[str, Any]], *, delivery_price: int = 0
) -> list[dict[str, object]]:
    """Convert priced checkout lines to Saby's kg/piece fiscal units."""

    if not lines:
        raise SabyPurchaseError("В заказе нет позиций")
    result: list[dict[str, object]] = []
    for line in lines:
        name = str(line.get("name", "")).strip()
        qty = line.get("qty")
        pack = line.get("pack")
        total = _money(line.get("total"), f"суммы позиции {name or '<без названия>'}")
        if not name or isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise SabyPurchaseError("Некорректная позиция заказа")
        if pack == "pc":
            quantity = Decimal(qty)
            price = _money(line.get("unit_price"), f"цены позиции {name}")
            measure = "шт"
        else:
            if isinstance(pack, bool) or not isinstance(pack, int) or pack <= 0:
                raise SabyPurchaseError(f"Некорректная фасовка позиции {name}")
            quantity = Decimal(pack * qty) / Decimal(1000)
            price = total / quantity
            measure = "кг"
        if (price * quantity).quantize(Decimal("0.01")) != total.quantize(Decimal("0.01")):
            raise SabyPurchaseError(f"Цена и сумма позиции {name} не совпадают")
        result.append({
            "nameNomenclature": name,
            "priceNomenclature": _decimal_text(price),
            "quantityNomenclature": _decimal_text(quantity),
            "measureNomenclature": measure,
            "kindNomenclature": "т",
            "totalPriceNomenclature": _decimal_text(total),
            "taxRateNomenclature": None,
            "totalVat": "0.00",
        })
    if delivery_price:
        delivery = _money(delivery_price, "стоимости доставки")
        result.append({
            "nameNomenclature": "Доставка",
            "priceNomenclature": _decimal_text(delivery),
            "quantityNomenclature": "1.00",
            "measureNomenclature": "шт",
            "kindNomenclature": "у",
            "totalPriceNomenclature": _decimal_text(delivery),
            "taxRateNomenclature": None,
            "totalVat": "0.00",
        })
    return result


def build_fiscal_sale(
    order: Mapping[str, Any], *, settings: SabyFiscalSettings,
    refund: bool = False, settlement: bool = False,
) -> dict[str, object]:
    """Build a one-stage full-settlement sale or its full return."""

    if not settings.configured:
        raise SabyPurchaseError("Параметры ККТ Saby настроены не полностью")
    order_id = str(order.get("id", "")).strip()
    customer = order.get("customer")
    if not order_id or not isinstance(customer, Mapping):
        raise SabyPurchaseError("В заказе нет идентификатора или покупателя")
    total = _money(order.get("total"), "итоговой суммы")
    lines = build_fiscal_nomenclatures(
        order.get("items") or [], delivery_price=int(order.get("delivery_price") or 0)
    )
    lines_total = sum(
        (Decimal(str(item["totalPriceNomenclature"])) for item in lines), Decimal(0)
    )
    if lines_total.quantize(Decimal("0.01")) != total.quantize(Decimal("0.01")):
        raise SabyPurchaseError("Сумма позиций не совпадает с итогом заказа")
    phone = _phone(customer.get("phone"))
    zero = "0.00"
    if settlement:
        raise SabyPurchaseError(
            "При одностадийной оплате полный расчёт уже создаётся после оплаты"
        )
    pay_method = settings.pay_method
    internet_sum = _decimal_text(total)
    prepay_sum = zero
    operation_suffix = "refund" if refund else "sale"
    return {
        "companyID": settings.company_id,
        "kktRegNumber": int(settings.kkt_reg_number),
        "cashierFIO": "Автоматический режим",
        "operationType": "2" if refund else "1",
        "cashSum": zero,
        "bankSum": zero,
        "internetSum": internet_sum,
        "accountSum": zero,
        "postpaySum": zero,
        "prepaySum": prepay_sum,
        "vatNone": _decimal_text(total),
        "vatSum0": zero,
        "vatSum5": zero,
        "vatSum7": zero,
        "vatSum10": zero,
        "vatSum20": zero,
        "vatSum22": zero,
        "vatSum110": zero,
        "vatSum120": zero,
        "allowRetailPayed": 1 if settings.allow_negative_stock else 0,
        "nomenclatures": lines,
        "customerFIO": str(customer.get("name", "")).strip(),
        "customerEmail": "",
        "customerPhone": phone,
        "customerINN": "",
        "customerExtId": phone,
        "taxSystem": str(settings.tax_system),
        "sendPhone": phone,
        "propName": "Номер заказа интернет-магазина",
        "propVa": order_id,
        "comment": (
            f"Возврат полного расчёта заказа сайта №{order_id}" if refund
            else f"Полный расчёт заказа сайта №{order_id}"
        ),
        "payMethod": str(pay_method),
        "externalId": f"chainya-{order_id.lower()}-{operation_suffix}",
    }


__all__ = [
    "SabyFiscalSettings", "SabyPurchaseError", "SabyPurchaseRoute",
    "SabyPurchaseRouteStatus", "build_fiscal_nomenclatures",
    "build_fiscal_sale", "purchase_route_status",
]

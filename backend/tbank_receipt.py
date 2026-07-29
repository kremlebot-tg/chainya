"""Pure, configurable builder for T-Bank sale and refund receipts.

No tax value is guessed. Receipt transmission stays off until the merchant's
tax system, VAT rate and cash-register FFD version are explicitly configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


TAXATIONS = {"osn", "usn_income", "usn_income_outcome", "esn", "patent"}
TAXES = {"none", "vat0", "vat5", "vat7", "vat10", "vat22", "vat105", "vat107", "vat110", "vat122"}
FFD_VERSIONS = {"1.05", "1.2"}
MAX_ITEM_NAME_LENGTH = 128


class TBankReceiptError(ValueError):
    pass


@dataclass(frozen=True)
class TBankReceiptSettings:
    enabled: bool = False
    taxation: str = ""
    item_tax: str = ""
    delivery_tax: str = ""
    ffd_version: str = ""

    @classmethod
    def from_env(cls) -> "TBankReceiptSettings":
        return cls(
            enabled=os.getenv("TBANK_RECEIPT_ENABLED", "0").strip() == "1",
            taxation=os.getenv("TBANK_RECEIPT_TAXATION", "").strip(),
            item_tax=os.getenv("TBANK_RECEIPT_ITEM_TAX", "").strip(),
            delivery_tax=os.getenv("TBANK_RECEIPT_DELIVERY_TAX", "").strip(),
            ffd_version=os.getenv("TBANK_RECEIPT_FFD_VERSION", "").strip(),
        )

    @property
    def missing(self) -> list[str]:
        if not self.enabled:
            return []
        return [
            name
            for name, valid in (
                ("TBANK_RECEIPT_TAXATION", self.taxation in TAXATIONS),
                ("TBANK_RECEIPT_ITEM_TAX", self.item_tax in TAXES),
                ("TBANK_RECEIPT_DELIVERY_TAX", self.delivery_tax in TAXES),
                ("TBANK_RECEIPT_FFD_VERSION", self.ffd_version in FFD_VERSIONS),
            )
            if not valid
        ]

    @property
    def configured(self) -> bool:
        return self.enabled and not self.missing


def build_receipt(
    *,
    phone: str,
    items: Sequence[Mapping[str, Any]],
    delivery_price: int,
    settings: TBankReceiptSettings,
) -> dict[str, Any]:
    if not settings.configured:
        raise TBankReceiptError("Параметры онлайн-чека Т-Банка не настроены")
    if not phone.startswith("+") or not phone[1:].isdigit():
        raise TBankReceiptError("Некорректный телефон для онлайн-чека")

    receipt_items: list[dict[str, Any]] = []
    for source in items:
        try:
            name = str(source["name"]).strip()
            quantity = int(source["qty"])
            unit_price = int(source["unit_price"]) * 100
            amount = int(source["total"]) * 100
        except (KeyError, TypeError, ValueError):
            raise TBankReceiptError("Некорректная позиция онлайн-чека") from None
        if (
            not name
            or len(name) > MAX_ITEM_NAME_LENGTH
            or quantity <= 0
            or unit_price <= 0
            or amount != unit_price * quantity
        ):
            raise TBankReceiptError("Некорректная позиция онлайн-чека")

        pack = source.get("pack")
        if pack is not None and pack != "pc":
            try:
                grams = int(pack)
            except (TypeError, ValueError):
                raise TBankReceiptError("Некорректная фасовка онлайн-чека") from None
            if isinstance(pack, bool) or grams <= 0 or str(pack).strip() != str(grams):
                raise TBankReceiptError("Некорректная фасовка онлайн-чека")
            suffix = f" · {grams} г"
            available = MAX_ITEM_NAME_LENGTH - len(suffix)
            name = f"{name[:available].rstrip()}{suffix}"

        item = {
            "Name": name,
            "Price": unit_price,
            "Quantity": quantity,
            "Amount": amount,
            "Tax": settings.item_tax,
            "PaymentMethod": "full_prepayment",
            "PaymentObject": "commodity",
        }
        if settings.ffd_version == "1.2":
            item["MeasurementUnit"] = "шт"
        receipt_items.append(item)

    if delivery_price:
        delivery = {
            "Name": "Доставка",
            "Price": int(delivery_price) * 100,
            "Quantity": 1,
            "Amount": int(delivery_price) * 100,
            "Tax": settings.delivery_tax,
            "PaymentMethod": "full_prepayment",
            "PaymentObject": "service",
        }
        if settings.ffd_version == "1.2":
            delivery["MeasurementUnit"] = "шт"
        receipt_items.append(delivery)

    if not receipt_items:
        raise TBankReceiptError("Онлайн-чек не может быть пустым")
    return {
        "Phone": phone,
        "Taxation": settings.taxation,
        "Items": receipt_items,
    }


__all__ = [
    "TBankReceiptError",
    "TBankReceiptSettings",
    "build_receipt",
]

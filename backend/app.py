#!/usr/bin/env python3
"""Тестовый backend магазина «Чайня».

Считает заказ только по серверному каталогу, хранит его в SQLite и имитирует
платёж. Реальные Saby/CDEK/acquiring адаптеры подключаются вместо mock-функций.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
DATA_DIR = Path(os.getenv("CHAINYA_DATA_DIR", ROOT / "backend" / "data"))
DB_PATH = DATA_DIR / "orders.sqlite3"
CATALOG_PATH = Path(os.getenv("CHAINYA_CATALOG_PATH", PROJECT / "telegram-bot" / "teas.json"))
TEST_MODE = os.getenv("CHAINYA_TEST_MODE", "1") == "1"

DELIVERY_PRICES = {"pickup": 0, "cdek_pvz": 490, "cdek_courier": 790}
DELIVERY_LABELS = {
    "pickup": "Самовывоз · Острякова, 3",
    "cdek_pvz": "СДЭК · пункт выдачи",
    "cdek_courier": "СДЭК · курьер",
}

app = FastAPI(title="Chainya checkout", version="0.1.0")


class OrderItem(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    pack: int | Literal["pc"]
    qty: int = Field(ge=1, le=20)

    @field_validator("pack")
    @classmethod
    def valid_pack(cls, value):
        if value != "pc" and value not in (25, 50, 100):
            raise ValueError("неподдерживаемая фасовка")
        return value


class CreateOrder(BaseModel):
    items: list[OrderItem] = Field(min_length=1, max_length=50)
    delivery: Literal["pickup", "cdek_pvz", "cdek_courier"]
    payment_method: Literal["bank_card", "sbp"]
    name: str = Field(default="", max_length=120)
    phone: str = Field(min_length=7, max_length=40)
    city: str = Field(default="", max_length=160)
    address: str = Field(default="", max_length=300)
    pvz_code: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=1000)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value):
        if len(re.sub(r"\D", "", value)) < 10:
            raise ValueError("укажите полный номер телефона")
        return value.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_catalog() -> dict[str, dict]:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось загрузить каталог {CATALOG_PATH}: {exc}") from exc
    return {item["id"]: item for item in data["teas"]}


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                subtotal INTEGER NOT NULL,
                delivery_price INTEGER NOT NULL,
                total INTEGER NOT NULL,
                payment_method TEXT NOT NULL,
                delivery TEXT NOT NULL,
                customer_json TEXT NOT NULL,
                items_json TEXT NOT NULL,
                provider_payment_id TEXT
            )
        """)


@app.on_event("startup")
def startup():
    init_db()


def pack_price(per_10g: int, grams: int) -> int:
    return round(per_10g * grams / 10 / 5) * 5


def price_order(payload: CreateOrder) -> tuple[list[dict], int]:
    catalog = load_catalog()
    lines, subtotal = [], 0
    for requested in payload.items:
        tea = catalog.get(requested.id)
        if not tea or tea.get("stock") is False:
            raise HTTPException(409, f"Позиция недоступна: {requested.id}")
        if tea["unit"] == "pc":
            if requested.pack != "pc":
                raise HTTPException(422, f"Позиция {requested.id} продаётся поштучно")
            unit_price = int(tea["price"])
        else:
            if requested.pack == "pc":
                raise HTTPException(422, f"Для позиции {requested.id} нужна фасовка")
            unit_price = pack_price(int(tea["price"]), requested.pack)
        line_total = unit_price * requested.qty
        subtotal += line_total
        lines.append({
            "id": requested.id,
            "name": tea["name"],
            "pack": requested.pack,
            "qty": requested.qty,
            "unit_price": unit_price,
            "total": line_total,
        })
    return lines, subtotal


def validate_delivery(payload: CreateOrder) -> None:
    if payload.delivery == "cdek_pvz" and (not payload.city.strip() or not payload.pvz_code.strip()):
        raise HTTPException(422, "Для доставки в ПВЗ укажите город и код пункта")
    if payload.delivery == "cdek_courier" and (not payload.city.strip() or not payload.address.strip()):
        raise HTTPException(422, "Для курьерской доставки укажите город и адрес")


def public_order(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "status": row["status"], "created_at": row["created_at"],
        "subtotal": row["subtotal"], "delivery_price": row["delivery_price"],
        "total": row["total"], "payment_method": row["payment_method"],
        "delivery": row["delivery"], "items": json.loads(row["items_json"]),
    }


@app.get("/api/health")
def health():
    return {"ok": True, "test_mode": TEST_MODE, "catalog_items": len(load_catalog())}


@app.get("/api/delivery/quote")
def delivery_quote(method: Literal["pickup", "cdek_pvz", "cdek_courier"]):
    return {
        "method": method,
        "label": DELIVERY_LABELS[method],
        "price": DELIVERY_PRICES[method],
        "is_test": method != "pickup",
    }


@app.post("/api/orders", status_code=201)
def create_order(payload: CreateOrder, request: Request):
    validate_delivery(payload)
    lines, subtotal = price_order(payload)
    delivery_price = DELIVERY_PRICES[payload.delivery]
    order_id = uuid.uuid4().hex[:12].upper()
    created = now_iso()
    customer = payload.model_dump(exclude={"items", "payment_method", "delivery"})
    with db() as con:
        con.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, "pending_payment", created, created, subtotal, delivery_price,
             subtotal + delivery_price, payload.payment_method, payload.delivery,
             json.dumps(customer, ensure_ascii=False), json.dumps(lines, ensure_ascii=False), None),
        )
    base = str(request.base_url).rstrip("/")
    return {
        "order": get_order(order_id),
        "payment": {
            "mode": "test" if TEST_MODE else "unconfigured",
            "url": f"{base}/test-payment/{order_id}" if TEST_MODE else None,
        },
    }


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    return public_order(row)


@app.post("/api/orders/{order_id}/test-pay")
def test_pay(order_id: str):
    if not TEST_MODE:
        raise HTTPException(404, "Тестовая оплата отключена")
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Заказ не найден")
        if row["status"] == "pending_payment":
            con.execute(
                "UPDATE orders SET status = 'paid', updated_at = ?, provider_payment_id = ? WHERE id = ?",
                (now_iso(), f"mock_{uuid.uuid4().hex}", order_id),
            )
    return get_order(order_id)


@app.get("/test-payment/{order_id}")
def test_payment_page(order_id: str):
    get_order(order_id)
    return FileResponse(ROOT / "backend" / "test-payment.html")


# В локальной разработке backend одновременно раздаёт собранный сайт.
if (ROOT / "dist").exists():
    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="site")

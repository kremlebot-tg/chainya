#!/usr/bin/env python3
"""Тестовый backend магазина «Чайня».

Считает заказ только по серверному каталогу, хранит его в SQLite и имитирует
платёж. Реальные Saby/CDEK/acquiring адаптеры подключаются вместо mock-функций.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
DATA_DIR = Path(os.getenv("CHAINYA_DATA_DIR", ROOT / "backend" / "data"))
DB_PATH = DATA_DIR / "orders.sqlite3"
CATALOG_PATH = Path(os.getenv("CHAINYA_CATALOG_PATH", PROJECT / "telegram-bot" / "teas.json"))
TEST_MODE = os.getenv("CHAINYA_TEST_MODE", "1") == "1"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
OWNER_CHAT_IDS = [
    value for value in re.split(r"[\s,]+", os.getenv("OWNER_CHAT_ID", "").strip()) if value
]

DELIVERY_PRICES = {"pickup": 0, "cdek_pvz": 490, "cdek_courier": 790}
DELIVERY_LABELS = {
    "pickup": "Самовывоз · Острякова, 3",
    "cdek_pvz": "СДЭК · пункт выдачи",
    "cdek_courier": "СДЭК · курьер",
}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Chainya checkout", version="0.1.0", lifespan=lifespan)
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


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
    privacy_accepted: Literal[True]

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value):
        if len(re.sub(r"\D", "", value)) < 10:
            raise ValueError("укажите полный номер телефона")
        return value.strip()


class CreateBusinessLead(BaseModel):
    company: str = Field(default="", max_length=160)
    name: str = Field(default="", max_length=120)
    contact: str = Field(min_length=3, max_length=120)
    note: str = Field(default="", max_length=1000)
    privacy_accepted: Literal[True]

    @field_validator("contact")
    @classmethod
    def valid_contact(cls, value):
        value = value.strip()
        if len(value) < 3:
            raise ValueError("укажите телефон или Telegram")
        return value


class UpdateOrderStatus(BaseModel):
    status: Literal["paid", "confirmed", "packing", "shipped", "completed", "cancelled"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rate_limit(request: Request, bucket: str, limit: int, window: int) -> None:
    """Небольшой per-IP лимит для одного процесса checkout."""
    address = request.client.host if request.client else "unknown"
    key, now = f"{bucket}:{address}", time.monotonic()
    with _rate_lock:
        hits = _rate_buckets[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(429, "Слишком много запросов. Попробуйте немного позже.")
        hits.append(now)


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
        columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        if "payment_token" not in columns:
            con.execute("ALTER TABLE orders ADD COLUMN payment_token TEXT")
        con.execute("""
            CREATE TABLE IF NOT EXISTS business_leads (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                company TEXT NOT NULL,
                name TEXT NOT NULL,
                contact TEXT NOT NULL,
                note TEXT NOT NULL
            )
        """)


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


def order_row(order_id: str) -> sqlite3.Row:
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    return row


def require_order_token(row: sqlite3.Row, token: str) -> None:
    if not row["payment_token"] or not secrets.compare_digest(token, row["payment_token"]):
        raise HTTPException(403, "Недействительная ссылка заказа")


def require_admin(authorization: str) -> None:
    supplied = authorization.removeprefix("Bearer ").strip()
    if not ADMIN_TOKEN or not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(401, "Требуется доступ владельца", headers={"WWW-Authenticate": "Bearer"})


def admin_order(row: sqlite3.Row) -> dict:
    result = public_order(row)
    result["updated_at"] = row["updated_at"]
    result["customer"] = json.loads(row["customer_json"])
    return result


def paid_notification(row: sqlite3.Row) -> str:
    customer = json.loads(row["customer_json"])
    items = json.loads(row["items_json"])
    lines = [
        "🧪 Тестовый заказ оплачен",
        f"№ {row['id']}",
        "",
        *[
            f"• {item['name']} — {'шт' if item['pack'] == 'pc' else str(item['pack']) + ' г'} "
            f"×{item['qty']} — {item['total']} ₽"
            for item in items
        ],
        "",
        f"Товары: {row['subtotal']} ₽",
        f"Доставка: {DELIVERY_LABELS.get(row['delivery'], row['delivery'])} — {row['delivery_price']} ₽",
        f"Итого: {row['total']} ₽",
        f"Оплата: {'СБП' if row['payment_method'] == 'sbp' else 'банковская карта'}",
    ]
    for key, label in (
        ("name", "Имя"), ("phone", "Телефон"), ("city", "Город"),
        ("pvz_code", "ПВЗ"), ("address", "Адрес"), ("note", "Комментарий"),
    ):
        if customer.get(key):
            lines.append(f"{label}: {customer[key]}")
    return "\n".join(lines)


def send_to_owners(text: str, label: str) -> None:
    if not BOT_TOKEN or not OWNER_CHAT_IDS:
        logging.info("Telegram-уведомление отключено: BOT_TOKEN/OWNER_CHAT_ID не заданы")
        return
    for chat_id in OWNER_CHAT_IDS:
        for attempt, pause in enumerate((0, 1, 3), start=1):
            if pause:
                time.sleep(pause)
            try:
                body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
                telegram_request = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=body, method="POST"
                )
                with urllib.request.urlopen(telegram_request, timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Telegram HTTP {response.status}")
                break
            except Exception:
                if attempt == 3:
                    logging.exception("Не удалось отправить %s владельцу %s после 3 попыток", label, chat_id)
                else:
                    logging.warning("Повтор Telegram %s для %s, попытка %s", label, chat_id, attempt + 1)


def notify_owners(row: sqlite3.Row) -> None:
    """Отправляет владельцам оплаченный заказ с короткими повторами при сбое."""
    send_to_owners(paid_notification(row), "заказ")


def notify_business_lead(lead: dict) -> None:
    text = "\n".join(filter(None, [
        "🏢 Новая заявка для бизнеса",
        f"№ {lead['id']}",
        f"Заведение: {lead['company']}" if lead["company"] else "",
        f"Имя: {lead['name']}" if lead["name"] else "",
        f"Связь: {lead['contact']}",
        f"Комментарий: {lead['note']}" if lead["note"] else "",
    ]))
    send_to_owners(text, "B2B-заявку")


@app.get("/api/health")
def health():
    return {"ok": True, "test_mode": TEST_MODE, "catalog_items": len(load_catalog())}


@app.get("/api/admin/orders")
def admin_orders(authorization: str = Header(default=""), status: str = ""):
    require_admin(authorization)
    allowed = {"pending_payment", "paid", "confirmed", "packing", "shipped", "completed", "cancelled"}
    if status and status not in allowed:
        raise HTTPException(422, "Неизвестный статус")
    with db() as con:
        if status:
            rows = con.execute("SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT 200", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200").fetchall()
    return {"orders": [admin_order(row) for row in rows]}


@app.patch("/api/admin/orders/{order_id}")
def admin_update_order(order_id: str, payload: UpdateOrderStatus, authorization: str = Header(default="")):
    require_admin(authorization)
    row = order_row(order_id)
    if row["status"] == "pending_payment" and payload.status not in {"paid", "cancelled"}:
        raise HTTPException(409, "Сначала заказ должен быть оплачен или отменён")
    with db() as con:
        con.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (payload.status, now_iso(), order_id))
    return admin_order(order_row(order_id))


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
    rate_limit(request, "create-order", 12, 600)
    validate_delivery(payload)
    lines, subtotal = price_order(payload)
    delivery_price = DELIVERY_PRICES[payload.delivery]
    order_id = uuid.uuid4().hex[:12].upper()
    created = now_iso()
    customer = payload.model_dump(exclude={"items", "payment_method", "delivery"})
    payment_token = uuid.uuid4().hex
    with db() as con:
        con.execute(
            """INSERT INTO orders
               (id, status, created_at, updated_at, subtotal, delivery_price, total,
                payment_method, delivery, customer_json, items_json, provider_payment_id, payment_token)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, "pending_payment", created, created, subtotal, delivery_price,
             subtotal + delivery_price, payload.payment_method, payload.delivery,
             json.dumps(customer, ensure_ascii=False), json.dumps(lines, ensure_ascii=False), None,
             payment_token),
        )
    base = str(request.base_url).rstrip("/")
    return {
        "order": public_order(order_row(order_id)),
        "payment": {
            "mode": "test" if TEST_MODE else "unconfigured",
            "url": f"{base}/test-payment/{order_id}?token={payment_token}" if TEST_MODE else None,
        },
    }


@app.post("/api/business-leads", status_code=202)
def create_business_lead(payload: CreateBusinessLead, background_tasks: BackgroundTasks, request: Request):
    rate_limit(request, "business-lead", 5, 600)
    lead = {"id": uuid.uuid4().hex[:12].upper(), "created_at": now_iso(), **payload.model_dump()}
    with db() as con:
        con.execute(
            "INSERT INTO business_leads VALUES (?, ?, ?, ?, ?, ?)",
            (lead["id"], lead["created_at"], lead["company"], lead["name"], lead["contact"], lead["note"]),
        )
    background_tasks.add_task(notify_business_lead, lead)
    return {"id": lead["id"], "accepted": True}


@app.get("/api/orders/{order_id}")
def get_order(order_id: str, token: str):
    row = order_row(order_id)
    require_order_token(row, token)
    return public_order(row)


@app.post("/api/orders/{order_id}/test-pay")
def test_pay(order_id: str, token: str, background_tasks: BackgroundTasks, request: Request):
    if not TEST_MODE:
        raise HTTPException(404, "Тестовая оплата отключена")
    rate_limit(request, "test-pay", 20, 60)
    should_notify = False
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Заказ не найден")
        require_order_token(row, token)
        if row["status"] == "pending_payment":
            con.execute(
                "UPDATE orders SET status = 'paid', updated_at = ?, provider_payment_id = ? WHERE id = ?",
                (now_iso(), f"mock_{uuid.uuid4().hex}", order_id),
            )
            should_notify = True
    if should_notify:
        with db() as con:
            paid_row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        background_tasks.add_task(notify_owners, paid_row)
    return public_order(order_row(order_id))


@app.get("/test-payment/{order_id}")
def test_payment_page(order_id: str, token: str):
    row = order_row(order_id)
    require_order_token(row, token)
    return FileResponse(ROOT / "backend" / "test-payment.html")


@app.get("/admin/orders")
def admin_page():
    return FileResponse(ROOT / "backend" / "admin.html")


# В локальной разработке backend одновременно раздаёт собранный сайт.
if (ROOT / "dist").exists():
    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="site")

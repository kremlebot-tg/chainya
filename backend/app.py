#!/usr/bin/env python3
"""Тестовый backend магазина «Чайня».

Считает заказ только по серверному каталогу, хранит его в SQLite и имитирует
платёж. Реальные Saby/CDEK/acquiring адаптеры подключаются вместо mock-функций.
"""

from __future__ import annotations

import json
import hashlib
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .saby import SabyClient, SabyError


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
ANALYTICS_RETENTION_DAYS = 360
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
saby_client = SabyClient()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()
_rate_salt = secrets.token_bytes(16)
_analytics_cleanup_lock = threading.Lock()
_analytics_cleanup_after = 0.0


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
    analytics_session: str | None = Field(default=None, min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")

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


class UpdateLeadStatus(BaseModel):
    status: Literal["new", "contacted", "closed"]


class AnalyticsEvent(BaseModel):
    session_id: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    event: Literal[
        "page_view", "section_view", "tea_view", "cart_open", "checkout_start",
        "booking_start", "booking_sent", "booking_handoff", "b2b_sent",
    ]
    section: Literal["", "home", "shop", "tea", "cart", "book", "b2b", "payment"] = ""
    language: Literal["ru", "en", "zh"] = "ru"
    device: Literal["mobile", "tablet", "desktop"] = "desktop"
    referrer: str = Field(default="direct", max_length=160, pattern=r"^[A-Za-z0-9.:-]+$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rate_limit(request: Request, bucket: str, limit: int, window: int) -> None:
    """Небольшой per-IP лимит для одного процесса checkout."""
    address = request.client.host if request.client else "unknown"
    address_hash = hashlib.blake2b(address.encode(), key=_rate_salt, digest_size=12).hexdigest()
    key, now = f"{bucket}:{address_hash}", time.monotonic()
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
        if "paid_at" not in columns:
            con.execute("ALTER TABLE orders ADD COLUMN paid_at TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_paid ON orders(status, paid_at)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS business_leads (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                company TEXT NOT NULL,
                name TEXT NOT NULL,
                contact TEXT NOT NULL,
                note TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)
        lead_columns = {row["name"] for row in con.execute("PRAGMA table_info(business_leads)")}
        if "status" not in lead_columns:
            con.execute("ALTER TABLE business_leads ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
        if "updated_at" not in lead_columns:
            con.execute("ALTER TABLE business_leads ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_business_leads_created ON business_leads(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_business_leads_status ON business_leads(status)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                event TEXT NOT NULL,
                section TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru',
                device TEXT NOT NULL DEFAULT 'desktop',
                referrer TEXT NOT NULL DEFAULT 'direct'
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics_events(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event_created ON analytics_events(event, created_at)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_session_created "
            "ON analytics_events(session_hash, created_at)"
        )
        # Сырые обезличенные события нужны только для сравнений и сезонности.
        # Года достаточно; старая телеметрия не должна бесконечно раздувать базу.
        con.execute(
            "DELETE FROM analytics_events WHERE created_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=ANALYTICS_RETENTION_DAYS)).isoformat(),),
        )
    try:
        DATA_DIR.chmod(0o700)
        DB_PATH.chmod(0o600)
    except OSError:
        logging.warning("Не удалось ужесточить права на каталог данных", exc_info=True)


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
        "paid_at": row["paid_at"] if "paid_at" in row.keys() else None,
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


PAID_ORDER_STATUSES = ("paid", "confirmed", "packing", "shipped", "completed")


def hash_session(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def cleanup_analytics_if_due(con: sqlite3.Connection) -> None:
    global _analytics_cleanup_after
    now = time.monotonic()
    if now < _analytics_cleanup_after:
        return
    with _analytics_cleanup_lock:
        if now < _analytics_cleanup_after:
            return
        con.execute(
            "DELETE FROM analytics_events WHERE created_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=ANALYTICS_RETENTION_DAYS)).isoformat(),),
        )
        _analytics_cleanup_after = now + 86400


def analytics_window(days: int, offset: int = 0) -> tuple[datetime, datetime]:
    """UTC boundaries for Moscow calendar days and an equal comparison window."""
    now = datetime.now(timezone.utc)
    moscow_now = now + timedelta(hours=3)
    current_start = (
        moscow_now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=days - 1, hours=3)
    )
    duration = now - current_start
    if not offset:
        return current_start, now
    end = current_start - duration * (offset - 1)
    return end - duration, end


def analytics_summary(con: sqlite3.Connection, start: datetime, end: datetime) -> dict:
    row = con.execute(
        """
        WITH window_events AS (
          SELECT * FROM analytics_events WHERE created_at >= ? AND created_at < ?
        ), cohort AS (
          SELECT DISTINCT session_hash FROM window_events WHERE event = 'page_view'
        )
        SELECT
          SUM(event = 'page_view') AS visits,
          COUNT(DISTINCT CASE WHEN event = 'page_view' THEN session_hash END) AS visitors,
          COUNT(DISTINCT CASE WHEN event = 'section_view' AND section = 'shop' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS shop_visitors,
          COUNT(DISTINCT CASE WHEN event = 'cart_open' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS cart_visitors,
          COUNT(DISTINCT CASE WHEN event = 'checkout_start' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS checkout_visitors,
          COUNT(DISTINCT CASE WHEN event = 'booking_start' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS booking_visitors,
          COUNT(DISTINCT CASE WHEN event = 'b2b_sent' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS b2b_visitors,
          COUNT(DISTINCT CASE WHEN event = 'order_created' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS order_visitors
        FROM window_events
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    result = {key: int(row[key] or 0) for key in row.keys()}
    result["shop_conversion"] = round(100 * result["shop_visitors"] / result["visitors"], 1) if result["visitors"] else 0
    return result


def commerce_summary(con: sqlite3.Connection, start: datetime, end: datetime) -> dict:
    placeholders = ",".join("?" for _ in PAID_ORDER_STATUSES)
    created = int(con.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND created_at < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0])
    paid = con.execute(
        f"""
        SELECT
          COUNT(*) AS paid_orders,
          COALESCE(SUM(total), 0) AS revenue
        FROM orders
        WHERE status IN ({placeholders})
          AND COALESCE(paid_at, created_at) >= ? AND COALESCE(paid_at, created_at) < ?
        """,
        (*PAID_ORDER_STATUSES, start.isoformat(), end.isoformat()),
    ).fetchone()
    queue = con.execute(
        """
        SELECT
          SUM(status = 'pending_payment') AS awaiting_payment,
          SUM(status IN ('paid','confirmed')) AS needs_attention,
          SUM(status IN ('packing','shipped')) AS in_fulfilment
        FROM orders
        """
    ).fetchone()
    result = {
        "orders_created": created,
        "paid_orders": int(paid["paid_orders"] or 0),
        "revenue": int(paid["revenue"] or 0),
        **{key: int(queue[key] or 0) for key in queue.keys()},
    }
    result["average_order"] = round(result["revenue"] / result["paid_orders"]) if result["paid_orders"] else 0
    result["leads"] = int(con.execute(
        "SELECT COUNT(*) FROM business_leads WHERE created_at >= ? AND created_at < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0])
    result["new_leads"] = int(con.execute(
        "SELECT COUNT(*) FROM business_leads WHERE status = 'new'"
    ).fetchone()[0])
    return result


def percent_change(current: int, previous: int) -> float | None:
    if previous == 0:
        return None if current else 0
    return round((current - previous) * 100 / previous, 1)


def dashboard_data(days: int) -> dict:
    start, end = analytics_window(days)
    previous_start, previous_end = analytics_window(days, 1)
    with db() as con:
        cleanup_analytics_if_due(con)
        traffic = analytics_summary(con, start, end)
        previous_traffic = analytics_summary(con, previous_start, previous_end)
        commerce = commerce_summary(con, start, end)
        previous_commerce = commerce_summary(con, previous_start, previous_end)
        traffic["order_conversion"] = round(
            100 * traffic["order_visitors"] / traffic["visitors"], 1
        ) if traffic["visitors"] else 0

        traffic_rows = con.execute(
            """
            WITH window_events AS (
              SELECT * FROM analytics_events WHERE created_at >= ? AND created_at < ?
            ), cohort AS (
              SELECT DISTINCT session_hash FROM window_events WHERE event = 'page_view'
            )
            SELECT date(created_at, '+3 hours') AS day,
              SUM(event = 'page_view') AS visits,
              COUNT(DISTINCT CASE WHEN event = 'page_view' THEN session_hash END) AS visitors,
              COUNT(DISTINCT CASE WHEN event = 'section_view' AND section = 'shop' AND session_hash IN (SELECT session_hash FROM cohort) THEN session_hash END) AS shop
            FROM window_events GROUP BY day
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        order_rows = con.execute(
            """
            SELECT date(created_at, '+3 hours') AS day,
              COUNT(*) AS orders
            FROM orders WHERE created_at >= ? AND created_at < ? GROUP BY day
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        revenue_rows = con.execute(
            """
            SELECT date(COALESCE(paid_at, created_at), '+3 hours') AS day, COALESCE(SUM(total), 0) AS revenue
            FROM orders
            WHERE status IN ('paid','confirmed','packing','shipped','completed')
              AND COALESCE(paid_at, created_at) >= ? AND COALESCE(paid_at, created_at) < ?
            GROUP BY day
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        traffic_by_day = {row["day"]: dict(row) for row in traffic_rows}
        orders_by_day = {row["day"]: dict(row) for row in order_rows}
        revenue_by_day = {row["day"]: int(row["revenue"] or 0) for row in revenue_rows}
        daily = []
        moscow_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
        for index in range(days):
            day = (moscow_today - timedelta(days=days - 1 - index)).isoformat()
            trow, orow = traffic_by_day.get(day, {}), orders_by_day.get(day, {})
            daily.append({
                "date": day,
                "visits": int(trow.get("visits") or 0),
                "visitors": int(trow.get("visitors") or 0),
                "shop": int(trow.get("shop") or 0),
                "orders": int(orow.get("orders") or 0),
                "revenue": revenue_by_day.get(day, 0),
            })

        breakdown = {}
        for field in ("device", "language", "referrer"):
            rows = con.execute(
                f"""SELECT {field} AS name, COUNT(DISTINCT session_hash) AS value
                    FROM analytics_events WHERE event = 'page_view' AND created_at >= ? AND created_at < ?
                    GROUP BY {field} ORDER BY value DESC LIMIT 8""",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            breakdown[field] = [{"name": row["name"], "value": int(row["value"])} for row in rows]
        section_rows = con.execute(
            """WITH window_events AS (
                  SELECT * FROM analytics_events WHERE created_at >= ? AND created_at < ?
                ), cohort AS (
                  SELECT DISTINCT session_hash FROM window_events WHERE event = 'page_view'
                )
                SELECT section AS name, COUNT(DISTINCT session_hash) AS value
                FROM window_events
                WHERE event = 'section_view' AND session_hash IN (SELECT session_hash FROM cohort)
                GROUP BY section ORDER BY value DESC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        breakdown["section"] = [{"name": row["name"], "value": int(row["value"])} for row in section_rows]

        tea_totals: dict[str, dict] = {}
        order_item_rows = con.execute(
            "SELECT items_json FROM orders WHERE created_at >= ? AND created_at < ? AND status != 'cancelled'",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        for row in order_item_rows:
            for item in json.loads(row["items_json"]):
                tea = tea_totals.setdefault(item["id"], {"id": item["id"], "name": item["name"], "qty": 0, "revenue": 0})
                tea["qty"] += int(item["qty"])
                tea["revenue"] += int(item["total"])

        first_event = con.execute("SELECT MIN(created_at) FROM analytics_events").fetchone()[0]

    changes = {
        "visits": percent_change(traffic["visits"], previous_traffic["visits"]),
        "visitors": percent_change(traffic["visitors"], previous_traffic["visitors"]),
        "paid_orders": percent_change(commerce["paid_orders"], previous_commerce["paid_orders"]),
        "revenue": percent_change(commerce["revenue"], previous_commerce["revenue"]),
    }
    return {
        "period": {"days": days, "start": start.isoformat(), "end": end.isoformat()},
        "traffic": traffic,
        "commerce": commerce,
        "changes": changes,
        "daily": daily,
        "breakdown": breakdown,
        "top_teas": sorted(tea_totals.values(), key=lambda item: (item["revenue"], item["qty"]), reverse=True)[:5],
        "system": {
            "checkout": "test" if TEST_MODE else "unconfigured",
            "saby_configured": bool(saby_client.configuration().get("configured")),
            "telegram_configured": bool(BOT_TOKEN and OWNER_CHAT_IDS),
            "notification_recipients": len(OWNER_CHAT_IDS),
            "catalog_items": len(load_catalog()),
            "analytics_since": first_event,
            "database_size": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        },
    }


@app.get("/api/health")
def health():
    return {"ok": True, "test_mode": TEST_MODE, "catalog_items": len(load_catalog())}


@app.post("/api/analytics/events", status_code=204)
def collect_analytics(payload: AnalyticsEvent, request: Request):
    """Store a small anonymous product event; IP and user-agent are never persisted."""
    rate_limit(request, "analytics", 180, 600)
    session_hash = hash_session(payload.session_id)
    with db() as con:
        cleanup_analytics_if_due(con)
        con.execute(
            """INSERT INTO analytics_events
               (created_at, session_hash, event, section, language, device, referrer)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), session_hash, payload.event, payload.section, payload.language, payload.device, payload.referrer),
        )
    return Response(status_code=204)


@app.get("/api/admin/dashboard")
def admin_dashboard(
    days: int = Query(default=30, ge=7, le=90), authorization: str = Header(default="")
):
    require_admin(authorization)
    return dashboard_data(days)


@app.get("/api/admin/orders")
def admin_orders(
    authorization: str = Header(default=""),
    status: str = "",
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    require_admin(authorization)
    allowed = {"pending_payment", "paid", "confirmed", "packing", "shipped", "completed", "cancelled"}
    if status and status not in allowed:
        raise HTTPException(422, "Неизвестный статус")
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    query = q.strip()
    if query:
        conditions.append("casefold(id || ' ' || customer_json || ' ' || items_json) LIKE ?")
        params.append(f"%{query.casefold()}%")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with db() as con:
        con.create_function("casefold", 1, lambda value: (value or "").casefold())
        total = int(con.execute(f"SELECT COUNT(*) FROM orders{where}", params).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM orders{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "orders": [admin_order(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.patch("/api/admin/orders/{order_id}")
def admin_update_order(order_id: str, payload: UpdateOrderStatus, authorization: str = Header(default="")):
    require_admin(authorization)
    row = order_row(order_id)
    if row["status"] == "pending_payment" and payload.status not in {"paid", "cancelled"}:
        raise HTTPException(409, "Сначала заказ должен быть оплачен или отменён")
    updated = now_iso()
    with db() as con:
        if payload.status == "paid":
            con.execute(
                "UPDATE orders SET status = ?, updated_at = ?, paid_at = COALESCE(paid_at, ?) WHERE id = ?",
                (payload.status, updated, updated, order_id),
            )
        else:
            con.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (payload.status, updated, order_id))
    return admin_order(order_row(order_id))


@app.get("/api/admin/business-leads")
def admin_business_leads(
    authorization: str = Header(default=""),
    status: str = "",
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    require_admin(authorization)
    if status and status not in {"new", "contacted", "closed"}:
        raise HTTPException(422, "Неизвестный статус")
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    query = q.strip()
    if query:
        conditions.append("casefold(company || ' ' || name || ' ' || contact || ' ' || note) LIKE ?")
        params.append(f"%{query.casefold()}%")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with db() as con:
        con.create_function("casefold", 1, lambda value: (value or "").casefold())
        total = int(con.execute(f"SELECT COUNT(*) FROM business_leads{where}", params).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM business_leads{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "leads": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.patch("/api/admin/business-leads/{lead_id}")
def admin_update_business_lead(
    lead_id: str, payload: UpdateLeadStatus, authorization: str = Header(default="")
):
    require_admin(authorization)
    with db() as con:
        exists = con.execute("SELECT 1 FROM business_leads WHERE id = ?", (lead_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Заявка не найдена")
        con.execute(
            "UPDATE business_leads SET status = ?, updated_at = ? WHERE id = ?",
            (payload.status, now_iso(), lead_id),
        )
        row = con.execute("SELECT * FROM business_leads WHERE id = ?", (lead_id,)).fetchone()
    return dict(row)


@app.get("/api/admin/saby/status")
def admin_saby_status(authorization: str = Header(default="")):
    require_admin(authorization)
    return saby_client.configuration()


@app.post("/api/admin/saby/test")
def admin_saby_test(authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        result = saby_client.sales_points()
    except SabyError as exc:
        raise HTTPException(502, str(exc)) from exc
    points = result.get("salesPoints", []) if isinstance(result, dict) else []
    return {
        "connected": True,
        "points": [
            {key: point.get(key) for key in ("id", "name", "address", "locality", "prices")}
            for point in points[:50]
        ],
    }


@app.get("/api/admin/saby/catalog-preview")
def admin_saby_catalog_preview(authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        result = saby_client.catalog(page_size=25)
    except SabyError as exc:
        raise HTTPException(502, str(exc)) from exc
    items = result.get("nomenclatures", result.get("items", [])) if isinstance(result, dict) else []
    return {
        "items": [
            {key: item.get(key) for key in ("id", "externalId", "name", "cost", "balance", "published", "unit")}
            for item in items[:25]
        ]
    }


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
    if not TEST_MODE:
        raise HTTPException(503, "Онлайн-оплата пока не подключена")
    validate_delivery(payload)
    lines, subtotal = price_order(payload)
    delivery_price = DELIVERY_PRICES[payload.delivery]
    order_id = uuid.uuid4().hex[:12].upper()
    created = now_iso()
    customer = payload.model_dump(exclude={"items", "payment_method", "delivery", "analytics_session"})
    analytics_session_hash = hash_session(payload.analytics_session) if payload.analytics_session else None
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
        if analytics_session_hash:
            context = con.execute(
                """SELECT language, device, referrer FROM analytics_events
                    WHERE session_hash = ? ORDER BY id DESC LIMIT 1""",
                (analytics_session_hash,),
            ).fetchone()
            if context:
                con.execute(
                    """INSERT INTO analytics_events
                       (created_at, session_hash, event, section, language, device, referrer)
                       VALUES (?, ?, 'order_created', 'payment', ?, ?, ?)""",
                    (created, analytics_session_hash, context["language"], context["device"], context["referrer"]),
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
            """INSERT INTO business_leads
               (id, created_at, company, name, contact, note, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'new', ?)""",
            (lead["id"], lead["created_at"], lead["company"], lead["name"], lead["contact"], lead["note"], lead["created_at"]),
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
                "UPDATE orders SET status = 'paid', updated_at = ?, paid_at = ?, provider_payment_id = ? WHERE id = ?",
                (now_iso(), now_iso(), f"mock_{uuid.uuid4().hex}", order_id),
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


@app.get("/manage/")
@app.get("/manage")
def management_page():
    """Short, memorable alias for the owner dashboard; access still requires #token."""
    return FileResponse(ROOT / "backend" / "admin.html")


# В локальной разработке backend одновременно раздаёт собранный сайт.
if (ROOT / "dist").exists():
    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="site")

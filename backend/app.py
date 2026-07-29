#!/usr/bin/env python3
"""Тестовый backend магазина «Чайня».

Считает заказ только по серверному каталогу, хранит его в SQLite и имитирует
платёж. Реальные Saby/CDEK/acquiring адаптеры подключаются вместо mock-функций.
"""

from __future__ import annotations

import json
import hashlib
import hmac
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
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .cdek import CdekClient, CdekError
from .cdek_delivery import (
    CdekDeliverySettings,
    build_order_payload as build_cdek_order_payload,
    normalized_quote as normalize_cdek_quote,
    package_spec as cdek_package_spec,
    tariff_payload as cdek_tariff_payload,
)
from .integration_writes import IntegrationWriter
from .integration_guard import ExternalWriteBlocked
from .saby import SabyClient, SabyError
from .saby_sync import (
    SABY_NOMENCLATURE_BY_SITE_ID,
    SabySyncError,
    build_saby_order,
    sync_mode_from_env,
    validate_mapping_file,
    write_allowed as saby_write_allowed,
)
from .tbank import TBankClient, TBankError, validate_payment_url, verify_notification_token
from .tbank_receipt import TBankReceiptError, TBankReceiptSettings, build_receipt


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
DATA_DIR = Path(os.getenv("CHAINYA_DATA_DIR", ROOT / "backend" / "data"))
DB_PATH = DATA_DIR / "orders.sqlite3"
CATALOG_PATH = Path(os.getenv("CHAINYA_CATALOG_PATH", PROJECT / "telegram-bot" / "teas.json"))
CDEK_CITIES_PATH = Path(
    os.getenv("CDEK_CITIES_PATH", DATA_DIR / "cdek-cities-ru.json")
)


def test_mode_from_value(value: str | None) -> bool:
    """Fail closed: only the exact value `0` can disable the safety mode."""
    return (value if value is not None else "1").strip() != "0"


TEST_MODE = test_mode_from_value(os.getenv("CHAINYA_TEST_MODE"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
ADMIN_SESSION_COOKIE = "chainya_admin_session"
ADMIN_SESSION_SECONDS = 12 * 60 * 60
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
tbank_client = TBankClient()
cdek_client = CdekClient()
cdek_delivery_settings = CdekDeliverySettings.from_env()
integration_writer = IntegrationWriter(
    test_mode=TEST_MODE,
    exposed_providers=frozenset({"tbank", "saby", "cdek"}),
)
tbank_receipt_settings = TBankReceiptSettings.from_env()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()
_rate_salt = secrets.token_bytes(16)
_analytics_cleanup_lock = threading.Lock()
_analytics_cleanup_after = 0.0
_cdek_cache: dict[str, tuple[float, object]] = {}
_cdek_cache_lock = threading.Lock()
_cdek_cities_index: tuple[float, list[dict]] | None = None
_cdek_cities_index_lock = threading.Lock()


def normalized_search_text(value: object) -> str:
    """Normalize Russian search text without changing displayed CDEK data."""
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def cached_cdek_cities_index() -> list[dict]:
    """Load the periodically refreshed CDEK city index from protected storage."""
    global _cdek_cities_index
    try:
        modified = CDEK_CITIES_PATH.stat().st_mtime
    except OSError:
        return []
    with _cdek_cities_index_lock:
        if _cdek_cities_index and _cdek_cities_index[0] == modified:
            return _cdek_cities_index[1]
        try:
            data = json.loads(CDEK_CITIES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        cities = data if isinstance(data, list) else []
        clean = [
            city for city in cities
            if isinstance(city, dict) and city.get("city")
        ]
        _cdek_cities_index = (modified, clean)
        return clean


def fuzzy_cdek_cities(query: str, limit: int = 20) -> list[dict]:
    """Return prefix/substring city matches from the local CDEK index."""
    needle = normalized_search_text(query)
    if not needle:
        return []
    matches: list[tuple[int, int, str, str, dict]] = []
    for city in cached_cdek_cities_index():
        name = normalized_search_text(city.get("city"))
        region = normalized_search_text(city.get("region"))
        if name == needle:
            rank = 0
        elif name.startswith(needle):
            rank = 1
        elif needle in name:
            rank = 2
        elif region.startswith(needle):
            rank = 3
        elif needle in region:
            rank = 4
        else:
            continue
        population = int(city.get("population") or 0)
        matches.append((rank, -population, name, region, city))
    matches.sort(key=lambda item: (item[0], item[1], len(item[2]), item[2], item[3]))
    return [item[4] for item in matches[:limit]]


def rollout_mode(name: str, *, allow_demo: bool = False) -> tuple[str, bool]:
    """Read a rollout mode without ever turning an invalid value into `auto`."""
    value = os.getenv(name, "off").strip().lower()
    allowed = {"off", "shadow", "manual", "auto"}
    if allow_demo:
        allowed.add("demo")
    return (value, True) if value in allowed else ("invalid", False)


def tbank_checkout_ready() -> bool:
    """Whether this process may create a payment for a public checkout.

    Test deployments may call T-Bank only with a real DEMO terminal. Production
    deployments require an explicit ``auto`` rollout and reject DEMO keys.
    """
    mode, valid = rollout_mode("TBANK_CHECKOUT_MODE", allow_demo=True)
    settings = tbank_client.settings
    urls_ready = bool(
        settings.notification_url and settings.success_url and settings.fail_url
    )
    receipt_ready = (
        not tbank_receipt_settings.enabled or tbank_receipt_settings.configured
    )
    if not valid or not settings.configured or not urls_ready or not receipt_ready:
        return False
    if TEST_MODE:
        return mode == "demo" and settings.is_demo
    return mode == "auto" and not settings.is_demo and tbank_receipt_settings.configured


def integrations_status() -> dict:
    """Return a network-free, secret-free snapshot of integration readiness."""
    tbank_mode, tbank_mode_valid = rollout_mode("TBANK_CHECKOUT_MODE", allow_demo=True)
    cdek_mode, cdek_mode_valid = rollout_mode("CDEK_INTEGRATION_MODE")
    tbank = tbank_client.configuration()
    cdek = cdek_client.configuration()
    saby = saby_client.configuration()

    saby_mode_error = ""
    try:
        saby_mode_value = sync_mode_from_env()
    except SabySyncError as exc:
        saby_mode_value, saby_mode_error = None, str(exc)
    mapping_error = ""
    try:
        validate_mapping_file(CATALOG_PATH)
    except SabySyncError as exc:
        mapping_error = str(exc)

    tbank_workflow_exposed = integration_writer.provider_exposed("tbank")
    tbank_writes_enabled = tbank_checkout_ready()
    saby_policy_allows = bool(
        saby_mode_value
        and saby_write_allowed(saby_mode_value, test_mode=TEST_MODE, manual_approved=False)
    )
    return {
        "guard": {
            "test_mode": TEST_MODE,
            "external_writes_locked": TEST_MODE,
            "demo_writes_enabled": bool(TEST_MODE and tbank_writes_enabled),
            "workflow_exposed": tbank_workflow_exposed,
            "exposed_providers": sorted(integration_writer.exposed_providers),
        },
        "tbank": {
            **tbank,
            "adapter_ready": True,
            "mode": tbank_mode,
            "mode_valid": tbank_mode_valid,
            "credentials_ready": bool(tbank.get("configured")),
            "demo_credentials": bool(tbank_client.settings.is_demo),
            "callback_ready": bool(tbank.get("notification_url_configured")),
            "receipt_enabled": tbank_receipt_settings.enabled,
            "receipt_configured": tbank_receipt_settings.configured,
            "receipt_missing": tbank_receipt_settings.missing,
            "writes_enabled": tbank_writes_enabled,
        },
        "saby": {
            **saby,
            "adapter_ready": True,
            "mode": saby_mode_value.value if saby_mode_value else "invalid",
            "mode_valid": saby_mode_value is not None,
            "mode_error": saby_mode_error,
            "mapping_items": len(SABY_NOMENCLATURE_BY_SITE_ID),
            "mapping_valid": not mapping_error,
            "mapping_error": mapping_error,
            "credentials_ready": bool(
                saby.get("configured") and saby.get("point_id") and saby.get("price_list_id")
            ),
            "policy_allows_write": saby_policy_allows,
            "writes_enabled": bool(
                saby_policy_allows
                and saby.get("configured")
                and saby.get("point_id")
                and saby.get("price_list_id")
                and not mapping_error
            ),
        },
        "cdek": {
            **cdek,
            "adapter_ready": True,
            "mode": cdek_mode,
            "mode_valid": cdek_mode_valid,
            "credentials_ready": bool(cdek.get("configured")),
            "from_city_code": cdek_delivery_settings.from_city_code,
            "shipment_point_ready": bool(cdek_delivery_settings.shipment_point),
            "quotes_enabled": bool(cdek.get("configured")),
            "writes_enabled": bool(
                not TEST_MODE
                and cdek_mode_valid
                and cdek_mode in {"manual", "auto"}
                and cdek.get("configured")
                and cdek_delivery_settings.shipment_point
            ),
        },
    }


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
    city_code: int | None = Field(default=None, ge=1, le=9_999_999)
    address: str = Field(default="", max_length=300)
    pvz_code: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=1000)
    privacy_accepted: Literal[True]
    language: Literal["ru", "en", "zh"] = "ru"
    analytics_session: str | None = Field(default=None, min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value):
        if len(re.sub(r"\D", "", value)) < 10:
            raise ValueError("укажите полный номер телефона")
        return value.strip()


class DeliveryQuoteRequest(BaseModel):
    items: list[OrderItem] = Field(min_length=1, max_length=50)
    method: Literal["cdek_pvz", "cdek_courier"]
    city_code: int = Field(ge=1, le=9_999_999)


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


class AdminLogin(BaseModel):
    token: str = Field(min_length=16, max_length=256)


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
                provider_payment_id TEXT,
                payment_token TEXT,
                paid_at TEXT,
                payment_provider TEXT NOT NULL DEFAULT 'none',
                payment_state TEXT NOT NULL DEFAULT 'not_started',
                payment_attempts INTEGER NOT NULL DEFAULT 0,
                payment_last_error TEXT NOT NULL DEFAULT '',
                payment_updated_at TEXT,
                payment_url TEXT,
                payment_provider_status TEXT NOT NULL DEFAULT '',
                saby_state TEXT NOT NULL DEFAULT 'not_queued',
                saby_external_id TEXT,
                saby_payload_hash TEXT,
                saby_attempts INTEGER NOT NULL DEFAULT 0,
                saby_last_error TEXT NOT NULL DEFAULT '',
                saby_synced_at TEXT,
                cdek_state TEXT NOT NULL DEFAULT 'not_requested',
                cdek_order_uuid TEXT,
                cdek_number TEXT,
                cdek_last_error TEXT NOT NULL DEFAULT '',
                cdek_updated_at TEXT,
                cdek_quote_json TEXT NOT NULL DEFAULT '{}',
                idempotency_key_hash TEXT,
                request_hash TEXT
            )
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        order_migrations = {
            "payment_token": "TEXT",
            "paid_at": "TEXT",
            "payment_provider": "TEXT NOT NULL DEFAULT 'none'",
            "payment_state": "TEXT NOT NULL DEFAULT 'not_started'",
            "payment_attempts": "INTEGER NOT NULL DEFAULT 0",
            "payment_last_error": "TEXT NOT NULL DEFAULT ''",
            "payment_updated_at": "TEXT",
            "payment_url": "TEXT",
            "payment_provider_status": "TEXT NOT NULL DEFAULT ''",
            "saby_state": "TEXT NOT NULL DEFAULT 'not_queued'",
            "saby_external_id": "TEXT",
            "saby_payload_hash": "TEXT",
            "saby_attempts": "INTEGER NOT NULL DEFAULT 0",
            "saby_last_error": "TEXT NOT NULL DEFAULT ''",
            "saby_synced_at": "TEXT",
            "cdek_state": "TEXT NOT NULL DEFAULT 'not_requested'",
            "cdek_order_uuid": "TEXT",
            "cdek_number": "TEXT",
            "cdek_last_error": "TEXT NOT NULL DEFAULT ''",
            "cdek_updated_at": "TEXT",
            "cdek_quote_json": "TEXT NOT NULL DEFAULT '{}'",
            "idempotency_key_hash": "TEXT",
            "request_hash": "TEXT",
        }
        for column, declaration in order_migrations.items():
            if column not in columns:
                con.execute(f"ALTER TABLE orders ADD COLUMN {column} {declaration}")
        con.execute(
            """UPDATE orders
               SET payment_state = CASE
                     WHEN paid_at IS NOT NULL OR provider_payment_id IS NOT NULL THEN 'paid'
                     WHEN status IN ('paid','confirmed','packing','shipped','completed') THEN 'paid'
                     WHEN status = 'pending_payment' THEN 'awaiting'
                     WHEN status = 'cancelled' THEN 'cancelled'
                     ELSE 'not_started'
                   END,
                   payment_provider = CASE
                     WHEN payment_provider != 'none' THEN payment_provider
                     WHEN provider_payment_id LIKE 'mock_%' THEN 'test'
                     WHEN provider_payment_id IS NOT NULL THEN 'external'
                     WHEN status IN ('paid','confirmed','packing','shipped','completed') THEN 'manual'
                     ELSE ?
                   END,
                   payment_updated_at = COALESCE(payment_updated_at, updated_at)
               WHERE payment_state = 'not_started'""",
            ("test" if TEST_MODE else "none",),
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_paid ON orders(status, paid_at)")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_provider_payment "
            "ON orders(payment_provider, provider_payment_id) "
            "WHERE provider_payment_id IS NOT NULL"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency "
            "ON orders(idempotency_key_hash) WHERE idempotency_key_hash IS NOT NULL"
        )
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
    if payload.delivery != "pickup" and (not payload.city.strip() or not payload.city_code):
        raise HTTPException(422, "Выберите город из подсказок CDEK")
    if payload.delivery == "cdek_pvz" and not payload.pvz_code.strip():
        raise HTTPException(422, "Для доставки в ПВЗ укажите город и код пункта")
    if payload.delivery == "cdek_courier" and (not payload.city.strip() or not payload.address.strip()):
        raise HTTPException(422, "Для курьерской доставки укажите город и адрес")


def cached_cdek(key: str, ttl: int, loader):
    now = time.monotonic()
    with _cdek_cache_lock:
        cached = _cdek_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    value = loader()
    with _cdek_cache_lock:
        if len(_cdek_cache) > 500:
            expired = [item_key for item_key, item in _cdek_cache.items() if item[0] <= now]
            for item_key in expired:
                _cdek_cache.pop(item_key, None)
        _cdek_cache[key] = (now + ttl, value)
    return value


def cdek_quote_for_lines(
    method: str, city_code: int, lines: list[dict]
) -> dict:
    if not cdek_client.settings.configured:
        raise HTTPException(503, "Расчёт доставки CDEK пока недоступен")
    package = cdek_package_spec(lines, cdek_delivery_settings)
    payload = cdek_tariff_payload(
        method, city_code, package, cdek_delivery_settings
    )
    cache_key = (
        f"quote:{method}:{city_code}:{package['weight']}:"
        f"{package['length']}:{package['width']}:{package['height']}"
    )
    try:
        result = cached_cdek(
            cache_key, 300, lambda: cdek_client.calculate_tariff(payload)
        )
        return normalize_cdek_quote(method, city_code, package, result)
    except (CdekError, ValueError) as exc:
        raise HTTPException(502, "CDEK не смог рассчитать доставку") from exc


def cdek_point(city_code: int, code: str) -> dict:
    normalized = code.strip().upper()
    if not re.fullmatch(r"[A-ZА-Я0-9-]{2,20}", normalized):
        raise HTTPException(422, "Некорректный код пункта CDEK")
    try:
        points = cached_cdek(
            f"point:{city_code}:{normalized}",
            600,
            lambda: cdek_client.delivery_points(
                city_code=city_code, code=normalized, is_handout="true"
            ),
        )
    except CdekError as exc:
        raise HTTPException(502, "Не удалось проверить пункт CDEK") from exc
    valid = [
        point for point in points
        if point.get("code") == normalized
        and point.get("status") == "ACTIVE"
        and point.get("is_handout") is True
        and int(point.get("location", {}).get("city_code", 0)) == city_code
    ]
    if not valid:
        raise HTTPException(422, "Выбранный пункт CDEK не найден в этом городе")
    return valid[0]


def public_order(row: sqlite3.Row) -> dict:
    result = {
        "id": row["id"], "status": row["status"], "created_at": row["created_at"],
        "subtotal": row["subtotal"], "delivery_price": row["delivery_price"],
        "total": row["total"], "payment_method": row["payment_method"],
        "delivery": row["delivery"], "items": json.loads(row["items_json"]),
        "paid_at": row["paid_at"] if "paid_at" in row.keys() else None,
        "payment_state": row["payment_state"] if "payment_state" in row.keys() else "not_started",
    }
    if "cdek_quote_json" in row.keys() and row["cdek_quote_json"]:
        quote = json.loads(row["cdek_quote_json"])
        if quote:
            result["delivery_quote"] = {
                key: quote.get(key)
                for key in (
                    "provider", "tariff_name", "price", "period_min", "period_max", "point"
                )
            }
    return result


def order_request_hash(payload: CreateOrder) -> str:
    semantic_payload = payload.model_dump(mode="json", exclude={"analytics_session"})
    encoded = json.dumps(
        semantic_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def idempotency_hash(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > 128
        or not re.fullmatch(r"[A-Za-z0-9._:-]+", value)
    ):
        raise HTTPException(422, "Некорректный Idempotency-Key")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def checkout_response(row: sqlite3.Row, request: Request, *, reused: bool = False) -> dict:
    base = str(request.base_url).rstrip("/")
    token = row["payment_token"]
    is_tbank = row["payment_provider"] in {"tbank_demo", "tbank"}
    payment_url = row["payment_url"] if "payment_url" in row.keys() else None
    return {
        "order": public_order(row),
        "payment": {
            "mode": row["payment_provider"] if is_tbank else ("test" if TEST_MODE else "unconfigured"),
            "url": (
                payment_url
                if is_tbank
                else f"{base}/test-payment/{row['id']}?token={token}" if TEST_MODE and token else None
            ),
            "reused": reused,
        },
    }


def order_access_url(base_url: str, row: sqlite3.Row, language: str) -> str:
    """Append non-authoritative order access data to a configured return URL."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise TBankError("Некорректный адрес возврата после оплаты")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"order_id": row["id"], "token": row["payment_token"], "lang": language})
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def normalized_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def receipt_for_order(row: sqlite3.Row) -> dict | None:
    if not tbank_receipt_settings.enabled:
        return None
    try:
        return build_receipt(
            phone=normalized_phone(json.loads(row["customer_json"])["phone"]),
            items=json.loads(row["items_json"]),
            delivery_price=int(row["delivery_price"]),
            settings=tbank_receipt_settings,
        )
    except TBankReceiptError as exc:
        raise TBankError(str(exc)) from exc


def initialize_tbank_payment(row: sqlite3.Row, language: str) -> sqlite3.Row:
    """Create exactly one bank payment for a freshly persisted local order."""
    mode, _valid = rollout_mode("TBANK_CHECKOUT_MODE", allow_demo=True)
    settings = tbank_client.settings
    try:
        result = integration_writer.create_tbank_payment(
            tbank_client,
            row["id"],
            int(row["total"]) * 100,
            mode=mode,
            description=f"Заказ Чайни {row['id']}",
            pay_type="O",
            language=language if language in {"ru", "en"} else "en",
            notification_url=settings.notification_url,
            success_url=order_access_url(settings.success_url, row, language),
            fail_url=order_access_url(settings.fail_url, row, language),
            data={"Phone": normalized_phone(json.loads(row["customer_json"])["phone"])},
            receipt=receipt_for_order(row),
        )
        if result.get("Success") is not True:
            raise TBankError("Т-Банк вернул некорректный результат создания платежа")
        payment_id = str(result.get("PaymentId", ""))
        if not payment_id.isdigit() or len(payment_id) > 20:
            raise TBankError("Т-Банк не вернул идентификатор платежа")
        payment_url = validate_payment_url(result.get("PaymentURL"))
    except TBankError as exc:
        updated = now_iso()
        with db() as con:
            con.execute(
                """UPDATE orders
                   SET payment_state = 'failed', payment_last_error = ?,
                       payment_updated_at = ?, updated_at = ?
                   WHERE id = ? AND provider_payment_id IS NULL""",
                (str(exc), updated, updated, row["id"]),
            )
        form_label = (
            "Тестовая"
            if TEST_MODE and row["payment_provider"] == "tbank_demo"
            else "Платёжная"
        )
        raise HTTPException(
            502, f"{form_label} форма Т-Банка временно недоступна"
        ) from exc

    updated = now_iso()
    with db() as con:
        con.execute(
            """UPDATE orders
               SET provider_payment_id = ?, payment_url = ?, payment_state = 'awaiting',
                   payment_provider_status = ?, payment_attempts = payment_attempts + 1,
                   payment_last_error = '', payment_updated_at = ?, updated_at = ?
               WHERE id = ? AND provider_payment_id IS NULL AND payment_state = 'initializing'""",
            (
                payment_id, payment_url, str(result.get("Status", "NEW")),
                updated, updated, row["id"],
            ),
        )
    return order_row(row["id"])


def order_row(order_id: str) -> sqlite3.Row:
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    return row


def saby_ready_at(order: dict) -> datetime:
    """Choose a conservative planned time when the buyer did not select a slot."""
    moscow_now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    if order.get("delivery") == "pickup":
        return (moscow_now + timedelta(hours=2)).replace(second=0, microsecond=0)
    quote = order.get("delivery_quote")
    period_max = quote.get("period_max") if isinstance(quote, dict) else None
    days = period_max if isinstance(period_max, int) and period_max > 0 else 1
    target = (moscow_now + timedelta(days=days)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return target if target > moscow_now else moscow_now + timedelta(hours=2)


def _saby_external_id(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    for candidate in (
        result.get("externalId"),
        (result.get("order") or {}).get("externalId")
        if isinstance(result.get("order"), dict) else None,
        (result.get("result") or {}).get("externalId")
        if isinstance(result.get("result"), dict) else None,
    ):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def sync_paid_order_to_saby(order_id: str) -> None:
    """Create exactly one Saby order after a confirmed online payment."""
    try:
        mode = sync_mode_from_env()
    except SabySyncError:
        return
    if mode.value != "auto":
        return

    started = now_iso()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if (
            not row
            or row["payment_state"] != "paid"
            or row["status"] not in {"paid", "confirmed", "packing", "shipped", "completed"}
            or row["saby_state"] not in {"not_queued", "failed"}
        ):
            return
        claimed = con.execute(
            """UPDATE orders
               SET saby_state = 'sending', saby_attempts = saby_attempts + 1,
                   saby_last_error = '', updated_at = ?
               WHERE id = ? AND saby_state IN ('not_queued','failed')""",
            (started, order_id),
        )
        if claimed.rowcount != 1:
            return

    try:
        order = admin_order(order_row(order_id))
        payload = build_saby_order(
            order,
            settings=saby_client.settings,
            ready_at=saby_ready_at(order),
        )
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        payload_hash = hashlib.sha256(encoded).hexdigest()
        with db() as con:
            con.execute(
                "UPDATE orders SET saby_payload_hash = ?, updated_at = ? WHERE id = ?",
                (payload_hash, now_iso(), order_id),
            )
        result = integration_writer.create_saby_order(
            saby_client, payload, mode=mode.value
        )
        external_id = _saby_external_id(result)
        if not external_id:
            raise SabyError("Saby не вернул идентификатор созданного заказа")
    except (SabySyncError, ExternalWriteBlocked) as exc:
        failed = now_iso()
        with db() as con:
            con.execute(
                """UPDATE orders SET saby_state = 'failed', saby_last_error = ?,
                       updated_at = ? WHERE id = ? AND saby_state = 'sending'""",
                (str(exc)[:500], failed, order_id),
            )
        logging.warning("Заказ %s не передан в Saby: %s", order_id, exc)
        return
    except SabyError as exc:
        # Transport/API failures can be ambiguous: do not retry automatically,
        # otherwise a replay could create a duplicate order in Saby.
        failed = now_iso()
        with db() as con:
            con.execute(
                """UPDATE orders SET saby_state = 'ambiguous', saby_last_error = ?,
                       updated_at = ? WHERE id = ? AND saby_state = 'sending'""",
                (str(exc)[:500], failed, order_id),
            )
        logging.warning("Нужна ручная проверка заказа %s в Saby: %s", order_id, exc)
        return

    finished = now_iso()
    with db() as con:
        con.execute(
            """UPDATE orders SET saby_state = 'synced', saby_external_id = ?,
                   saby_last_error = '', saby_synced_at = ?, updated_at = ?
               WHERE id = ? AND saby_state = 'sending'""",
            (external_id, finished, finished, order_id),
        )


def require_order_token(row: sqlite3.Row, token: str) -> None:
    if not row["payment_token"] or not secrets.compare_digest(token, row["payment_token"]):
        raise HTTPException(403, "Недействительная ссылка заказа")


def admin_session_value(issued_at: int) -> str:
    signature = hmac.new(
        ADMIN_TOKEN.encode("utf-8"),
        str(issued_at).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{signature}"


def valid_admin_session(value: str) -> bool:
    if not ADMIN_TOKEN or not value or "." not in value:
        return False
    issued_raw, supplied_signature = value.split(".", 1)
    try:
        issued_at = int(issued_raw)
    except ValueError:
        return False
    age = int(time.time()) - issued_at
    if age < -60 or age > ADMIN_SESSION_SECONDS:
        return False
    expected = admin_session_value(issued_at).split(".", 1)[1]
    return secrets.compare_digest(supplied_signature, expected)


def require_admin(authorization: str) -> None:
    supplied = authorization.removeprefix("Bearer ").strip()
    if not ADMIN_TOKEN or not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(401, "Требуется доступ владельца", headers={"WWW-Authenticate": "Bearer"})


@app.middleware("http")
async def admin_session_to_authorization(request: Request, call_next):
    """Keep the raw owner token out of browser JavaScript after login."""
    path = request.url.path
    if (
        path.startswith("/api/admin/")
        and path != "/api/admin/session"
        and not request.headers.get("authorization")
        and valid_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE, ""))
    ):
        request.scope["headers"].append(
            (b"authorization", f"Bearer {ADMIN_TOKEN}".encode("ascii"))
        )
    return await call_next(request)


def admin_order(row: sqlite3.Row) -> dict:
    result = public_order(row)
    result["updated_at"] = row["updated_at"]
    result["customer"] = json.loads(row["customer_json"])
    result["integrations"] = {
        "payment": {
            "provider": row["payment_provider"],
            "state": row["payment_state"],
            "provider_status": row["payment_provider_status"],
            "attempts": row["payment_attempts"],
            "provider_id": row["provider_payment_id"],
            "last_error": row["payment_last_error"],
            "updated_at": row["payment_updated_at"],
        },
        "saby": {
            "state": row["saby_state"],
            "external_id": row["saby_external_id"],
            "payload_hash": row["saby_payload_hash"],
            "attempts": row["saby_attempts"],
            "last_error": row["saby_last_error"],
            "synced_at": row["saby_synced_at"],
        },
        "cdek": {
            "state": row["cdek_state"],
            "order_uuid": row["cdek_order_uuid"],
            "number": row["cdek_number"],
            "last_error": row["cdek_last_error"],
            "updated_at": row["cdek_updated_at"],
            "quote": (
                json.loads(row["cdek_quote_json"])
                if "cdek_quote_json" in row.keys() and row["cdek_quote_json"]
                else {}
            ),
        },
    }
    return result


def paid_notification(row: sqlite3.Row) -> str:
    customer = json.loads(row["customer_json"])
    items = json.loads(row["items_json"])
    title = (
        "🧪 Тестовый заказ оплачен"
        if TEST_MODE and row["payment_provider"] in {"test", "tbank_demo"}
        else "💳 Новый заказ оплачен"
    )
    lines = [
        title,
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
    integration = integrations_status()
    catalog = load_catalog()
    return {
        "period": {"days": days, "start": start.isoformat(), "end": end.isoformat()},
        "traffic": traffic,
        "commerce": commerce,
        "changes": changes,
        "daily": daily,
        "breakdown": breakdown,
        "top_teas": sorted(tea_totals.values(), key=lambda item: (item["revenue"], item["qty"]), reverse=True)[:5],
        "system": {
            "checkout": (
                "test"
                if TEST_MODE
                else "live" if integration["tbank"]["writes_enabled"] else "unconfigured"
            ),
            "saby_configured": bool(saby_client.configuration().get("configured")),
            "saby_order_mode": integration["saby"]["mode"],
            "saby_mapping_valid": integration["saby"]["mapping_valid"],
            "tbank_configured": integration["tbank"]["configured"],
            "tbank_mode": integration["tbank"]["mode"],
            "tbank_writes_enabled": integration["tbank"]["writes_enabled"],
            "tbank_callback_ready": integration["tbank"]["callback_ready"],
            "tbank_receipt_configured": integration["tbank"]["receipt_configured"],
            "cdek_configured": integration["cdek"]["configured"],
            "cdek_mode": integration["cdek"]["mode"],
            "cdek_quotes_enabled": integration["cdek"]["quotes_enabled"],
            "cdek_shipment_point_ready": integration["cdek"]["shipment_point_ready"],
            "cdek_writes_enabled": integration["cdek"]["writes_enabled"],
            "external_writes_locked": integration["guard"]["external_writes_locked"],
            "telegram_configured": bool(BOT_TOKEN and OWNER_CHAT_IDS),
            "notification_recipients": len(OWNER_CHAT_IDS),
            "catalog_items": len(catalog),
            "catalog_active_items": sum(item.get("stock") is True for item in catalog.values()),
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


@app.post("/api/admin/session", status_code=204)
def create_admin_session(payload: AdminLogin, request: Request):
    rate_limit(request, "admin-login", 10, 600)
    if not ADMIN_TOKEN or not secrets.compare_digest(payload.token, ADMIN_TOKEN):
        raise HTTPException(401, "Неверный ключ доступа")
    issued_at = int(time.time())
    response = Response(status_code=204)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        admin_session_value(issued_at),
        max_age=ADMIN_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/api/admin/session", status_code=204)
def delete_admin_session():
    response = Response(status_code=204)
    response.delete_cookie(
        ADMIN_SESSION_COOKIE,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/admin/dashboard")
def admin_dashboard(
    days: int = Query(default=30, ge=7, le=90), authorization: str = Header(default="")
):
    require_admin(authorization)
    return dashboard_data(days)


@app.get("/api/admin/integrations/status")
def admin_integrations_status(authorization: str = Header(default="")):
    require_admin(authorization)
    return integrations_status()


@app.post("/api/admin/cdek/test")
def admin_cdek_test(authorization: str = Header(default="")):
    """Read-only production API check: OAuth, origin city and one tariff."""
    require_admin(authorization)
    try:
        cities = cdek_client.cities(
            code=cdek_delivery_settings.from_city_code,
            country_codes="RU",
            size=1,
        )
        sample = cdek_client.calculate_tariff(
            cdek_tariff_payload(
                "cdek_pvz",
                cdek_delivery_settings.from_city_code,
                {
                    "weight": 500,
                    "length": cdek_delivery_settings.package_length,
                    "width": cdek_delivery_settings.package_width,
                    "height": cdek_delivery_settings.package_height,
                },
                cdek_delivery_settings,
            )
        )
    except CdekError as exc:
        raise HTTPException(502, "CDEK API не прошёл проверку") from exc
    quote = normalize_cdek_quote(
        "cdek_pvz",
        cdek_delivery_settings.from_city_code,
        {
            "weight": 500,
            "length": cdek_delivery_settings.package_length,
            "width": cdek_delivery_settings.package_width,
            "height": cdek_delivery_settings.package_height,
        },
        sample,
    )
    return {
        "connected": True,
        "origin": {
            "code": cdek_delivery_settings.from_city_code,
            "city": str(cities[0].get("city", "")) if cities else "",
        },
        "sample_quote": {
            key: quote[key]
            for key in ("tariff_code", "tariff_name", "price", "period_min", "period_max")
        },
        "shipment_point_ready": bool(cdek_delivery_settings.shipment_point),
        "writes_enabled": integrations_status()["cdek"]["writes_enabled"],
    }


@app.get("/api/admin/orders/{order_id}/integration-preview")
def admin_order_integration_preview(
    order_id: str,
    ready_at: str = Query(default="", max_length=19),
    authorization: str = Header(default=""),
):
    """Build provider inputs for inspection; never performs a network request."""
    require_admin(authorization)
    row = order_row(order_id)
    order = admin_order(row)
    integration = integrations_status()

    tbank_blockers = []
    if row["status"] != "pending_payment":
        tbank_blockers.append("Новый платёж можно создавать только для заказа, ожидающего оплаты")
    if not integration["tbank"]["configured"]:
        tbank_blockers.append("Не добавлены реквизиты интернет-эквайринга Т-Банка")
    if not integration["tbank"]["callback_ready"]:
        tbank_blockers.append("Не настроен адрес уведомлений Т-Банка")
    if not integration["tbank"]["receipt_configured"]:
        tbank_blockers.append("Нужно утвердить схему онлайн-чека и ставку НДС")
    payment_preview = {
        "operation": "Init",
        "order_id": row["id"],
        "amount_kopeks": int(row["total"]) * 100,
        "payment_method": row["payment_method"],
        "configured": integration["tbank"]["configured"],
        "ready": not tbank_blockers,
        "blockers": tbank_blockers,
        "network_called": False,
    }

    saby_blockers = []
    if row["status"] == "cancelled":
        saby_blockers.append("Отменённый заказ нельзя передавать в Saby")
    elif row["status"] not in {"paid", "confirmed", "packing", "shipped", "completed"}:
        saby_blockers.append("Заказ ещё не оплачен")
    if not integration["saby"]["credentials_ready"]:
        saby_blockers.append("Не завершена конфигурация точки и прайс-листа Saby")
    if not integration["saby"]["mapping_valid"]:
        saby_blockers.append(integration["saby"]["mapping_error"] or "Не совпадает каталог Saby")
    if not ready_at:
        saby_blockers.append("Не указано плановое время готовности ready_at")
    else:
        try:
            ready_datetime = datetime.strptime(ready_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            saby_blockers.append("Некорректное плановое время готовности")
        else:
            moscow_now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
            if ready_datetime <= moscow_now + timedelta(minutes=5):
                saby_blockers.append("Плановое время готовности должно быть в будущем")
    saby_preview: dict = {
        "configured": integration["saby"]["credentials_ready"],
        "ready": False,
        "blockers": saby_blockers,
        "network_called": False,
    }
    if not saby_blockers:
        try:
            payload = build_saby_order(order, settings=saby_client.settings, ready_at=ready_at)
        except SabySyncError as exc:
            saby_preview["blockers"] = [str(exc)]
        else:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            saby_preview.update({
                "ready": True,
                "payload": payload,
                "payload_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                "network_preflight_required": True,
            })

    cdek_required = row["delivery"] != "pickup"
    cdek_blockers = []
    cdek_quote = (
        json.loads(row["cdek_quote_json"])
        if "cdek_quote_json" in row.keys() and row["cdek_quote_json"]
        else {}
    )
    if cdek_required and not integration["cdek"]["configured"]:
        cdek_blockers.append("Не добавлены ключи CDEK API")
    if cdek_required:
        if row["status"] not in {"paid", "confirmed", "packing", "shipped"}:
            cdek_blockers.append("Заказ должен быть оплачен до регистрации отправления")
        if not cdek_quote.get("tariff_code") or not cdek_quote.get("city_code"):
            cdek_blockers.append("В заказе нет подтверждённого расчёта CDEK")
        if not cdek_delivery_settings.shipment_point:
            cdek_blockers.append("Нужно выбрать пункт CDEK, куда Чайня сдаёт посылки")
    cdek_preview = {
        "required": cdek_required,
        "method": row["delivery"],
        "configured": integration["cdek"]["configured"],
        "ready": not cdek_required,
        "blockers": cdek_blockers,
        "network_called": False,
    }
    if cdek_required and not cdek_blockers:
        try:
            provider_payload = build_cdek_order_payload(
                order, cdek_quote, cdek_delivery_settings
            )
        except ValueError as exc:
            cdek_preview["blockers"] = [str(exc)]
        else:
            encoded = json.dumps(
                provider_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            cdek_preview.update({
                "ready": True,
                "quote": cdek_quote,
                "payload": provider_payload,
                "payload_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            })
    return {
        "order_id": row["id"],
        "test_mode": TEST_MODE,
        "external_writes_locked": True,
        "payment": payment_preview,
        "saby": saby_preview,
        "cdek": cdek_preview,
    }


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
    is_tbank = row["payment_provider"] in {"tbank_demo", "tbank"}
    if is_tbank and payload.status == "paid" and row["status"] == "pending_payment":
        raise HTTPException(409, "Оплату Т-Банка подтверждает только подписанное уведомление банка")
    if is_tbank and payload.status == "cancelled" and row["payment_state"] not in {
        "failed", "cancelled", "refunded",
    }:
        raise HTTPException(409, "Оплаченный заказ Т-Банка отменяется только через возврат")
    if is_tbank and row["payment_state"] == "refunded" and payload.status != "cancelled":
        raise HTTPException(409, "Возвращённый платёж должен оставаться отменённым")
    if row["status"] == "pending_payment" and payload.status not in {"paid", "cancelled"}:
        raise HTTPException(409, "Сначала заказ должен быть оплачен или отменён")
    updated = now_iso()
    with db() as con:
        if payload.status == "paid":
            con.execute(
                """UPDATE orders
                   SET status = ?, updated_at = ?, paid_at = COALESCE(paid_at, ?),
                       payment_state = 'paid', payment_updated_at = ?
                   WHERE id = ?""",
                (payload.status, updated, updated, updated, order_id),
            )
        elif payload.status == "cancelled" and row["status"] == "pending_payment":
            con.execute(
                """UPDATE orders
                   SET status = ?, updated_at = ?, payment_state = 'cancelled',
                       payment_updated_at = ?
                   WHERE id = ?""",
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
        retail_result = saby_client.sales_points("retail")
    except SabyError as exc:
        raise HTTPException(502, str(exc)) from exc

    def rows(result: object, key: str) -> list[dict]:
        value = result.get(key, []) if isinstance(result, dict) else []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    configuration = saby_client.configuration()
    point_id, price_list_id = configuration.get("point_id"), configuration.get("price_list_id")
    points = rows(retail_result, "salesPoints")
    errors: dict[str, str] = {}

    try:
        delivery_points = rows(saby_client.sales_points("delivery"), "salesPoints")
    except SabyError as exc:
        delivery_points, errors["delivery"] = [], str(exc)
    try:
        price_lists = rows(saby_client.price_lists(), "priceLists") if point_id else []
    except SabyError as exc:
        price_lists, errors["price_list"] = [], str(exc)
    try:
        catalog = saby_client.catalog_all(with_balance=True) if point_id and price_list_id else []
    except SabyError as exc:
        catalog, errors["catalog"] = [], str(exc)

    point_found = any(str(point.get("id")) == str(point_id) for point in points)
    delivery_found = any(str(point.get("id")) == str(point_id) for point in delivery_points)
    price_list_found = any(str(item.get("id")) == str(price_list_id) for item in price_lists)
    products = [item for item in catalog if not item.get("isParent")]
    priced_products = [item for item in products if item.get("cost") is not None]
    site_catalog = load_catalog()
    site_id_by_external_id = {
        ref.external_id: site_id
        for site_id, ref in SABY_NOMENCLATURE_BY_SITE_ID.items()
    }

    def product_display_name(item: dict) -> str:
        site_id = site_id_by_external_id.get(str(item.get("externalId") or ""))
        site_item = site_catalog.get(site_id or "", {})
        return str(site_item.get("name") or item.get("name") or item.get("id") or "Позиция")

    expected_external_ids = {
        ref.external_id for ref in SABY_NOMENCLATURE_BY_SITE_ID.values()
    }
    live_external_id_rows = [
        str(item.get("externalId")) for item in products if item.get("externalId")
    ]
    live_external_ids = set(live_external_id_rows)
    missing_external_ids = sorted(expected_external_ids - live_external_ids)
    unexpected_external_ids = sorted(live_external_ids - expected_external_ids)
    catalog_mapping_valid = bool(products) and (
        len(live_external_id_rows) == len(set(live_external_id_rows))
        and not missing_external_ids
    )
    active_products = [
        item for item in products
        if str(item.get("externalId") or "") in expected_external_ids
    ]
    zero_balance_products = [
        item for item in active_products
        if isinstance(item.get("balance"), (int, float))
        and not isinstance(item.get("balance"), bool)
        and item.get("balance") <= 0
    ]
    in_stock_products = [
        item for item in active_products
        if isinstance(item.get("balance"), (int, float))
        and not isinstance(item.get("balance"), bool)
        and item.get("balance") > 0
    ]
    unknown_balance_products = [
        item for item in active_products
        if not isinstance(item.get("balance"), (int, float))
        or isinstance(item.get("balance"), bool)
    ]
    blockers = []
    if not point_id or not point_found:
        blockers.append("Не выбрана розничная точка Saby")
    if not price_list_id or not price_list_found:
        blockers.append("Не найден привязанный прайс-лист Saby")
    if not products:
        blockers.append("В прайс-листе нет товаров")
    if not delivery_found:
        blockers.append("Точка «Чайня» ещё не включена для продукта delivery")
    if products and not catalog_mapping_valid:
        blockers.append("Каталог сайта не совпадает с externalId номенклатуры Saby")
    if zero_balance_products:
        count = len(zero_balance_products)
        label = "активной позиции" if count == 1 else "активных позиций"
        blockers.append(f"В Saby нет остатка у {count} {label} сайта")
    if unknown_balance_products:
        count = len(unknown_balance_products)
        label = "позиции" if count == 1 else "позиций"
        blockers.append(f"Saby не вернул числовой остаток для {count} {label}")

    return {
        "connected": True,
        "points": [
            {key: point.get(key) for key in ("id", "name", "address", "locality", "prices")}
            for point in points[:50]
        ],
        "point_id": point_id,
        "point_found": point_found,
        "price_list_id": price_list_id,
        "price_list_found": price_list_found,
        "catalog_items": len(products),
        "priced_items": len(priced_products),
        "in_stock_items": len(in_stock_products),
        "catalog_mapping_valid": catalog_mapping_valid,
        "missing_external_ids": missing_external_ids,
        "unexpected_external_ids": unexpected_external_ids,
        "zero_balance_items": [
            {"id": item.get("id"), "name": product_display_name(item)}
            for item in zero_balance_products[:20]
        ],
        "unknown_balance_items": [
            {"id": item.get("id"), "name": product_display_name(item)}
            for item in unknown_balance_products[:20]
        ],
        "delivery_configured": delivery_found,
        "ready_for_orders": not blockers,
        "blockers": blockers,
        "warnings": (
            (["В Saby нет остатка: " + ", ".join(product_display_name(item) for item in zero_balance_products)]
             if zero_balance_products else [])
            + (["Saby не вернул остаток: " + ", ".join(product_display_name(item) for item in unknown_balance_products)]
               if unknown_balance_products else [])
            + ([f"В Saby есть скрытые на сайте позиции: {len(unexpected_external_ids)}"]
               if unexpected_external_ids else [])
        ),
        "errors": errors,
    }


@app.get("/api/admin/saby/catalog-preview")
def admin_saby_catalog_preview(authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        items = saby_client.catalog_all()
    except SabyError as exc:
        raise HTTPException(502, str(exc)) from exc
    products = [item for item in items if not item.get("isParent")]
    return {
        "items": [
            {key: item.get(key) for key in ("id", "externalId", "name", "cost", "balance", "published", "unit")}
            for item in products[:50]
        ],
        "total": len(products),
    }


@app.get("/api/delivery/cities")
def delivery_cities(
    request: Request,
    q: str = Query(min_length=2, max_length=80),
):
    rate_limit(request, "cdek-cities", 90, 600)
    query = q.strip()
    try:
        cities = cached_cdek(
            f"cities:{query.casefold()}",
            900,
            lambda: cdek_client.cities(
                city=query, country_codes="RU", size=20, page=0
            ),
        )
    except CdekError as exc:
        raise HTTPException(502, "Не удалось загрузить города CDEK") from exc
    if not cities:
        resolved: list[dict] = []
        seen: set[int] = set()
        for candidate in fuzzy_cdek_cities(query, limit=8):
            candidate_name = str(candidate.get("city", "")).strip()
            if not candidate_name:
                continue
            try:
                rows = cached_cdek(
                    f"cities:{candidate_name.casefold()}",
                    900,
                    lambda name=candidate_name: cdek_client.cities(
                        city=name, country_codes="RU", size=20, page=0
                    ),
                )
            except CdekError:
                continue
            for city in rows:
                code = int(city.get("code") or 0)
                if (
                    code
                    and code not in seen
                    and normalized_search_text(city.get("city"))
                    == normalized_search_text(candidate_name)
                ):
                    seen.add(code)
                    resolved.append(city)
        cities = resolved
    return {
        "cities": [
            {
                "code": int(city["code"]),
                "city": str(city.get("city", "")),
                "region": str(city.get("region", "")),
                "country": str(city.get("country", "")),
            }
            for city in cities[:20]
            if city.get("code") and city.get("city")
        ]
    }


@app.get("/api/delivery/points")
def delivery_points(
    request: Request,
    city_code: int = Query(ge=1, le=9_999_999),
    q: str = Query(default="", max_length=100),
):
    rate_limit(request, "cdek-points", 90, 600)
    try:
        points = cached_cdek(
            f"points:{city_code}",
            900,
            lambda: cdek_client.delivery_points(
                city_code=city_code, is_handout="true"
            ),
        )
    except CdekError as exc:
        raise HTTPException(502, "Не удалось загрузить пункты CDEK") from exc
    needle = q.strip().casefold()
    result = []
    for point in points:
        location = point.get("location") or {}
        if (
            point.get("status") != "ACTIVE"
            or point.get("is_handout") is not True
            or int(location.get("city_code", 0)) != city_code
        ):
            continue
        searchable = " ".join(str(value or "") for value in (
            point.get("code"), point.get("name"), location.get("address"),
            point.get("nearest_metro_station"), point.get("nearest_station"),
        )).casefold()
        if needle and needle not in searchable:
            continue
        result.append({
            "code": str(point.get("code", "")),
            "name": str(point.get("name", "")),
            "address": str(location.get("address", "")),
            "work_time": str(point.get("work_time", "")),
            "metro": str(point.get("nearest_metro_station", "")),
            "type": str(point.get("type", "PVZ")),
        })
        if len(result) >= 30:
            break
    return {"points": result}


@app.post("/api/delivery/quote")
def delivery_quote(payload: DeliveryQuoteRequest, request: Request):
    rate_limit(request, "cdek-quote", 60, 600)
    temporary_order = CreateOrder(
        items=payload.items,
        delivery=payload.method,
        payment_method="bank_card",
        phone="+79990000000",
        city="CDEK",
        city_code=payload.city_code,
        pvz_code="TEMP" if payload.method == "cdek_pvz" else "",
        address="TEMP" if payload.method == "cdek_courier" else "",
        privacy_accepted=True,
    )
    lines, _subtotal = price_order(temporary_order)
    return cdek_quote_for_lines(payload.method, payload.city_code, lines)


@app.post("/api/orders", status_code=201)
def create_order(
    payload: CreateOrder,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    rate_limit(request, "create-order", 12, 600)
    tbank_enabled = tbank_checkout_ready()
    tbank_mode, tbank_mode_valid = rollout_mode("TBANK_CHECKOUT_MODE", allow_demo=True)
    mock_enabled = TEST_MODE and tbank_mode_valid and tbank_mode == "off"
    if not tbank_enabled and not mock_enabled:
        raise HTTPException(503, "Онлайн-оплата пока не подключена")
    validate_delivery(payload)
    key_hash = idempotency_hash(idempotency_key) if idempotency_key else None
    request_fingerprint = order_request_hash(payload)
    if key_hash:
        with db() as con:
            existing = con.execute(
                "SELECT * FROM orders WHERE idempotency_key_hash = ?", (key_hash,)
            ).fetchone()
        if existing:
            if existing["request_hash"] != request_fingerprint:
                raise HTTPException(409, "Idempotency-Key уже использован для другого заказа")
            return checkout_response(existing, request, reused=True)

    lines, subtotal = price_order(payload)
    cdek_quote: dict = {}
    if payload.delivery == "pickup":
        delivery_price = 0
    else:
        cdek_quote = cdek_quote_for_lines(
            payload.delivery, int(payload.city_code), lines
        )
        if payload.delivery == "cdek_pvz":
            point = cdek_point(int(payload.city_code), payload.pvz_code)
            location = point.get("location") or {}
            cdek_quote["point"] = {
                "code": str(point.get("code", "")),
                "name": str(point.get("name", "")),
                "address": str(location.get("address", "")),
                "work_time": str(point.get("work_time", "")),
            }
        delivery_price = int(cdek_quote["price"])
    order_id = uuid.uuid4().hex[:12].upper()
    created = now_iso()
    customer = payload.model_dump(
        exclude={"items", "payment_method", "delivery", "language", "analytics_session"}
    )
    analytics_session_hash = hash_session(payload.analytics_session) if payload.analytics_session else None
    payment_token = uuid.uuid4().hex
    payment_provider = "tbank_demo" if tbank_enabled and TEST_MODE else "tbank" if tbank_enabled else "test"
    payment_state = "initializing" if tbank_enabled else "awaiting"
    reused_row = None
    with db() as con:
        if key_hash:
            con.execute("BEGIN IMMEDIATE")
            reused_row = con.execute(
                "SELECT * FROM orders WHERE idempotency_key_hash = ?", (key_hash,)
            ).fetchone()
            if reused_row and reused_row["request_hash"] != request_fingerprint:
                raise HTTPException(409, "Idempotency-Key уже использован для другого заказа")
        if not reused_row:
            con.execute(
                """INSERT INTO orders
                   (id, status, created_at, updated_at, subtotal, delivery_price, total,
                    payment_method, delivery, customer_json, items_json, provider_payment_id, payment_token,
                    payment_provider, payment_state, payment_updated_at,
                    cdek_quote_json, idempotency_key_hash, request_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, "pending_payment", created, created, subtotal, delivery_price,
                 subtotal + delivery_price, payload.payment_method, payload.delivery,
                 json.dumps(customer, ensure_ascii=False), json.dumps(lines, ensure_ascii=False), None,
                 payment_token, payment_provider, payment_state, created,
                 json.dumps(cdek_quote, ensure_ascii=False), key_hash, request_fingerprint),
            )
        if not reused_row and analytics_session_hash:
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
    if reused_row:
        return checkout_response(reused_row, request, reused=True)
    created_row = order_row(order_id)
    if tbank_enabled:
        created_row = initialize_tbank_payment(created_row, payload.language)
    return checkout_response(created_row, request)


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


async def tbank_notification_payload(request: Request) -> dict:
    """Read the small T-Bank JSON/form body without trusting its content type."""
    raw = await request.body()
    if not raw or len(raw) > 32_768:
        raise HTTPException(400, "Некорректное уведомление Т-Банка")
    try:
        if raw.lstrip().startswith(b"{"):
            payload = json.loads(raw.decode("utf-8"))
        else:
            payload = dict(urllib.parse.parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPException(400, "Некорректное уведомление Т-Банка") from None
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(400, "Некорректное уведомление Т-Банка")
    return payload


@app.post("/api/payments/tbank/notification", response_class=PlainTextResponse)
async def tbank_notification(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Verify and apply an idempotent payment status notification."""
    payload = await tbank_notification_payload(request)
    if not verify_notification_token(payload, tbank_client.settings):
        raise HTTPException(403, "Недействительная подпись Т-Банка")

    order_id = payload.get("OrderId")
    payment_id = payload.get("PaymentId")
    status = payload.get("Status")
    raw_amount = payload.get("Amount")
    if not all(isinstance(value, (str, int)) and not isinstance(value, bool) for value in (
        order_id, payment_id, raw_amount,
    )) or not isinstance(status, str):
        raise HTTPException(400, "Некорректные поля уведомления Т-Банка")
    order_id, payment_id = str(order_id), str(payment_id)
    try:
        amount = int(str(raw_amount))
    except ValueError:
        raise HTTPException(400, "Некорректная сумма уведомления Т-Банка") from None

    should_notify = False
    with db() as con:
        row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Заказ уведомления Т-Банка не найден")
        if row["payment_provider"] not in {"tbank_demo", "tbank"}:
            raise HTTPException(409, "Заказ не относится к Т-Банку")
        if not row["provider_payment_id"] or not secrets.compare_digest(
            str(row["provider_payment_id"]), payment_id
        ):
            raise HTTPException(409, "Не совпадает идентификатор платежа Т-Банка")
        if amount != int(row["total"]) * 100:
            raise HTTPException(409, "Не совпадает сумма платежа Т-Банка")

        updated = now_iso()
        success = payload.get("Success") is True or payload.get("Success") == "true"
        provider_status = status[:80]
        if success and status == "CONFIRMED":
            # A delayed/replayed confirmation must never undo a refund that is
            # already running, ambiguous, partial or complete.
            if row["payment_state"] not in {
                "refunding", "refund_ambiguous", "partially_refunded", "refunded",
            }:
                should_notify = row["paid_at"] is None
                next_order_status = "paid" if row["status"] == "pending_payment" else row["status"]
                con.execute(
                    """UPDATE orders
                       SET status = ?, paid_at = COALESCE(paid_at, ?), payment_state = 'paid',
                           payment_provider_status = ?, payment_last_error = '',
                           payment_updated_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (next_order_status, updated, provider_status, updated, updated, order_id),
                )
        elif success and status == "REFUNDED":
            con.execute(
                """UPDATE orders
                   SET status = 'cancelled', payment_state = 'refunded',
                       payment_provider_status = ?, payment_last_error = '',
                       payment_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (provider_status, updated, updated, order_id),
            )
        elif success and status == "PARTIAL_REFUNDED":
            con.execute(
                """UPDATE orders
                   SET payment_state = 'partially_refunded', payment_provider_status = ?,
                       payment_updated_at = ?, updated_at = ? WHERE id = ?""",
                (provider_status, updated, updated, order_id),
            )
        elif status in {"REJECTED", "CANCELED", "REVERSED", "DEADLINE_EXPIRED"}:
            # A late failure must never downgrade a payment already confirmed.
            if row["paid_at"] is None:
                con.execute(
                    """UPDATE orders
                       SET payment_state = 'failed', payment_provider_status = ?,
                           payment_updated_at = ?, updated_at = ? WHERE id = ?""",
                    (provider_status, updated, updated, order_id),
                )
        else:
            con.execute(
                """UPDATE orders SET payment_provider_status = ?, payment_updated_at = ?,
                       updated_at = ? WHERE id = ?""",
                (provider_status, updated, updated, order_id),
            )

    if should_notify:
        background_tasks.add_task(sync_paid_order_to_saby, order_id)
        background_tasks.add_task(notify_owners, order_row(order_id))
    return "OK"


@app.post("/api/admin/orders/{order_id}/tbank/refund")
def admin_tbank_refund(order_id: str, authorization: str = Header(default="")):
    """Perform one full refund; designed for the required DEMO test and later live use."""
    require_admin(authorization)
    row = order_row(order_id)
    if row["payment_provider"] not in {"tbank_demo", "tbank"} or not row["provider_payment_id"]:
        raise HTTPException(409, "У заказа нет платежа Т-Банка")
    if row["payment_state"] == "refunded":
        return admin_order(row)
    if row["payment_state"] != "paid":
        raise HTTPException(409, "Вернуть можно только подтверждённый платёж")

    mode, _valid = rollout_mode("TBANK_CHECKOUT_MODE", allow_demo=True)
    started = now_iso()
    with db() as con:
        claimed = con.execute(
            """UPDATE orders SET payment_state = 'refunding', payment_updated_at = ?,
                   updated_at = ? WHERE id = ? AND payment_state = 'paid'""",
            (started, started, order_id),
        )
        if claimed.rowcount != 1:
            current = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if current and current["payment_state"] == "refunded":
                return admin_order(current)
            raise HTTPException(409, "Возврат уже выполняется или состояние платежа изменилось")
    try:
        result = integration_writer.refund_tbank_payment(
            tbank_client,
            row["provider_payment_id"],
            mode=mode,
        )
        if result.get("Success") is not True:
            raise TBankError("Т-Банк вернул некорректный результат возврата")
    except TBankError as exc:
        failed = now_iso()
        with db() as con:
            con.execute(
                """UPDATE orders SET payment_state = 'refund_ambiguous',
                       payment_last_error = ?, payment_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (str(exc), failed, failed, order_id),
            )
        raise HTTPException(502, "Не удалось однозначно подтвердить возврат в Т-Банке") from exc

    finished = now_iso()
    provider_status = str(result.get("Status", "REFUNDING"))[:80]
    refunded = provider_status == "REFUNDED"
    with db() as con:
        con.execute(
            """UPDATE orders
               SET status = CASE WHEN ? THEN 'cancelled' ELSE status END,
                   payment_state = ?, payment_provider_status = ?, payment_last_error = '',
                   payment_updated_at = ?, updated_at = ? WHERE id = ?""",
            (
                refunded,
                "refunded" if refunded else "refunding",
                provider_status,
                finished,
                finished,
                order_id,
            ),
        )
    return admin_order(order_row(order_id))


@app.post("/api/admin/orders/{order_id}/cdek/create")
def admin_cdek_create(order_id: str, authorization: str = Header(default="")):
    """Register one prepaid shipment after an explicit owner action."""
    require_admin(authorization)
    row = order_row(order_id)
    if row["delivery"] == "pickup":
        raise HTTPException(409, "Для самовывоза отправление CDEK не требуется")
    if row["status"] not in {"paid", "confirmed", "packing"}:
        raise HTTPException(409, "Передать в CDEK можно только оплаченный заказ")
    if row["cdek_state"] == "created":
        return admin_order(row)
    if not cdek_client.settings.configured:
        raise HTTPException(503, "Ключи CDEK не настроены")
    quote = json.loads(row["cdek_quote_json"] or "{}")
    try:
        payload = build_cdek_order_payload(
            admin_order(row), quote, cdek_delivery_settings
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    started = now_iso()
    with db() as con:
        claimed = con.execute(
            """UPDATE orders SET cdek_state = 'creating', cdek_last_error = '',
                   cdek_updated_at = ?, updated_at = ?
               WHERE id = ? AND cdek_state IN ('not_requested','failed','ambiguous')""",
            (started, started, order_id),
        )
        if claimed.rowcount != 1:
            current = con.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if current and current["cdek_state"] == "created":
                return admin_order(current)
            raise HTTPException(409, "Отправление CDEK уже создаётся")

    mode, _valid = rollout_mode("CDEK_INTEGRATION_MODE")
    try:
        result = integration_writer.create_cdek_order(
            cdek_client,
            payload,
            mode=mode,
            manual_approved=True,
        )
        entity = result.get("entity") if isinstance(result, dict) else None
        requests = result.get("requests") if isinstance(result, dict) else None
        request_state = (
            str(requests[0].get("state", ""))
            if isinstance(requests, list) and requests and isinstance(requests[0], dict)
            else ""
        )
        errors = (
            requests[0].get("errors", [])
            if isinstance(requests, list) and requests and isinstance(requests[0], dict)
            else []
        )
        order_uuid = str(entity.get("uuid", "")) if isinstance(entity, dict) else ""
        if not order_uuid or errors or request_state not in {"ACCEPTED", "SUCCESSFUL"}:
            raise CdekError("CDEK не подтвердил создание отправления")
    except ExternalWriteBlocked as exc:
        with db() as con:
            con.execute(
                """UPDATE orders SET cdek_state = 'not_requested',
                       cdek_last_error = ?, cdek_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (str(exc), now_iso(), now_iso(), order_id),
            )
        raise HTTPException(409, str(exc)) from exc
    except CdekError as exc:
        failed = now_iso()
        with db() as con:
            con.execute(
                """UPDATE orders SET cdek_state = 'ambiguous',
                       cdek_last_error = ?, cdek_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (str(exc), failed, failed, order_id),
            )
        raise HTTPException(
            502, "Не удалось однозначно подтвердить создание отправления CDEK"
        ) from exc

    finished = now_iso()
    with db() as con:
        con.execute(
            """UPDATE orders SET cdek_state = 'created', cdek_order_uuid = ?,
                   cdek_number = NULL, cdek_last_error = '', cdek_updated_at = ?,
                   updated_at = ? WHERE id = ?""",
            (order_uuid, finished, finished, order_id),
        )
    return admin_order(order_row(order_id))


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
        if row["payment_provider"] != "test":
            raise HTTPException(404, "Тестовая оплата недоступна")
        if row["status"] == "pending_payment":
            paid_at = now_iso()
            con.execute(
                """UPDATE orders
                   SET status = 'paid', updated_at = ?, paid_at = ?, provider_payment_id = ?,
                       payment_state = 'paid', payment_attempts = payment_attempts + 1,
                       payment_last_error = '', payment_updated_at = ?
                   WHERE id = ?""",
                (paid_at, paid_at, f"mock_{uuid.uuid4().hex}", paid_at, order_id),
            )
            should_notify = True
    if should_notify:
        with db() as con:
            paid_row = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        background_tasks.add_task(notify_owners, paid_row)
    return public_order(order_row(order_id))


def test_payment_page(order_id: str, token: str):
    if not TEST_MODE:
        raise HTTPException(404, "Тестовая оплата отключена")
    row = order_row(order_id)
    require_order_token(row, token)
    if row["payment_provider"] != "test":
        raise HTTPException(404, "Тестовая оплата недоступна")
    return FileResponse(ROOT / "backend" / "test-payment.html")


@app.get("/payment/success")
@app.get("/payment/fail")
def tbank_result_page(order_id: str = "", token: str = ""):
    # The browser redirect is never proof of payment. The page only receives a
    # read token and polls the server state written by the signed callback.
    # Terminal-level fallback URLs do not carry per-order query parameters.
    # They must still render a safe generic page instead of FastAPI's 422.
    if order_id or token:
        if not order_id or not token:
            raise HTTPException(400, "Неполные данные платежа")
        row = order_row(order_id)
        require_order_token(row, token)
    return FileResponse(
        ROOT / "backend" / "payment-result.html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def owner_page(request: Request):
    page = "admin.html" if valid_admin_session(
        request.cookies.get(ADMIN_SESSION_COOKIE, "")
    ) else "admin-login.html"
    return FileResponse(
        ROOT / "backend" / page,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.get("/admin/orders")
def admin_page(request: Request):
    return owner_page(request)


@app.get("/manage/")
@app.get("/manage")
def management_page(request: Request):
    """Owner dashboard protected by a server-validated HttpOnly session."""
    return owner_page(request)


if TEST_MODE:
    app.add_api_route(
        "/api/orders/{order_id}/test-pay",
        test_pay,
        methods=["POST"],
    )
    app.add_api_route(
        "/test-payment/{order_id}",
        test_payment_page,
        methods=["GET"],
    )
else:
    def production_test_payment_not_found():
        raise HTTPException(404, "Не найдено")

    app.add_api_route(
        "/api/orders/{order_id}/test-pay",
        production_test_payment_not_found,
        methods=["POST"],
        include_in_schema=False,
    )


# В локальной разработке backend одновременно раздаёт собранный сайт.
if (ROOT / "dist").exists():
    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="site")

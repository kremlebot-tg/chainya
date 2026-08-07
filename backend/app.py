#!/usr/bin/env python3
"""Backend интернет-магазина и операционной панели «Чайня».

Сервер проверяет каталог и суммы, хранит заказы в SQLite и безопасно связывает
оплату Т-Банка, доставку CDEK, учёт Saby и уведомления Telegram. Тестовый режим
остаётся fail-closed и включается отдельно от боевых интеграций.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import date as datetime_date
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import (
    BackgroundTasks,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator, model_validator

from .catalog_store import (
    MEDIA_FILE_RE,
    CatalogConflict,
    CatalogError,
    CatalogStore,
)
from .catalog_store import (
    image_url as catalog_image_url,
)
from .cdek import CdekClient, CdekError
from .cdek_delivery import (
    CdekDeliverySettings,
)
from .cdek_delivery import (
    build_order_payload as build_cdek_order_payload,
)
from .cdek_delivery import (
    normalized_quote as normalize_cdek_quote,
)
from .cdek_delivery import (
    package_spec as cdek_package_spec,
)
from .cdek_delivery import (
    tariff_payload as cdek_tariff_payload,
)
from .integration_guard import ExternalWriteBlocked
from .integration_writes import IntegrationWriter
from .saby import SabyClient, SabyError
from .saby_shadow import SabyShadowSettings, compare_catalogs
from .saby_sync import (
    SABY_NOMENCLATURE_BY_SITE_ID,
    SabySyncError,
    build_saby_order,
    sync_mode_from_env,
    validate_mapping_file,
)
from .saby_sync import (
    write_allowed as saby_write_allowed,
)
from .tbank import (
    TBankClient,
    TBankError,
    validate_payment_url,
    verify_notification_token,
)
from .tbank_receipt import TBankReceiptError, TBankReceiptSettings, build_receipt

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
DATA_DIR = Path(os.getenv("CHAINYA_DATA_DIR", ROOT / "backend" / "data"))
DB_PATH = DATA_DIR / "orders.sqlite3"
CATALOG_PATH = Path(os.getenv("CHAINYA_CATALOG_PATH", DATA_DIR / "catalog.json"))
CATALOG_SEED_PATH = Path(
    os.getenv("CHAINYA_CATALOG_SEED_PATH", ROOT / "backend" / "catalog.seed.json")
)
CATALOG_MEDIA_DIR = Path(
    os.getenv("CHAINYA_CATALOG_MEDIA_DIR", DATA_DIR / "catalog-media")
)
CDEK_CITIES_PATH = Path(
    os.getenv("CDEK_CITIES_PATH", DATA_DIR / "cdek-cities-ru.json")
)
RELEASE_COMMIT_PATH = ROOT / "RELEASE_COMMIT"


def release_version() -> str:
    """Return a non-secret deploy identifier for production diagnostics."""
    try:
        commit = RELEASE_COMMIT_PATH.read_text(encoding="ascii").strip().lower()
    except OSError:
        return "development"
    return commit[:12] if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) else "unknown"


def test_mode_from_value(value: str | None) -> bool:
    """Fail closed: only the exact value `0` can disable the safety mode."""
    return (value if value is not None else "1").strip() != "0"


TEST_MODE = test_mode_from_value(os.getenv("CHAINYA_TEST_MODE"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOOKING_BOT_SECRET = os.getenv("BOOKING_BOT_SECRET", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
ADMIN_SESSION_COOKIE = "chainya_admin_session"
ADMIN_SESSION_SECONDS = 12 * 60 * 60
CUSTOMER_SESSION_COOKIE = "chainya_customer_session"
CUSTOMER_SESSION_SECONDS = 30 * 24 * 60 * 60
CUSTOMER_PASSWORD_ITERATIONS = 310_000
TBANK_INIT_LEASE_SECONDS = 45
PAID_EFFECT_LEASE_SECONDS = 90
PAID_EFFECT_RETRY_SECONDS = 30
OWNER_CHAT_IDS = [
    value for value in re.split(r"[\s,]+", os.getenv("OWNER_CHAT_ID", "").strip()) if value
]

DELIVERY_PRICES = {"pickup": 0, "cdek_pvz": 490, "cdek_courier": 790}
ANALYTICS_RETENTION_DAYS = 360
SABY_SHADOW_RETENTION_RUNS = 50
SABY_SHADOW_MANUAL_LIMIT = 3
SABY_SHADOW_MANUAL_WINDOW_SECONDS = 5 * 60
DELIVERY_LABELS = {
    "pickup": "Самовывоз · Острякова, 3",
    "cdek_pvz": "СДЭК · пункт выдачи",
    "cdek_courier": "СДЭК · курьер",
}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
BOOKING_SESSION_MINUTES = 120
BOOKING_SLOT_STEP_MINUTES = 30
BOOKING_OPEN_MINUTES = 12 * 60
BOOKING_LAST_START_MINUTES = 20 * 60
BOOKING_CLOSE_MINUTES = 22 * 60
BOOKING_BLOCKING_STATUSES = ("new", "confirmed")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_catalog_store().ensure()
    init_db()
    stop_paid_effect_worker = threading.Event()
    paid_effect_thread = threading.Thread(
        target=paid_effect_worker,
        args=(stop_paid_effect_worker,),
        name="chainya-paid-effects",
        daemon=True,
    )
    paid_effect_thread.start()
    shadow_settings = SabyShadowSettings.from_env()
    stop_saby_shadow_worker = threading.Event()
    saby_shadow_thread = None
    if shadow_settings.enabled:
        saby_shadow_thread = threading.Thread(
            target=saby_shadow_worker,
            args=(stop_saby_shadow_worker, shadow_settings.interval_seconds),
            name="chainya-saby-shadow",
            daemon=True,
        )
        saby_shadow_thread.start()
    try:
        yield
    finally:
        stop_saby_shadow_worker.set()
        if saby_shadow_thread:
            saby_shadow_thread.join(timeout=1)
        stop_paid_effect_worker.set()
        paid_effect_thread.join(timeout=1)


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
_saby_shadow_lock = threading.Lock()
_cdek_cache: dict[str, tuple[float, object]] = {}
_cdek_cache_lock = threading.Lock()
_cdek_cities_index: tuple[float, list[dict]] | None = None
_cdek_cities_index_lock = threading.Lock()
_catalog_stores: dict[tuple[Path, Path, Path], CatalogStore] = {}
_catalog_stores_lock = threading.Lock()


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
        if value != "pc" and value not in (10, 25, 50, 100):
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


def moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ)


class CreateBooking(BaseModel):
    format: Literal["master", "self"]
    date: datetime_date
    time: datetime_time
    guests: int = Field(ge=1, le=7)
    name: str = Field(default="", max_length=120)
    phone: str = Field(min_length=3, max_length=80)
    note: str = Field(default="", max_length=1000)
    privacy_accepted: Literal[True]
    source: Literal["website", "telegram"] = "website"

    @field_validator("name", "note")
    @classmethod
    def strip_booking_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("phone")
    @classmethod
    def valid_booking_phone(cls, value: str) -> str:
        value = value.strip()
        if len(re.sub(r"\D", "", value)) >= 10:
            return value
        if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
            return value
        if re.fullmatch(r"Telegram ID [1-9][0-9]{3,19}", value):
            return value
        raise ValueError("укажите полный номер телефона или контакт Telegram")

    @field_validator("time")
    @classmethod
    def valid_booking_slot(cls, value: datetime_time) -> datetime_time:
        if value.second or value.microsecond or value.minute not in (0, 30):
            raise ValueError("время брони должно быть указано с шагом 30 минут")
        if not datetime_time(12, 0) <= value <= datetime_time(20, 0):
            raise ValueError("время брони должно быть с 12:00 до 20:00")
        return value

    @model_validator(mode="after")
    def booking_is_in_future(self):
        current = moscow_now()
        if self.date > current.date() + timedelta(days=14):
            raise ValueError(
                "бронь доступна не более чем на 14 дней вперёд по московскому времени"
            )
        scheduled = datetime.combine(self.date, self.time, tzinfo=MOSCOW_TZ)
        if scheduled <= current:
            raise ValueError("дата и время брони уже прошли по московскому времени")
        if self.source == "website" and len(re.sub(r"\D", "", self.phone)) < 10:
            raise ValueError("для брони с сайта укажите полный номер телефона")
        return self


class UpdateOrderStatus(BaseModel):
    status: Literal["paid", "confirmed", "packing", "shipped", "completed", "cancelled"]


class UpdateLeadStatus(BaseModel):
    status: Literal["new", "contacted", "closed"]


class UpdateBookingStatus(BaseModel):
    status: Literal["new", "confirmed", "completed", "cancelled"]


class SabyShadowAcknowledgement(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledged: bool = True


class CreateBookingBlock(BaseModel):
    date: datetime_date
    start: datetime_time
    end: datetime_time
    note: str = Field(default="", max_length=240)

    @field_validator("start", "end")
    @classmethod
    def valid_half_hour(cls, value: datetime_time) -> datetime_time:
        if value.second or value.microsecond or value.minute not in (0, 30):
            raise ValueError("время должно быть указано с шагом 30 минут")
        return value

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def valid_interval(self):
        if not datetime_time(12, 0) <= self.start < self.end <= datetime_time(22, 0):
            raise ValueError("закрываемое время должно быть внутри часов 12:00–22:00")
        if self.date < moscow_now().date():
            raise ValueError("нельзя закрыть прошедшую дату")
        return self


class AdminLogin(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class CustomerRegister(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=40)
    password: str = Field(min_length=10, max_length=128)
    privacy_accepted: Literal[True]

    @field_validator("name")
    @classmethod
    def strip_customer_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("укажите имя")
        return value

    @field_validator("phone")
    @classmethod
    def valid_customer_phone(cls, value: str) -> str:
        return normalize_customer_phone(value)


class CustomerLogin(BaseModel):
    phone: str = Field(min_length=7, max_length=40)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("phone")
    @classmethod
    def valid_customer_phone(cls, value: str) -> str:
        return normalize_customer_phone(value)


class CustomerOrderClaim(BaseModel):
    order_id: str = Field(min_length=8, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    token: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class CustomerProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def strip_customer_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("укажите имя")
        return value


class CustomerPasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class CustomerAccountDelete(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class CatalogMutation(BaseModel):
    revision: int = Field(ge=1)
    item: dict


class CatalogReorder(BaseModel):
    revision: int = Field(ge=1)
    ids: list[str] = Field(min_length=1, max_length=500)


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
    campaign: str = Field(
        default="organic",
        max_length=160,
        pattern=r"^[A-Za-z0-9._:/ -]+$",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_customer_phone(value: str) -> str:
    """Return one stable E.164-like phone form used only for customer identity."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif not value.strip().startswith("+"):
        raise ValueError("для иностранного номера укажите + и код страны")
    if not 10 <= len(digits) <= 15:
        raise ValueError("укажите номер телефона полностью")
    return "+" + digits


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


def get_catalog_store() -> CatalogStore:
    """Return a stable store while still allowing tests to replace env paths."""
    key = (CATALOG_PATH, CATALOG_SEED_PATH, CATALOG_MEDIA_DIR)
    with _catalog_stores_lock:
        if key not in _catalog_stores:
            _catalog_stores[key] = CatalogStore(*key)
        return _catalog_stores[key]


def load_catalog() -> dict[str, dict]:
    try:
        document = get_catalog_store().get()
    except CatalogError as exc:
        raise RuntimeError(f"Не удалось загрузить каталог {CATALOG_PATH}: {exc}") from exc
    return {item["id"]: item for item in document["teas"]}


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
                request_hash TEXT,
                paid_effects_enqueued INTEGER NOT NULL DEFAULT 1,
                customer_account_id TEXT
            )
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        paid_effects_column_missing = "paid_effects_enqueued" not in columns
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
            "customer_account_id": "TEXT",
            # Existing paid orders predate the durable outbox and must not
            # suddenly resend historical notifications after this migration.
            # New orders explicitly store 0 in create_order().
            "paid_effects_enqueued": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, declaration in order_migrations.items():
            if column not in columns:
                con.execute(f"ALTER TABLE orders ADD COLUMN {column} {declaration}")
        if paid_effects_column_missing:
            # Pending legacy orders have not produced paid side effects yet;
            # they are safe to enqueue when a future CONFIRMED callback arrives.
            con.execute(
                """UPDATE orders SET paid_effects_enqueued = 0
                   WHERE paid_at IS NULL AND status = 'pending_payment'"""
            )
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
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_customer_created "
            "ON orders(customer_account_id, created_at)"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS customer_accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_accounts_phone "
            "ON customer_accounts(phone)"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS customer_sessions (
                token_hash TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES customer_accounts(id)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_customer_sessions_account "
            "ON customer_sessions(account_id, expires_at)"
        )
        con.execute("DELETE FROM customer_sessions WHERE expires_at <= ?", (now_iso(),))
        con.execute("""
            CREATE TABLE IF NOT EXISTS paid_order_effects (
                order_id TEXT NOT NULL,
                effect TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (order_id, effect),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_paid_order_effects_state "
            "ON paid_order_effects(state, updated_at)"
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
                source TEXT NOT NULL DEFAULT 'website',
                updated_at TEXT NOT NULL DEFAULT '',
                idempotency_key_hash TEXT,
                request_hash TEXT
            )
        """)
        lead_columns = {row["name"] for row in con.execute("PRAGMA table_info(business_leads)")}
        lead_migrations = {
            "status": "TEXT NOT NULL DEFAULT 'new'",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "idempotency_key_hash": "TEXT",
            "request_hash": "TEXT",
        }
        for column, declaration in lead_migrations.items():
            if column not in lead_columns:
                con.execute(
                    f"ALTER TABLE business_leads ADD COLUMN {column} {declaration}"
                )
        con.execute("CREATE INDEX IF NOT EXISTS idx_business_leads_created ON business_leads(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_business_leads_status ON business_leads(status)")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_business_leads_idempotency "
            "ON business_leads(idempotency_key_hash) "
            "WHERE idempotency_key_hash IS NOT NULL"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                booking_time TEXT NOT NULL,
                format TEXT NOT NULL,
                guests INTEGER NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                note TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                updated_at TEXT NOT NULL DEFAULT '',
                idempotency_key_hash TEXT,
                request_hash TEXT,
                customer_account_id TEXT
            )
        """)
        booking_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(bookings)")
        }
        booking_migrations = {
            "source": "TEXT NOT NULL DEFAULT 'website'",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "idempotency_key_hash": "TEXT",
            "request_hash": "TEXT",
            "customer_account_id": "TEXT",
        }
        for column, declaration in booking_migrations.items():
            if column not in booking_columns:
                con.execute(
                    f"ALTER TABLE bookings ADD COLUMN {column} {declaration}"
                )
        con.execute("CREATE INDEX IF NOT EXISTS idx_bookings_created ON bookings(created_at)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_slot "
            "ON bookings(booking_date, booking_time)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_idempotency "
            "ON bookings(idempotency_key_hash) "
            "WHERE idempotency_key_hash IS NOT NULL"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_customer_slot "
            "ON bookings(customer_account_id, booking_date, booking_time)"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS booking_blocks (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_booking_blocks_slot "
            "ON booking_blocks(booking_date, start_time, end_time)"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                event TEXT NOT NULL,
                section TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru',
                device TEXT NOT NULL DEFAULT 'desktop',
                referrer TEXT NOT NULL DEFAULT 'direct',
                campaign TEXT NOT NULL DEFAULT 'organic'
            )
        """)
        analytics_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(analytics_events)")
        }
        if "campaign" not in analytics_columns:
            con.execute(
                "ALTER TABLE analytics_events "
                "ADD COLUMN campaign TEXT NOT NULL DEFAULT 'organic'"
            )
        con.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics_events(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event_created ON analytics_events(event, created_at)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_session_created "
            "ON analytics_events(session_hash, created_at)"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS catalog_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                item_id TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalog_audit_created "
            "ON catalog_audit(created_at)"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS saby_shadow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                report_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_saby_shadow_runs_started "
            "ON saby_shadow_runs(started_at DESC)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_saby_shadow_single_running "
            "ON saby_shadow_runs(status) WHERE status = 'running'"
        )
        con.execute("""
            CREATE TABLE IF NOT EXISTS saby_shadow_acknowledgements (
                fingerprint TEXT PRIMARY KEY,
                acknowledged_at TEXT NOT NULL
            )
        """)
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
        if not tea or tea.get("stock") is False or tea.get("published") is False:
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


def booking_request_hash(payload: CreateBooking) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def booking_time_minutes(value: str | datetime_time) -> int:
    """Convert a stored/API booking time to minutes after midnight."""
    if isinstance(value, datetime_time):
        return value.hour * 60 + value.minute
    parsed = datetime_time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def booking_time_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def booking_intervals(con: sqlite3.Connection, day: datetime_date) -> list[tuple[int, int]]:
    intervals = [
        (booking_time_minutes(row["booking_time"]),
         booking_time_minutes(row["booking_time"]) + BOOKING_SESSION_MINUTES)
        for row in con.execute(
            """SELECT booking_time FROM bookings
               WHERE booking_date = ? AND status IN (?, ?)""",
            (day.isoformat(), *BOOKING_BLOCKING_STATUSES),
        ).fetchall()
    ]
    intervals.extend(
        (booking_time_minutes(row["start_time"]), booking_time_minutes(row["end_time"]))
        for row in con.execute(
            """SELECT start_time, end_time FROM booking_blocks
               WHERE booking_date = ?""",
            (day.isoformat(),),
        ).fetchall()
    )
    return intervals


def intervals_overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and end > other_start


def booking_interval_conflicts(
    con: sqlite3.Connection,
    day: datetime_date,
    start: int,
    end: int,
    *,
    exclude_booking_id: str = "",
) -> bool:
    booking_rows = con.execute(
        """SELECT id, booking_time FROM bookings
           WHERE booking_date = ? AND status IN (?, ?)""",
        (day.isoformat(), *BOOKING_BLOCKING_STATUSES),
    ).fetchall()
    for row in booking_rows:
        if exclude_booking_id and row["id"] == exclude_booking_id:
            continue
        other_start = booking_time_minutes(row["booking_time"])
        if intervals_overlap(
            start, end, other_start, other_start + BOOKING_SESSION_MINUTES
        ):
            return True
    for row in con.execute(
        """SELECT start_time, end_time FROM booking_blocks
           WHERE booking_date = ?""",
        (day.isoformat(),),
    ).fetchall():
        if intervals_overlap(
            start,
            end,
            booking_time_minutes(row["start_time"]),
            booking_time_minutes(row["end_time"]),
        ):
            return True
    return False


def booking_slots(con: sqlite3.Connection, day: datetime_date) -> list[dict]:
    current = moscow_now()
    intervals = booking_intervals(con, day)
    result = []
    for start in range(
        BOOKING_OPEN_MINUTES,
        BOOKING_LAST_START_MINUTES + 1,
        BOOKING_SLOT_STEP_MINUTES,
    ):
        scheduled = datetime.combine(
            day,
            datetime_time(start // 60, start % 60),
            tzinfo=MOSCOW_TZ,
        )
        occupied = any(
            intervals_overlap(
                start, start + BOOKING_SESSION_MINUTES, other_start, other_end
            )
            for other_start, other_end in intervals
        )
        result.append({
            "time": booking_time_text(start),
            "available": scheduled > current and not occupied,
        })
    return result


def business_lead_request_hash(payload: CreateBusinessLead) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def _payment_init_is_stale(row: sqlite3.Row, *, now: datetime | None = None) -> bool:
    """Whether an Init lease can be reclaimed after a worker/process crash."""
    raw_updated = row["payment_updated_at"]
    if not raw_updated:
        return True
    try:
        updated = datetime.fromisoformat(str(raw_updated))
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - updated >= timedelta(seconds=TBANK_INIT_LEASE_SECONDS)


def recover_tbank_initialization(row: sqlite3.Row, _language: str) -> sqlite3.Row:
    """Resolve an ambiguous Init through CheckOrder, never a blind re-Init.

    T-Bank requires a unique OrderId for every operation. If Init timed out, a
    second Init with the same OrderId is therefore unsafe: the first request
    may already have created a payment. CheckOrder is the documented recovery
    read. Since its normal Payments objects do not promise PaymentURL, finding
    a payment can recover its identity/status but may still require manual
    reconciliation instead of issuing a duplicate operation.
    """
    if (
        row["payment_provider"] not in {"tbank_demo", "tbank"}
        or row["payment_url"]
        or row["payment_state"]
        not in {"failed", "initializing", "checking", "init_ambiguous"}
    ):
        return row

    claimed = False
    claimed_at = now_iso()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM orders WHERE id = ?", (row["id"],)
        ).fetchone()
        if (
            current
            and not current["payment_url"]
            and (
                current["payment_state"] == "failed"
                or (
                    current["payment_state"]
                    in {"initializing", "checking", "init_ambiguous"}
                    and _payment_init_is_stale(current)
                )
            )
        ):
            updated = con.execute(
                """UPDATE orders
                   SET payment_state = 'checking',
                       payment_last_error = '',
                       payment_updated_at = ?, updated_at = ?
                   WHERE id = ? AND payment_url IS NULL
                     AND payment_state IN
                         ('failed','initializing','checking','init_ambiguous')""",
                (claimed_at, claimed_at, row["id"]),
            )
            claimed = updated.rowcount == 1
    if not claimed:
        raise HTTPException(503, "Платёжная форма Т-Банка ещё восстанавливается")

    row = order_row(row["id"])
    try:
        result = tbank_client.check_order(row["id"])
        payments = result.get("Payments")
        if not isinstance(payments, list):
            raise TBankError("Т-Банк не вернул список платежей заказа")
    except TBankError as exc:
        updated = now_iso()
        with db() as con:
            con.execute(
                """UPDATE orders
                   SET payment_state = 'init_ambiguous',
                       payment_last_error = ?, payment_updated_at = ?,
                       updated_at = ?
                   WHERE id = ? AND payment_state = 'checking'""",
                (str(exc)[:500], updated, updated, row["id"]),
            )
        raise HTTPException(
            503, "Платёжная форма Т-Банка временно недоступна"
        ) from exc

    expected_amount = int(row["total"]) * 100
    candidates: list[tuple[str, str, str | None]] = []
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        payment_id = str(payment.get("PaymentId", ""))
        raw_amount = payment.get("Amount")
        if (
            payment_id.isdigit()
            and len(payment_id) <= 20
            and isinstance(raw_amount, int)
            and not isinstance(raw_amount, bool)
            and raw_amount == expected_amount
        ):
            raw_url = payment.get("PaymentURL")
            try:
                payment_url = (
                    validate_payment_url(raw_url)
                    if raw_url is not None
                    else None
                )
            except TBankError:
                payment_url = None
            candidates.append(
                (payment_id, str(payment.get("Status", ""))[:80], payment_url)
            )

    stored_payment_id = str(row["provider_payment_id"] or "")
    if stored_payment_id:
        candidates = [
            candidate for candidate in candidates
            if secrets.compare_digest(candidate[0], stored_payment_id)
        ]

    updated = now_iso()
    if len(candidates) == 1:
        payment_id, provider_status, payment_url = candidates[0]
        with db() as con:
            duplicate = con.execute(
                """SELECT id FROM orders
                   WHERE payment_provider = ? AND provider_payment_id = ?
                     AND id != ?""",
                (row["payment_provider"], payment_id, row["id"]),
            ).fetchone()
            if not duplicate:
                con.execute(
                    """UPDATE orders
                       SET provider_payment_id = ?, payment_url = ?,
                           payment_state = ?, payment_provider_status = ?,
                           payment_last_error = ?, payment_updated_at = ?,
                           updated_at = ?
                       WHERE id = ? AND payment_state = 'checking'""",
                    (
                        payment_id,
                        payment_url,
                        "awaiting" if payment_url else "init_ambiguous",
                        provider_status,
                        "" if payment_url else (
                            "Платёж найден через CheckOrder, но ссылка оплаты недоступна"
                        ),
                        updated,
                        updated,
                        row["id"],
                    ),
                )
            else:
                con.execute(
                    """UPDATE orders
                       SET payment_state = 'init_ambiguous',
                           payment_last_error =
                               'PaymentId уже связан с другим локальным заказом',
                           payment_updated_at = ?, updated_at = ?
                       WHERE id = ? AND payment_state = 'checking'""",
                    (updated, updated, row["id"]),
                )
        recovered = order_row(row["id"])
        if recovered["payment_url"]:
            return recovered
    else:
        reason = (
            "CheckOrder не нашёл платёж; повторный Init с тем же OrderId запрещён"
            if not candidates
            else "CheckOrder вернул несколько подходящих платежей"
        )
        with db() as con:
            con.execute(
                """UPDATE orders
                   SET payment_state = 'init_ambiguous',
                       payment_last_error = ?, payment_updated_at = ?,
                       updated_at = ?
                   WHERE id = ? AND payment_state = 'checking'""",
                (reason, updated, updated, row["id"]),
            )

    raise HTTPException(
        503,
        "Платёж Т-Банка требует безопасной проверки; новый платёж не создан",
    )


def initialize_tbank_payment(
    row: sqlite3.Row,
    language: str,
) -> sqlite3.Row:
    """Create one bank payment for a freshly persisted local order."""
    attempted = now_iso()
    with db() as con:
        claimed = con.execute(
            """UPDATE orders
               SET payment_attempts = payment_attempts + 1,
                   payment_updated_at = ?, updated_at = ?
               WHERE id = ? AND provider_payment_id IS NULL
                 AND payment_state = 'initializing'""",
            (attempted, attempted, row["id"]),
        )
    if claimed.rowcount != 1:
        return order_row(row["id"])

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
                   payment_provider_status = ?,
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
            or not _saby_order_is_after_rollout_cutoff(row)
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


def hash_customer_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, CUSTOMER_PASSWORD_ITERATIONS
    )
    return (
        f"pbkdf2_sha256${CUSTOMER_PASSWORD_ITERATIONS}$"
        f"{salt.hex()}${digest.hex()}"
    )


def valid_customer_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = stored.split("$", 3)
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_raw)
        expected = bytes.fromhex(digest_raw)
    except (TypeError, ValueError):
        return False
    if (
        algorithm != "pbkdf2_sha256"
        or iterations != CUSTOMER_PASSWORD_ITERATIONS
        or len(salt) != 16
        or len(expected) != 32
    ):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return secrets.compare_digest(actual, expected)


def customer_session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def customer_account_for_request(
    request: Request, *, required: bool = True
) -> sqlite3.Row | None:
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE, "")
    if not token or len(token) > 160:
        if required:
            raise HTTPException(401, "Войдите в личный кабинет")
        return None
    try:
        token_hash = customer_session_hash(token)
    except UnicodeEncodeError:
        if required:
            raise HTTPException(401, "Войдите в личный кабинет") from None
        return None
    with db() as con:
        row = con.execute(
            """SELECT account.* FROM customer_sessions AS session
               JOIN customer_accounts AS account ON account.id = session.account_id
               WHERE session.token_hash = ? AND session.expires_at > ?""",
            (token_hash, now_iso()),
        ).fetchone()
    if not row and required:
        raise HTTPException(401, "Сессия истекла. Войдите снова")
    return row


def create_customer_session(response: Response, account_id: str) -> None:
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(seconds=CUSTOMER_SESSION_SECONDS)
    with db() as con:
        con.execute("DELETE FROM customer_sessions WHERE expires_at <= ?", (created.isoformat(),))
        con.execute(
            """INSERT INTO customer_sessions
               (token_hash, account_id, created_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (
                customer_session_hash(token), account_id,
                created.isoformat(), expires.isoformat(),
            ),
        )
    response.set_cookie(
        CUSTOMER_SESSION_COOKIE,
        token,
        max_age=CUSTOMER_SESSION_SECONDS,
        httponly=True,
        secure=not TEST_MODE,
        samesite="strict",
        path="/",
    )


def customer_profile(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "created_at": row["created_at"],
    }


def customer_order(row: sqlite3.Row) -> dict:
    result = public_order(row)
    result["customer"] = json.loads(row["customer_json"])
    payment_url = row["payment_url"] or ""
    parsed_payment_url = urllib.parse.urlsplit(payment_url)
    result["payment_url"] = payment_url if (
        row["payment_state"] in {"initializing", "awaiting"}
        and parsed_payment_url.scheme == "https"
        and bool(parsed_payment_url.netloc)
        and not parsed_payment_url.username
        and not parsed_payment_url.password
    ) else None
    return result


def customer_booking(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "date": row["booking_date"],
        "time": row["booking_time"],
        "format": row["format"],
        "guests": row["guests"],
        "note": row["note"],
        "status": row["status"],
    }


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


def admin_booking(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "date": row["booking_date"],
        "time": row["booking_time"],
        "format": row["format"],
        "guests": row["guests"],
        "name": row["name"],
        "phone": row["phone"],
        "note": row["note"],
        "status": row["status"],
        "source": row["source"],
    }


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


def send_to_owners(text: str, label: str) -> bool:
    if not BOT_TOKEN or not OWNER_CHAT_IDS:
        logging.info("Telegram-уведомление отключено: BOT_TOKEN/OWNER_CHAT_ID не заданы")
        return False
    all_sent = True
    for chat_id in OWNER_CHAT_IDS:
        sent = False
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
                sent = True
                break
            except Exception:
                if attempt == 3:
                    logging.exception("Не удалось отправить %s владельцу %s после 3 попыток", label, chat_id)
                else:
                    logging.warning("Повтор Telegram %s для %s, попытка %s", label, chat_id, attempt + 1)
        all_sent = all_sent and sent
    return all_sent


def notify_owners(row: sqlite3.Row) -> bool:
    """Отправляет владельцам оплаченный заказ с короткими повторами при сбое."""
    return send_to_owners(paid_notification(row), "заказ")


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


def notify_booking(booking: dict) -> None:
    format_label = (
        "Церемония с мастером"
        if booking["format"] == "master"
        else "Самостоятельно"
    )
    text = "\n".join(filter(None, [
        "🫖 Новая бронь" + (" из Telegram-бота" if booking.get("source") == "telegram" else " с сайта"),
        f"№ {booking['id']}",
        f"Формат: {format_label}",
        f"Дата: {booking['date']}",
        f"Время: {booking['time']}",
        f"Гостей: {booking['guests']}",
        f"Имя: {booking['name']}" if booking["name"] else "",
        f"{'Telegram' if booking.get('source') == 'telegram' else 'Телефон'}: {booking['phone']}",
        f"Пожелания: {booking['note']}" if booking["note"] else "",
    ]))
    send_to_owners(text, "бронь")


def enqueue_paid_order_effects(
    con: sqlite3.Connection, order_id: str, updated_at: str
) -> None:
    """Persist post-payment work in the same transaction as CONFIRMED."""
    for effect in ("telegram", "saby"):
        con.execute(
            """INSERT OR IGNORE INTO paid_order_effects
               (order_id, effect, state, attempts, last_error, updated_at)
               VALUES (?, ?, 'pending', 0, '', ?)""",
            (order_id, effect, updated_at),
        )
    con.execute(
        """UPDATE orders SET paid_effects_enqueued = 1
           WHERE id = ?""",
        (order_id,),
    )


def _mark_paid_effect(
    order_id: str,
    effect: str,
    state: str,
    *,
    error: str = "",
) -> None:
    updated = now_iso()
    with db() as con:
        con.execute(
            """UPDATE paid_order_effects
               SET state = ?, last_error = ?, updated_at = ?,
                   completed_at = CASE WHEN ? = 'sent' THEN ? ELSE completed_at END
               WHERE order_id = ? AND effect = ? AND state = 'sending'""",
            (state, error[:300], updated, state, updated, order_id, effect),
        )


def _claim_paid_effect(order_id: str, effect: str) -> bool:
    """Claim a retryable effect, safely resolving stale Saby uncertainty."""
    claimed_at = now_iso()
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=PAID_EFFECT_LEASE_SECONDS)
    ).isoformat()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT e.state, e.updated_at, o.saby_state
               FROM paid_order_effects AS e
               JOIN orders AS o ON o.id = e.order_id
               WHERE e.order_id = ? AND e.effect = ?""",
            (order_id, effect),
        ).fetchone()
        if not row:
            return False
        if (
            effect == "saby"
            and row["state"] == "sending"
            and str(row["updated_at"]) <= stale_before
            and row["saby_state"] == "sending"
        ):
            # After a process crash we cannot know whether Saby accepted the
            # write. Retrying could create a duplicate order, so retain a
            # durable, visible ambiguous state for manual reconciliation.
            con.execute(
                """UPDATE paid_order_effects
                   SET state = 'ambiguous',
                       last_error = 'Нужна проверка заказа в Saby после прерванной отправки',
                       updated_at = ?
                   WHERE order_id = ? AND effect = ? AND state = 'sending'""",
                (claimed_at, order_id, effect),
            )
            con.execute(
                """UPDATE orders
                   SET saby_state = 'ambiguous',
                       saby_last_error = 'Нужна проверка заказа после прерванной отправки',
                       updated_at = ?
                   WHERE id = ? AND saby_state = 'sending'""",
                (claimed_at, order_id),
            )
            return False
        claimed = con.execute(
            """UPDATE paid_order_effects
               SET state = 'sending', attempts = attempts + 1,
                   last_error = '', updated_at = ?
               WHERE order_id = ? AND effect = ?
                 AND (
                   state IN ('pending','failed')
                   OR (state = 'sending' AND updated_at <= ?)
                 )""",
            (claimed_at, order_id, effect, stale_before),
        )
        return claimed.rowcount == 1


def _saby_auto_sync_enabled() -> bool:
    try:
        return sync_mode_from_env().value == "auto"
    except SabySyncError:
        return False


def _saby_order_is_after_rollout_cutoff(row: sqlite3.Row) -> bool:
    """Fail closed for paid orders created before the explicitly chosen rollout.

    This prevents enabling ``auto`` from replaying historical/test payments that
    accumulated while Saby writes were disabled.  An absent cutoff preserves the
    previous behaviour for existing installations and tests.
    """
    raw_cutoff = os.getenv("SABY_ORDER_SYNC_STARTED_AT", "").strip()
    if not raw_cutoff:
        return True
    try:
        cutoff = datetime.fromisoformat(raw_cutoff.replace("Z", "+00:00"))
        paid_at = datetime.fromisoformat(str(row["paid_at"] or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logging.error("Некорректный SABY_ORDER_SYNC_STARTED_AT или paid_at; запись в Saby заблокирована")
        return False
    if cutoff.tzinfo is None or paid_at.tzinfo is None:
        logging.error("SABY_ORDER_SYNC_STARTED_AT и paid_at должны содержать часовой пояс")
        return False
    return paid_at >= cutoff


def _telegram_notifications_enabled() -> bool:
    return bool(BOT_TOKEN and OWNER_CHAT_IDS)


def process_paid_order_effects(order_id: str) -> None:
    """Deliver durable paid-order effects; safe to call on every callback."""
    try:
        row = order_row(order_id)
    except HTTPException:
        return
    if row["payment_state"] != "paid":
        return

    if _telegram_notifications_enabled() and _claim_paid_effect(
        order_id, "telegram"
    ):
        try:
            delivered = notify_owners(order_row(order_id))
        except Exception:
            logging.exception("Не удалось обработать Telegram-уведомление заказа %s", order_id)
            delivered = False
        if delivered is False:
            _mark_paid_effect(
                order_id,
                "telegram",
                "failed",
                error="Telegram временно недоступен или не настроен",
            )
        else:
            # Compatibility: existing injected/test notifiers returned None.
            _mark_paid_effect(order_id, "telegram", "sent")

    if _saby_auto_sync_enabled() and _claim_paid_effect(order_id, "saby"):
        sync_paid_order_to_saby(order_id)
        saby_state = order_row(order_id)["saby_state"]
        if saby_state == "synced":
            _mark_paid_effect(order_id, "saby", "sent")
        elif saby_state == "ambiguous":
            _mark_paid_effect(
                order_id,
                "saby",
                "ambiguous",
                error="Нужна ручная проверка результата отправки в Saby",
            )
        else:
            _mark_paid_effect(
                order_id,
                "saby",
                "failed",
                error="Заказ пока не передан в Saby",
            )


def recover_paid_order_effects() -> None:
    """Enqueue unfinished new paid orders and retry persisted work."""
    with db() as con:
        pending_orders = con.execute(
            """SELECT id FROM orders
               WHERE paid_effects_enqueued = 0
                 AND payment_state = 'paid'"""
        ).fetchall()
        for pending_order in pending_orders:
            enqueue_paid_order_effects(con, pending_order["id"], now_iso())

        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=PAID_EFFECT_LEASE_SECONDS)
        ).isoformat()
        order_ids = [
            row["order_id"]
            for row in con.execute(
                """SELECT DISTINCT order_id FROM paid_order_effects
                   WHERE state IN ('pending','failed')
                      OR (state = 'sending' AND updated_at <= ?)
                   ORDER BY updated_at ASC""",
                (stale_before,),
            ).fetchall()
        ]
    for order_id in order_ids:
        process_paid_order_effects(order_id)


def paid_effect_worker(stop: threading.Event) -> None:
    """Small persistent retry loop started by the FastAPI lifespan."""
    while not stop.is_set():
        try:
            recover_paid_order_effects()
        except Exception:
            logging.exception("Ошибка восстановления действий оплаченных заказов")
        if stop.wait(PAID_EFFECT_RETRY_SECONDS):
            return


class SabyShadowBusy(RuntimeError):
    """Only one read-only Saby comparison may run at a time."""


@contextmanager
def saby_shadow_process_lock():
    """Cross-process guard for a future multi-worker deployment."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_DIR / "saby-shadow.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SabyShadowBusy("Сравнение Saby уже выполняется") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _saby_shadow_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    try:
        report = json.loads(row["report_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        report = {}
    return {
        "id": int(row["id"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "trigger": row["trigger"],
        "status": row["status"],
        "report": report,
        "error": row["error"],
    }


def _saby_difference_fingerprint(item: dict) -> str:
    """Identify one exact observed difference without including display copy."""

    identity = {
        "kind": item.get("kind"),
        "site_id": item.get("site_id"),
        "saby_id": item.get("saby_id"),
        "site_value": item.get("site_value"),
        "saby_value": item.get("saby_value"),
    }
    payload = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _annotate_saby_acknowledgements(latest: dict | None) -> None:
    if not latest or not isinstance(latest.get("report"), dict):
        return
    differences = latest["report"].get("differences")
    if not isinstance(differences, list):
        return
    fingerprints = [_saby_difference_fingerprint(item) for item in differences]
    with db() as con:
        acknowledged = {
            row["fingerprint"]
            for row in con.execute(
                "SELECT fingerprint FROM saby_shadow_acknowledgements"
            ).fetchall()
        }
    pending = 0
    acknowledged_actionable = 0
    for item, fingerprint in zip(differences, fingerprints, strict=True):
        item["fingerprint"] = fingerprint
        item["acknowledged"] = fingerprint in acknowledged
        if item.get("severity") != "info":
            if item["acknowledged"]:
                acknowledged_actionable += 1
            else:
                pending += 1
    counts = latest["report"].setdefault("counts", {})
    counts["unacknowledged_actionable_differences"] = pending
    counts["acknowledged_actionable_differences"] = acknowledged_actionable


def saby_shadow_status(limit: int = 10) -> dict:
    settings = SabyShadowSettings.from_env()
    with db() as con:
        rows = con.execute(
            "SELECT * FROM saby_shadow_runs ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 25),),
        ).fetchall()
    history = [_saby_shadow_row(row) for row in rows]
    _annotate_saby_acknowledgements(history[0] if history else None)
    return {
        "enabled": settings.enabled,
        "interval_seconds": settings.interval_seconds,
        "read_only": True,
        "writes_enabled": False,
        "running": bool(history and history[0] and history[0]["status"] == "running"),
        "latest": history[0] if history else None,
        "history": history,
    }


def _finish_saby_shadow_run(
    run_id: int,
    *,
    status: str,
    report: dict | None = None,
    error: str = "",
) -> dict:
    with db() as con:
        updated = con.execute(
            """UPDATE saby_shadow_runs
               SET completed_at = ?, status = ?, report_json = ?, error = ?
               WHERE id = ? AND status = 'running'""",
            (
                now_iso(),
                status,
                json.dumps(report or {}, ensure_ascii=False, separators=(",", ":")),
                error,
                run_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Состояние теневой проверки Saby уже изменилось")
        con.execute(
            """DELETE FROM saby_shadow_runs
               WHERE id NOT IN (
                   SELECT id FROM saby_shadow_runs ORDER BY id DESC LIMIT ?
               )""",
            (SABY_SHADOW_RETENTION_RUNS,),
        )
        row = con.execute(
            "SELECT * FROM saby_shadow_runs WHERE id = ?", (run_id,)
        ).fetchone()
    result = _saby_shadow_row(row)
    if result is None:
        raise RuntimeError("Не удалось сохранить результат теневой проверки Saby")
    return result


def run_saby_shadow_check(trigger: str) -> dict:
    """Read Saby once, compare it to the site, and persist an audit report.

    No method capable of changing Saby or the storefront is called here.
    """

    if trigger not in {"manual", "scheduler"}:
        raise ValueError("Некорректный источник теневой проверки")
    if not _saby_shadow_lock.acquire(blocking=False):
        raise SabyShadowBusy("Сравнение Saby уже выполняется")
    try:
        with saby_shadow_process_lock():
            started_at = now_iso()
            try:
                with db() as con:
                    # Holding the process lock proves that an older 'running'
                    # row has no live owner (for example after a hard restart).
                    con.execute(
                        """UPDATE saby_shadow_runs
                           SET status = 'error', completed_at = ?,
                               error = 'Проверка прервана перезапуском сервиса'
                           WHERE status = 'running'""",
                        (started_at,),
                    )
                    cursor = con.execute(
                        """INSERT INTO saby_shadow_runs
                           (started_at, trigger, status, report_json, error)
                           VALUES (?, ?, 'running', '{}', '')""",
                        (started_at, trigger),
                    )
                    run_id = int(cursor.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise SabyShadowBusy("Сравнение Saby уже выполняется") from exc

            try:
                document = get_catalog_store().get()
                site_catalog = {item["id"]: item for item in document["teas"]}
                saby_catalog = saby_client.catalog_all(with_balance=True)
                report = compare_catalogs(site_catalog, saby_catalog)
                configuration = saby_client.configuration()
                catalog_bytes = json.dumps(
                    document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                report["source"] = {
                    "site_revision": document["revision"],
                    "site_updated_at": document["updated_at"],
                    "site_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                    "point_id": configuration.get("point_id"),
                    "price_list_id": configuration.get("price_list_id"),
                }
                return _finish_saby_shadow_run(
                    run_id,
                    status="ok" if report["state"] == "ok" else "differences",
                    report=report,
                )
            except SabyError as exc:
                return _finish_saby_shadow_run(run_id, status="error", error=str(exc))
            except Exception:
                logging.exception("Ошибка теневого сравнения каталога Saby")
                return _finish_saby_shadow_run(
                    run_id,
                    status="error",
                    error="Не удалось безопасно сравнить каталоги",
                )
    finally:
        _saby_shadow_lock.release()


def saby_shadow_worker(stop: threading.Event, interval_seconds: int) -> None:
    """Run the read-only comparison immediately and then on a fixed interval."""

    while not stop.is_set():
        try:
            result = run_saby_shadow_check("scheduler")
            if result["status"] == "error":
                logging.warning("Теневая проверка Saby не выполнена: %s", result["error"])
        except SabyShadowBusy:
            logging.info("Теневая проверка Saby уже выполняется")
        except Exception:
            logging.exception("Ошибка фонового контроля каталога Saby")
        if stop.wait(interval_seconds):
            return


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
    result["new_bookings"] = int(con.execute(
        "SELECT COUNT(*) FROM bookings WHERE status = 'new'"
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
        for field in ("device", "language", "referrer", "campaign"):
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
            "catalog_active_items": sum(
                item.get("stock") is True and item.get("published", True)
                for item in catalog.values()
            ),
            "analytics_since": first_event,
            "database_size": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        },
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "test_mode": TEST_MODE,
        "catalog_items": len(load_catalog()),
        "version": release_version(),
    }


@app.get("/api/checkout/status")
def checkout_status(response: Response):
    """Expose only whether the public checkout can currently create a payment."""
    response.headers["Cache-Control"] = "no-store"
    return {"available": tbank_checkout_ready(), "provider": "tbank"}


@app.get("/api/catalog")
def public_catalog():
    document = get_catalog_store().get()
    return {
        "revision": document["revision"],
        "types": document["types"],
        "axes": document["axes"],
        "packs": document["packs"],
        "teas": [
            {
                "id": item["id"],
                "type": item["type"],
                "price": item["price"],
                "unit": item["unit"],
                "stock": item.get("stock", True),
                "image_url": catalog_image_url(item),
                "taste": item["taste"],
                "translations": item["translations"],
            }
            for item in document["teas"]
            if item.get("published", True)
        ]
    }


def admin_catalog_response(document: dict) -> dict:
    result = dict(document)
    result["teas"] = [
        {**item, "image_url": catalog_image_url(item)} for item in document["teas"]
    ]
    return result


def require_catalog_write_request(request: Request) -> None:
    """Block cross-site form posts even though the owner cookie is SameSite."""
    if request.headers.get("x-chainya-admin") != "catalog":
        raise HTTPException(403, "Требуется подтверждение запроса админ-панели")
    origin = request.headers.get("origin", "").rstrip("/")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin and origin != expected:
        raise HTTPException(403, "Недопустимый источник запроса")


def catalog_error_response(exc: Exception) -> HTTPException:
    if isinstance(exc, CatalogConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, "Товар не найден")
    return HTTPException(422, str(exc))


def audit_catalog(action: str, item_id: str, revision: int) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO catalog_audit (created_at, action, item_id, revision) VALUES (?, ?, ?, ?)",
            (now_iso(), action, item_id, revision),
        )


@app.get("/api/admin/catalog")
def admin_catalog(authorization: str = Header(default="")):
    require_admin(authorization)
    return admin_catalog_response(get_catalog_store().get())


@app.post("/api/admin/catalog/items", status_code=201)
def admin_create_catalog_item(
    payload: CatalogMutation,
    request: Request,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    require_catalog_write_request(request)
    try:
        document = get_catalog_store().create_item(payload.item, payload.revision)
    except (CatalogError, KeyError) as exc:
        raise catalog_error_response(exc) from exc
    item_id = payload.item.get("id", "")
    audit_catalog("create", item_id, document["revision"])
    return admin_catalog_response(document)


@app.put("/api/admin/catalog/items/{item_id}")
def admin_update_catalog_item(
    item_id: str,
    payload: CatalogMutation,
    request: Request,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    require_catalog_write_request(request)
    try:
        document = get_catalog_store().update_item(item_id, payload.item, payload.revision)
    except (CatalogError, KeyError) as exc:
        raise catalog_error_response(exc) from exc
    audit_catalog("update", item_id, document["revision"])
    return admin_catalog_response(document)


@app.put("/api/admin/catalog/order")
def admin_reorder_catalog(
    payload: CatalogReorder,
    request: Request,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    require_catalog_write_request(request)
    try:
        document = get_catalog_store().reorder(payload.ids, payload.revision)
    except (CatalogError, KeyError) as exc:
        raise catalog_error_response(exc) from exc
    audit_catalog("reorder", "", document["revision"])
    return admin_catalog_response(document)


async def bounded_request_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > maximum:
                raise HTTPException(413, "Файл больше 8 МБ")
        except ValueError:
            raise HTTPException(400, "Некорректный Content-Length") from None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(413, "Файл больше 8 МБ")
        chunks.append(chunk)
    return b"".join(chunks)


def prepare_catalog_image(source: bytes) -> bytes:
    if not source:
        raise HTTPException(422, "Выберите изображение")
    try:
        with Image.open(io.BytesIO(source)) as original:
            width, height = original.size
            if width * height > 40_000_000:
                raise HTTPException(413, "Изображение слишком большое")
            image = ImageOps.exif_transpose(original)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            if image.mode == "RGBA":
                background = Image.new("RGB", image.size, (246, 241, 232))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, "WEBP", quality=88, method=6)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(422, "Файл не является поддерживаемым изображением") from exc


def persist_catalog_image(data: bytes) -> str:
    filename = hashlib.blake2b(data, digest_size=16).hexdigest() + ".webp"
    store = get_catalog_store()
    store.media_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = store.media_dir / filename
    if target.exists():
        return filename
    descriptor, temporary = tempfile.mkstemp(prefix=".image-", suffix=".webp", dir=store.media_dir)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return filename


@app.post("/api/admin/catalog/items/{item_id}/image")
async def admin_upload_catalog_image(
    item_id: str,
    request: Request,
    revision: int = Query(ge=1),
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    require_catalog_write_request(request)
    current = get_catalog_store().get()
    if current["revision"] != revision:
        raise HTTPException(409, "Каталог уже изменён в другой вкладке. Обновите страницу.")
    if item_id not in {item["id"] for item in current["teas"]}:
        raise HTTPException(404, "Сначала сохраните новый товар")
    data = prepare_catalog_image(await bounded_request_body(request, 8 * 1024 * 1024))
    filename = persist_catalog_image(data)
    try:
        document = get_catalog_store().set_image(item_id, filename, revision)
    except (CatalogError, KeyError) as exc:
        raise catalog_error_response(exc) from exc
    audit_catalog("image", item_id, document["revision"])
    return admin_catalog_response(document)


@app.get("/catalog-media/{filename}")
@app.head("/catalog-media/{filename}")
def catalog_media(filename: str):
    if not MEDIA_FILE_RE.fullmatch(filename):
        raise HTTPException(404, "Изображение не найдено")
    path = get_catalog_store().media_dir / filename
    if not path.is_file():
        raise HTTPException(404, "Изображение не найдено")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/api/analytics/events", status_code=204)
def collect_analytics(payload: AnalyticsEvent, request: Request):
    """Store a small anonymous product event; IP and user-agent are never persisted."""
    rate_limit(request, "analytics", 180, 600)
    session_hash = hash_session(payload.session_id)
    with db() as con:
        cleanup_analytics_if_due(con)
        con.execute(
            """INSERT INTO analytics_events
               (created_at, session_hash, event, section, language, device, referrer, campaign)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso(), session_hash, payload.event, payload.section,
                payload.language, payload.device, payload.referrer, payload.campaign,
            ),
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


@app.get("/api/admin/bookings")
def admin_bookings(
    authorization: str = Header(default=""),
    status: str = "",
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    require_admin(authorization)
    if status and status not in {"new", "confirmed", "completed", "cancelled"}:
        raise HTTPException(422, "Неизвестный статус")
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    query = q.strip()
    if query:
        conditions.append(
            "casefold(id || ' ' || booking_date || ' ' || booking_time || ' ' || "
            "format || ' ' || name || ' ' || phone || ' ' || note) LIKE ?"
        )
        params.append(f"%{query.casefold()}%")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with db() as con:
        con.create_function("casefold", 1, lambda value: (value or "").casefold())
        total = int(
            con.execute(f"SELECT COUNT(*) FROM bookings{where}", params).fetchone()[0]
        )
        rows = con.execute(
            f"SELECT * FROM bookings{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "bookings": [admin_booking(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/admin/booking-blocks")
def admin_booking_blocks(
    authorization: str = Header(default=""),
    date_from: datetime_date | None = None,
    date_to: datetime_date | None = None,
):
    require_admin(authorization)
    first = date_from or moscow_now().date()
    last = date_to or first + timedelta(days=30)
    if last < first or (last - first).days > 366:
        raise HTTPException(422, "Неверный период закрытых окон")
    with db() as con:
        rows = con.execute(
            """SELECT * FROM booking_blocks
               WHERE booking_date BETWEEN ? AND ?
               ORDER BY booking_date, start_time""",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
    return {
        "blocks": [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "date": row["booking_date"],
                "start": row["start_time"],
                "end": row["end_time"],
                "note": row["note"],
            }
            for row in rows
        ]
    }


@app.post("/api/admin/booking-blocks", status_code=201)
def admin_create_booking_block(
    payload: CreateBookingBlock,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    start = booking_time_minutes(payload.start)
    end = booking_time_minutes(payload.end)
    block = {
        "id": uuid.uuid4().hex[:12].upper(),
        "created_at": now_iso(),
        "date": payload.date.isoformat(),
        "start": payload.start.isoformat(),
        "end": payload.end.isoformat(),
        "note": payload.note,
    }
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        if booking_interval_conflicts(con, payload.date, start, end):
            raise HTTPException(409, "Это время пересекается с бронью или другим закрытым окном")
        con.execute(
            """INSERT INTO booking_blocks
               (id, created_at, booking_date, start_time, end_time, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                block["id"], block["created_at"], block["date"],
                block["start"], block["end"], block["note"],
            ),
        )
    return block


@app.delete("/api/admin/booking-blocks/{block_id}", status_code=204)
def admin_delete_booking_block(
    block_id: str,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    with db() as con:
        deleted = con.execute(
            "DELETE FROM booking_blocks WHERE id = ?", (block_id,)
        ).rowcount
    if not deleted:
        raise HTTPException(404, "Закрытое окно не найдено")
    return Response(status_code=204)


@app.patch("/api/admin/bookings/{booking_id}")
def admin_update_booking(
    booking_id: str,
    payload: UpdateBookingStatus,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        if not current:
            raise HTTPException(404, "Бронь не найдена")
        if payload.status in BOOKING_BLOCKING_STATUSES:
            start = booking_time_minutes(current["booking_time"])
            if booking_interval_conflicts(
                con,
                datetime_date.fromisoformat(current["booking_date"]),
                start,
                start + BOOKING_SESSION_MINUTES,
                exclude_booking_id=booking_id,
            ):
                raise HTTPException(409, "Время уже занято другой бронью или закрыто владельцем")
        con.execute(
            "UPDATE bookings SET status = ?, updated_at = ? WHERE id = ?",
            (payload.status, now_iso(), booking_id),
        )
        row = con.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
    return admin_booking(row)


@app.get("/api/admin/saby/status")
def admin_saby_status(authorization: str = Header(default="")):
    require_admin(authorization)
    return saby_client.configuration()


def require_saby_shadow_run_request(request: Request) -> None:
    """Require an intentional same-origin admin action for the manual read."""

    if request.headers.get("x-chainya-admin") != "saby-shadow":
        raise HTTPException(403, "Требуется подтверждение запроса админ-панели")
    origin = request.headers.get("origin", "").rstrip("/")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin and origin != expected:
        raise HTTPException(403, "Недопустимый источник запроса")


def require_saby_shadow_ack_request(request: Request) -> None:
    """Require an intentional same-origin acknowledgement from the admin UI."""

    if request.headers.get("x-chainya-admin") != "saby-shadow-ack":
        raise HTTPException(403, "Требуется подтверждение запроса админ-панели")
    origin = request.headers.get("origin", "").rstrip("/")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin and origin != expected:
        raise HTTPException(403, "Недопустимый источник запроса")


@app.get("/api/admin/saby/catalog-shadow")
def admin_saby_catalog_shadow(
    response: Response,
    history_limit: int = Query(default=10, ge=1, le=25),
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    response.headers["Cache-Control"] = "no-store"
    return saby_shadow_status(history_limit)


@app.post("/api/admin/saby/catalog-shadow/run")
def admin_run_saby_catalog_shadow(
    request: Request,
    response: Response,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    require_saby_shadow_run_request(request)
    rate_limit(
        request,
        "admin-saby-shadow",
        SABY_SHADOW_MANUAL_LIMIT,
        SABY_SHADOW_MANUAL_WINDOW_SECONDS,
    )
    response.headers["Cache-Control"] = "no-store"
    try:
        return run_saby_shadow_check("manual")
    except SabyShadowBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/admin/saby/catalog-shadow/acknowledge")
def admin_acknowledge_saby_catalog_difference(
    payload: SabyShadowAcknowledgement,
    request: Request,
    response: Response,
    authorization: str = Header(default=""),
):
    require_admin(authorization)
    require_saby_shadow_ack_request(request)
    response.headers["Cache-Control"] = "no-store"
    with db() as con:
        row = con.execute(
            """SELECT * FROM saby_shadow_runs
               WHERE status IN ('ok', 'differences') ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        latest = _saby_shadow_row(row)
        differences = (latest or {}).get("report", {}).get("differences", [])
        current = {
            _saby_difference_fingerprint(item)
            for item in differences
            if item.get("severity") != "info"
        }
        if payload.fingerprint not in current:
            raise HTTPException(409, "Расхождение уже изменилось. Обновите сверку.")
        if payload.acknowledged:
            con.execute(
                """INSERT INTO saby_shadow_acknowledgements
                   (fingerprint, acknowledged_at) VALUES (?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET acknowledged_at=excluded.acknowledged_at""",
                (payload.fingerprint, now_iso()),
            )
        else:
            con.execute(
                "DELETE FROM saby_shadow_acknowledgements WHERE fingerprint = ?",
                (payload.fingerprint,),
            )
    return {"ok": True, "acknowledged": payload.acknowledged}


@app.post("/api/admin/saby/test")
def admin_saby_test(authorization: str = Header(default="")):
    require_admin(authorization)
    try:
        retail_result = saby_client.sales_points("retail")
    except SabyError as exc:
        raise HTTPException(502, str(exc)) from exc

    def rows(result: object, key: str) -> list[dict]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if not isinstance(result, dict):
            return []
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        # Some Saby Retail responses return a single result row without a
        # resource-specific wrapper. Keep the readiness check compatible with
        # both documented response forms.
        return [result] if result.get("id") is not None else []

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
    delivery_confirmation = "point_list" if delivery_found else ""
    # Saby can temporarily omit an enabled Retail point from point/list while
    # still returning its live delivery calendar.  The calendar endpoint is
    # read-only and is also a documented prerequisite for creating an order,
    # so use it as a conservative secondary readiness signal.
    if point_id and point_found and not delivery_found:
        try:
            delivery_calendar = rows(saby_client.delivery_calendar(point_id), "dates")
        except SabyError as exc:
            errors["delivery_calendar"] = str(exc)
        else:
            if delivery_calendar:
                delivery_found = True
                delivery_confirmation = "calendar"
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
    mapped_products = [
        item for item in products
        if str(item.get("externalId") or "") in expected_external_ids
    ]
    sellable_external_ids = {
        ref.external_id
        for site_id, ref in SABY_NOMENCLATURE_BY_SITE_ID.items()
        if site_catalog.get(site_id, {}).get("published", True)
        and site_catalog.get(site_id, {}).get("stock", True)
    }
    sellable_products = [
        item for item in mapped_products
        if str(item.get("externalId") or "") in sellable_external_ids
    ]
    zero_balance_products = [
        item for item in sellable_products
        if isinstance(item.get("balance"), (int, float))
        and not isinstance(item.get("balance"), bool)
        and item.get("balance") <= 0
    ]
    in_stock_products = [
        item for item in sellable_products
        if isinstance(item.get("balance"), (int, float))
        and not isinstance(item.get("balance"), bool)
        and item.get("balance") > 0
    ]
    unknown_balance_products = [
        item for item in sellable_products
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
        "delivery_confirmation": delivery_confirmation,
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

    def coordinate(value, minimum: float, maximum: float):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if minimum <= number <= maximum else None

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
            "longitude": coordinate(location.get("longitude"), -180, 180),
            "latitude": coordinate(location.get("latitude"), -90, 90),
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


@app.post("/api/account/register", status_code=201)
def register_customer(
    payload: CustomerRegister,
    request: Request,
    response: Response,
):
    rate_limit(request, "customer-register", 5, 600)
    account_id = uuid.uuid4().hex
    created = now_iso()
    try:
        with db() as con:
            con.execute(
                """INSERT INTO customer_accounts
                   (id, name, phone, password_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    account_id, payload.name, payload.phone,
                    hash_customer_password(payload.password), created, created,
                ),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Для этого телефона уже создан личный кабинет") from None
    create_customer_session(response, account_id)
    with db() as con:
        row = con.execute(
            "SELECT * FROM customer_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return {"account": customer_profile(row)}


@app.post("/api/account/login")
def login_customer(
    payload: CustomerLogin,
    request: Request,
    response: Response,
):
    rate_limit(request, "customer-login", 10, 600)
    with db() as con:
        row = con.execute(
            "SELECT * FROM customer_accounts WHERE phone = ?", (payload.phone,)
        ).fetchone()
    if row:
        password_ok = valid_customer_password(payload.password, row["password_hash"])
    else:
        # Equal-cost check keeps the response useful without making account
        # existence visible through a large timing difference.
        hashlib.pbkdf2_hmac(
            "sha256", payload.password.encode("utf-8"), b"\0" * 16,
            CUSTOMER_PASSWORD_ITERATIONS,
        )
        password_ok = False
    if not row or not password_ok:
        raise HTTPException(401, "Неверный телефон или пароль")
    create_customer_session(response, row["id"])
    return {"account": customer_profile(row)}


@app.delete("/api/account/session", status_code=204)
def logout_customer(request: Request, response: Response):
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE, "")
    if token and len(token) <= 160:
        try:
            token_hash = customer_session_hash(token)
        except UnicodeEncodeError:
            token_hash = ""
        if token_hash:
            with db() as con:
                con.execute(
                    "DELETE FROM customer_sessions WHERE token_hash = ?", (token_hash,)
                )
    response.delete_cookie(
        CUSTOMER_SESSION_COOKIE,
        path="/",
        secure=not TEST_MODE,
        httponly=True,
        samesite="strict",
    )


@app.get("/api/account/session")
def customer_session_status(request: Request, response: Response):
    """Return a quiet authentication probe for public storefront pages.

    The protected ``/api/account`` endpoint intentionally keeps its 401
    contract.  Public pages use this endpoint instead, so a normal signed-out
    visit does not create a failed network request in the browser console.
    """
    account = customer_account_for_request(request, required=False)
    response.headers["Cache-Control"] = "no-store"
    return {
        "authenticated": account is not None,
        "account": customer_profile(account) if account else None,
    }


@app.get("/api/account")
def get_customer_account(request: Request, response: Response):
    account = customer_account_for_request(request)
    response.headers["Cache-Control"] = "no-store"
    return {"account": customer_profile(account)}


@app.patch("/api/account")
def update_customer_account(payload: CustomerProfileUpdate, request: Request):
    account = customer_account_for_request(request)
    updated = now_iso()
    with db() as con:
        con.execute(
            "UPDATE customer_accounts SET name = ?, updated_at = ? WHERE id = ?",
            (payload.name, updated, account["id"]),
        )
        row = con.execute(
            "SELECT * FROM customer_accounts WHERE id = ?", (account["id"],)
        ).fetchone()
    return {"account": customer_profile(row)}


@app.post("/api/account/password", status_code=204)
def change_customer_password(payload: CustomerPasswordChange, request: Request):
    account = customer_account_for_request(request)
    if not valid_customer_password(payload.current_password, account["password_hash"]):
        raise HTTPException(403, "Текущий пароль указан неверно")
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE, "")
    token_hash = customer_session_hash(token)
    with db() as con:
        con.execute(
            """UPDATE customer_accounts SET password_hash = ?, updated_at = ?
               WHERE id = ?""",
            (hash_customer_password(payload.new_password), now_iso(), account["id"]),
        )
        con.execute(
            """DELETE FROM customer_sessions
               WHERE account_id = ? AND token_hash != ?""",
            (account["id"], token_hash),
        )


@app.delete("/api/account", status_code=204)
def delete_customer_account(
    payload: CustomerAccountDelete,
    request: Request,
    response: Response,
):
    account = customer_account_for_request(request)
    if not valid_customer_password(payload.password, account["password_hash"]):
        raise HTTPException(403, "Пароль указан неверно")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE orders SET customer_account_id = NULL WHERE customer_account_id = ?",
            (account["id"],),
        )
        con.execute(
            "UPDATE bookings SET customer_account_id = NULL WHERE customer_account_id = ?",
            (account["id"],),
        )
        con.execute("DELETE FROM customer_sessions WHERE account_id = ?", (account["id"],))
        con.execute("DELETE FROM customer_accounts WHERE id = ?", (account["id"],))
    response.delete_cookie(
        CUSTOMER_SESSION_COOKIE,
        path="/",
        secure=not TEST_MODE,
        httponly=True,
        samesite="strict",
    )


@app.get("/api/account/orders")
def customer_orders(request: Request, response: Response):
    account = customer_account_for_request(request)
    with db() as con:
        rows = con.execute(
            """SELECT * FROM orders WHERE customer_account_id = ?
               ORDER BY created_at DESC LIMIT 100""",
            (account["id"],),
        ).fetchall()
    response.headers["Cache-Control"] = "no-store"
    return {"orders": [customer_order(row) for row in rows], "total": len(rows)}


@app.get("/api/account/bookings")
def customer_bookings(request: Request, response: Response):
    account = customer_account_for_request(request)
    with db() as con:
        rows = con.execute(
            """SELECT * FROM bookings WHERE customer_account_id = ?
               ORDER BY booking_date DESC, booking_time DESC LIMIT 100""",
            (account["id"],),
        ).fetchall()
    response.headers["Cache-Control"] = "no-store"
    return {"bookings": [customer_booking(row) for row in rows], "total": len(rows)}


@app.post("/api/account/orders/claim")
def claim_customer_order(payload: CustomerOrderClaim, request: Request):
    account = customer_account_for_request(request)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM orders WHERE id = ?", (payload.order_id.upper(),)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Заказ не найден")
        require_order_token(row, payload.token)
        linked_account = row["customer_account_id"]
        if linked_account and linked_account != account["id"]:
            raise HTTPException(409, "Заказ уже привязан к другому кабинету")
        if not linked_account:
            con.execute(
                """UPDATE orders SET customer_account_id = ?, updated_at = ?
                   WHERE id = ? AND customer_account_id IS NULL""",
                (account["id"], now_iso(), row["id"]),
            )
        row = con.execute("SELECT * FROM orders WHERE id = ?", (row["id"],)).fetchone()
    return {"order": customer_order(row)}


@app.post("/api/orders", status_code=201)
def create_order(
    payload: CreateOrder,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    customer_account = customer_account_for_request(request, required=False)
    customer_account_id = customer_account["id"] if customer_account else None
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
            if tbank_enabled:
                existing = recover_tbank_initialization(existing, payload.language)
            return checkout_response(existing, request, reused=True)

    # Повтор ответа на уже созданный заказ не должен расходовать лимит клиента:
    # идемпотентный replay — нормальная часть восстановления мобильной сети.
    rate_limit(request, "create-order", 12, 600)

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
                    cdek_quote_json, idempotency_key_hash, request_hash,
                    paid_effects_enqueued, customer_account_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, "pending_payment", created, created, subtotal, delivery_price,
                 subtotal + delivery_price, payload.payment_method, payload.delivery,
                 json.dumps(customer, ensure_ascii=False), json.dumps(lines, ensure_ascii=False), None,
                 payment_token, payment_provider, payment_state, created,
                 json.dumps(cdek_quote, ensure_ascii=False), key_hash,
                 request_fingerprint, 0, customer_account_id),
            )
        if not reused_row and analytics_session_hash:
            context = con.execute(
                """SELECT language, device, referrer, campaign FROM analytics_events
                    WHERE session_hash = ? ORDER BY id DESC LIMIT 1""",
                (analytics_session_hash,),
            ).fetchone()
            if context:
                con.execute(
                    """INSERT INTO analytics_events
                       (created_at, session_hash, event, section, language, device, referrer, campaign)
                       VALUES (?, ?, 'order_created', 'payment', ?, ?, ?, ?)""",
                    (
                        created, analytics_session_hash, context["language"],
                        context["device"], context["referrer"], context["campaign"],
                    ),
                )
    if reused_row:
        if tbank_enabled:
            reused_row = recover_tbank_initialization(
                reused_row, payload.language
            )
        return checkout_response(reused_row, request, reused=True)
    created_row = order_row(order_id)
    if tbank_enabled:
        created_row = initialize_tbank_payment(created_row, payload.language)
    return checkout_response(created_row, request)


@app.post("/api/business-leads", status_code=202)
def create_business_lead(
    payload: CreateBusinessLead,
    background_tasks: BackgroundTasks,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    key_hash = idempotency_hash(idempotency_key) if idempotency_key else None
    request_fingerprint = business_lead_request_hash(payload)
    if key_hash:
        with db() as con:
            existing = con.execute(
                """SELECT * FROM business_leads
                   WHERE idempotency_key_hash = ?""",
                (key_hash,),
            ).fetchone()
        if existing:
            if existing["request_hash"] != request_fingerprint:
                raise HTTPException(
                    409, "Idempotency-Key уже использован для другой заявки"
                )
            return {"id": existing["id"], "accepted": True}

    # Mobile/network replay of an accepted lead is not a new submission and
    # therefore must be resolved before consuming the per-IP rate limit.
    rate_limit(request, "business-lead", 5, 600)
    lead = {"id": uuid.uuid4().hex[:12].upper(), "created_at": now_iso(), **payload.model_dump()}
    reused_row = None
    with db() as con:
        if key_hash:
            con.execute("BEGIN IMMEDIATE")
            reused_row = con.execute(
                """SELECT * FROM business_leads
                   WHERE idempotency_key_hash = ?""",
                (key_hash,),
            ).fetchone()
            if reused_row and reused_row["request_hash"] != request_fingerprint:
                raise HTTPException(
                    409, "Idempotency-Key уже использован для другой заявки"
                )
        if not reused_row:
            con.execute(
                """INSERT INTO business_leads
                   (id, created_at, company, name, contact, note, status,
                    updated_at, idempotency_key_hash, request_hash)
                   VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
                (
                    lead["id"], lead["created_at"], lead["company"],
                    lead["name"], lead["contact"], lead["note"],
                    lead["created_at"], key_hash, request_fingerprint,
                ),
            )
    if reused_row:
        return {"id": reused_row["id"], "accepted": True}
    background_tasks.add_task(notify_business_lead, lead)
    return {"id": lead["id"], "accepted": True}


@app.get("/api/bookings/availability")
def booking_availability(
    date: datetime_date,
    response: Response,
):
    current_day = moscow_now().date()
    if not current_day <= date <= current_day + timedelta(days=14):
        raise HTTPException(422, "Свободные окна доступны только на ближайшие 14 дней")
    with db() as con:
        slots = booking_slots(con, date)
    response.headers["Cache-Control"] = "no-store"
    return {
        "date": date.isoformat(),
        "duration_minutes": BOOKING_SESSION_MINUTES,
        "slots": slots,
    }


@app.post("/api/bookings", status_code=201)
def create_booking(
    payload: CreateBooking,
    background_tasks: BackgroundTasks,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    booking_bot_secret: str = Header(default="", alias="X-Booking-Bot-Secret"),
):
    customer_account = (
        customer_account_for_request(request, required=False)
        if payload.source == "website" else None
    )
    customer_account_id = customer_account["id"] if customer_account else None
    if payload.source == "telegram":
        if not BOOKING_BOT_SECRET or not hmac.compare_digest(
            booking_bot_secret, BOOKING_BOT_SECRET
        ):
            raise HTTPException(403, "Бот не авторизован для создания брони")
    key_hash = idempotency_hash(idempotency_key) if idempotency_key else None
    request_fingerprint = booking_request_hash(payload)
    if key_hash:
        with db() as con:
            existing = con.execute(
                "SELECT * FROM bookings WHERE idempotency_key_hash = ?",
                (key_hash,),
            ).fetchone()
        if existing:
            if existing["request_hash"] != request_fingerprint:
                raise HTTPException(
                    409, "Idempotency-Key уже использован для другой брони"
                )
            return {"id": existing["id"], "accepted": True}

    # Повтор уже принятой идемпотентной заявки не является новой попыткой:
    # мобильная сеть может запросить тот же ответ много раз после таймаута.
    # Лимитируем только создание действительно новой брони.
    rate_limit(
        request,
        f"booking-{payload.source}",
        30 if payload.source == "telegram" else 5,
        600,
    )

    booking = {
        "id": uuid.uuid4().hex[:12].upper(),
        "created_at": now_iso(),
        **payload.model_dump(mode="json", exclude={"privacy_accepted"}),
    }
    reused_row = None
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        if key_hash:
            reused_row = con.execute(
                "SELECT * FROM bookings WHERE idempotency_key_hash = ?",
                (key_hash,),
            ).fetchone()
            if reused_row and reused_row["request_hash"] != request_fingerprint:
                raise HTTPException(
                    409, "Idempotency-Key уже использован для другой брони"
                )
        if not reused_row:
            start = booking_time_minutes(payload.time)
            if booking_interval_conflicts(
                con,
                payload.date,
                start,
                start + BOOKING_SESSION_MINUTES,
            ):
                raise HTTPException(
                    409,
                    "Это время уже занято. Выберите другое свободное окно.",
                )
            con.execute(
                """INSERT INTO bookings
                   (id, created_at, booking_date, booking_time, format, guests,
                    name, phone, note, status, source, updated_at,
                    idempotency_key_hash, request_hash, customer_account_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)""",
                (
                    booking["id"], booking["created_at"], booking["date"],
                    booking["time"], booking["format"], booking["guests"],
                    booking["name"], booking["phone"], booking["note"],
                    booking["source"], booking["created_at"], key_hash,
                    request_fingerprint, customer_account_id,
                ),
            )
    if reused_row:
        return {"id": reused_row["id"], "accepted": True}
    background_tasks.add_task(notify_booking, booking)
    return {"id": booking["id"], "accepted": True}


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

    process_effects = False
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
                next_order_status = "paid" if row["status"] == "pending_payment" else row["status"]
                con.execute(
                    """UPDATE orders
                       SET status = ?, paid_at = COALESCE(paid_at, ?), payment_state = 'paid',
                           payment_provider_status = ?, payment_last_error = '',
                           payment_updated_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (next_order_status, updated, provider_status, updated, updated, order_id),
                )
                if not row["paid_effects_enqueued"]:
                    enqueue_paid_order_effects(con, order_id, updated)
                process_effects = True
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

    if process_effects:
        # A replay is also a recovery signal: the paid transition and its
        # outbox entries may have committed immediately before a process died.
        background_tasks.add_task(process_paid_order_effects, order_id)
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


@app.head("/payment/success")
@app.head("/payment/fail")
def tbank_result_page_head():
    return Response(headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def owner_page(request: Request, authenticated_page: str = "admin.html"):
    page = authenticated_page if valid_admin_session(
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


@app.get("/manage/catalog")
def management_catalog_page(request: Request):
    """Catalog editor protected by the same owner session as the dashboard."""
    return owner_page(request, "admin-catalog.html")


@app.head("/admin/orders")
@app.head("/manage/")
@app.head("/manage")
@app.head("/manage/catalog")
def management_page_head():
    return Response(
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        }
    )


@app.get("/account/")
@app.get("/account")
def customer_account_page():
    return FileResponse(
        ROOT / "backend" / "account.html",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.head("/account/")
@app.head("/account")
def customer_account_page_head():
    return Response(
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        }
    )


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

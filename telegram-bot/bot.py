#!/usr/bin/env python3
"""Телеграм-бот чайной «Чайня» — самодостаточный, закрывает потребность в чате.

Всё делается прямо в боте, кнопками, и НА КАЖДОМ ЭКРАНЕ — картинка:
  • Выбрать чай      — каталог по категориям: категория → чай → карточка (фото с
    вкусовым профилем на самой картинке, описание, цена по фасовкам).
  • Подобрать по вкусу — выбрать ноту (цветочный, фруктовый…), бот подберёт чаи.
  • Наугад           — случайный чай, если глаза разбегаются.
  • Избранное        — сохранённые чаи (❤️ на карточке), хранятся между сессиями.
  • Забронировать    — пошаговая бронь через общий календарь сайта и бота.
  • Наш зал          — фото, адрес, часы, карта.

Ссылки на сайт и Telegram Mini App временно не публикуются: браузер Telegram на
iPhone показывает Apple Deceptive Website Warning для домена chainya.ru.

Экраны — фото-сообщения; навигация правит их на месте через edit_media (с кэшем
file_id, чтобы не перезаливать баннеры). Данные — teas.json (build_data.py),
карточки — media/cards (make_cards.py), баннеры разделов — media/banners
(make_banners.py). Long-polling. Нужен BOT_TOKEN (@BotFather) в .env.
"""
import asyncio
import contextlib
import hashlib
import html
import json
import logging
import os
import random
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MenuButtonCommands,
    Message,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_IDS = [x for x in os.getenv("OWNER_CHAT_ID", "").replace(",", " ").split() if x]
BOOKING_API_URL = os.getenv("BOOKING_API_URL", "https://chainya.ru").strip().rstrip("/")
BOOKING_BOT_SECRET = os.getenv("BOOKING_BOT_SECRET", "").strip()
CATALOG_API_URL = os.getenv("CATALOG_API_URL", BOOKING_API_URL + "/api/catalog").strip()
try:
    CATALOG_REFRESH_SECONDS = max(60, int(os.getenv("CATALOG_REFRESH_SECONDS", "300")))
except ValueError:
    CATALOG_REFRESH_SECONDS = 300
CHANNEL = "https://t.me/chainyamsk"   # это КАНАЛ (не чат), только для анонсов/подписки
PHONE = "+7 905 590-88-01"
DLV_LABEL = {
    "pickup": "Самовывоз · Острякова, 3",
    "cdek": "СДЭК по России",  # совместимость со старыми версиями мини-аппа
    "cdek_pvz": "СДЭК · пункт выдачи",
    "cdek_courier": "СДЭК · курьер",
}
MAPS = "https://yandex.ru/maps/?mode=search&text=" + urllib.parse.quote("Москва, улица Острякова, 3")
MEDIA = os.path.join(HERE, "media")

DATA = json.load(open(os.path.join(HERE, "teas.json"), encoding="utf-8"))
TEAS = DATA["teas"]
BY_ID = {t["id"]: t for t in TEAS}
TYPE_NAME = {t["id"]: t["name"] for t in DATA["types"]}
AX_NAME = {a["id"]: a["name"] for a in DATA["axes"]}
AX_ORDER = [a["id"] for a in DATA["axes"]]
PACKS = DATA["packs"]
CATALOG_REVISION = DATA.get("revision", "local")
router = Router()
BOOKING_FLOWS = {}


def normalize_public_catalog(document):
    """Validate the public API document and flatten its Russian translations."""
    if not isinstance(document, dict):
        raise ValueError("каталог не является объектом")
    types = document.get("types")
    axes = document.get("axes")
    packs = document.get("packs")
    teas = document.get("teas")
    if not all(isinstance(value, list) and value for value in (types, axes, packs, teas)):
        raise ValueError("в каталоге отсутствуют обязательные списки")
    if any(not isinstance(pack, int) or isinstance(pack, bool) or pack <= 0 for pack in packs):
        raise ValueError("некорректная фасовка")

    def named_rows(rows, label):
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"некорректная строка {label}")
            row_id = row.get("id")
            name = row.get("name")
            if not isinstance(row_id, str) or not row_id or row_id in seen:
                raise ValueError(f"некорректный id {label}")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"нет названия {label}")
            seen.add(row_id)
            result.append({"id": row_id, "name": name.strip()})
        return result

    normalized_types = named_rows(types, "категории")
    normalized_axes = named_rows(axes, "вкуса")
    type_ids = {row["id"] for row in normalized_types}
    axis_ids = {row["id"] for row in normalized_axes}
    normalized_teas = []
    tea_ids = set()
    for tea in teas:
        if not isinstance(tea, dict):
            raise ValueError("некорректная карточка товара")
        tea_id = tea.get("id")
        tea_type = tea.get("type")
        translations = tea.get("translations")
        ru = translations.get("ru") if isinstance(translations, dict) else None
        taste = tea.get("taste")
        price = tea.get("price")
        if not isinstance(tea_id, str) or not tea_id or tea_id in tea_ids:
            raise ValueError("некорректный id товара")
        if tea_type not in type_ids:
            raise ValueError(f"неизвестная категория товара {tea_id}")
        if tea.get("unit") not in {"g", "pc"}:
            raise ValueError(f"некорректная единица товара {tea_id}")
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            raise ValueError(f"некорректная цена товара {tea_id}")
        if not isinstance(tea.get("stock"), bool):
            raise ValueError(f"некорректное наличие товара {tea_id}")
        image_url = tea.get("image_url")
        if not isinstance(image_url, str) or not image_url.startswith(("/img/", "/catalog-media/")):
            raise ValueError(f"некорректное фото товара {tea_id}")
        if not isinstance(ru, dict) or any(
            not isinstance(ru.get(key), str) or not ru[key].strip()
            for key in ("name", "orig", "desc")
        ):
            raise ValueError(f"нет русского текста товара {tea_id}")
        if not isinstance(taste, dict) or set(taste) != axis_ids or any(
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 5
            for value in taste.values()
        ):
            raise ValueError(f"некорректный вкус товара {tea_id}")
        tea_ids.add(tea_id)
        normalized_teas.append({
            "id": tea_id,
            "type": tea_type,
            "price": price,
            "unit": tea["unit"],
            "stock": tea["stock"],
            "image_url": image_url,
            "taste": dict(taste),
            "name": ru["name"].strip(),
            "orig": ru["orig"].strip(),
            "desc": ru["desc"].strip(),
        })
    return {
        "revision": document.get("revision", "api"),
        "types": normalized_types,
        "axes": normalized_axes,
        "packs": list(packs),
        "teas": normalized_teas,
    }


def apply_catalog(document):
    global DATA, TEAS, BY_ID, TYPE_NAME, AX_NAME, AX_ORDER, PACKS, CATALOG_REVISION
    normalized = normalize_public_catalog(document)
    DATA = normalized
    TEAS = normalized["teas"]
    BY_ID = {tea["id"]: tea for tea in TEAS}
    TYPE_NAME = {row["id"]: row["name"] for row in normalized["types"]}
    AX_NAME = {row["id"]: row["name"] for row in normalized["axes"]}
    AX_ORDER = [row["id"] for row in normalized["axes"]]
    PACKS = normalized["packs"]
    CATALOG_REVISION = normalized["revision"]


def _catalog_api_json():
    request = urllib.request.Request(CATALOG_API_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


async def refresh_catalog():
    try:
        document = await asyncio.to_thread(_catalog_api_json)
        apply_catalog(document)
        logging.info("Каталог обновлён с сайта: revision=%s, позиций=%d", CATALOG_REVISION, len(TEAS))
        return True
    except Exception as exc:
        logging.warning("Каталог сайта не обновлён, остаётся последняя корректная копия: %s", exc)
        return False


async def catalog_refresh_loop():
    while True:
        await asyncio.sleep(CATALOG_REFRESH_SECONDS)
        await refresh_catalog()

# ─────────────  КАРТИНКИ ЭКРАНОВ  ─────────────
START_IMG = os.path.join(MEDIA, "start.jpg")
HALL_IMG = os.path.join(MEDIA, "hall.jpg")


def banner(name):
    return os.path.join(MEDIA, "banners", name + ".jpg")


B_MENU, B_CAT, B_TASTE, B_BOOK = banner("menu"), banner("catalog"), banner("taste"), banner("booking")


def card_path(t):
    image_url = t.get("image_url", "")
    # Фото, загруженные через админ-панель, сразу показываем в боте. Seed-фото
    # продолжают использовать подготовленные карточки со вкусовым профилем.
    if image_url.startswith("/catalog-media/"):
        return BOOKING_API_URL + image_url
    for p in (os.path.join(MEDIA, "cards", f"{t['id']}.jpg"),
              os.path.join(MEDIA, "teas", f"{t['id']}.jpg")):
        if os.path.exists(p):
            return p
    if image_url.startswith("/img/"):
        return BOOKING_API_URL + image_url
    return B_CAT  # запасной баннер: img_input не вернёт None → edit_media правит на месте


# кэш file_id: баннер заливается один раз, дальше переиспользуем id (быстро)
FILE_IDS = {}


def img_input(path):
    fid = FILE_IDS.get(os.path.basename(path))
    if fid:
        return fid
    if path.startswith(BOOKING_API_URL + "/"):
        return path
    return FSInputFile(path) if os.path.exists(path) else None


def _remember(path, r):
    if isinstance(r, Message) and r.photo:
        FILE_IDS[os.path.basename(path)] = r.photo[-1].file_id


async def send_screen(m: Message, path, caption, kb):
    """Новый экран (ответ на команду) — фото-сообщение (или текст, если фото нет)."""
    src = img_input(path)
    if src is None:
        return await m.answer(caption, reply_markup=kb)
    r = await m.answer_photo(src, caption=caption, reply_markup=kb)
    _remember(path, r)
    return r


async def show(c: CallbackQuery, path, caption, kb):
    """Навигация: правим текущее фото-сообщение на месте (edit_media)."""
    src = img_input(path)
    if src is None:
        # экраны — фото-сообщения, edit_text по ним всегда падает; последний
        # резерв (баннер отсутствует на диске) — просто отправить текстом
        await c.message.answer(caption, reply_markup=kb)
        return
    try:
        r = await c.message.edit_media(
            InputMediaPhoto(media=src, caption=caption, parse_mode=ParseMode.HTML),
            reply_markup=kb)
        _remember(path, r)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return  # тот же экран — просто гасим повторный тап
        r = await c.message.answer_photo(src, caption=caption, reply_markup=kb)
        _remember(path, r)
    except Exception:
        r = await c.message.answer_photo(src, caption=caption, reply_markup=kb)
        _remember(path, r)


# ─────────────  ИЗБРАННОЕ (файловое хранилище)  ─────────────
FAVS_PATH = os.getenv("FAVS_PATH", os.path.join(HERE, "favs.json"))


def load_favs():
    try:
        return json.load(open(FAVS_PATH, encoding="utf-8"))
    except Exception:
        return {}


FAVS = load_favs()


def save_favs():
    os.makedirs(os.path.dirname(FAVS_PATH), mode=0o700, exist_ok=True)
    tmp = FAVS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(FAVS, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, FAVS_PATH)


def is_fav(uid, tid):
    return tid in FAVS.get(str(uid), [])


def toggle_fav(uid, tid):
    k = str(uid)
    lst = FAVS.setdefault(k, [])
    on = tid not in lst
    if on:
        lst.append(tid)
    else:
        lst.remove(tid)
    if not lst:
        FAVS.pop(k, None)
    save_favs()
    return on


# ─────────────  ВСПОМОГАТЕЛЬНОЕ  ─────────────
def esc(s):
    return html.escape(str(s), quote=False)


def plural(n, forms=("сорт", "сорта", "сортов")):
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def pack_price(per10, g):
    # на сайте packPrice = Math.round(...) — округление .5 ВВЕРХ; питоновский round()
    # округляет к чётному, из-за чего цены в боте/заказе расходились с витриной на 5 ₽
    return int(per10 * g / 10 / 5 + 0.5) * 5


def price_line(t):
    if t["unit"] == "pc":
        return f"💰 {t['price']} ₽ за штуку"
    parts = " · ".join(f"{g} г — {pack_price(t['price'], g)} ₽" for g in PACKS)
    return "💰 " + parts


def tea_caption(t):
    # вкусовой профиль нарисован на самой картинке — в тексте только суть.
    # parse_mode=HTML → экранируем данные каталога, иначе < > & порвут отправку.
    parts = [esc(t["desc"]), "", price_line(t)]
    if not t["stock"]:
        parts += ["", "⚠️ Сейчас нет в наличии — напишите, скажем, когда будет."]
    cap = "\n".join(parts)
    return cap[:1020] + "…" if len(cap) > 1024 else cap


def btn(text, cb):
    return InlineKeyboardButton(text=text, callback_data=cb)


def rows(buttons, per):
    return [buttons[i:i + per] for i in range(0, len(buttons), per)]


def type_count():
    c = {}
    for t in TEAS:
        c[t["type"]] = c.get(t["type"], 0) + 1
    return c


# ─────────────  КЛАВИАТУРЫ  ─────────────
def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🍵 Выбрать чай", "cats"), btn("🎯 Подобрать по вкусу", "taste")],
        [btn("📅 Забронировать стол", "bk:d")],
        [btn("📍 Наш зал", "loc"), btn("❤️ Избранное", "favs")],
        [InlineKeyboardButton(text="📣 Наш канал", url=CHANNEL)],
    ])


def cats_kb():
    cnt = type_count()
    bs = [btn(f"{TYPE_NAME[ty['id']]} · {cnt[ty['id']]}", f"cat:{ty['id']}")
          for ty in DATA["types"] if cnt.get(ty["id"])]
    kb = rows(bs, 2)
    kb.append([btn("🎯 По вкусу", "taste"), btn("🎲 Наугад", "rnd")])
    kb.append([btn("❤️ Избранное", "favs")])
    kb.append([btn("↩︎ В меню", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def type_kb(type_id):
    kb = [[btn(t["name"] + ("" if t["stock"] else " · нет"), f"tea:{t['id']}")]
          for t in TEAS if t["type"] == type_id]
    kb.append([btn("↩︎ К категориям", "cats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def tea_kb(t, uid):
    fav = "❤️ В избранном" if is_fav(uid, t["id"]) else "🤍 В избранное"
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(fav, f"fav:{t['id']}")],
        [btn(f"↩︎ {TYPE_NAME.get(t['type'], 'Категория')}", f"cat:{t['type']}"),
         btn("🍵 Все", "cats")],
    ])


def axes_kb():
    bs = [btn(AX_NAME[a], f"tx:{a}") for a in AX_ORDER]
    return InlineKeyboardMarkup(inline_keyboard=rows(bs, 2) + [[btn("↩︎ В меню", "menu")]])


def taste_result_kb(axis):
    picks = sorted((t for t in TEAS if t["taste"].get(axis, 0) >= 2),
                   key=lambda t: -t["taste"].get(axis, 0))[:8]
    if not picks:
        picks = sorted(TEAS, key=lambda t: -t["taste"].get(axis, 0))[:6]
    kb = [[btn(t["name"] + ("" if t["stock"] else " · нет"), f"tea:{t['id']}")] for t in picks]
    kb.append([btn("↩︎ К вкусам", "taste"), btn("🍵 Все категории", "cats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def favs_kb(uid):
    ids = FAVS.get(str(uid), [])
    kb = [[btn(BY_ID[i]["name"], f"tea:{i}")] for i in ids if i in BY_ID]
    kb.append([btn("🍵 К каталогу", "cats"), btn("↩︎ В меню", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def loc_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть на карте", url=MAPS)],
        [btn("📅 Забронировать", "bk:d")],
        [btn("↩︎ В меню", "menu")],
    ])


# бронь
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
FMT = {"m": ("Церемония с мастером", 1500), "s": ("Самостоятельно", 1200)}


class BookingApiError(RuntimeError):
    def __init__(self, message, *, conflict=False):
        super().__init__(message)
        self.conflict = conflict


def _booking_api_json(path, *, payload=None, headers=None):
    url = BOOKING_API_URL + path
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST" if payload is not None else "GET",
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {}), **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = ""
        raise BookingApiError(
            detail or f"HTTP {exc.code}", conflict=exc.code == 409
        ) from exc
    except Exception as exc:
        raise BookingApiError("Сервис бронирования временно недоступен") from exc


async def available_booking_times(iso):
    data = await asyncio.to_thread(
        _booking_api_json,
        "/api/bookings/availability?" + urllib.parse.urlencode({"date": iso}),
    )
    return {slot["time"].replace(":", "") for slot in data.get("slots", []) if slot.get("available") is True}


async def create_shared_booking(c, iso, t, f, g):
    if not BOOKING_BOT_SECRET:
        raise BookingApiError("BOOKING_BOT_SECRET не настроен")
    user = c.from_user
    contact = f"@{user.username}" if user.username else f"Telegram ID {user.id}"
    flow = BOOKING_FLOWS.setdefault(user.id, secrets.token_hex(8))
    digest = hashlib.sha256(
        f"{user.id}:{flow}:{iso}:{t}:{f}:{g}".encode("utf-8")
    ).hexdigest()[:32]
    payload = {
        "format": "master" if f == "m" else "self",
        "date": iso,
        "time": hhmm(t),
        "guests": int(g),
        "name": user.full_name or "Гость Telegram",
        "phone": contact,
        "note": "",
        "privacy_accepted": True,
        "source": "telegram",
    }
    return await asyncio.to_thread(
        _booking_api_json,
        "/api/bookings",
        payload=payload,
        headers={
            "X-Booking-Bot-Secret": BOOKING_BOT_SECRET,
            "Idempotency-Key": f"booking-telegram-{digest}",
        },
    )


async def cancel_shared_booking(booking_id, token):
    return await asyncio.to_thread(
        _booking_api_json,
        f"/api/bookings/{urllib.parse.quote(booking_id)}/cancel",
        payload={"token": token},
    )


def date_days():
    t = date.today()
    return [t + timedelta(days=i) for i in range(7)]


def date_label(d, i):
    return "Сегодня" if i == 0 else "Завтра" if i == 1 else f"{WD[d.weekday()]} {d.day}"


def times():
    return [f"{m // 60:02d}{m % 60:02d}" for m in range(12 * 60, 20 * 60 + 1, 30)]


def hhmm(x):
    return f"{x[:2]}:{x[2:]}"


def dates_kb():
    bs = [btn(date_label(d, i), f"bk:t:{d.isoformat()}") for i, d in enumerate(date_days())]
    return InlineKeyboardMarkup(inline_keyboard=rows(bs, 4) + [[btn("↩︎ В меню", "menu")]])


def times_kb(iso, available):
    bs = [
        btn(hhmm(x), f"bk:f:{iso}:{x}")
        if x in available
        else btn(f"✕ {hhmm(x)}", "bk:busy")
        for x in times()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows(bs, 4) + [[btn("↩︎ Другой день", "bk:d")]])


def fmt_kb(iso, t):
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Церемония с мастером · 1500 ₽", f"bk:g:{iso}:{t}:m")],
        [btn("Самостоятельно · 1200 ₽", f"bk:g:{iso}:{t}:s")],
        [btn("↩︎ Другое время", f"bk:t:{iso}")],
    ])


def guests_kb(iso, t, f):
    bs = [btn(str(g), f"bk:c:{iso}:{t}:{f}:{g}") for g in range(1, 8)]
    return InlineKeyboardMarkup(inline_keyboard=rows(bs, 3) + [[btn("↩︎ Формат", f"bk:f:{iso}:{t}")]])


def confirm_kb(iso, t, f, g):
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Подтвердить бронь", f"bk:ok:{iso}:{t}:{f}:{g}")],
        [btn("↩︎ Заново", "bk:d")],
    ])


def date_h(iso):
    d = date.fromisoformat(iso)
    i = (d - date.today()).days
    return date_label(d, i) if 0 <= i < 7 else f"{WD[d.weekday()]} {d.day}"


# ─────────────  ТЕКСТЫ  ─────────────
WELCOME = (
    "<b>Чайня</b> — чайная у метро Аэропорт.\n\n"
    "Китайский чай навынос с доставкой по России — и стол с мастером, если хочется "
    "провести вечер за чаем. Всё можно прямо здесь, в чате:\n\n"
    "🍵 выбрать чай в каталоге\n"
    "🎯 подобрать по вкусу или взять наугад\n"
    "📅 забронировать стол\n"
    "📍 посмотреть, как нас найти\n\n"
    "Новости и анонсы — в нашем канале @chainyamsk."
)
MENU_CAP = "Чем помочь? Выберите:"
INFO = (
    "<b>Наш зал</b>\n\n"
    "📍 Москва, ул. Острякова, 3 — 1 этаж, помещение 114 (метро Аэропорт, 5 минут пешком)\n"
    "🕛 Ежедневно 12:00–22:00 · последняя посадка в 20:00\n"
    f"📞 {PHONE}\n"
    "📣 Канал: @chainyamsk\n\n"
    "Небольшой зал на несколько столов: заварка при вас, чайник, знакомство с сортами."
)


def cats_caption():
    return f"<b>Каталог чая</b> · {len(TEAS)} {plural(len(TEAS))}\nВыберите категорию:"


def favs_caption(uid):
    return ("❤️ <b>Избранное</b>\nВаши сохранённые чаи:"
            if FAVS.get(str(uid)) else
            "❤️ <b>Избранное</b>\n\nПока пусто. Откройте любой чай и нажмите 🤍 — он сохранится сюда.")


# ─────────────  КОМАНДЫ  ─────────────
@router.message(CommandStart())
async def cmd_start(m: Message):
    await send_screen(m, START_IMG, WELCOME, menu_kb())


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    await send_screen(m, B_CAT, cats_caption(), cats_kb())


@router.message(Command("taste"))
async def cmd_taste(m: Message):
    await send_screen(m, B_TASTE, "🎯 <b>Подбор по вкусу</b>\n\nКакую ноту хотите поймать?", axes_kb())


@router.message(Command("fav"))
async def cmd_fav(m: Message):
    await send_screen(m, B_CAT, favs_caption(m.from_user.id), favs_kb(m.from_user.id))


@router.message(Command("book"))
async def cmd_book(m: Message):
    await send_screen(m, B_BOOK, "<b>Бронь стола</b>\nНа какой день?", dates_kb())


@router.message(Command("contacts"))
async def cmd_contacts(m: Message):
    await send_screen(m, HALL_IMG, INFO, loc_kb())


@router.message(Command("site"))
async def cmd_site(m: Message):
    await m.answer(
        "Веб-приложение временно отключено. Выберите чай или забронируйте стол прямо в боте.",
        reply_markup=menu_kb(),
    )


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш chat_id: <code>{m.from_user.id}</code>")


# ─────────────  МЕНЮ / КАТАЛОГ  ─────────────
@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await show(c, B_MENU, MENU_CAP, menu_kb())
    await c.answer()


@router.callback_query(F.data == "cats")
async def cb_cats(c: CallbackQuery):
    await show(c, B_CAT, cats_caption(), cats_kb())
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(c: CallbackQuery):
    tid = c.data.split(":", 1)[1]
    await show(c, B_CAT, f"<b>{esc(TYPE_NAME.get(tid, 'Чай'))}</b> — выберите сорт:", type_kb(tid))
    await c.answer()


async def show_tea(c: CallbackQuery, t):
    await show(c, card_path(t), tea_caption(t), tea_kb(t, c.from_user.id))


@router.callback_query(F.data.startswith("tea:"))
async def cb_tea(c: CallbackQuery):
    t = BY_ID.get(c.data.split(":", 1)[1])
    if not t:
        return await c.answer("Не нашёл этот чай")
    await show_tea(c, t)
    await c.answer()


# ─────────────  ПОДБОР ПО ВКУСУ  ─────────────
@router.callback_query(F.data == "taste")
async def cb_taste(c: CallbackQuery):
    await show(c, B_TASTE, "🎯 <b>Подбор по вкусу</b>\n\nКакую ноту хотите поймать?", axes_kb())
    await c.answer()


@router.callback_query(F.data.startswith("tx:"))
async def cb_tx(c: CallbackQuery):
    axis = c.data.split(":", 1)[1]
    name = AX_NAME.get(axis, "вкус")
    await show(c, B_TASTE, f"🎯 <b>{esc(name)}</b> — вот что ярче всего звучит:", taste_result_kb(axis))
    await c.answer()


# ─────────────  НАУГАД  ─────────────
@router.callback_query(F.data == "rnd")
async def cb_rnd(c: CallbackQuery):
    pool = [t for t in TEAS if t["stock"]] or TEAS
    await show_tea(c, random.choice(pool))
    await c.answer("Держите 🎲")


# ─────────────  ИЗБРАННОЕ  ─────────────
@router.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery):
    tid = c.data.split(":", 1)[1]
    t = BY_ID.get(tid)
    if not t:
        return await c.answer()
    on = toggle_fav(c.from_user.id, tid)
    try:
        await c.message.edit_reply_markup(reply_markup=tea_kb(t, c.from_user.id))
    except Exception:
        pass
    await c.answer("Добавили в избранное ❤️" if on else "Убрали из избранного")


@router.callback_query(F.data == "favs")
async def cb_favs(c: CallbackQuery):
    await show(c, B_CAT, favs_caption(c.from_user.id), favs_kb(c.from_user.id))
    await c.answer()


# ─────────────  НАШ ЗАЛ  ─────────────
@router.callback_query(F.data == "loc")
async def cb_loc(c: CallbackQuery):
    await show(c, HALL_IMG, INFO, loc_kb())
    await c.answer()


# ─────────────  БРОНЬ  ─────────────
@router.callback_query(F.data == "bk:d")
async def cb_bk_date(c: CallbackQuery):
    # edit на месте: при возврате «Заново»/«Другой день» правим текущий шаг,
    # иначе плодятся параллельные брони со старой живой кнопкой «Подтвердить».
    BOOKING_FLOWS[c.from_user.id] = secrets.token_hex(8)
    await show(c, B_BOOK, "<b>Бронь стола</b>\nНа какой день?", dates_kb())
    await c.answer()


@router.callback_query(F.data.startswith("bk:t:"))
async def cb_bk_time(c: CallbackQuery):
    iso = c.data.split(":")[2]
    try:
        available = await available_booking_times(iso)
    except BookingApiError:
        logging.exception("не удалось получить свободные окна")
        await show(
            c,
            B_BOOK,
            "Не удалось проверить расписание. Попробуйте ещё раз чуть позже или позвоните " + PHONE + ".",
            InlineKeyboardMarkup(inline_keyboard=[[btn("↩︎ Выбрать день", "bk:d")]]),
        )
        return await c.answer("Расписание временно недоступно", show_alert=True)
    await show(
        c,
        B_BOOK,
        f"📅 {date_h(iso)}\nВо сколько посадить?\n\n✕ — время пересекается с другой двухчасовой сессией.",
        times_kb(iso, available),
    )
    await c.answer()


@router.callback_query(F.data == "bk:busy")
async def cb_bk_busy(c: CallbackQuery):
    await c.answer("Это время занято. Выберите свободное окно.", show_alert=True)


@router.callback_query(F.data.startswith("bk:f:"))
async def cb_bk_fmt(c: CallbackQuery):
    _, _, iso, t = c.data.split(":")
    await show(c, B_BOOK, f"📅 {date_h(iso)}, {hhmm(t)}\nФормат?", fmt_kb(iso, t))
    await c.answer()


@router.callback_query(F.data.startswith("bk:g:"))
async def cb_bk_guests(c: CallbackQuery):
    _, _, iso, t, f = c.data.split(":")
    await show(c, B_BOOK, f"📅 {date_h(iso)}, {hhmm(t)} · {FMT[f][0]}\nСколько гостей?", guests_kb(iso, t, f))
    await c.answer()


@router.callback_query(F.data.startswith("bk:c:"))
async def cb_bk_confirm(c: CallbackQuery):
    _, _, iso, t, f, g = c.data.split(":")
    fn, price = FMT[f]
    text = ("<b>Проверьте бронь</b>\n\n"
            f"📅 {date_h(iso)}\n🕛 {hhmm(t)}\n☕ {fn} ({price} ₽ с гостя)\n👥 {g}\n\n"
            "Сессия длится 2 часа. Оплата сейчас не нужна.\n"
            "Нажимая «Подтвердить бронь», вы соглашаетесь с "
            '<a href="https://chainya.ru/privacy.html">политикой конфиденциальности</a>.' )
    await show(c, B_BOOK, text, confirm_kb(iso, t, f, g))
    await c.answer()


@router.callback_query(F.data.startswith("bk:ok:"))
async def cb_bk_ok(c: CallbackQuery):
    _, _, iso, t, f, g = c.data.split(":")
    fn, _price = FMT[f]
    try:
        result = await create_shared_booking(c, iso, t, f, g)
    except BookingApiError as exc:
        if exc.conflict:
            await show(
                c,
                B_BOOK,
                "Это время только что заняли. Выберите другое свободное окно.",
                InlineKeyboardMarkup(inline_keyboard=[[btn("↩︎ Выбрать время", f"bk:t:{iso}")]]),
            )
            return await c.answer("Время уже занято", show_alert=True)
        logging.exception("бронь из Telegram не сохранена в общем календаре")
        await show(
            c,
            B_BOOK,
            "Не удалось сохранить бронь. Ничего не потеряно: повторите попытку или позвоните " + PHONE + ".",
            InlineKeyboardMarkup(inline_keyboard=[
                [btn("Повторить", f"bk:ok:{iso}:{t}:{f}:{g}")],
                [btn("↩︎ Выбрать время", f"bk:t:{iso}")],
            ]),
        )
        return await c.answer("Сервис временно недоступен", show_alert=True)
    cancel_token = str(result.get("cancel_token") or "")
    done_rows = []
    if cancel_token:
        done_rows.append([btn("❌ Отменить бронь", f"bk:x:{result.get('id')}:{cancel_token}")])
    done_rows.append([btn("🍵 Выбрать чай", "cats"), btn("↩︎ В меню", "menu")])
    done_kb = InlineKeyboardMarkup(inline_keyboard=done_rows)
    await show(c, B_BOOK,
               f"✅ <b>Бронь принята!</b>\n\n№ {esc(result.get('id'))}\n📅 {date_h(iso)}, {hhmm(t)}\n☕ {fn}\n👥 {g}\n\n"
               f"Время зарезервировано. Мы свяжемся и подтвердим бронь. Если планы поменяются — позвоните {PHONE}.", done_kb)
    await c.answer("Готово!")
    BOOKING_FLOWS.pop(c.from_user.id, None)


@router.callback_query(F.data.startswith("bk:x:"))
async def cb_bk_cancel(c: CallbackQuery):
    try:
        _, _, booking_id, token = c.data.split(":", 3)
        await cancel_shared_booking(booking_id, token)
    except BookingApiError as exc:
        return await c.answer(str(exc), show_alert=True)
    await show(
        c,
        B_BOOK,
        f"❌ <b>Бронь № {esc(booking_id)} отменена.</b>\n\nВремя снова доступно другим гостям.",
        InlineKeyboardMarkup(inline_keyboard=[[btn("↩︎ В меню", "menu")]]),
    )
    await c.answer("Бронь отменена")


async def notify_owners(bot, note, what="сообщение"):
    for cid in OWNER_CHAT_IDS:
        try:
            await bot.send_message(cid, note)
        except Exception as e:
            logging.warning("%s не ушло на %s: %s", what, cid, e)
    if not OWNER_CHAT_IDS:
        logging.info("%s (OWNER_CHAT_ID не задан):\n%s", what, note)


def pack_label(pk):
    return "шт" if pk == "pc" else f"{pk} г"


# бронь и заказ из мини-аппа (если прилетит sendData)
@router.message(F.web_app_data)
async def on_webapp(m: Message):
    try:
        d = json.loads(m.web_app_data.data)
    except Exception:
        return
    typ = d.get("type")
    u = m.from_user
    who = f"@{u.username}" if u.username else esc(u.full_name or "гость")

    if typ == "booking":
        # Старые Mini App клиенты не передавали машинную дату и обходили общий
        # календарь. Не создаём фантомную бронь: отправляем в новый единый поток.
        await m.answer(
            "Форма бронирования обновилась. Нажмите /book — бот покажет только свободное время."
        )

    elif typ == "order":
        # цену считаем на стороне бота из своей же базы — не доверяем суммам клиента
        lines, total = [], 0
        for it in d.get("items", []):
            t = BY_ID.get(it.get("id"))
            if not t:
                continue
            try:
                qty = max(1, int(it.get("qty", 1)))
            except Exception:
                qty = 1
            pk = it.get("pack")
            if pk == "pc":
                unit = t["price"]
            else:
                try:
                    unit = pack_price(t["price"], int(pk))
                except Exception:
                    continue
            s = unit * qty
            total += s
            lines.append(f"• {esc(t['name'])} — {pack_label(pk)} ×{qty} — {s} ₽")
        if not lines:
            return
        await m.answer("✅ Заказ принят! Свяжемся, подтвердим состав и посчитаем доставку. Оплата переводом или при получении.")
        note = "🛒 <b>Новый заказ</b> (мини-апп)\n" + "\n".join(lines)
        note += f"\n<b>Итого: {total} ₽</b>\nДоставка: {DLV_LABEL.get(d.get('delivery'), esc(str(d.get('delivery') or '—')))}"
        for k, lab in (("name", "Имя"), ("phone", "Телефон"), ("city", "Город"),
                       ("pvz_code", "ПВЗ"), ("address", "Адрес"),
                       ("payment_method", "Оплата"), ("note", "Коммент")):
            if d.get(k):
                note += f"\n{lab}: {esc(d[k])}"
        note += f"\nОт: {who} (id {u.id})"
        await notify_owners(m.bot, note, "заказ")


async def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (.env).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await refresh_catalog()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="menu", description="Каталог чая"),
        BotCommand(command="taste", description="Подобрать по вкусу"),
        BotCommand(command="fav", description="Избранное"),
        BotCommand(command="book", description="Забронировать стол"),
        BotCommand(command="contacts", description="Наш зал · адрес"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    dp = Dispatcher()
    dp.include_router(router)
    logging.info("Бот запущен. Чаёв: %d | брони → %s",
                 len(TEAS), ", ".join(OWNER_CHAT_IDS) or "(в лог)")
    refresh_task = asyncio.create_task(catalog_refresh_loop())
    try:
        await dp.start_polling(bot)
    finally:
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task


if __name__ == "__main__":
    asyncio.run(main())

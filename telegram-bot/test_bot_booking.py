import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import bot


def public_catalog_fixture():
    source = json.loads((Path(bot.HERE) / "teas.json").read_text(encoding="utf-8"))
    teas = []
    for tea in source["teas"]:
        current = copy.deepcopy(tea)
        current["image_url"] = f"/img/{current['img']}.webp"
        current["translations"] = {
            "ru": {
                "name": current.pop("name"),
                "orig": current.pop("orig"),
                "desc": current.pop("desc"),
            }
        }
        teas.append(current)
    return {**source, "revision": 7, "teas": teas}


def restore_local_catalog(original):
    bot.DATA = original
    bot.TEAS = original["teas"]
    bot.BY_ID = {tea["id"]: tea for tea in bot.TEAS}
    bot.TYPE_NAME = {row["id"]: row["name"] for row in original["types"]}
    bot.AX_NAME = {row["id"]: row["name"] for row in original["axes"]}
    bot.AX_ORDER = [row["id"] for row in original["axes"]]
    bot.PACKS = original["packs"]
    bot.CATALOG_REVISION = original.get("revision", "local")


def test_public_catalog_is_validated_and_applied():
    document = public_catalog_fixture()
    document["teas"][0]["price"] = 777
    original = bot.DATA
    try:
        bot.apply_catalog(document)
        tea_id = document["teas"][0]["id"]
        assert bot.CATALOG_REVISION == 7
        assert bot.BY_ID[tea_id]["price"] == 777
        assert bot.BY_ID[tea_id]["name"] == document["teas"][0]["translations"]["ru"]["name"]
    finally:
        restore_local_catalog(original)


def test_invalid_public_catalog_does_not_replace_current_data(monkeypatch):
    document = public_catalog_fixture()
    document["teas"][0]["taste"].pop(next(iter(document["teas"][0]["taste"])))
    before = bot.DATA
    monkeypatch.setattr(bot, "_catalog_api_json", lambda: document)
    assert asyncio.run(bot.refresh_catalog()) is False
    assert bot.DATA is before


def test_admin_uploaded_photo_uses_same_origin_url():
    tea = {"id": "new-tea", "image_url": "/catalog-media/new-tea/photo.webp"}
    expected = "https://chainya.ru/catalog-media/new-tea/photo.webp"
    assert bot.card_path(tea) == expected
    assert bot.img_input(bot.card_path(tea)) == expected


def test_busy_times_are_not_selectable():
    keyboard = bot.times_kb("2026-08-05", {"1300", "1500"})
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    callbacks = {button.text: button.callback_data for button in buttons}

    assert callbacks["13:00"] == "bk:f:2026-08-05:1300"
    assert callbacks["15:00"] == "bk:f:2026-08-05:1500"
    assert callbacks["✕ 13:30"] == "bk:busy"


def test_availability_uses_shared_backend(monkeypatch):
    calls = []

    def fake_api(path, **kwargs):
        calls.append((path, kwargs))
        return {
            "slots": [
                {"time": "13:00", "available": False},
                {"time": "15:00", "available": True},
            ]
        }

    monkeypatch.setattr(bot, "_booking_api_json", fake_api)
    available = asyncio.run(bot.available_booking_times("2026-08-05"))

    assert available == {"1500"}
    assert calls[0][0] == "/api/bookings/availability?date=2026-08-05"


def test_telegram_booking_is_authenticated_and_idempotent(monkeypatch):
    calls = []

    def fake_api(path, **kwargs):
        calls.append((path, kwargs))
        return {"id": "BOOKING123", "accepted": True}

    monkeypatch.setattr(bot, "_booking_api_json", fake_api)
    monkeypatch.setattr(bot, "BOOKING_BOT_SECRET", "shared-secret")
    bot.BOOKING_FLOWS.clear()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=123456789,
            username="chainyaguest",
            full_name="Анна",
        )
    )

    first = asyncio.run(
        bot.create_shared_booking(callback, "2026-08-05", "1300", "m", "2")
    )
    second = asyncio.run(
        bot.create_shared_booking(callback, "2026-08-05", "1300", "m", "2")
    )

    assert first == second == {"id": "BOOKING123", "accepted": True}
    assert calls[0][0] == "/api/bookings"
    assert calls[0][1]["payload"] == {
        "format": "master",
        "date": "2026-08-05",
        "time": "13:00",
        "guests": 2,
        "name": "Анна",
        "phone": "@chainyaguest",
        "note": "",
        "privacy_accepted": True,
        "source": "telegram",
    }
    assert calls[0][1]["headers"]["X-Booking-Bot-Secret"] == "shared-secret"
    assert calls[0][1]["headers"]["Idempotency-Key"] == calls[1][1]["headers"]["Idempotency-Key"]


def test_telegram_booking_fails_closed_without_secret(monkeypatch):
    monkeypatch.setattr(bot, "BOOKING_BOT_SECRET", "")
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123456789, username=None, full_name="Анна")
    )

    try:
        asyncio.run(
            bot.create_shared_booking(callback, "2026-08-05", "1300", "s", "1")
        )
    except bot.BookingApiError as exc:
        assert "BOOKING_BOT_SECRET" in str(exc)
    else:
        raise AssertionError("booking must fail closed without a shared secret")


def test_repair_is_available_from_main_menu():
    buttons = [button for row in bot.menu_kb().inline_keyboard for button in row]
    repair = next(button for button in buttons if button.callback_data == "rep:info")
    assert repair.text == "🏺 Кинцуги / ремонт посуды"
    assert "Окончательная цена" in bot.REPAIR_CAP
    assert len(bot.REPAIR_CAP) <= 1024


def test_telegram_repair_uses_shared_backend_and_uploads_photo(monkeypatch):
    json_calls = []
    image_calls = []

    def fake_json(path, **kwargs):
        json_calls.append((path, kwargs))
        return {"id": "REPAIR123", "accepted": True, "upload_required": True}

    def fake_image(path, data, content_type, headers=None):
        image_calls.append((path, data, content_type, headers))
        return {"id": "REPAIR123", "accepted": True, "image_received": True}

    monkeypatch.setattr(bot, "_booking_api_json", fake_json)
    monkeypatch.setattr(bot, "_repair_api_image", fake_image)
    monkeypatch.setattr(bot, "BOOKING_BOT_SECRET", "shared-secret")
    flow = {
        "name": "Анна",
        "phone": "+7 999 111-22-33",
        "description": "Треснула крышка чайника",
        "photo": b"jpeg-data",
        "photo_content_type": "image/jpeg",
        "request_flow": "fixed-flow",
    }

    result = asyncio.run(bot.create_shared_repair(123456789, flow))

    assert result["id"] == "REPAIR123"
    assert json_calls[0][0] == "/api/repair-requests"
    assert json_calls[0][1]["payload"] == {
        "name": "Анна",
        "phone": "+7 999 111-22-33",
        "description": "Треснула крышка чайника",
        "has_image": True,
        "upload_token": flow["upload_token"],
        "privacy_accepted": True,
    }
    assert json_calls[0][1]["headers"]["X-Booking-Bot-Secret"] == "shared-secret"
    assert json_calls[0][1]["headers"]["X-Telegram-User-ID"] == "123456789"
    assert json_calls[0][1]["headers"]["Idempotency-Key"].startswith("repair-telegram-")
    assert image_calls == [(
        "/api/repair-requests/REPAIR123/image",
        b"jpeg-data",
        "image/jpeg",
        {
            "X-Repair-Upload-Token": flow["upload_token"],
            "X-Booking-Bot-Secret": "shared-secret",
        },
    )]


def test_telegram_repair_without_photo_skips_upload(monkeypatch):
    image_calls = []

    monkeypatch.setattr(
        bot,
        "_booking_api_json",
        lambda path, **kwargs: {"id": "REPAIR456", "accepted": True, "upload_required": False},
    )
    monkeypatch.setattr(bot, "_repair_api_image", lambda *args, **kwargs: image_calls.append(args))
    monkeypatch.setattr(bot, "BOOKING_BOT_SECRET", "shared-secret")
    flow = {
        "name": "Иван",
        "phone": "+7 999 444-55-66",
        "description": "Скол на пиале",
        "request_flow": "fixed-flow",
    }

    result = asyncio.run(bot.create_shared_repair(42, flow))

    assert result["id"] == "REPAIR456"
    assert image_calls == []


def test_repair_photo_is_downloaded_only_when_submitting():
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def download(self, file_id, destination):
            self.calls.append(file_id)
            destination.write(b"telegram-jpeg")

    fake_bot = FakeBot()
    flow = {"photo_file_id": "telegram-file-123"}

    asyncio.run(bot.materialize_repair_photo(fake_bot, flow))

    assert fake_bot.calls == ["telegram-file-123"]
    assert flow["photo"] == b"telegram-jpeg"
    assert flow["photo_content_type"] == "image/jpeg"


def test_abandoned_repair_drafts_expire(monkeypatch):
    bot.REPAIR_FLOWS.clear()
    bot.REPAIR_FLOWS[7] = {"state": "photo", "updated_at": 100.0}

    bot.cleanup_repair_flows(now=100.0 + bot.REPAIR_FLOW_TTL_SECONDS + 1)

    assert 7 not in bot.REPAIR_FLOWS

"""Persistent, validated catalog storage for the storefront and owner panel."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
TYPE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SEED_IMAGE_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
MEDIA_FILE_RE = re.compile(r"^[a-f0-9]{32}\.webp$")
PARTNER_LOGO_RE = re.compile(r"^/img/partner-[a-z0-9-]{1,80}\.webp$")
LANGUAGES = ("ru", "en", "zh")
MAX_PRODUCT_IMAGES = 8
MAX_PARTNERS = 30
LEGACY_PARTNER_LOGOS = {
    "/img/partner-rolf.webp": "/img/partner-rolf-wordmark.webp",
    "/img/partner-relikta.webp": "/img/partner-relikta-emblem.webp",
}
CATALOG_TYPE_DEFAULTS = (
    ("white", "tea", "Белый чай", "White tea", "白茶"),
    ("green", "tea", "Зелёный чай", "Green tea", "绿茶"),
    ("gaba", "tea", "Габа", "GABA", "佳叶龙茶"),
    ("fujian", "tea", "Улуны Южной Фуцзяни", "Fujian oolong", "闽南乌龙"),
    ("dancong", "tea", "Улуны Гуандуна", "Guangdong oolong", "广东乌龙"),
    ("wuyi", "tea", "Улуны Уишаня", "Wuyi oolong", "武夷岩茶"),
    ("red", "tea", "Красный чай", "Black tea", "红茶"),
    ("sheng", "tea", "Шэн Пуэр", "Sheng pu-erh", "生普洱"),
    ("shu", "tea", "Шу Пуэр", "Shou pu-erh", "熟普洱"),
    ("heicha", "tea", "Хэй Ча", "Hei cha", "黑茶"),
    ("herbs", "tea", "Травы и добавки", "Herbs & extras", "花草与配料"),
    ("tea-sets", "tea", "Чайные наборы", "Tea sets", "茶叶礼盒"),
    ("teaware-teapots", "teaware", "Чайники", "Teapots", "茶壶"),
    ("teaware-gaiwans", "teaware", "Гайвани", "Gaiwans", "盖碗"),
    ("teaware-cups", "teaware", "Пиалы", "Tea cups", "茶杯"),
    ("teaware-chahai", "teaware", "Чахаи", "Chahai", "公道杯"),
    ("teaware-chahe", "teaware", "Чахэ", "Chahe", "茶荷"),
    ("teaware-figurines", "teaware", "Фигурки", "Tea pets", "茶宠"),
    ("teaware-tools", "teaware", "Инструменты", "Tea tools", "茶道工具"),
    ("teaware-sets", "teaware", "Наборы посуды", "Teaware sets", "茶具套装"),
)
TASTE_AXES = (
    "floral", "fruity", "driedfruit", "honey", "nutty",
    "roasted", "spicy", "woody", "herbal",
)
DEFAULT_PARTNERS = (
    {
        "id": "rolf",
        "published": True,
        "logo": "/img/partner-rolf-wordmark.webp",
        "translations": {
            "ru": {"name": "РОЛЬФ", "type": "Крупнейший автодилер"},
            "en": {"name": "ROLF", "type": "Russia's largest automotive retailer"},
            "zh": {"name": "ROLF", "type": "俄罗斯最大的汽车经销商"},
        },
    },
    {
        "id": "relikta",
        "published": True,
        "logo": "/img/partner-relikta-emblem.webp",
        "translations": {
            "ru": {"name": "Реликта", "type": "Винодельня"},
            "en": {"name": "Relikta", "type": "Winery"},
            "zh": {"name": "Relikta", "type": "酒庄"},
        },
    },
)


class CatalogError(ValueError):
    """The catalog or a proposed mutation is invalid."""


class CatalogConflict(CatalogError):
    """A mutation was based on a stale catalog revision."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"Поле {field} должно быть строкой")
    value = " ".join(value.split()) if field.endswith(".name") else value.strip()
    if required and not value:
        raise CatalogError(f"Поле {field} обязательно")
    if len(value) > limit:
        raise CatalogError(f"Поле {field} длиннее {limit} символов")
    return value


def normalize_item(raw: dict[str, Any], *, existing_id: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CatalogError("Товар должен быть объектом")
    item_id = _text(raw.get("id", existing_id or ""), "id", 80, required=True).lower()
    if existing_id and item_id != existing_id:
        raise CatalogError("Идентификатор существующего товара менять нельзя")
    if not ITEM_ID_RE.fullmatch(item_id):
        raise CatalogError("ID: латиница в нижнем регистре, цифры и дефисы")

    type_id = _text(raw.get("type", ""), "type", 40, required=True).lower()
    if not TYPE_ID_RE.fullmatch(type_id):
        raise CatalogError("Некорректная категория")
    try:
        price = int(raw.get("price"))
    except (TypeError, ValueError):
        raise CatalogError("Цена должна быть целым числом") from None
    if not 0 <= price <= 10_000_000:
        raise CatalogError("Цена вне допустимого диапазона")
    published = bool(raw.get("published", True))
    if published and price == 0:
        raise CatalogError("Для публикации укажите цену больше нуля")
    unit = raw.get("unit")
    if unit not in {"g", "pc"}:
        raise CatalogError("Единица должна быть g или pc")

    translations_raw = raw.get("translations")
    if not isinstance(translations_raw, dict):
        translations_raw = {
            "ru": {
                "name": raw.get("name", ""),
                "orig": raw.get("orig", ""),
                "desc": raw.get("desc", ""),
            }
        }
    translations: dict[str, dict[str, str]] = {}
    ru_raw = translations_raw.get("ru")
    if not isinstance(ru_raw, dict):
        ru_raw = {}
    for lang in LANGUAGES:
        source = translations_raw.get(lang)
        if source is None:
            source = {}
        if not isinstance(source, dict):
            raise CatalogError(f"Некорректный перевод {lang}")
        translations[lang] = {
            "name": _text(source.get("name", ""), f"{lang}.name", 160),
            "orig": _text(source.get("orig", ""), f"{lang}.orig", 240),
            "desc": _text(source.get("desc", ""), f"{lang}.desc", 5000),
            "composition": _text(source.get("composition", ""), f"{lang}.composition", 2000),
            "manufacturer": _text(source.get("manufacturer", ""), f"{lang}.manufacturer", 1000),
            "shelf_life": _text(source.get("shelf_life", ""), f"{lang}.shelf_life", 500),
            "storage": _text(source.get("storage", ""), f"{lang}.storage", 1000),
        }
    if not any(translation["name"] for translation in translations.values()):
        raise CatalogError("Укажите название хотя бы на одном языке")

    taste_raw = raw.get("taste", {})
    if not isinstance(taste_raw, dict):
        raise CatalogError("Вкусовой профиль должен быть объектом")
    taste: dict[str, int] = {}
    for axis in TASTE_AXES:
        try:
            value = int(taste_raw.get(axis, 0))
        except (TypeError, ValueError):
            raise CatalogError(f"Некорректное значение вкуса {axis}") from None
        if not 0 <= value <= 5:
            raise CatalogError(f"Значение вкуса {axis} должно быть от 0 до 5")
        taste[axis] = value

    image_raw = raw.get("image")
    if not isinstance(image_raw, dict):
        image_raw = {"kind": "seed", "name": raw.get("img", item_id)}
    images_raw = raw.get("images")
    if images_raw is None:
        images_raw = [image_raw]
    if not isinstance(images_raw, list) or not images_raw:
        raise CatalogError("Добавьте хотя бы одно изображение")
    if len(images_raw) > MAX_PRODUCT_IMAGES:
        raise CatalogError(f"Для одного товара можно добавить не более {MAX_PRODUCT_IMAGES} фото")
    images: list[dict[str, str]] = []
    seen_images: set[tuple[str, str]] = set()
    for index, entry in enumerate(images_raw):
        if not isinstance(entry, dict):
            raise CatalogError("Некорректное изображение")
        kind = entry.get("kind")
        name = _text(entry.get("name", ""), f"images.{index}.name", 140, required=True)
        if kind == "seed":
            if not SEED_IMAGE_RE.fullmatch(name):
                raise CatalogError("Некорректное имя встроенного изображения")
        elif kind == "uploaded":
            if not MEDIA_FILE_RE.fullmatch(name):
                raise CatalogError("Некорректное имя загруженного изображения")
        else:
            raise CatalogError("Неизвестный тип изображения")
        key = (kind, name)
        if key not in seen_images:
            images.append({"kind": kind, "name": name})
            seen_images.add(key)
    image = images[0]
    kind, name = image["kind"], image["name"]

    saby: dict[str, Any] | None = None
    saby_raw = raw.get("saby")
    if saby_raw is not None:
        if not isinstance(saby_raw, dict):
            raise CatalogError("Связь с Saby должна быть объектом")
        saby_id = saby_raw.get("id")
        if isinstance(saby_id, bool):
            raise CatalogError("Некорректный id номенклатуры Saby")
        try:
            saby_id = int(saby_id)
        except (TypeError, ValueError):
            raise CatalogError("Некорректный id номенклатуры Saby") from None
        if saby_id <= 0:
            raise CatalogError("Некорректный id номенклатуры Saby")
        external_id = _text(
            saby_raw.get("external_id", ""), "saby.external_id", 64, required=True
        )
        try:
            UUID(external_id)
        except (ValueError, AttributeError) as exc:
            raise CatalogError("Некорректный externalId номенклатуры Saby") from exc
        saby = {
            "id": saby_id,
            "external_id": external_id,
            "image_pending": bool(saby_raw.get("image_pending", False)),
        }
        if published and saby["image_pending"]:
            raise CatalogError("Перед публикацией товара из Saby загрузите его фотографию")

    ru = translations["ru"]
    display = next(
        (translation for translation in translations.values() if translation["name"]),
        ru,
    )
    item = {
        "id": item_id,
        "type": type_id,
        "name": display["name"],
        "orig": ru["orig"] or display["orig"],
        "desc": ru["desc"] or display["desc"],
        "price": price,
        "unit": unit,
        "stock": bool(raw.get("stock", True)),
        "published": published,
        "img": name if kind == "seed" else item_id,
        "image": image,
        "images": images,
        "taste": taste,
        "translations": translations,
    }
    if saby is not None:
        item["saby"] = saby
    return item


def normalize_partner(raw: dict[str, Any], *, existing_id: str | None = None) -> dict[str, Any]:
    """Validate an editable public partner without making translations blocking."""
    if not isinstance(raw, dict):
        raise CatalogError("Партнёр должен быть объектом")
    partner_id = _text(raw.get("id", existing_id or ""), "id", 80, required=True).lower()
    if existing_id and partner_id != existing_id:
        raise CatalogError("Идентификатор существующего партнёра менять нельзя")
    if not ITEM_ID_RE.fullmatch(partner_id):
        raise CatalogError("ID: латиница в нижнем регистре, цифры и дефисы")

    translations_raw = raw.get("translations")
    if not isinstance(translations_raw, dict):
        translations_raw = {
            "ru": {"name": raw.get("name", ""), "type": raw.get("type", "")}
        }
    translations: dict[str, dict[str, str]] = {}
    for language in LANGUAGES:
        source = translations_raw.get(language) or {}
        if not isinstance(source, dict):
            raise CatalogError(f"Некорректный перевод {language}")
        translations[language] = {
            "name": _text(source.get("name", ""), f"{language}.name", 160),
            "type": _text(source.get("type", ""), f"{language}.type", 240),
        }
    if partner_id == "rolf":
        legacy_types = {
            "ru": ("Автомобильная группа", "Крупнейший автодилер"),
            "en": ("Automotive group", "Russia's largest automotive retailer"),
            "zh": ("汽车集团", "俄罗斯最大的汽车经销商"),
        }
        for language, (legacy, current) in legacy_types.items():
            if translations[language]["type"] == legacy:
                translations[language]["type"] = current
    if not any(value["name"] for value in translations.values()):
        raise CatalogError("Укажите название партнёра хотя бы на одном языке")
    default_logo = next(
        (partner.get("logo", "") for partner in DEFAULT_PARTNERS if partner["id"] == partner_id),
        "",
    )
    logo = _text(raw.get("logo", default_logo), "logo", 160)
    logo = LEGACY_PARTNER_LOGOS.get(logo, logo)
    if logo and not PARTNER_LOGO_RE.fullmatch(logo):
        raise CatalogError("Некорректный путь к логотипу партнёра")
    return {
        "id": partner_id,
        "published": bool(raw.get("published", False)),
        "logo": logo,
        "translations": translations,
    }


def normalize_document(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("teas"), list):
        raise CatalogError("В каталоге отсутствует список teas")
    teas = [normalize_item(item) for item in raw["teas"]]
    ids = [item["id"] for item in teas]
    if len(ids) != len(set(ids)):
        raise CatalogError("ID товаров не должны повторяться")
    saby_links = [item["saby"] for item in teas if isinstance(item.get("saby"), dict)]
    if len({link["id"] for link in saby_links}) != len(saby_links):
        raise CatalogError("Одна позиция Saby не может быть связана с несколькими товарами")
    if len({link["external_id"] for link in saby_links}) != len(saby_links):
        raise CatalogError("Одна позиция Saby не может быть связана с несколькими товарами")
    types = raw.get("types", [])
    if not isinstance(types, list):
        raise CatalogError("Некорректный список категорий")
    defaults = {
        type_id: {
            "id": type_id,
            "name": ru,
            "group": group,
            "names": {"ru": ru, "en": en, "zh": zh},
        }
        for type_id, group, ru, en, zh in CATALOG_TYPE_DEFAULTS
    }
    clean_types = []
    for entry in types:
        if not isinstance(entry, dict):
            raise CatalogError("Некорректная категория")
        type_id = _text(entry.get("id", ""), "type.id", 40, required=True).lower()
        if not TYPE_ID_RE.fullmatch(type_id):
            raise CatalogError("Некорректный ID категории")
        default = defaults.get(type_id, {})
        group = _text(entry.get("group", default.get("group", "tea")), "type.group", 20, required=True)
        if group not in {"tea", "teaware"}:
            raise CatalogError("Некорректная группа категории")
        names_raw = entry.get("names") if isinstance(entry.get("names"), dict) else {}
        name = _text(entry.get("name", default.get("name", "")), "type.name", 120, required=True)
        clean_types.append({
            "id": type_id,
            "name": name,
            "group": group,
            "names": {
                language: _text(
                    names_raw.get(language, default.get("names", {}).get(language, name)),
                    f"type.names.{language}", 120, required=True,
                )
                for language in LANGUAGES
            },
        })
    present_types = {entry["id"] for entry in clean_types}
    clean_types.extend(
        copy.deepcopy(defaults[type_id])
        for type_id, *_rest in CATALOG_TYPE_DEFAULTS
        if type_id not in present_types
    )
    known_types = {entry["id"] for entry in clean_types}
    missing = sorted({item["type"] for item in teas} - known_types)
    if missing:
        raise CatalogError("Неизвестные категории: " + ", ".join(missing))
    partners_raw = raw.get(
        "partners", [copy.deepcopy(partner) for partner in DEFAULT_PARTNERS]
    )
    if not isinstance(partners_raw, list):
        raise CatalogError("Некорректный список партнёров")
    if len(partners_raw) > MAX_PARTNERS:
        raise CatalogError(f"Можно добавить не более {MAX_PARTNERS} партнёров")
    partners = [normalize_partner(partner) for partner in partners_raw]
    partner_ids = [partner["id"] for partner in partners]
    if len(partner_ids) != len(set(partner_ids)):
        raise CatalogError("ID партнёров не должны повторяться")
    revision = raw.get("revision", 1)
    if not isinstance(revision, int) or revision < 1:
        revision = 1
    return {
        "schema_version": 5,
        "revision": revision,
        "updated_at": str(raw.get("updated_at") or utc_now()),
        "types": clean_types,
        "axes": copy.deepcopy(raw.get("axes", [])),
        "packs": copy.deepcopy(raw.get("packs", [10, 25, 50, 100])),
        "teas": teas,
        "partners": partners,
    }


class CatalogStore:
    """JSON store with process-local locking and atomic replacement."""

    def __init__(self, path: Path, seed_path: Path, media_dir: Path):
        self.path = path
        self.seed_path = seed_path
        self.media_dir = media_dir
        self._lock = threading.RLock()

    def ensure(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.media_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.exists():
                self._read()
                return
            try:
                seed = json.loads(self.seed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CatalogError(f"Не удалось прочитать исходный каталог: {exc}") from exc
            document = normalize_document(seed)
            document["revision"] = 1
            document["updated_at"] = utc_now()
            self._write(document)

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Не удалось прочитать каталог: {exc}") from exc
        return normalize_document(raw)

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temp_name = tempfile.mkstemp(prefix=".catalog-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def get(self) -> dict[str, Any]:
        with self._lock:
            self.ensure()
            return copy.deepcopy(self._read())

    @staticmethod
    def _require_revision(document: dict[str, Any], supplied: int) -> None:
        if supplied != document["revision"]:
            raise CatalogConflict("Каталог уже изменён в другой вкладке. Обновите страницу.")

    def _finish(self, document: dict[str, Any]) -> dict[str, Any]:
        document["revision"] += 1
        document["updated_at"] = utc_now()
        normalized = normalize_document(document)
        self._write(normalized)
        return copy.deepcopy(normalized)

    def create_item(self, raw: dict[str, Any], revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            item = normalize_item(raw)
            if any(current["id"] == item["id"] for current in document["teas"]):
                raise CatalogError("Товар с таким ID уже существует")
            document["teas"].append(item)
            return self._finish(document)

    def update_item(self, item_id: str, raw: dict[str, Any], revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            for index, current in enumerate(document["teas"]):
                if current["id"] == item_id:
                    document["teas"][index] = normalize_item(raw, existing_id=item_id)
                    return self._finish(document)
            raise KeyError(item_id)

    def set_image(self, item_id: str, filename: str, revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            for current in document["teas"]:
                if current["id"] == item_id:
                    uploaded = {"kind": "uploaded", "name": filename}
                    current["images"] = [uploaded]
                    current["image"] = uploaded
                    current["img"] = item_id
                    if isinstance(current.get("saby"), dict):
                        current["saby"]["image_pending"] = False
                    return self._finish(document)
            raise KeyError(item_id)

    def add_image(
        self, item_id: str, filename: str, revision: int, *, make_primary: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            uploaded = {"kind": "uploaded", "name": filename}
            for current in document["teas"]:
                if current["id"] != item_id:
                    continue
                images = [
                    image for image in current.get("images", [current["image"]])
                    if image != uploaded
                ]
                if make_primary:
                    images.insert(0, uploaded)
                else:
                    images.append(uploaded)
                if len(images) > MAX_PRODUCT_IMAGES:
                    raise CatalogError(
                        f"Для одного товара можно добавить не более {MAX_PRODUCT_IMAGES} фото"
                    )
                current["images"] = images
                current["image"] = images[0]
                current["img"] = item_id if images[0]["kind"] == "uploaded" else images[0]["name"]
                if isinstance(current.get("saby"), dict):
                    current["saby"]["image_pending"] = False
                return self._finish(document)
            raise KeyError(item_id)

    def set_primary_image(self, item_id: str, index: int, revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            for current in document["teas"]:
                if current["id"] != item_id:
                    continue
                images = list(current.get("images", [current["image"]]))
                if index < 0 or index >= len(images):
                    raise CatalogError("Фотография не найдена")
                image = images.pop(index)
                images.insert(0, image)
                current["images"] = images
                current["image"] = image
                current["img"] = item_id if image["kind"] == "uploaded" else image["name"]
                return self._finish(document)
            raise KeyError(item_id)

    def remove_image(self, item_id: str, index: int, revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            for current in document["teas"]:
                if current["id"] != item_id:
                    continue
                images = list(current.get("images", [current["image"]]))
                if index < 0 or index >= len(images):
                    raise CatalogError("Фотография не найдена")
                if len(images) == 1:
                    raise CatalogError("У товара должна остаться хотя бы одна фотография")
                images.pop(index)
                current["images"] = images
                current["image"] = images[0]
                current["img"] = item_id if images[0]["kind"] == "uploaded" else images[0]["name"]
                return self._finish(document)
            raise KeyError(item_id)

    def reorder(self, item_ids: list[str], revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            current = {item["id"]: item for item in document["teas"]}
            if len(item_ids) != len(set(item_ids)) or set(item_ids) != set(current):
                raise CatalogError("Порядок должен содержать каждый товар ровно один раз")
            document["teas"] = [current[item_id] for item_id in item_ids]
            return self._finish(document)

    def create_partner(self, raw: dict[str, Any], revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            partner = normalize_partner(raw)
            if any(current["id"] == partner["id"] for current in document["partners"]):
                raise CatalogError("Партнёр с таким ID уже существует")
            if len(document["partners"]) >= MAX_PARTNERS:
                raise CatalogError(f"Можно добавить не более {MAX_PARTNERS} партнёров")
            document["partners"].append(partner)
            return self._finish(document)

    def update_partner(
        self, partner_id: str, raw: dict[str, Any], revision: int
    ) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            for index, current in enumerate(document["partners"]):
                if current["id"] == partner_id:
                    document["partners"][index] = normalize_partner(
                        raw, existing_id=partner_id
                    )
                    return self._finish(document)
            raise KeyError(partner_id)

    def reorder_partners(self, partner_ids: list[str], revision: int) -> dict[str, Any]:
        with self._lock:
            document = self.get()
            self._require_revision(document, revision)
            current = {partner["id"]: partner for partner in document["partners"]}
            if len(partner_ids) != len(set(partner_ids)) or set(partner_ids) != set(current):
                raise CatalogError("Порядок должен содержать каждого партнёра ровно один раз")
            document["partners"] = [current[partner_id] for partner_id in partner_ids]
            return self._finish(document)


def image_url(item: dict[str, Any]) -> str:
    image = item.get("image", {})
    if image.get("kind") == "uploaded":
        return f"/catalog-media/{image['name']}"
    return f"/img/{image.get('name', item['id'])}.webp"


def image_urls(item: dict[str, Any]) -> list[str]:
    return [image_url({**item, "image": image}) for image in item.get("images", [item.get("image", {})])]

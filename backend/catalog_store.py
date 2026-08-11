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
LANGUAGES = ("ru", "en", "zh")
TASTE_AXES = (
    "floral", "fruity", "driedfruit", "honey", "nutty",
    "roasted", "spicy", "woody", "herbal",
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
        raise CatalogError("Русский перевод обязателен")
    for lang in LANGUAGES:
        source = translations_raw.get(lang)
        if source is None:
            source = ru_raw
        if not isinstance(source, dict):
            raise CatalogError(f"Некорректный перевод {lang}")
        translations[lang] = {
            "name": _text(source.get("name", ""), f"{lang}.name", 160, required=True),
            "orig": _text(source.get("orig", ""), f"{lang}.orig", 240),
            "desc": _text(source.get("desc", ""), f"{lang}.desc", 5000),
            "composition": _text(source.get("composition", ""), f"{lang}.composition", 2000),
            "manufacturer": _text(source.get("manufacturer", ""), f"{lang}.manufacturer", 1000),
            "shelf_life": _text(source.get("shelf_life", ""), f"{lang}.shelf_life", 500),
            "storage": _text(source.get("storage", ""), f"{lang}.storage", 1000),
        }

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
    kind = image_raw.get("kind")
    name = _text(image_raw.get("name", ""), "image.name", 140, required=True)
    if kind == "seed":
        if not SEED_IMAGE_RE.fullmatch(name):
            raise CatalogError("Некорректное имя встроенного изображения")
    elif kind == "uploaded":
        if not MEDIA_FILE_RE.fullmatch(name):
            raise CatalogError("Некорректное имя загруженного изображения")
    else:
        raise CatalogError("Неизвестный тип изображения")

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
    item = {
        "id": item_id,
        "type": type_id,
        "name": ru["name"],
        "orig": ru["orig"],
        "desc": ru["desc"],
        "price": price,
        "unit": unit,
        "stock": bool(raw.get("stock", True)),
        "published": published,
        "img": name if kind == "seed" else item_id,
        "image": {"kind": kind, "name": name},
        "taste": taste,
        "translations": translations,
    }
    if saby is not None:
        item["saby"] = saby
    return item


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
    clean_types = []
    for entry in types:
        if not isinstance(entry, dict):
            raise CatalogError("Некорректная категория")
        type_id = _text(entry.get("id", ""), "type.id", 40, required=True).lower()
        if not TYPE_ID_RE.fullmatch(type_id):
            raise CatalogError("Некорректный ID категории")
        clean_types.append({"id": type_id, "name": _text(entry.get("name", ""), "type.name", 120, required=True)})
    known_types = {entry["id"] for entry in clean_types}
    missing = sorted({item["type"] for item in teas} - known_types)
    if missing:
        raise CatalogError("Неизвестные категории: " + ", ".join(missing))
    revision = raw.get("revision", 1)
    if not isinstance(revision, int) or revision < 1:
        revision = 1
    return {
        "schema_version": 3,
        "revision": revision,
        "updated_at": str(raw.get("updated_at") or utc_now()),
        "types": clean_types,
        "axes": copy.deepcopy(raw.get("axes", [])),
        "packs": copy.deepcopy(raw.get("packs", [10, 25, 50, 100])),
        "teas": teas,
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
                    current["image"] = {"kind": "uploaded", "name": filename}
                    current["img"] = item_id
                    if isinstance(current.get("saby"), dict):
                        current["saby"]["image_pending"] = False
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


def image_url(item: dict[str, Any]) -> str:
    image = item.get("image", {})
    if image.get("kind") == "uploaded":
        return f"/catalog-media/{image['name']}"
    return f"/img/{image.get('name', item['id'])}.webp"

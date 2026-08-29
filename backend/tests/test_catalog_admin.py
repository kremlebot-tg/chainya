from io import BytesIO

from PIL import Image

from backend.catalog_store import (
    CATALOG_TYPE_DEFAULTS,
    normalize_document,
    normalize_partner,
)
from backend.tests.test_orders import app_client

AUTH = {
    "Authorization": "Bearer test-admin-token",
    "X-Chainya-Admin": "catalog",
}
SITE_AUTH = {
    "Authorization": "Bearer test-admin-token",
    "X-Chainya-Admin": "site",
}


def test_owner_requested_tea_categories_are_added_to_legacy_catalogs():
    normalized = normalize_document({"types": [], "teas": []})
    by_id = {item[0]: item for item in CATALOG_TYPE_DEFAULTS}
    public_by_id = {item["id"]: item for item in normalized["types"]}

    assert by_id["yellow"] == ("yellow", "tea", "Жёлтый чай", "Yellow tea", "黄茶")
    assert by_id["taiwan"] == (
        "taiwan", "tea", "Тайваньские улуны", "Taiwanese oolong", "台湾乌龙",
    )
    assert public_by_id["yellow"]["names"]["ru"] == "Жёлтый чай"
    assert public_by_id["taiwan"]["names"]["en"] == "Taiwanese oolong"


def test_legacy_partner_logo_paths_are_migrated_without_cache_collision():
    legacy = {
        "id": "rolf",
        "published": True,
        "logo": "/img/partner-rolf.webp",
        "translations": {
            "ru": {"name": "РОЛЬФ", "type": "Крупнейший автодилер"},
            "en": {"name": "ROLF", "type": "Russia's largest automotive retailer"},
            "zh": {"name": "ROLF", "type": "俄罗斯最大的汽车经销商"},
        },
    }

    assert normalize_partner(legacy)["logo"] == "/img/partner-rolf-wordmark.webp"


def test_owner_catalog_page_uses_existing_admin_session(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        anonymous = client.get("/manage/catalog")
        login = client.post("/api/admin/session", json={"token": "test-admin-token"})
        authenticated = client.get("/manage/catalog")
        script = client.get("/manage/catalog.js")
        script_head = client.head("/manage/catalog.js")

    assert login.status_code == 204
    assert "Вход владельца" in anonymous.text
    assert "Управление витриной" in authenticated.text
    assert script.status_code == 200
    assert script_head.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "Что добавляем?" in authenticated.text
    assert "function completionState(item)" in script.text
    assert module.ADMIN_TOKEN not in authenticated.text
    assert module.ADMIN_TOKEN not in script.text


def test_owner_catalog_script_is_not_public(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        anonymous = client.get("/manage/catalog.js")
        anonymous_head = client.head("/manage/catalog.js")

    assert anonymous.status_code == 404
    assert anonymous_head.status_code == 404


def test_owner_site_editor_uses_existing_session_and_keeps_script_private(
    tmp_path, monkeypatch
):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        anonymous_page = client.get("/manage/site")
        anonymous_script = client.get("/manage/site.js")
        login = client.post("/api/admin/session", json={"token": "test-admin-token"})
        page = client.get("/manage/site")
        script = client.get("/manage/site.js")
        script_head = client.head("/manage/site.js")

    assert login.status_code == 204
    assert "Вход владельца" in anonymous_page.text
    assert anonymous_script.status_code == 404
    assert page.status_code == 200
    assert "Страницы без разработчика" in page.text
    assert "Добавить партнёра" in page.text
    assert "Ничего не удаляется безвозвратно" in page.text
    assert script.status_code == 200
    assert script_head.status_code == 200
    assert "async function savePartner()" in script.text
    assert module.ADMIN_TOKEN not in page.text
    assert module.ADMIN_TOKEN not in script.text


def test_partner_editor_create_publish_hide_reorder_and_history(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    new_partner = {
        "id": "new-partner",
        "published": True,
        "translations": {
            "ru": {"name": "Новый партнёр", "type": "Гостеприимство"},
            "en": {"name": "", "type": ""},
            "zh": {"name": "", "type": ""},
        },
    }
    with client:
        initial = client.get("/api/admin/site/partners", headers=SITE_AUTH)
        missing_marker = client.post(
            "/api/admin/site/partners",
            headers={"Authorization": SITE_AUTH["Authorization"]},
            json={"revision": initial.json()["revision"], "item": new_partner},
        )
        created = client.post(
            "/api/admin/site/partners",
            headers=SITE_AUTH,
            json={"revision": initial.json()["revision"], "item": new_partner},
        )
        public_after_create = client.get("/api/catalog").json()["partners"]
        stale = client.put(
            "/api/admin/site/partners/new-partner",
            headers=SITE_AUTH,
            json={"revision": initial.json()["revision"], "item": new_partner},
        )
        hidden_partner = next(
            item for item in created.json()["partners"] if item["id"] == "new-partner"
        )
        hidden_partner["published"] = False
        hidden = client.put(
            "/api/admin/site/partners/new-partner",
            headers=SITE_AUTH,
            json={"revision": created.json()["revision"], "item": hidden_partner},
        )
        ids = [item["id"] for item in hidden.json()["partners"]]
        ids.insert(0, ids.pop(ids.index("new-partner")))
        reordered = client.put(
            "/api/admin/site/partner-order",
            headers=SITE_AUTH,
            json={"revision": hidden.json()["revision"], "ids": ids},
        )
        site_history = client.get("/api/admin/site/history", headers=SITE_AUTH)
        catalog_history = client.get("/api/admin/catalog/history", headers=AUTH)

    assert initial.status_code == 200
    assert [item["id"] for item in initial.json()["partners"]] == ["rolf", "relikta"]
    rolf = initial.json()["partners"][0]
    assert rolf["logo"] == "/img/partner-rolf-wordmark.webp"
    assert rolf["translations"]["ru"]["type"] == "Крупнейший автодилер"
    assert initial.json()["partners"][1]["logo"] == "/img/partner-relikta-emblem.webp"
    assert missing_marker.status_code == 403
    assert created.status_code == 201
    assert next(item for item in public_after_create if item["id"] == "new-partner")[
        "translations"
    ]["en"]["name"] == ""
    assert stale.status_code == 409
    assert hidden.status_code == 200
    assert all(
        item["id"] != "new-partner" for item in client.get("/api/catalog").json()["partners"]
    )
    assert reordered.status_code == 200
    assert reordered.json()["partners"][0]["id"] == "new-partner"
    assert [item["action"] for item in site_history.json()["history"][:3]] == [
        "partner_reorder", "partner_update", "partner_create",
    ]
    assert site_history.json()["history"][1]["item_name"] == "Новый партнёр"
    assert all(not item["action"].startswith("partner_") for item in catalog_history.json()["history"])


def test_partner_editor_rejects_duplicate_ids_and_all_blank_names(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        initial = client.get("/api/admin/site/partners", headers=SITE_AUTH).json()
        duplicate = client.post(
            "/api/admin/site/partners",
            headers=SITE_AUTH,
            json={"revision": initial["revision"], "item": initial["partners"][0]},
        )
        blank = {
            "id": "blank-partner",
            "published": False,
            "translations": {
                language: {"name": "", "type": ""}
                for language in ("ru", "en", "zh")
            },
        }
        blank_response = client.post(
            "/api/admin/site/partners",
            headers=SITE_AUTH,
            json={"revision": initial["revision"], "item": blank},
        )

    assert duplicate.status_code == 422
    assert "уже существует" in duplicate.json()["detail"]
    assert blank_response.status_code == 422
    assert "хотя бы на одном языке" in blank_response.json()["detail"]


def test_owner_guides_use_existing_admin_session_and_explain_safe_boundaries(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        anonymous = client.get("/manage/guides")
        login = client.post("/api/admin/session", json={"token": "test-admin-token"})
        authenticated = client.get("/manage/guides")

    assert login.status_code == 204
    assert "Вход владельца" in anonymous.text
    assert "Как управлять заказами" in authenticated.text
    assert "СБИС: каталог и чеки покупок" in authenticated.text
    assert "полный расчёт" in authenticated.text
    assert "Один чек, продажа и складской учёт" in authenticated.text
    assert "Сверка каталога работает только на чтение" in authenticated.text
    assert "возврат — реальная денежная операция" in authenticated.text
    assert module.ADMIN_TOKEN not in authenticated.text


def test_catalog_admin_create_update_reorder_and_conflict(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        initial_response = client.get("/api/admin/catalog", headers=AUTH)
        initial = initial_response.json()
        item = dict(initial["teas"][0])
        item["id"] = "new-safe-tea"
        item["type"] = "yellow"
        item["published"] = True
        item["stock"] = True
        item["translations"] = {
            language: {
                **translation,
                "name": {"ru": "Новый чай", "en": "New tea", "zh": "新茶"}[language],
                "composition": {"ru": "Чайный лист", "en": "Tea leaves", "zh": "茶叶"}[language],
                "manufacturer": "ИП Давтян А. К.",
                "shelf_life": "24 месяца",
                "storage": "Хранить в сухом месте",
            }
            for language, translation in item["translations"].items()
        }

        missing_csrf = client.post(
            "/api/admin/catalog/items",
            headers={"Authorization": AUTH["Authorization"]},
            json={"revision": initial["revision"], "item": item},
        )
        created_response = client.post(
            "/api/admin/catalog/items",
            headers=AUTH,
            json={"revision": initial["revision"], "item": item},
        )
        created = created_response.json()
        public = client.get("/api/catalog").json()
        stale = client.put(
            "/api/admin/catalog/items/new-safe-tea",
            headers=AUTH,
            json={"revision": initial["revision"], "item": item},
        )

        updated_item = next(tea for tea in created["teas"] if tea["id"] == "new-safe-tea")
        updated_item["published"] = False
        hidden_response = client.put(
            "/api/admin/catalog/items/new-safe-tea",
            headers=AUTH,
            json={"revision": created["revision"], "item": updated_item},
        )
        hidden = hidden_response.json()
        ids = [tea["id"] for tea in hidden["teas"]]
        ids.insert(0, ids.pop(ids.index("new-safe-tea")))
        reordered = client.put(
            "/api/admin/catalog/order",
            headers=AUTH,
            json={"revision": hidden["revision"], "ids": ids},
        )
        anonymous_history = client.get("/api/admin/catalog/history")
        history = client.get(
            "/api/admin/catalog/history", params={"limit": 3}, headers=AUTH
        )

    assert initial_response.status_code == 200
    assert missing_csrf.status_code == 403
    assert created_response.status_code == 201
    public_item = next(tea for tea in public["teas"] if tea["id"] == "new-safe-tea")
    assert public_item["type"] == "yellow"
    assert public_item["translations"]["ru"]["composition"] == "Чайный лист"
    assert public_item["translations"]["ru"]["manufacturer"] == "ИП Давтян А. К."
    assert public_item["translations"]["ru"]["shelf_life"] == "24 месяца"
    assert public_item["translations"]["ru"]["storage"] == "Хранить в сухом месте"
    assert stale.status_code == 409
    assert hidden_response.status_code == 200
    assert reordered.status_code == 200
    assert reordered.json()["teas"][0]["id"] == "new-safe-tea"
    assert all(tea["id"] != "new-safe-tea" for tea in client.get("/api/catalog").json()["teas"])
    assert anonymous_history.status_code == 401
    assert history.status_code == 200
    assert [row["action"] for row in history.json()["history"]] == [
        "reorder",
        "update",
        "create",
    ]
    assert history.json()["history"][1]["item_name"] == "Новый чай"
    assert all("token" not in row for row in history.json()["history"])


def test_owner_can_create_edit_reorder_and_delete_empty_category(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    category = {
        "id": "author-tea",
        "group": "tea",
        "name": "Авторский чай",
        "names": {"ru": "Авторский чай", "en": "Signature tea", "zh": "特色茶"},
    }
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        missing_marker = client.post(
            "/api/admin/catalog/types",
            headers={"Authorization": AUTH["Authorization"]},
            json={"revision": initial["revision"], "item": category},
        )
        created_response = client.post(
            "/api/admin/catalog/types",
            headers=AUTH,
            json={"revision": initial["revision"], "item": category},
        )
        created = created_response.json()
        public = client.get("/api/catalog").json()
        editable = next(item for item in created["types"] if item["id"] == "author-tea")
        editable["names"]["ru"] = "Авторская коллекция"
        editable["name"] = "Авторская коллекция"
        updated_response = client.put(
            "/api/admin/catalog/types/author-tea",
            headers=AUTH,
            json={"revision": created["revision"], "item": editable},
        )
        updated = updated_response.json()
        ids = [item["id"] for item in updated["types"]]
        ids.insert(0, ids.pop(ids.index("author-tea")))
        reordered_response = client.put(
            "/api/admin/catalog/type-order",
            headers=AUTH,
            json={"revision": updated["revision"], "ids": ids},
        )
        reordered = reordered_response.json()
        builtin_delete = client.delete(
            f"/api/admin/catalog/types/white?revision={reordered['revision']}",
            headers=AUTH,
        )
        deleted_response = client.delete(
            f"/api/admin/catalog/types/author-tea?revision={reordered['revision']}",
            headers=AUTH,
        )
        history = client.get(
            "/api/admin/catalog/history", params={"limit": 4}, headers=AUTH
        ).json()["history"]

    assert missing_marker.status_code == 403
    assert created_response.status_code == 201
    admin_category = next(item for item in created["types"] if item["id"] == "author-tea")
    assert admin_category["system"] is False
    public_category = next(item for item in public["types"] if item["id"] == "author-tea")
    assert "system" not in public_category
    assert public_category["names"]["zh"] == "特色茶"
    assert updated_response.status_code == 200
    assert next(item for item in updated["types"] if item["id"] == "author-tea")["name"] == "Авторская коллекция"
    assert reordered_response.status_code == 200
    assert reordered["types"][0]["id"] == "author-tea"
    assert builtin_delete.status_code == 422
    assert deleted_response.status_code == 200
    assert all(item["id"] != "author-tea" for item in deleted_response.json()["types"])
    assert [item["action"] for item in history] == [
        "category_delete", "category_reorder", "category_update", "category_create",
    ]


def test_category_with_products_cannot_be_deleted_or_change_group(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    category = {
        "id": "limited-tea",
        "group": "tea",
        "name": "Лимитированный чай",
        "names": {"ru": "Лимитированный чай", "en": "Limited tea", "zh": "限量茶"},
    }
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        created = client.post(
            "/api/admin/catalog/types",
            headers=AUTH,
            json={"revision": initial["revision"], "item": category},
        ).json()
        product = dict(created["teas"][0])
        product["id"] = "limited-product"
        product["type"] = "limited-tea"
        product.pop("saby", None)
        product_created = client.post(
            "/api/admin/catalog/items",
            headers=AUTH,
            json={"revision": created["revision"], "item": product},
        ).json()
        category["group"] = "teaware"
        changed_group = client.put(
            "/api/admin/catalog/types/limited-tea",
            headers=AUTH,
            json={"revision": product_created["revision"], "item": category},
        )
        deleted = client.delete(
            f"/api/admin/catalog/types/limited-tea?revision={product_created['revision']}",
            headers=AUTH,
        )

    assert changed_group.status_code == 422
    assert "перенесите товары" in changed_group.json()["detail"]
    assert deleted.status_code == 422
    assert "перенесите товары" in deleted.json()["detail"]


def test_catalog_image_is_reencoded_and_served_immutably(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    source = BytesIO()
    image = Image.new("RGB", (320, 240), (170, 80, 45))
    image.save(source, "PNG", pnginfo=None)

    with client:
        catalog = client.get("/api/admin/catalog", headers=AUTH).json()
        item_id = catalog["teas"][0]["id"]
        uploaded = client.post(
            f"/api/admin/catalog/items/{item_id}/image?revision={catalog['revision']}",
            headers={**AUTH, "Content-Type": "image/png"},
            content=source.getvalue(),
        )
        invalid = client.post(
            f"/api/admin/catalog/items/{item_id}/image?revision={uploaded.json()['revision']}",
            headers={**AUTH, "Content-Type": "image/png"},
            content=b"not-an-image",
        )
        image_url = next(
            tea for tea in uploaded.json()["teas"] if tea["id"] == item_id
        )["image_url"]
        served = client.get(image_url)
        head = client.head(image_url)

    assert uploaded.status_code == 200
    assert image_url.startswith("/catalog-media/")
    assert invalid.status_code == 422
    assert served.status_code == 200
    assert head.status_code == 200
    assert head.headers["content-type"] == "image/webp"
    assert served.headers["content-type"] == "image/webp"
    assert "immutable" in served.headers["cache-control"]
    with Image.open(BytesIO(served.content)) as stored:
        assert stored.format == "WEBP"
        assert stored.size == (320, 240)
    assert (module.CATALOG_MEDIA_DIR / image_url.rsplit("/", 1)[1]).is_file()


def test_saby_import_stays_hidden_until_real_photo_is_uploaded(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    external_id = "11111111-1111-4111-8111-111111111111"
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        item = dict(initial["teas"][0])
        item.update({
            "id": "saby-owner-draft",
            "published": False,
            "saby": {"id": 777, "external_id": external_id, "image_pending": True},
        })
        created = client.post(
            "/api/admin/catalog/items", headers=AUTH,
            json={"revision": initial["revision"], "item": item},
        )
        document = created.json()
        draft = next(row for row in document["teas"] if row["id"] == "saby-owner-draft")
        draft["published"] = True
        blocked = client.put(
            "/api/admin/catalog/items/saby-owner-draft", headers=AUTH,
            json={"revision": document["revision"], "item": draft},
        )

        source = BytesIO()
        Image.new("RGB", (160, 120), (80, 100, 60)).save(source, "PNG")
        uploaded = client.post(
            f"/api/admin/catalog/items/saby-owner-draft/image?revision={document['revision']}",
            headers={**AUTH, "Content-Type": "image/png"}, content=source.getvalue(),
        )
        uploaded_document = uploaded.json()
        ready = next(row for row in uploaded_document["teas"] if row["id"] == "saby-owner-draft")
        ready["translations"] = {
            language: {
                **translation,
                "orig": translation["orig"] or "Китай",
                "desc": translation["desc"] or "Описание чая",
                "composition": "Чайный лист",
                "manufacturer": "Изготовитель указан владельцем",
                "shelf_life": "24 месяца",
                "storage": "Хранить в сухом месте",
            }
            for language, translation in ready["translations"].items()
        }
        ready["published"] = True
        published = client.put(
            "/api/admin/catalog/items/saby-owner-draft", headers=AUTH,
            json={"revision": uploaded_document["revision"], "item": ready},
        )

    assert created.status_code == 201
    assert blocked.status_code == 422
    assert "фотограф" in blocked.json()["detail"]
    assert uploaded.status_code == 200
    assert ready["saby"] == {
        "id": 777, "external_id": external_id, "image_pending": False,
    }
    assert published.status_code == 200


def test_catalog_blocks_zero_price_but_allows_incomplete_first_publication(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        source = dict(initial["teas"][0])

        zero_price = dict(source)
        zero_price["id"] = "zero-price-tea"
        zero_price["price"] = 0
        zero_response = client.post(
            "/api/admin/catalog/items",
            headers=AUTH,
            json={"revision": initial["revision"], "item": zero_price},
        )

        hidden = dict(source)
        hidden["id"] = "incomplete-hidden-tea"
        hidden["published"] = False
        hidden["price"] = 0
        hidden["translations"] = {
            language: {
                **translation,
                "orig": "",
                "desc": "",
                "composition": "",
                "manufacturer": "",
                "shelf_life": "",
                "storage": "",
            }
            for language, translation in source["translations"].items()
        }
        hidden_response = client.post(
            "/api/admin/catalog/items",
            headers=AUTH,
            json={"revision": initial["revision"], "item": hidden},
        )
        hidden_document = hidden_response.json()
        hidden_item = next(
            row for row in hidden_document["teas"] if row["id"] == hidden["id"]
        )
        hidden_item["price"] = 100
        hidden_item["published"] = True
        publish_response = client.put(
            f"/api/admin/catalog/items/{hidden['id']}",
            headers=AUTH,
            json={"revision": hidden_document["revision"], "item": hidden_item},
        )

    assert zero_response.status_code == 422
    assert "больше нуля" in zero_response.json()["detail"]
    assert hidden_response.status_code == 201
    assert publish_response.status_code == 200
    public = client.get("/api/catalog").json()["teas"]
    published = next(row for row in public if row["id"] == hidden["id"])
    assert published["translations"]["en"]["desc"] == ""


def test_catalog_allows_missing_secondary_languages_and_falls_back_for_product_page(
    tmp_path, monkeypatch
):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        item = dict(initial["teas"][0])
        item["id"] = "russian-only-item"
        item["published"] = True
        item["translations"] = {
            "ru": {**item["translations"]["ru"], "name": "Товар без перевода"},
            "en": {key: "" for key in item["translations"]["en"]},
            "zh": {key: "" for key in item["translations"]["zh"]},
        }
        created = client.post(
            "/api/admin/catalog/items", headers=AUTH,
            json={"revision": initial["revision"], "item": item},
        )
        english_page = client.get("/en/tea/russian-only-item")

    assert created.status_code == 201
    assert english_page.status_code == 200
    assert "Товар без перевода" in english_page.text


def test_catalog_allows_published_teaware_with_only_a_name_and_price(tmp_path, monkeypatch):
    """Owner can publish a draft now and complete photos, copy and translations later."""
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        item = dict(initial["teas"][0])
        item.update({
            "id": "unfinished-owner-teapot",
            "type": "teaware-teapots",
            "price": 2500,
            "unit": "pc",
            "published": True,
            "img": "logo-mark",
            "image": {"kind": "seed", "name": "logo-mark"},
            "images": [{"kind": "seed", "name": "logo-mark"}],
        })
        fields = (
            "name", "orig", "desc", "composition", "manufacturer",
            "shelf_life", "storage",
        )
        item["translations"] = {
            language: {field: "" for field in fields}
            for language in ("ru", "en", "zh")
        }
        item["translations"]["en"]["name"] = "Work-in-progress teapot"
        created = client.post(
            "/api/admin/catalog/items", headers=AUTH,
            json={"revision": initial["revision"], "item": item},
        )

    assert created.status_code == 201
    public = client.get("/api/catalog").json()["teas"]
    published = next(row for row in public if row["id"] == item["id"])
    assert published["unit"] == "pc"
    assert published["translations"]["ru"]["name"] == ""
    assert published["translations"]["en"]["name"] == "Work-in-progress teapot"


def test_catalog_supports_multiple_images_and_primary_selection(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    sources = []
    for color in ((170, 80, 45), (60, 100, 150)):
        source = BytesIO()
        Image.new("RGB", (160, 120), color).save(source, "PNG")
        sources.append(source.getvalue())

    with client:
        catalog = client.get("/api/admin/catalog", headers=AUTH).json()
        item_id = catalog["teas"][0]["id"]
        first = client.post(
            f"/api/admin/catalog/items/{item_id}/images?revision={catalog['revision']}",
            headers={**AUTH, "Content-Type": "image/png"}, content=sources[0],
        )
        second = client.post(
            f"/api/admin/catalog/items/{item_id}/images?revision={first.json()['revision']}",
            headers={**AUTH, "Content-Type": "image/png"}, content=sources[1],
        )
        second_item = next(row for row in second.json()["teas"] if row["id"] == item_id)
        selected = client.put(
            f"/api/admin/catalog/items/{item_id}/images/2/primary?revision={second.json()['revision']}",
            headers=AUTH,
        )
        selected_item = next(row for row in selected.json()["teas"] if row["id"] == item_id)
        public_item = next(row for row in client.get("/api/catalog").json()["teas"] if row["id"] == item_id)
        removed = client.delete(
            f"/api/admin/catalog/items/{item_id}/images/2?revision={selected.json()['revision']}",
            headers=AUTH,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(second_item["image_urls"]) == 3
    assert selected.status_code == 200
    assert selected_item["image_urls"][0] == second_item["image_urls"][2]
    assert public_item["image_urls"] == selected_item["image_urls"]
    assert removed.status_code == 200
    assert len(next(row for row in removed.json()["teas"] if row["id"] == item_id)["image_urls"]) == 2
    assert all((module.CATALOG_MEDIA_DIR / url.rsplit("/", 1)[1]).is_file() for url in second_item["image_urls"][1:])


def test_legacy_published_item_can_be_edited_before_labels_are_completed(tmp_path, monkeypatch):
    client, _ = app_client(tmp_path, monkeypatch)
    with client:
        initial = client.get("/api/admin/catalog", headers=AUTH).json()
        item = dict(initial["teas"][0])
        item["stock"] = not item["stock"]
        response = client.put(
            f"/api/admin/catalog/items/{item['id']}",
            headers=AUTH,
            json={"revision": initial["revision"], "item": item},
        )

    assert response.status_code == 200


def test_saby_catalog_review_is_read_only_and_owner_only(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    external_id = "22222222-2222-4222-8222-222222222222"
    selected = [{
        "id": 778, "externalId": external_id, "name": "Новый чай из СБИС",
        "unit": "г", "cost": 175, "balance": 9,
    }]
    base = [{
        "id": 778, "externalId": external_id, "name": "Новый чай из СБИС",
        "unit": "г", "cost": 17.5, "balance": 90,
    }]
    monkeypatch.setattr(module.saby_client, "catalog_all", lambda with_balance=False: selected)
    monkeypatch.setattr(module.saby_client, "base_catalog_all", lambda with_balance=False: base)

    with client:
        before = client.get("/api/admin/catalog", headers=AUTH).json()
        anonymous = client.get("/api/admin/saby/catalog-review")
        response = client.get("/api/admin/saby/catalog-review", headers=AUTH)
        after = client.get("/api/admin/catalog", headers=AUTH).json()

    assert anonymous.status_code == 401
    assert response.status_code == 200
    assert response.json()["read_only_source"] is True
    assert response.json()["catalog_changed"] is False
    assert response.json()["items"][0] == {
        "status": "new", "site_id": "", "saby_id": 778,
        "external_id": external_id, "name": "Новый чай из СБИС",
        "unit": "g", "suggested_price": 175, "suggested_stock": True,
        "note": "Прайс-лист Saby: порция 10 г", "can_create_draft": True,
    }
    assert after["revision"] == before["revision"]

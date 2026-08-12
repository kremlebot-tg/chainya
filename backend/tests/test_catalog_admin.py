from io import BytesIO

from PIL import Image

from backend.tests.test_orders import app_client

AUTH = {
    "Authorization": "Bearer test-admin-token",
    "X-Chainya-Admin": "catalog",
}


def test_owner_catalog_page_uses_existing_admin_session(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    with client:
        anonymous = client.get("/manage/catalog")
        login = client.post("/api/admin/session", json={"token": "test-admin-token"})
        authenticated = client.get("/manage/catalog")

    assert login.status_code == 204
    assert "Вход владельца" in anonymous.text
    assert "Управление витриной" in authenticated.text
    assert module.ADMIN_TOKEN not in authenticated.text


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
    assert "100% предоплата" in authenticated.text
    assert "Предоплата, выдача и складской учёт" in authenticated.text
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


def test_catalog_blocks_zero_price_and_incomplete_first_publication(tmp_path, monkeypatch):
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
    assert publish_response.status_code == 422
    assert "Перед публикацией заполните карточку" in publish_response.json()["detail"]


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

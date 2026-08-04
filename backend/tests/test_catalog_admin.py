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

    assert uploaded.status_code == 200
    assert image_url.startswith("/catalog-media/")
    assert invalid.status_code == 422
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"
    assert "immutable" in served.headers["cache-control"]
    with Image.open(BytesIO(served.content)) as stored:
        assert stored.format == "WEBP"
        assert stored.size == (320, 240)
    assert (module.CATALOG_MEDIA_DIR / image_url.rsplit("/", 1)[1]).is_file()

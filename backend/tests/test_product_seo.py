import json
import re
import xml.etree.ElementTree as ET

from backend.tests.test_orders import app_client


def json_ld(response_text: str) -> dict:
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        response_text,
        flags=re.DOTALL,
    )
    assert match
    return json.loads(match.group(1))


def test_product_page_is_indexable_and_uses_live_catalog(tmp_path, monkeypatch):
    client, _module = app_client(tmp_path, monkeypatch)

    with client:
        response = client.get("/tea/baihao")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public, max-age=60")
    assert '<link rel="canonical" href="https://chainya.ru/tea/baihao">' in response.text
    assert '<meta property="og:type" content="product">' in response.text
    assert "Бай Хао Инь Чжень — купить китайский чай" in response.text
    data = json_ld(response.text)
    assert {item.get("@type") for item in data["@graph"]} >= {
        "Organization",
        "WebSite",
        "WebPage",
        "Product",
        "BreadcrumbList",
    }
    product = next(item for item in data["@graph"] if item.get("@type") == "Product")
    assert product["name"] == "Бай Хао Инь Чжень"
    assert product["offers"]["price"] == 175
    assert product["offers"]["priceCurrency"] == "RUB"
    assert product["offers"]["priceSpecification"]["referenceQuantity"] == {
        "@type": "QuantitativeValue",
        "value": 10,
        "unitCode": "GRM",
    }
    assert product["offers"]["availability"] == "https://schema.org/InStock"
    assert ".product{width:100%;max-width:1180px" in response.text


def test_product_pages_have_localized_urls_content_and_reciprocal_hreflang(tmp_path, monkeypatch):
    client, _module = app_client(tmp_path, monkeypatch)

    with client:
        english = client.get("/en/tea/baihao")
        chinese = client.get("/zh/tea/baihao")

    assert english.status_code == chinese.status_code == 200
    assert '<html lang="en">' in english.text
    assert '<link rel="canonical" href="https://chainya.ru/en/tea/baihao">' in english.text
    assert "Bai Hao Yin Zhen" in english.text
    assert "Open in the shop" in english.text
    assert '<html lang="zh-CN">' in chinese.text
    assert '<link rel="canonical" href="https://chainya.ru/zh/tea/baihao">' in chinese.text
    assert "白毫银针" in chinese.text
    assert "在商店中打开" in chinese.text
    for page in (english.text, chinese.text):
        assert 'hreflang="ru" href="https://chainya.ru/tea/baihao"' in page
        assert 'hreflang="en" href="https://chainya.ru/en/tea/baihao"' in page
        assert 'hreflang="zh-CN" href="https://chainya.ru/zh/tea/baihao"' in page
        assert 'hreflang="x-default" href="https://chainya.ru/tea/baihao"' in page
    assert json_ld(english.text)["@graph"][1]["inLanguage"] == "en"
    assert json_ld(chinese.text)["@graph"][1]["inLanguage"] == "zh-CN"


def test_teaware_product_has_its_own_routes_breadcrumbs_and_canonical(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    document = module.get_catalog_store().get()
    item = next(row for row in document["teas"] if row["id"] == "baihao")
    item["type"] = "teaware-teapots"
    item["unit"] = "pc"
    module.get_catalog_store()._write(document)

    with client:
        russian = client.get("/teaware/baihao")
        english = client.get("/en/teaware/baihao")
        wrong_section = client.get("/tea/baihao", follow_redirects=False)
        teaware_head = client.head("/teaware/baihao")
        wrong_head = client.head("/tea/baihao", follow_redirects=False)
        wrong_head_en = client.head("/en/tea/baihao", follow_redirects=False)

    assert russian.status_code == english.status_code == teaware_head.status_code == 200
    assert wrong_section.status_code == wrong_head.status_code == 308
    assert wrong_section.headers["location"] == wrong_head.headers["location"] == "/teaware/baihao"
    assert wrong_head_en.status_code == 308
    assert wrong_head_en.headers["location"] == "/en/teaware/baihao"
    assert '<link rel="canonical" href="https://chainya.ru/teaware/baihao">' in russian.text
    assert "купить чайную посуду" in russian.text
    assert 'href="/teaware?lang=ru"' in russian.text
    assert '<link rel="canonical" href="https://chainya.ru/en/teaware/baihao">' in english.text
    data = json_ld(russian.text)
    breadcrumb = next(row for row in data["@graph"] if row.get("@type") == "BreadcrumbList")
    assert breadcrumb["itemListElement"][1]["name"] == "Посуда"
    assert breadcrumb["itemListElement"][1]["item"] == "https://chainya.ru/teaware?lang=ru"
    assert "Эта вещь уже ушла с полки" in client.get("/teaware/missing-item").text


def test_product_page_escapes_catalog_text_and_json_ld(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    document = module.get_catalog_store().get()
    tea = next(item for item in document["teas"] if item["id"] == "baihao")
    tea["translations"]["ru"]["name"] = 'Чай <script>alert("x")</script>'
    tea["translations"]["ru"]["desc"] = "Лист </script><script>alert(1)</script>"
    module.get_catalog_store()._write(document)

    with client:
        response = client.get("/tea/baihao")

    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    assert '&lt;script&gt;alert(&quot;' in response.text
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in response.text
    json_ld(response.text)


def test_missing_or_hidden_product_is_noindex_404(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    document = module.get_catalog_store().get()
    next(item for item in document["teas"] if item["id"] == "baihao")["published"] = False
    module.get_catalog_store()._write(document)

    with client:
        hidden = client.get("/tea/baihao")
        hidden_en = client.get("/en/tea/baihao")
        hidden_zh = client.get("/zh/tea/baihao")
        missing = client.get("/tea/not-a-real-tea")
        malformed = client.get("/tea/INVALID")

    for response in (hidden, hidden_en, hidden_zh, missing, malformed):
        assert response.status_code == 404
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        assert '<meta name="robots" content="noindex,nofollow">' in response.text


def test_dynamic_sitemap_tracks_only_published_catalog_items(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    document = module.get_catalog_store().get()
    next(item for item in document["teas"] if item["id"] == "chongshicha")["published"] = False
    module.get_catalog_store()._write(document)

    with client:
        response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.text)
    namespace = {
        "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
        "xhtml": "http://www.w3.org/1999/xhtml",
    }
    locations = {node.text for node in root.findall("sm:url/sm:loc", namespace)}
    assert "https://chainya.ru/tea/baihao" in locations
    assert "https://chainya.ru/en/tea/baihao" in locations
    assert "https://chainya.ru/zh/tea/baihao" in locations
    assert "https://chainya.ru/tea/chongshicha" not in locations
    assert "https://chainya.ru/en/tea/chongshicha" not in locations
    assert "https://chainya.ru/zh/tea/chongshicha" not in locations
    assert "https://chainya.ru/shop" in locations
    assert "https://chainya.ru/teaware" in locations
    baihao = next(
        node
        for node in root.findall("sm:url", namespace)
        if node.findtext("sm:loc", namespaces=namespace) == "https://chainya.ru/en/tea/baihao"
    )
    alternates = {
        (node.attrib["hreflang"], node.attrib["href"])
        for node in baihao.findall("xhtml:link", namespace)
    }
    assert ("ru", "https://chainya.ru/tea/baihao") in alternates
    assert ("en", "https://chainya.ru/en/tea/baihao") in alternates
    assert ("zh-CN", "https://chainya.ru/zh/tea/baihao") in alternates
    assert ("x-default", "https://chainya.ru/tea/baihao") in alternates


def test_dynamic_sitemap_uses_teaware_urls_for_teaware_items(tmp_path, monkeypatch):
    client, module = app_client(tmp_path, monkeypatch)
    document = module.get_catalog_store().get()
    item = next(row for row in document["teas"] if row["id"] == "baihao")
    item["type"] = "teaware-teapots"
    item["unit"] = "pc"
    module.get_catalog_store()._write(document)

    root = ET.fromstring(client.get("/sitemap.xml").text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = {node.text for node in root.findall("sm:url/sm:loc", namespace)}
    assert "https://chainya.ru/teaware/baihao" in locations
    assert "https://chainya.ru/en/teaware/baihao" in locations
    assert "https://chainya.ru/zh/teaware/baihao" in locations
    assert "https://chainya.ru/tea/baihao" not in locations

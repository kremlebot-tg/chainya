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
        missing = client.get("/tea/not-a-real-tea")
        malformed = client.get("/tea/INVALID")

    for response in (hidden, missing, malformed):
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
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = {node.text for node in root.findall("sm:url/sm:loc", namespace)}
    assert "https://chainya.ru/tea/baihao" in locations
    assert "https://chainya.ru/tea/chongshicha" not in locations
    assert "https://chainya.ru/shop" in locations

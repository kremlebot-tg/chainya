#!/usr/bin/env python3
"""Fail-closed checks for the public Chainya release."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request


FORBIDDEN_NAMES = {".DS_Store", ".env", ".git", "__pycache__", "README.md"}
FORBIDDEN_SUFFIXES = {
    ".py", ".pyc", ".zip", ".tar", ".gz", ".sql", ".sqlite", ".sqlite3",
    ".db", ".bak", ".backup", ".old",
}
SENSITIVE_PATHS = (
    "/.env",
    "/.env.production",
    "/.git/config",
    "/.DS_Store",
    "/README.md",
    "/src.html",
    "/build.py",
    "/deploy.sh",
    "/deploy-shop.sh",
    "/ops/nginx-chainya.ru",
    "/backend/app.py",
    "/backend/teas.json",
    "/backend/__pycache__/app.cpython-312.pyc",
    "/backup.zip",
    "/backup.sql",
    "/site.tar.gz",
    "/orders.sqlite3",
    "/RELEASE_COMMIT",
    "/test-payment/nonexistent",
    "/definitely-not-a-real-chainya-page-7f31",
)
PUBLIC_PATHS = (
    ("/", "text/html"),
    ("/en/", "text/html"),
    ("/zh/", "text/html"),
    ("/shop", "text/html"),
    ("/en/shop", "text/html"),
    ("/zh/shop", "text/html"),
    ("/teaware", "text/html"),
    ("/en/teaware", "text/html"),
    ("/zh/teaware", "text/html"),
    ("/business", "text/html"),
    ("/booking", "text/html"),
    ("/account", "text/html"),
    ("/privacy.html", "text/html"),
    ("/legal.html", "text/html"),
    ("/api/catalog", "application/json"),
    ("/sitemap.xml", "application/xml"),
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?i)(?:ADMIN_TOKEN|BOT_TOKEN|TBANK_PASSWORD|CDEK_CLIENT_SECRET|"
        rb"SABY_APP_SECRET|SABY_SECRET_KEY)\s*[:=]\s*[\"']?[A-Za-z0-9]"
    ),
)
SELLER_DETAILS = (
    "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ ДАВТЯН АРМАН КАРАПЕТОВИЧ",
    "772606053199",
    "326774600295390",
    "chainya@bk.ru",
)
REGISTERED_ADDRESS = (
    "129226, Россия, г. Москва, ул. Сергея Эйзенштейна, "
    "д. 6, корп. 2, стр. 2, кв. 233"
)
REGISTERED_ADDRESS_PARTS = (
    "129226",
    "Москва",
    "ул. Сергея Эйзенштейна, д. 6, корп. 2, стр. 2, кв. 233",
    '"addressCountry": "RU"',
)
LEGAL_PLACEHOLDERS = (
    re.compile(r"\bTODO(?:\s|:|-)", re.I),
    re.compile(r"example@example", re.I),
    re.compile(r"укажите реквизиты", re.I),
    re.compile(r"заполнить реквизиты", re.I),
)
PRIVATE_BANK_PATTERNS = (
    re.compile(r"\bБИК\s*[:№]?\s*\d", re.I),
    re.compile(r"корреспондентск\w*\s+сч[её]т\w*\s*[:№]?\s*\d", re.I),
    re.compile(r"расч[её]тн\w*\s+сч[её]т\w*\s*[:№]?\s*\d", re.I),
)
FULL_SETTLEMENT_NOTICE = (
    "При полной предоплате кассовый чек полного расчёта формируется в момент оплаты"
)


def check_dist(root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    required = {
        "index.html",
        "en/index.html",
        "zh/index.html",
        "shop/index.html",
        "en/shop/index.html",
        "zh/shop/index.html",
        "teaware/index.html",
        "en/teaware/index.html",
        "zh/teaware/index.html",
        "business/index.html",
        "en/business/index.html",
        "zh/business/index.html",
        "booking/index.html",
        "en/booking/index.html",
        "zh/booking/index.html",
        "404.html",
        "50x.html",
        "favicon.ico",
        "privacy.html",
        "legal.html",
        "assets/site.js",
        "assets/privacy.js",
        "assets/legal.js",
        "robots.txt",
        "sitemap.xml",
        ".well-known/security.txt",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        errors.append("нет обязательных файлов: " + ", ".join(missing))
    storefront_script = root / "assets/site.js"
    if storefront_script.is_file():
        script_text = storefront_script.read_text(encoding="utf-8")
        for marker, label in (
            ('c-promo', "поле промокода"),
            ('booking-cancel', "отмена брони"),
        ):
            if marker not in script_text:
                errors.append(f"assets/site.js: потеряна функция {label}")
    error_page_markers = {
        "404.html": "Лист сбился с пути",
        "50x.html": "Чайнику нужна минута",
    }
    for name, marker in error_page_markers.items():
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            errors.append(f"{name}: потерян фирменный текст")
        if '<meta name="robots" content="noindex,nofollow">' not in text:
            errors.append(f"{name}: нет noindex,nofollow")
        if "<script" in text.lower():
            errors.append(f"{name}: служебная страница не должна выполнять JavaScript")
    public_app_files = tuple(
        (f"{language}/" if language else "") + (f"{route}/" if route else "") + "index.html"
        for language in ("", "en", "zh")
        for route in ("", "shop", "teaware", "business", "booking")
    )
    for name in public_app_files + (
        "privacy.html", "legal.html",
    ):
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for detail in SELLER_DETAILS:
            if detail not in text:
                errors.append(f"{name}: отсутствуют подтверждённые реквизиты {detail!r}")
        if name in public_app_files:
            if '<script src="/assets/site.js" defer></script>' not in text:
                errors.append(f"{name}: основной JavaScript не вынесен в same-origin asset")
            for detail in REGISTERED_ADDRESS_PARTS:
                if detail not in text:
                    errors.append(
                        f"{name}: неполный структурированный адрес продавца {detail!r}"
                    )
        else:
            asset_name = name.removesuffix(".html")
            if f'<script src="/assets/{asset_name}.js" defer></script>' not in text:
                errors.append(f"{name}: inline JavaScript не вынесен в same-origin asset")
        executable_inline = re.search(
            r'<script(?![^>]*type="application/ld\+json")[^>]*>(?!\s*</script>)',
            text,
            flags=re.IGNORECASE,
        )
        if executable_inline:
            errors.append(f"{name}: остался executable inline JavaScript")
        if "telegram.org/js/telegram-web-app.js" in text:
            errors.append(f"{name}: публичная страница загружает отключённый Telegram SDK")
        if name not in public_app_files and REGISTERED_ADDRESS not in text:
            errors.append(
                f"{name}: отсутствует подтверждённый адрес {REGISTERED_ADDRESS!r}"
            )
        for placeholder in LEGAL_PLACEHOLDERS:
            if placeholder.search(text):
                errors.append(f"{name}: найдена юридическая заглушка {placeholder.pattern!r}")
        if any(pattern.search(text) for pattern in PRIVATE_BANK_PATTERNS):
            errors.append(f"{name}: обнаружены лишние банковские реквизиты")
        if (
            name in {"index.html", "shop/index.html", "teaware/index.html", "legal.html"}
            and FULL_SETTLEMENT_NOTICE not in text
        ):
            errors.append(
                f"{name}: отсутствует предупреждение об одном чеке полного расчёта"
            )
    site_script = root / "assets/site.js"
    if site_script.is_file():
        source = site_script.read_text(encoding="utf-8")
        for marker in (
            "telegram.org/js/telegram-web-app.js",
            "window.Telegram",
            "createElement('script')",
        ):
            if marker in source:
                errors.append(f"assets/site.js: остался внешний загрузчик {marker!r}")
    route_canonicals = {}
    route_views = {}
    for language in ("", "en", "zh"):
        prefix = f"/{language}" if language else ""
        file_prefix = f"{language}/" if language else ""
        route_canonicals[file_prefix + "index.html"] = (
            f"https://chainya.ru{prefix}/" if language else "https://chainya.ru/"
        )
        for route, view in (
            ("shop", "shop"), ("teaware", "shop"),
            ("business", "b2b"), ("booking", "book"),
        ):
            name = f"{file_prefix}{route}/index.html"
            route_canonicals[name] = f"https://chainya.ru{prefix}/{route}"
            route_views[name] = view
    for name, canonical in route_canonicals.items():
        text = (root / name).read_text(encoding="utf-8")
        if f'<link rel="canonical" href="{canonical}">' not in text:
            errors.append(f"{name}: неверный canonical")
        if f'<meta property="og:url" content="{canonical}">' not in text:
            errors.append(f"{name}: неверный og:url")
        expected_site_name = "Chaynya" if name.startswith("en/") else ("茶饮屋" if name.startswith("zh/") else "Чайня")
        for marker in (
            '<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1">',
            f'<meta property="og:site_name" content="{expected_site_name}">',
            '<meta property="og:image:alt"',
            '<meta name="twitter:image"',
            '<meta name="twitter:image:alt"',
        ):
            if marker not in text:
                errors.append(f"{name}: отсутствует SEO-маркер {marker!r}")
        ld_scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            text,
            flags=re.DOTALL,
        )
        if len(ld_scripts) != 1:
            errors.append(f"{name}: ожидается один JSON-LD блок")
        else:
            try:
                graph = json.loads(ld_scripts[0]).get("@graph", [])
            except (json.JSONDecodeError, AttributeError):
                errors.append(f"{name}: JSON-LD не разбирается")
            else:
                types = {
                    item_type
                    for item in graph
                    for item_type in (
                        item.get("@type")
                        if isinstance(item.get("@type"), list)
                        else [item.get("@type")]
                    )
                }
                for expected_type in ("Organization", "Store", "WebSite", "WebPage"):
                    if expected_type not in types:
                        errors.append(f"{name}: в JSON-LD нет {expected_type}")
                pages = [item for item in graph if item.get("@type") == "WebPage"]
                if len(pages) != 1 or pages[0].get("url") != canonical:
                    errors.append(f"{name}: JSON-LD WebPage указывает не на canonical")
                has_breadcrumbs = "BreadcrumbList" in types
                is_home = name in {"index.html", "en/index.html", "zh/index.html"}
                if (not is_home) != has_breadcrumbs:
                    errors.append(f"{name}: неверное наличие BreadcrumbList")
        route = next(
            (part for part in ("shop", "teaware", "business", "booking") if f"/{part}/" in f"/{name}"),
            "",
        )
        alternate_path = f"/{route}" if route else "/"
        alternate_urls = {
            "ru": f"https://chainya.ru{alternate_path}",
            "en": f"https://chainya.ru/en{alternate_path}",
            "zh-CN": f"https://chainya.ru/zh{alternate_path}",
        }
        if not route:
            alternate_urls = {
                "ru": "https://chainya.ru/",
                "en": "https://chainya.ru/en/",
                "zh-CN": "https://chainya.ru/zh/",
            }
        for hreflang, href in {**alternate_urls, "x-default": alternate_urls["ru"]}.items():
            marker = f'<link rel="alternate" hreflang="{hreflang}" href="{href}">'
            if marker not in text:
                errors.append(f"{name}: отсутствует {hreflang} alternate")
        if name in route_views:
            view = route_views[name]
            if f'<section class="view is-active" id="view-{view}">' not in text:
                errors.append(f"{name}: неверный исходный активный раздел")
        relative_assets = re.findall(
            r'(?:src|href)=["\'](?:img/|fonts/|favicon\.(?:png|ico))',
            text,
        )
        relative_assets += re.findall(r'url\((?:img/|fonts/)', text)
        if relative_assets:
            errors.append(
                f"{name}: относительные ассеты ломаются при URL с завершающим слэшем"
            )
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if (
            path.name.startswith("._")
            or path.name in FORBIDDEN_NAMES
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or any(part in FORBIDDEN_NAMES for part in relative.parts)
        ):
            errors.append(f"запрещённый публичный файл: {relative}")
        if path.suffix.lower() in {".html", ".js", ".json", ".txt", ".xml"}:
            content = path.read_bytes()
            if any(pattern.search(content) for pattern in SECRET_PATTERNS):
                errors.append(f"возможный секрет в публичном файле: {relative}")
    return errors


def response_metadata(url: str) -> tuple[int, str, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ChainyaReleaseVerifier/1.0"},
        method="GET",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.headers.get_content_type(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get_content_type(), exc.headers
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def raw_response_metadata(url: str, method: str = "GET") -> tuple[int, str, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ChainyaReleaseVerifier/1.0"},
        method=method,
    )
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(3):
        try:
            with opener.open(request, timeout=15) as response:
                return response.status, response.headers.get_content_type(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get_content_type(), exc.headers
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def json_response(url: str) -> tuple[int, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ChainyaReleaseVerifier/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(1)
                continue
            return 0, {}
        except (json.JSONDecodeError, ValueError):
            return 0, {}
    return 0, {}


def text_response(url: str) -> tuple[int, str, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ChainyaReleaseVerifier/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read().decode("utf-8"), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, "", exc.headers
        except (urllib.error.URLError, UnicodeDecodeError):
            if attempt < 2:
                time.sleep(1)
                continue
            return 0, "", {}
    return 0, "", {}


def status(url: str) -> tuple[int, str]:
    code, content_type, _headers = response_metadata(url)
    return code, content_type


def combined_header(headers: object, name: str) -> str:
    """Return every HTTP field value; repeated headers are semantically one list."""
    get_all = getattr(headers, "get_all", None)
    values = get_all(name) if callable(get_all) else None
    if values:
        return ", ".join(str(value) for value in values)
    get = getattr(headers, "get", None)
    return str(get(name, "")) if callable(get) else ""


def check_live(base_url: str) -> list[str]:
    errors: list[str] = []
    base = base_url.rstrip("/")
    root_code, root_type, root_headers = response_metadata(base + "/")
    if root_code == 200 and root_type == "text/html":
        expected_headers = {
            "strict-transport-security": "max-age=",
            "x-content-type-options": "nosniff",
            "x-frame-options": "deny",
            "cross-origin-opener-policy": "same-origin",
            "x-permitted-cross-domain-policies": "none",
            "cache-control": "no-cache",
        }
        for name, marker in expected_headers.items():
            value = combined_header(root_headers, name)
            if marker.lower() not in value.lower():
                errors.append(f"/: заголовок {name} не содержит {marker!r}")
        csp = combined_header(root_headers, "content-security-policy")
        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "script-src-attr 'none'",
            "object-src 'none'",
            "form-action 'self'",
        ):
            if directive not in csp:
                errors.append(f"/: CSP не содержит {directive!r}")
        if "'unsafe-inline'" in csp.split("style-src", 1)[0]:
            errors.append("/: CSP всё ещё разрешает inline JavaScript")
        if "telegram.org" in csp:
            errors.append("/: CSP всё ещё разрешает отключённый Telegram SDK")
    for path, expected_type in PUBLIC_PATHS:
        code, content_type = status(base + path)
        if code != 200 or content_type != expected_type:
            errors.append(
                f"{path}: ожидался 200 {expected_type}, "
                f"получен {code} {content_type}"
            )
    catalog_code, catalog = json_response(base + "/api/catalog")
    catalog_items = catalog.get("teas") if isinstance(catalog, dict) else None
    if catalog_code != 200 or not isinstance(catalog_items, list) or not catalog_items:
        errors.append("/api/catalog: отсутствует непустой список teas")
    elif any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or item.get("unit") not in {"g", "pc"}
        or not isinstance(item.get("price"), int)
        or not isinstance(item.get("translations"), dict)
        or not isinstance(item.get("image_url"), str)
        for item in catalog_items
    ):
        errors.append("/api/catalog: товар не соответствует публичной схеме")
    else:
        first_id = catalog_items[0]["id"]
        product_paths = (
            (f"/tea/{first_id}", "ru"),
            (f"/en/tea/{first_id}", "en"),
            (f"/zh/tea/{first_id}", "zh-CN"),
        )
        for product_path, page_language in product_paths:
            canonical = f"{base}{product_path}"
            product_code, product_html, product_headers = text_response(base + product_path)
            if product_code != 200:
                errors.append(f"{product_path}: ожидался 200, получен {product_code}")
                continue
            for marker in (
                f'<html lang="{page_language}">',
                f'<link rel="canonical" href="{canonical}">',
                f'hreflang="x-default" href="{base}/tea/{first_id}"',
                '<meta property="og:type" content="product">',
                '"@type":"Product"',
            ):
                if marker not in product_html:
                    errors.append(f"{product_path}: отсутствует SEO-маркер {marker!r}")
            if "public" not in combined_header(product_headers, "cache-control").lower():
                errors.append(f"{product_path}: отсутствует публичная cache policy")
            product_head, _type, product_head_headers = raw_response_metadata(
                base + product_path,
                method="HEAD",
            )
            if product_head != 200:
                errors.append(f"{product_path}: HEAD ожидался 200, получен {product_head}")
            elif "frame-ancestors 'none'" not in combined_header(
                product_head_headers,
                "content-security-policy",
            ):
                errors.append(f"{product_path}: публичная карточка допускает встраивание")
        sitemap_code, sitemap_xml, _headers = text_response(base + "/sitemap.xml")
        if sitemap_code != 200:
            errors.append("/sitemap.xml: не удалось прочитать карту сайта")
        else:
            for product_path, _language in product_paths:
                if f"{base}{product_path}" not in sitemap_xml:
                    errors.append(f"/sitemap.xml: нет опубликованной карточки {product_path}")
    for path in SENSITIVE_PATHS:
        code, _content_type = status(base + path)
        if code not in {403, 404}:
            errors.append(f"{path}: ожидался 403/404, получен {code}")
    redirect_code, _redirect_type, redirect_headers = raw_response_metadata(
        base + "/index.html"
    )
    if redirect_code not in {301, 308} or combined_header(
        redirect_headers, "location"
    ) not in {"/", base + "/"}:
        errors.append("/index.html: нет канонического redirect на /")
    for path in ("/manage", "/manage/catalog", "/manage/site", "/manage/guides", "/manage/promos", "/admin/orders", "/account", "/payment/success"):
        code, content_type, headers = raw_response_metadata(base + path, method="HEAD")
        # HEAD deliberately has an empty body; Starlette labels that empty
        # response text/plain even though the corresponding GET is HTML.
        if code != 200:
            errors.append(f"{path}: HEAD ожидался 200, получен {code}")
        csp = combined_header(headers, "content-security-policy")
        if "frame-ancestors 'none'" not in csp:
            errors.append(f"{path}: приватная страница допускает встраивание")
        if "noindex" not in combined_header(headers, "x-robots-tag").lower():
            errors.append(f"{path}: нет X-Robots-Tag noindex")
    for path in ("/", "/shop", "/business", "/booking"):
        code, _content_type, headers = raw_response_metadata(base + path, method="HEAD")
        if code != 200:
            errors.append(f"{path}: HEAD ожидался 200, получен {code}")
        elif "frame-ancestors 'none'" not in combined_header(
            headers, "content-security-policy"
        ):
            errors.append(f"{path}: публичная страница допускает встраивание")
    health_code, health = json_response(base + "/api/health")
    if health_code != 200 or not isinstance(health, dict) or not health.get("ok"):
        errors.append("/api/health: backend не подтвердил готовность")
    elif health.get("test_mode") is not False:
        errors.append("/api/health: production остался в тестовом режиме")
    elif not isinstance(health.get("catalog_items"), int) or health["catalog_items"] < 1:
        errors.append("/api/health: каталог пуст")
    elif not re.fullmatch(r"[0-9a-f]{7,64}", str(health.get("version", ""))):
        errors.append("/api/health: отсутствует корректная версия release")
    code, content_type = status(base + "/.well-known/security.txt")
    if code != 200 or content_type != "text/plain":
        errors.append(
            "/.well-known/security.txt: ожидался 200 text/plain, "
            f"получен {code} {content_type}"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=pathlib.Path, required=True)
    parser.add_argument("--base-url")
    args = parser.parse_args()
    errors = check_dist(args.dist)
    if args.base_url:
        errors.extend(check_live(args.base_url))
    if errors:
        print("ПРОВЕРКА RELEASE НЕ ПРОЙДЕНА:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("✓ release-проверка пройдена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

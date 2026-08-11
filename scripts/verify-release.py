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
    ("/shop", "text/html"),
    ("/business", "text/html"),
    ("/booking", "text/html"),
    ("/account", "text/html"),
    ("/privacy.html", "text/html"),
    ("/legal.html", "text/html"),
    ("/api/catalog", "application/json"),
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


def check_dist(root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    required = {
        "index.html",
        "shop/index.html",
        "business/index.html",
        "booking/index.html",
        "404.html",
        "50x.html",
        "favicon.ico",
        "privacy.html",
        "legal.html",
        "robots.txt",
        "sitemap.xml",
        ".well-known/security.txt",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        errors.append("нет обязательных файлов: " + ", ".join(missing))
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
    for name in (
        "index.html", "shop/index.html", "business/index.html", "booking/index.html",
        "privacy.html", "legal.html",
    ):
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for detail in SELLER_DETAILS:
            if detail not in text:
                errors.append(f"{name}: отсутствуют подтверждённые реквизиты {detail!r}")
        if name in {"index.html", "shop/index.html", "business/index.html", "booking/index.html"}:
            for detail in REGISTERED_ADDRESS_PARTS:
                if detail not in text:
                    errors.append(
                        f"{name}: неполный структурированный адрес продавца {detail!r}"
                    )
        elif REGISTERED_ADDRESS not in text:
            errors.append(
                f"{name}: отсутствует подтверждённый адрес {REGISTERED_ADDRESS!r}"
            )
        for placeholder in LEGAL_PLACEHOLDERS:
            if placeholder.search(text):
                errors.append(f"{name}: найдена юридическая заглушка {placeholder.pattern!r}")
        if any(pattern.search(text) for pattern in PRIVATE_BANK_PATTERNS):
            errors.append(f"{name}: обнаружены лишние банковские реквизиты")
    route_canonicals = {
        "shop/index.html": "https://chainya.ru/shop",
        "business/index.html": "https://chainya.ru/business",
        "booking/index.html": "https://chainya.ru/booking",
    }
    route_views = {
        "shop/index.html": "shop",
        "business/index.html": "b2b",
        "booking/index.html": "book",
    }
    for name, canonical in route_canonicals.items():
        text = (root / name).read_text(encoding="utf-8")
        if f'<link rel="canonical" href="{canonical}">' not in text:
            errors.append(f"{name}: неверный canonical")
        if f'<meta property="og:url" content="{canonical}">' not in text:
            errors.append(f"{name}: неверный og:url")
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
            "cache-control": "no-cache",
        }
        for name, marker in expected_headers.items():
            value = combined_header(root_headers, name)
            if marker.lower() not in value.lower():
                errors.append(f"/: заголовок {name} не содержит {marker!r}")
        csp = combined_header(root_headers, "content-security-policy")
        for directive in ("default-src 'self'", "object-src 'none'", "form-action 'self'"):
            if directive not in csp:
                errors.append(f"/: CSP не содержит {directive!r}")
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
    for path in ("/manage", "/manage/catalog", "/manage/guides", "/admin/orders", "/account", "/payment/success"):
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

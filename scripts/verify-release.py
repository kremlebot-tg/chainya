#!/usr/bin/env python3
"""Fail-closed checks for the public Chainya release."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
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
        "404.html",
        "privacy.html",
        "legal.html",
        "robots.txt",
        "sitemap.xml",
        ".well-known/security.txt",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        errors.append("нет обязательных файлов: " + ", ".join(missing))
    for name in ("index.html", "privacy.html", "legal.html"):
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for detail in SELLER_DETAILS:
            if detail not in text:
                errors.append(f"{name}: отсутствуют подтверждённые реквизиты {detail!r}")
        if name == "index.html":
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
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.headers.get_content_type(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.headers


def status(url: str) -> tuple[int, str]:
    code, content_type, _headers = response_metadata(url)
    return code, content_type


def check_live(base_url: str) -> list[str]:
    errors: list[str] = []
    base = base_url.rstrip("/")
    root_code, root_type, root_headers = response_metadata(base + "/")
    if root_code == 200 and root_type == "text/html":
        expected_headers = {
            "strict-transport-security": "max-age=",
            "x-content-type-options": "nosniff",
            "cache-control": "no-store",
        }
        for name, marker in expected_headers.items():
            value = root_headers.get(name, "")
            if marker.lower() not in value.lower():
                errors.append(f"/: заголовок {name} не содержит {marker!r}")
        csp = root_headers.get("content-security-policy", "")
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
    for path in SENSITIVE_PATHS:
        code, _content_type = status(base + path)
        if code not in {403, 404}:
            errors.append(f"{path}: ожидался 403/404, получен {code}")
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

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
    "/.git/config",
    "/README.md",
    "/backend/app.py",
    "/backup.zip",
    "/orders.sqlite3",
    "/test-payment/nonexistent",
    "/definitely-not-a-real-chainya-page-7f31",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?i)(?:ADMIN_TOKEN|BOT_TOKEN|TBANK_PASSWORD|CDEK_CLIENT_SECRET|"
        rb"SABY_APP_SECRET|SABY_SECRET_KEY)\s*[:=]\s*[\"']?[A-Za-z0-9]"
    ),
)


def check_dist(root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    required = {
        "index.html",
        "404.html",
        "privacy.html",
        "robots.txt",
        "sitemap.xml",
        ".well-known/security.txt",
    }
    missing = sorted(name for name in required if not (root / name).is_file())
    if missing:
        errors.append("нет обязательных файлов: " + ", ".join(missing))
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


def status(url: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ChainyaReleaseVerifier/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type()


def check_live(base_url: str) -> list[str]:
    errors: list[str] = []
    base = base_url.rstrip("/")
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

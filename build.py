#!/usr/bin/env python3
"""Сборка сайта «Чайня» из src.html.

Два режима:

  python3 build.py          один файл: шрифты и картинки вшиты в HTML
                            → index.html      (локальный просмотр)
                            → artifact.html   (публикация артефактом)
                            Удобно пересылать, но браузер не кэширует
                            картинки отдельно и качает всё заново.

  python3 build.py --web    раздельные файлы для хостинга
                            → dist/index.html + dist/img/ + dist/fonts/
                            HTML прилетает мгновенно, ассеты кэшируются.

Картинки подставляются по маркеру {{img:имя}} (файл img/имя.webp).
"""
import base64
import hashlib
import json
import pathlib
import re
import shutil
import sys

root = pathlib.Path(__file__).parent
web = "--web" in sys.argv

src = (root / "src.html").read_text(encoding="utf-8")
assert "/*@FONTS@*/" in src, "маркер /*@FONTS@*/ пропал из src.html"

# Тот же заголовок, что в словаре I18N.ru: JS перепишет его при старте,
# но краулерам и первой отрисовке достаётся статический.
TITLE = "Чайня · китайский чай с доставкой"
DESC = ("Китайский чай с доставкой по Москве и России: белый, зелёный, "
        "улуны, красный чай, пуэр и авторские сборы. Чайная на Острякова, 3.")
SELLER_NAME = "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ ДАВТЯН АРМАН КАРАПЕТОВИЧ"
SELLER_INN = "772606053199"
SELLER_OGRNIP = "326774600295390"
SELLER_EMAIL = "chainya@bk.ru"
SELLER_REGISTERED_ADDRESS = (
    "129226, Россия, г. Москва, ул. Сергея Эйзенштейна, "
    "д. 6, корп. 2, стр. 2, кв. 233"
)

# og:image обязан быть абсолютным: по относительному пути телеграм и соцсети
# картинку не подтянут. Меняется на свой домен, когда он появится.
SITE = "https://chainya.ru/"
SECURITY_CONTACT = "https://t.me/chainyabot"
SECURITY_EMAIL = "mailto:chainya@bk.ru"

# Телеграм кэширует саму картинку по её URL и по тому же адресу за новой не ходит:
# @WebpageBot перечитывает разметку страницы, но подменённый файл оставляет старый.
# Поэтому в имя подмешиваем хэш содержимого — правка карточки сама даёт новый URL,
# и ни телеграму, ни CDN нечего отдавать из старого кэша.
OG_SRC = root / "src-assets" / "og.jpg"
OG_NAME = f"og.{hashlib.sha256(OG_SRC.read_bytes()).hexdigest()[:8]}.jpg"

HEAD_EXTRA = f"""<meta name="description" content="{DESC}">
<meta name="theme-color" content="#141110">
<meta property="og:type" content="website">
<meta property="og:title" content="{TITLE}">
<meta property="og:description" content="{DESC}">
<meta property="og:locale" content="ru_RU">
<meta property="og:url" content="{SITE}">
<meta property="og:image" content="{SITE}{OG_NAME}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<link rel="canonical" href="{SITE}">
<link rel="icon" href="favicon.png" type="image/png">
<link rel="apple-touch-icon" href="favicon.png">
{json.dumps({
    "@context": "https://schema.org",
    "@graph": [
        {
            "@type": "Organization",
            "@id": SITE + "#seller",
            "name": SELLER_NAME,
            "alternateName": "Чайня",
            "taxID": SELLER_INN,
            "identifier": {
                "@type": "PropertyValue",
                "propertyID": "ОГРНИП",
                "value": SELLER_OGRNIP,
            },
            "email": SELLER_EMAIL,
            "telephone": "+7 905 590-88-01",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": SELLER_REGISTERED_ADDRESS,
                "addressCountry": "RU",
            },
        },
        {
            "@type": ["CafeOrCoffeeShop", "Store"],
            "@id": SITE + "#store",
            "name": "Чайня",
            "legalName": SELLER_NAME,
            "branchOf": {"@id": SITE + "#seller"},
            "description": DESC,
            "url": SITE,
            "image": SITE + OG_NAME,
            "email": SELLER_EMAIL,
            "telephone": "+7 905 590-88-01",
            "priceRange": "₽₽",
            "currenciesAccepted": "RUB",
            "servesCuisine": "Чай",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "улица Острякова, 3, помещение 114",
                "addressLocality": "Москва",
                "addressCountry": "RU",
            },
            "openingHoursSpecification": {
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "opens": "12:00",
                "closes": "22:00",
            },
            "hasMap": "https://yandex.com/maps/org/chaynya/49488428011/",
            "sameAs": ["https://t.me/chainyamsk", "https://yandex.com/maps/org/chaynya/49488428011/"],
        },
    ],
}, ensure_ascii=False).join(('<script type="application/ld+json">', '</script>'))}"""


def font_css(inline: bool) -> str:
    """CSS со шрифтами: либо base64 внутри, либо ссылками на файлы."""
    if inline:
        return (root / "fonts" / "fonts-inline.css").read_text(encoding="utf-8")
    css = (root / "fonts" / "fonts-inline.css").read_text(encoding="utf-8")
    # меняем data:-строки обратно на пути к файлам, порядок объявлений сохраняется
    names = ["prata-cyr", "prata-lat", "golos-cyr", "golos-lat"]
    parts = re.split(r"url\(data:font/woff2;base64,[^)]+\)", css)
    assert len(parts) == len(names) + 1, "не совпало число @font-face со списком файлов"
    out = parts[0]
    for name, tail in zip(names, parts[1:]):
        out += f"url(fonts/{name}.woff2)" + tail
    return out


used, missing = set(), set()


def img_ref(m):
    name = m.group(1)
    f = root / "img" / f"{name}.webp"
    if not f.exists():
        missing.add(name)
        return ""
    used.add(name)
    if web:
        return f"img/{name}.webp"
    return "data:image/webp;base64," + base64.b64encode(f.read_bytes()).decode()


content = src.replace("/*@FONTS@*/", font_css(inline=not web))
content = re.sub(r"\{\{img:([a-z0-9\-]+)\}\}", img_ref, content)

if missing:
    raise SystemExit("НЕТ КАРТИНОК: " + ", ".join(sorted(missing)))

have = {p.stem for p in (root / "img").glob("*.webp")}
if unused := have - used:
    print("не используются:", ", ".join(sorted(unused)))


def document(body: str, extra_head: str = "", *, telegram_sdk: bool = True) -> str:
    # В исходнике CSS хранится первым блоком, чтобы проект оставался одним
    # редактируемым файлом. В готовом HTML переносим его в <head>: браузер
    # получает стили до body, а документ остаётся валидным.
    style_match = re.match(r"\s*(<style>.*?</style>)\s*", body, flags=re.S)
    style_block = ""
    if style_match:
        style_block = style_match.group(1)
        body = body[style_match.end():]
    sdk = (
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>\n'
        if telegram_sdk else ""
    )
    return (
        '<!doctype html>\n<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{TITLE}</title>\n"
        # Telegram Mini App SDK нужен только основному приложению. Служебная
        # 404-страница не должна выполнять внешний JavaScript.
        f"{sdk}"
        f"{extra_head}\n"
        f"{style_block}\n"
        "<style>*{margin:0}</style>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )


if web:
    dist = root / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    (dist / "img").mkdir(parents=True)
    (dist / "fonts").mkdir()
    for name in sorted(used):
        shutil.copy(root / "img" / f"{name}.webp", dist / "img" / f"{name}.webp")
    for f in ("prata-cyr", "prata-lat", "golos-cyr", "golos-lat"):
        shutil.copy(root / "fonts" / f"{f}.woff2", dist / "fonts" / f"{f}.woff2")
    shutil.copy(root / "src-assets" / "favicon.png", dist / "favicon.png")
    shutil.copy(OG_SRC, dist / OG_NAME)
    shutil.copy(root / "privacy.html", dist / "privacy.html")
    shutil.copy(root / "legal.html", dist / "legal.html")
    well_known = dist / ".well-known"
    well_known.mkdir()
    (well_known / "security.txt").write_text(
        f"Contact: {SECURITY_EMAIL}\n"
        f"Contact: {SECURITY_CONTACT}\n"
        f"Canonical: {SITE}.well-known/security.txt\n"
        "Expires: 2027-07-29T00:00:00Z\n"
        "Preferred-Languages: ru, en\n",
        encoding="utf-8",
    )
    (dist / "404.html").write_text(
        document(
            """
<main style="min-height:100svh;display:grid;place-items:center;padding:24px;background:#141110;color:#e8e4dc;text-align:center">
  <section>
    <img src="/img/logo-mark.webp" alt="" width="54" height="64">
    <p style="margin:24px 0 8px;color:#b9afa4;letter-spacing:.16em;text-transform:uppercase">Ошибка 404</p>
    <h1 style="margin:0;font:clamp(34px,8vw,64px)/1.1 Prata,serif">Такой страницы нет</h1>
    <p style="margin:18px auto 28px;max-width:38rem;color:#b9afa4;font:16px/1.6 Golos,sans-serif">Вернитесь в чайную или откройте каталог — всё остальное на месте.</p>
    <a href="/" style="display:inline-block;padding:13px 22px;border:1px solid #d6c9b9;color:#e8e4dc;text-decoration:none;font:600 14px Golos,sans-serif">Вернуться на главную</a>
  </section>
</main>
""",
            '<meta name="robots" content="noindex,nofollow">',
            telegram_sdk=False,
        ),
        encoding="utf-8",
    )
    (dist / "robots.txt").write_text(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Disallow: /manage\n"
        "Disallow: /payment/\n"
        "Disallow: /test-payment/\n"
        f"Sitemap: {SITE}sitemap.xml\n",
        encoding="utf-8",
    )
    (dist / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{SITE}</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{SITE}legal.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>\n"
        "</urlset>\n",
        encoding="utf-8",
    )
    (dist / "index.html").write_text(document(content, HEAD_EXTRA), encoding="utf-8")

    forbidden = [
        path for path in dist.rglob("*")
        if path.name == ".DS_Store"
        or path.name.startswith("._")
        or path.suffix.lower() in {
            ".py", ".pyc", ".zip", ".tar", ".gz", ".sql", ".sqlite",
            ".sqlite3", ".db", ".bak", ".backup", ".old",
        }
    ]
    if forbidden:
        raise SystemExit(
            "ЗАПРЕЩЁННЫЕ ФАЙЛЫ В DIST: "
            + ", ".join(str(path.relative_to(dist)) for path in forbidden)
        )

    html_kb = round((dist / "index.html").stat().st_size / 1024)
    assets = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file()) - (dist / "index.html").stat().st_size
    print(f"dist/index.html   {html_kb} KB   (прилетает сразу)")
    print(f"dist/img + fonts  {round(assets / 1024)} KB  в {len(used) + 4} файлах (кэшируются)")
    print(f"итого             {round((html_kb * 1024 + assets) / 1024)} KB")
else:
    (root / "artifact.html").write_text(content, encoding="utf-8")
    (root / "index.html").write_text(document(content), encoding="utf-8")
    print(f"картинок вшито: {len(used)}")
    for f in ("index.html", "artifact.html"):
        print(f"{f:16} {round((root / f).stat().st_size / 1024)} KB")

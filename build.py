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
TITLE = "Чайня · чайная на Острякова"
DESC = ("Чайная у метро Аэропорт. Чайная церемония с мастером, два чая "
        "на выбор уже в стоимости. Китайский чай прямого привоза и доставка по России.")

# og:image обязан быть абсолютным: по относительному пути телеграм и соцсети
# картинку не подтянут. Меняется на свой домен, когда он появится.
SITE = "https://chainya.ru/"

# Телеграм кэширует саму картинку по её URL и по тому же адресу за новой не ходит:
# @WebpageBot перечитывает разметку страницы, но подменённый файл оставляет старый.
# Поэтому в имя подмешиваем хэш содержимого — правка карточки сама даёт новый URL,
# и ни телеграму, ни CDN нечего отдавать из старого кэша.
OG_SRC = root / "src-assets" / "og.jpg"
OG_NAME = f"og.{hashlib.sha256(OG_SRC.read_bytes()).hexdigest()[:8]}.jpg"

HEAD_EXTRA = f"""<meta name="description" content="{DESC}">
<meta name="theme-color" content="#141110" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#E7E6DF" media="(prefers-color-scheme: light)">
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
    "@type": ["CafeOrCoffeeShop", "Store"],
    "name": "Чайня",
    "description": DESC,
    "url": SITE,
    "image": SITE + OG_NAME,
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
    "hasMap": "https://yandex.ru/maps/org/chaynya/",
    "sameAs": ["https://t.me/chainyamsk", "https://yandex.ru/maps/org/chaynya/"],
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


def document(body: str, extra_head: str = "") -> str:
    return (
        '<!doctype html>\n<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{TITLE}</title>\n"
        # Telegram Mini App SDK: даёт window.Telegram.WebApp внутри телеграма.
        # В обычном браузере platform='unknown' и вся мини-апп-логика молчит.
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>\n'
        f"{extra_head}\n"
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
    # CNAME подключает домен на GitHub Pages: файл в артефакте = кастомный домен
    (dist / "CNAME").write_text("chainya.ru\n", encoding="utf-8")
    (dist / "index.html").write_text(document(content, HEAD_EXTRA), encoding="utf-8")

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

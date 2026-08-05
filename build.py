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

# og:image обязан быть абсолютным: по относительному пути Telegram и соцсети
# картинку не подтянут.
SITE = "https://chainya.ru/"
SECURITY_CONTACT = "https://t.me/chainyabot"
SECURITY_EMAIL = "mailto:chainya@bk.ru"

# Телеграм кэширует саму картинку по её URL и по тому же адресу за новой не ходит:
# @WebpageBot перечитывает разметку страницы, но подменённый файл оставляет старый.
# Поэтому в имя подмешиваем хэш содержимого — правка карточки сама даёт новый URL,
# и ни телеграму, ни CDN нечего отдавать из старого кэша.
OG_SRC = root / "src-assets" / "og.jpg"
OG_NAME = f"og.{hashlib.sha256(OG_SRC.read_bytes()).hexdigest()[:8]}.jpg"
HERO_PRELOAD = '<link rel="preload" as="image" href="img/tea-baihao.webp" fetchpriority="high">'

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
<meta name="twitter:title" content="{TITLE}">
<meta name="twitter:description" content="{DESC}">
<link rel="canonical" href="{SITE}">
<link rel="icon" href="favicon.png" type="image/png">
<link rel="apple-touch-icon" href="favicon.png">
{HERO_PRELOAD}
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
                "postalCode": "129226",
                "addressLocality": "Москва",
                "streetAddress": "ул. Сергея Эйзенштейна, д. 6, корп. 2, стр. 2, кв. 233",
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
            "hasMap": "https://yandex.ru/maps/org/chaynya/49488428011/",
            "sameAs": ["https://t.me/chainyamsk", "https://yandex.ru/maps/org/chaynya/49488428011/"],
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


def document(
    body: str,
    extra_head: str = "",
    *,
    telegram_sdk: bool = True,
    title: str = TITLE,
) -> str:
    # В исходнике CSS хранится первым блоком, чтобы проект оставался одним
    # редактируемым файлом. В готовом HTML переносим его в <head>: браузер
    # получает стили до body, а документ остаётся валидным.
    style_match = re.match(r"\s*(<style>.*?</style>)\s*", body, flags=re.S)
    style_block = ""
    if style_match:
        style_block = style_match.group(1)
        body = body[style_match.end():]
    sdk = (
        """<script>
(()=>{const source=location.search+location.hash;
if(!/(?:^|[?&#])tgWebApp(?:Data|Version|Platform)=/i.test(source))return;
const script=document.createElement('script');
script.src='https://telegram.org/js/telegram-web-app.js';
script.async=true;
script.onload=()=>dispatchEvent(new Event('chainya:telegram-ready'));
document.head.appendChild(script);
})();
</script>
"""
        if telegram_sdk else ""
    )
    return (
        '<!doctype html>\n<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{title}</title>\n"
        # Telegram Mini App SDK нужен только основному приложению. Служебная
        # 404-страница не должна выполнять внешний JavaScript.
        f"{sdk}"
        f"{extra_head}\n"
        f"{style_block}\n"
        "<style>*{margin:0}</style>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )


ERROR_STYLE = """
<style>
@font-face{font-family:'Prata';src:url('/fonts/prata-cyr.woff2') format('woff2');font-display:swap}
@font-face{font-family:'Golos Text';src:url('/fonts/golos-cyr.woff2') format('woff2');font-display:swap}
:root{color-scheme:dark;--ink:#f1ece4;--muted:#b9afa4;--line:#403833;--accent:#df6b66;--paper:#141110;--panel:#1b1715}
*{box-sizing:border-box}
html{min-width:320px;background:var(--paper)}
body{min-height:100svh;background:var(--paper);color:var(--ink);font-family:'Golos Text',Arial,sans-serif}
.error-page{position:relative;min-height:100svh;display:grid;grid-template-rows:auto 1fr;overflow:hidden;padding:clamp(20px,4vw,48px)}
.error-page::before{content:attr(data-code);position:absolute;right:-.04em;bottom:-.2em;color:#201b19;font:clamp(180px,38vw,560px)/.8 'Prata',Georgia,serif;letter-spacing:-.08em;pointer-events:none;user-select:none}
.error-nav{position:relative;z-index:2;display:flex;align-items:center;gap:13px;width:max-content;color:var(--ink);text-decoration:none;letter-spacing:.16em;font-size:13px}
.error-nav img{width:31px;height:42px;object-fit:contain}
.error-layout{position:relative;z-index:1;align-self:center;display:grid;grid-template-columns:minmax(0,600px) minmax(230px,360px);align-items:center;justify-content:center;gap:clamp(42px,8vw,120px);width:min(1120px,100%);margin:auto}
.error-copy{padding-block:48px}
.error-kicker{color:var(--accent);font-size:12px;font-weight:650;letter-spacing:.16em;text-transform:uppercase}
.error-title{max-width:12ch;margin-top:18px;font:clamp(44px,7vw,88px)/1.03 'Prata',Georgia,serif;letter-spacing:-.035em;text-wrap:balance}
.error-text{max-width:34rem;margin-top:24px;color:var(--muted);font-size:clamp(16px,2vw,19px);line-height:1.6}
.error-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:32px}
.error-button{display:inline-flex;min-height:48px;align-items:center;justify-content:center;padding:0 21px;border:1px solid var(--line);color:var(--ink);text-decoration:none;font-size:14px;font-weight:600;transition:background .18s ease,border-color .18s ease,color .18s ease}
.error-button--primary{border-color:var(--accent);background:var(--accent);color:#171210}
.error-button:hover{border-color:var(--ink)}
.error-button--primary:hover{background:#ed7a74;border-color:#ed7a74}
.tea-scene{position:relative;display:grid;place-items:center;aspect-ratio:1;border:1px solid var(--line);border-radius:50%;background:radial-gradient(circle at 50% 60%,#2b2420 0 32%,var(--panel) 33% 100%)}
.tea-cup{position:relative;width:46%;height:25%;border:2px solid #d6c9b9;border-top:0;border-radius:0 0 48% 48%}
.tea-cup::before{content:'';position:absolute;left:-8%;right:-8%;top:-7px;height:14px;border:2px solid #d6c9b9;border-radius:50%;background:#261f1c}
.tea-cup::after{content:'';position:absolute;right:-24%;top:17%;width:28%;height:50%;border:2px solid #d6c9b9;border-left:0;border-radius:0 70% 70% 0}
.tea-leaf{position:absolute;left:24%;bottom:19%;width:20%;height:8%;border-radius:100% 0 100% 0;background:var(--accent);transform:rotate(-18deg)}
.steam{position:absolute;left:50%;top:19%;display:flex;gap:18px;transform:translateX(-50%)}
.steam i{display:block;width:2px;height:58px;background:linear-gradient(transparent,#b9afa4,transparent);border-radius:50%;transform:skewX(-8deg);animation:steam 2.8s ease-in-out infinite}
.steam i:nth-child(2){height:43px;margin-top:12px;animation-delay:-1.1s}
@keyframes steam{0%,100%{opacity:.25;transform:translateY(8px) skewX(-8deg)}50%{opacity:.9;transform:translateY(-7px) skewX(8deg)}}
@media (max-width:720px){.error-page{padding:20px}.error-page::before{right:-.05em;bottom:-.08em;font-size:55vw}.error-layout{grid-template-columns:1fr;gap:10px}.error-copy{padding:56px 0 18px}.tea-scene{width:min(270px,72vw);margin:0 auto 30px}.error-title{font-size:clamp(42px,13vw,68px)}.error-actions{display:grid;grid-template-columns:1fr}.error-button{width:100%}}
@media (prefers-reduced-motion:reduce){.steam i{animation:none}}
</style>
"""


def error_document(
    code: str,
    title: str,
    text: str,
    *,
    primary_href: str,
    primary_label: str,
) -> str:
    return document(
        ERROR_STYLE
        + f"""
<main class="error-page" data-code="{code}">
  <a class="error-nav" href="/" aria-label="Чайня — на главную">
    <img src="/img/logo-mark.webp" alt="" width="31" height="42">
    <span>ЧАЙНЯ</span>
  </a>
  <div class="error-layout">
    <section class="error-copy">
      <p class="error-kicker">Ошибка {code}</p>
      <h1 class="error-title">{title}</h1>
      <p class="error-text">{text}</p>
      <div class="error-actions">
        <a class="error-button error-button--primary" href="{primary_href}">{primary_label}</a>
        <a class="error-button" href="/shop">Выбрать чай</a>
      </div>
    </section>
    <div class="tea-scene" aria-hidden="true">
      <span class="steam"><i></i><i></i></span>
      <span class="tea-cup"></span>
      <span class="tea-leaf"></span>
    </div>
  </div>
</main>
""",
        '<meta name="robots" content="noindex,nofollow">',
        telegram_sdk=False,
        title=f"{code} · Чайня",
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
    # Многие браузеры и поисковые роботы всё ещё запрашивают именно этот путь.
    # PNG корректно распознаётся по сигнатуре даже при историческом расширении.
    shutil.copy(root / "src-assets" / "favicon.png", dist / "favicon.ico")
    shutil.copy(OG_SRC, dist / OG_NAME)
    shutil.copy(root / "privacy.html", dist / "privacy.html")
    shutil.copy(root / "legal.html", dist / "legal.html")
    shutil.copy(root / "legal.css", dist / "legal.css")
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
        error_document(
            "404",
            "Лист сбился с пути",
            "Здесь ничего не заваривается. Вернитесь на главную или загляните в каталог — чай на месте.",
            primary_href="/",
            primary_label="На главную",
        ),
        encoding="utf-8",
    )
    (dist / "50x.html").write_text(
        error_document(
            "50×",
            "Чайнику нужна минута",
            "Мы уже возвращаем всё на место. Попробуйте обновить страницу через пару минут.",
            primary_href="",
            primary_label="Попробовать ещё раз",
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
        f"  <url><loc>{SITE}shop</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>\n"
        f"  <url><loc>{SITE}business</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>\n"
        f"  <url><loc>{SITE}booking</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>\n"
        f"  <url><loc>{SITE}legal.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>\n"
        f"  <url><loc>{SITE}privacy.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>\n"
        "</urlset>\n",
        encoding="utf-8",
    )
    (dist / "index.html").write_text(document(content, HEAD_EXTRA), encoding="utf-8")

    route_meta = {
        "shop": (
            "Купить китайский чай · Чайня",
            "Китайский чай в пакетах 10, 25, 50 и 100 г с доставкой СДЭК по Москве и России.",
        ),
        "business": (
            "Чай для бизнеса и мероприятий · Чайня",
            "Поставки китайского чая для бизнеса и выездные чайные церемонии в Москве.",
        ),
        "booking": (
            "Бронь чайной церемонии · Чайня",
            "Забронируйте чайную церемонию с мастером или самостоятельное чаепитие на Острякова, 3.",
        ),
    }
    route_views = {"shop": "shop", "business": "b2b", "booking": "book"}
    for route, (route_title, route_desc) in route_meta.items():
        route_url = f"{SITE}{route}"
        route_head = (
            HEAD_EXTRA
            .replace(HERO_PRELOAD, "")
            .replace(f'<meta name="description" content="{DESC}">', f'<meta name="description" content="{route_desc}">')
            .replace(f'<meta property="og:title" content="{TITLE}">', f'<meta property="og:title" content="{route_title}">')
            .replace(f'<meta property="og:description" content="{DESC}">', f'<meta property="og:description" content="{route_desc}">')
            .replace(f'<meta name="twitter:title" content="{TITLE}">', f'<meta name="twitter:title" content="{route_title}">')
            .replace(f'<meta name="twitter:description" content="{DESC}">', f'<meta name="twitter:description" content="{route_desc}">')
            .replace(f'<meta property="og:url" content="{SITE}">', f'<meta property="og:url" content="{route_url}">')
            .replace(f'<link rel="canonical" href="{SITE}">', f'<link rel="canonical" href="{route_url}">')
        )
        route_dir = dist / route
        route_dir.mkdir()
        route_content = content.replace(
            '<section class="view is-active" id="view-home">',
            '<section class="view" id="view-home">',
        ).replace(
            f'<section class="view" id="view-{route_views[route]}">',
            f'<section class="view is-active" id="view-{route_views[route]}">',
        )
        (route_dir / "index.html").write_text(
            document(route_content, route_head, title=route_title),
            encoding="utf-8",
        )

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

    html_files = list(dist.rglob("*.html"))
    max_html = max(path.stat().st_size for path in html_files)
    total = sum(path.stat().st_size for path in dist.rglob("*") if path.is_file())
    html_total = sum(path.stat().st_size for path in html_files)
    assets = total - html_total
    print(f"HTML на запрос    {round(max_html / 1024)} KB максимум")
    print(f"dist/img + fonts  {round(assets / 1024)} KB  в {len(used) + 4} файлах (кэшируются)")
    print(f"итого на диске    {round(total / 1024)} KB")
else:
    (root / "artifact.html").write_text(content, encoding="utf-8")
    (root / "index.html").write_text(document(content), encoding="utf-8")
    print(f"картинок вшито: {len(used)}")
    for f in ("index.html", "artifact.html"):
        print(f"{f:16} {round((root / f).stat().st_size / 1024)} KB")

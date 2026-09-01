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
asset_root = "/" if web else ""

src = (root / "src.html").read_text(encoding="utf-8")
assert "/*@FONTS@*/" in src, "маркер /*@FONTS@*/ пропал из src.html"

# Тот же заголовок, что в словаре I18N.ru: JS перепишет его при старте,
# но краулерам и первой отрисовке достаётся статический.
TITLE = "Чайня · китайский чай с доставкой"
DESC = ("Китайский чай с доставкой по Москве и России: белый, зелёный, "
        "улуны, красный чай, пуэр и авторские сборы. Чайная на Острякова, 3.")
PUBLIC_PAGE_META = {
    "ru": {
        "html_lang": "ru", "schema_lang": "ru-RU", "hreflang": "ru", "og_locale": "ru_RU",
        "brand": "Чайня", "home_label": "Главная",
        "home": (TITLE, DESC, "Главная"),
        "shop": ("Купить китайский чай · Чайня", "Китайский чай в пакетах 10, 25, 50 и 100 г с доставкой СДЭК по Москве и России.", "Чай"),
        "teaware": ("Чайная посуда · Чайня", "Чайники, гайвани, пиалы, чахаи, чахэ, чайные фигурки, инструменты и наборы в каталоге Чайни.", "Посуда"),
        "business": ("Чай для бизнеса и мероприятий · Чайня", "Поставки китайского чая для бизнеса и выездные чайные церемонии в Москве.", "Для бизнеса"),
        "booking": ("Бронь чайной церемонии · Чайня", "Забронируйте чайную церемонию с мастером или самостоятельное чаепитие на Острякова, 3.", "Бронь"),
        "image_alt": "Китайский чай и чайная «Чайня» в Москве",
    },
    "en": {
        "html_lang": "en", "schema_lang": "en", "hreflang": "en", "og_locale": "en_US",
        "brand": "Chaynya", "home_label": "Home",
        "home": ("Chaynya · Chinese tea delivered", "Chinese tea delivered across Moscow and Russia: white, green, oolong, black tea, pu-erh and house herbal blends. Tea room at 3 Ostryakova Street.", "Home"),
        "shop": ("Buy Chinese tea · Chaynya", "Chinese tea in 10, 25, 50 and 100 g packs, delivered by CDEK across Moscow and Russia.", "Tea"),
        "teaware": ("Chinese teaware · Chaynya", "Teapots, gaiwans, tea cups, chahai, chahe, tea pets, tools and teaware sets from Chaynya.", "Teaware"),
        "business": ("Tea for business and events · Chaynya", "Wholesale Chinese tea for businesses and off-site tea ceremonies in Moscow.", "For business"),
        "booking": ("Book a tea ceremony · Chaynya", "Book a hosted tea ceremony or a private self-service tea session at 3 Ostryakova Street.", "Booking"),
        "image_alt": "Chinese tea and the Chaynya tea room in Moscow",
    },
    "zh": {
        "html_lang": "zh-CN", "schema_lang": "zh-CN", "hreflang": "zh-CN", "og_locale": "zh_CN",
        "brand": "茶饮屋", "home_label": "首页",
        "home": ("Chaynya · 中国茶配送", "中国茶配送至莫斯科及俄罗斯各地：白茶、绿茶、乌龙茶、红茶、普洱茶和自制草本拼配。茶室位于奥斯特里亚科娃街3号。", "首页"),
        "shop": ("购买中国茶 · Chaynya", "中国茶提供10克、25克、50克和100克包装，由CDEK配送至莫斯科及俄罗斯各地。", "茶"),
        "teaware": ("茶具 · Chaynya", "Chaynya茶具目录：茶壶、盖碗、茶杯、公道杯、茶荷、茶宠、茶道工具和茶具套装。", "茶具"),
        "business": ("企业与活动用茶 · Chaynya", "为企业提供中国茶批发，并在莫斯科承办外出茶会。", "企业合作"),
        "booking": ("预约茶会 · Chaynya", "预约茶艺师主持的茶会，或在奥斯特里亚科娃街3号自行泡茶。", "预约"),
        "image_alt": "莫斯科Chaynya茶室与中国茶",
    },
}

CRITICAL_ROUTE_COPY = {
    "ru": {
        "shop_heading": "Каталог китайского чая",
        "teaware_heading": "Каталог чайной посуды",
        "hero_lead": "Отбираем китайский чай, который сами пьём и завариваем в Чайне. Можно взять небольшой пакет на пробу, собрать заказ домой или сначала познакомиться с чаем за нашим столом.",
        "book_lead": "Выберите формат, дату и свободное время. Каждая заявка занимает единственный стол на два часа.",
        "sum_note": "Платить сейчас не нужно. После отправки время закрепится за вашей заявкой, а владельцы свяжутся с вами и подтвердят бронь.",
    },
    "en": {
        "shop_heading": "Chinese tea catalogue",
        "teaware_heading": "Chinese teaware catalogue",
        "hero_lead": "We select the Chinese teas we drink and brew at Chaynya ourselves. Start with a small bag, build an order for home, or get to know the tea at our table first.",
        "book_lead": "Choose the format, date and an available time. Each request occupies our only table for two hours.",
        "sum_note": "Nothing to pay now. After you send the request, the time is held for you while the owners contact you and confirm the booking.",
    },
    "zh": {
        "shop_heading": "中国茶目录",
        "teaware_heading": "茶具目录",
        "hero_lead": "我们只挑自己会喝、也会在Chaynya亲手冲泡的中国茶。可以买一小包试喝，也可以为家里组合一份订单；还可以先到店里坐下来认识这款茶。",
        "book_lead": "请选择形式、日期和可用时间。每份申请都会占用店内唯一茶席两小时。",
        "sum_note": "现在无需付款。提交后该时段将为您的申请保留，店主会联系您并确认预订。",
    },
}
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


def public_page_url(route: str, language: str) -> str:
    """Return the one canonical URL for a localized public application route."""
    prefix = "" if language == "ru" else f"/{language}"
    if route == "home":
        return SITE if language == "ru" else f"{SITE.rstrip('/')}{prefix}/"
    return f"{SITE.rstrip('/')}{prefix}/{route}"

# Телеграм кэширует саму картинку по её URL и по тому же адресу за новой не ходит:
# @WebpageBot перечитывает разметку страницы, но подменённый файл оставляет старый.
# Поэтому в имя подмешиваем хэш содержимого — правка карточки сама даёт новый URL,
# и ни телеграму, ни CDN нечего отдавать из старого кэша.
OG_SRC = root / "src-assets" / "og.jpg"
OG_NAME = f"og.{hashlib.sha256(OG_SRC.read_bytes()).hexdigest()[:8]}.jpg"
HERO_SOURCE = "/catalog-media/current-hero.webp" if web else f"{asset_root}img/tea-baihao.webp"
HERO_PRELOAD = (
    f'<link rel="preload" as="image" href="{HERO_SOURCE}" fetchpriority="high">'
    if web else ""
)
FONT_PRELOAD = "\n".join(
    f'<link rel="preload" as="font" href="/fonts/{name}.woff2" type="font/woff2" crossorigin>'
    for name in ("prata-cyr", "prata-lat", "golos-cyr", "golos-lat")
) if web else ""

ROUTE_IMAGE_PRELOADS = {
    "teaware": '<link rel="preload" as="image" href="/img/kintsugi-work-1.webp" fetchpriority="high">',
} if web else {}

def seo_head(
    *,
    page_title: str = TITLE,
    page_desc: str = DESC,
    page_url: str = SITE,
    page_label: str = "Главная",
    route: str = "home",
    language: str = "ru",
    preload_hero: bool = True,
) -> str:
    """Статическое SEO для конкретного публичного URL.

    Контент переключается в браузере, но поисковому роботу сразу отдаём
    самостоятельную страницу с собственными метаданными и JSON-LD.
    """
    locale = PUBLIC_PAGE_META[language]
    alternates = {
        code: public_page_url(route, code)
        for code in PUBLIC_PAGE_META
    }
    graph = [
        {
            "@type": "Organization",
            "@id": SITE + "#seller",
            "name": SELLER_NAME,
            "alternateName": "Чайня",
            "url": SITE,
            "logo": SITE + "img/logo-mark.webp",
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
            "logo": SITE + "img/logo-mark.webp",
            "email": SELLER_EMAIL,
            "telephone": "+7 905 590-88-01",
            "priceRange": "₽₽",
            "currenciesAccepted": "RUB",
            "servesCuisine": "Чай",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "улица Острякова, 3, помещение 114",
                "addressLocality": "Москва",
                "postalCode": "125057",
                "addressCountry": "RU",
            },
            "geo": {
                "@type": "GeoCoordinates",
                "latitude": 55.799473,
                "longitude": 37.526791,
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
        {
            "@type": "WebSite",
            "@id": SITE + "#website",
            "url": SITE,
            "name": "Чайня",
            "alternateName": "Chainya",
            "inLanguage": ["ru-RU", "en", "zh-CN"],
            "publisher": {"@id": SITE + "#seller"},
        },
        {
            "@type": "WebPage",
            "@id": page_url + "#webpage",
            "url": page_url,
            "name": page_title,
            "description": page_desc,
            "inLanguage": locale["schema_lang"],
            "isPartOf": {"@id": SITE + "#website"},
            "about": {"@id": SITE + "#store"},
            "primaryImageOfPage": {
                "@type": "ImageObject",
                "url": SITE + OG_NAME,
                "width": 1200,
                "height": 630,
            },
        },
    ]
    if route != "home":
        graph.append({
            "@type": "BreadcrumbList",
            "@id": page_url + "#breadcrumb",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "name": locale["home_label"],
                    "item": SITE,
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "name": page_label,
                    "item": page_url,
                },
            ],
        })
    structured_data = json.dumps(
        {"@context": "https://schema.org", "@graph": graph},
        ensure_ascii=False,
    ).join(('<script type="application/ld+json">', '</script>'))
    preload = "\n".join(part for part in (
        FONT_PRELOAD,
        HERO_PRELOAD if preload_hero else "",
        ROUTE_IMAGE_PRELOADS.get(route, ""),
    ) if part)
    image_alt = locale["image_alt"]
    alternate_links = "\n".join(
        f'<link rel="alternate" hreflang="{PUBLIC_PAGE_META[code]["hreflang"]}" href="{url}">'
        for code, url in alternates.items()
    )
    alternate_links += f'\n<link rel="alternate" hreflang="x-default" href="{alternates["ru"]}">'
    og_alternates = "\n".join(
        f'<meta property="og:locale:alternate" content="{details["og_locale"]}">'
        for code, details in PUBLIC_PAGE_META.items()
        if code != language
    )
    return f"""<meta name="description" content="{page_desc}">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1">
<meta name="theme-color" content="#141110">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{locale['brand']}">
<meta property="og:title" content="{page_title}">
<meta property="og:description" content="{page_desc}">
<meta property="og:locale" content="{locale['og_locale']}">
{og_alternates}
<meta property="og:url" content="{page_url}">
<meta property="og:image" content="{SITE}{OG_NAME}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{image_alt}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{page_title}">
<meta name="twitter:description" content="{page_desc}">
<meta name="twitter:image" content="{SITE}{OG_NAME}">
<meta name="twitter:image:alt" content="{image_alt}">
<link rel="canonical" href="{page_url}">
{alternate_links}
<link rel="icon" href="{asset_root}favicon.png" type="image/png">
<link rel="apple-touch-icon" href="{asset_root}favicon.png">
{preload}
{structured_data}"""


HEAD_EXTRA = seo_head()


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
        out += f"url(/fonts/{name}.woff2)" + tail
    # На медленном мобильном соединении поздняя подмена метрик шрифта заметно
    # сдвигает каталог и форму бронирования. Если шрифт не успел в короткое
    # окно, браузер оставляет системный fallback вместо позднего layout shift.
    return out.replace("font-display:swap", "font-display:optional")


used, missing = set(), set()


def img_ref(m):
    name = m.group(1)
    f = root / "img" / f"{name}.webp"
    if not f.exists():
        missing.add(name)
        return ""
    used.add(name)
    if web:
        return f"/img/{name}.webp"
    return "data:image/webp;base64," + base64.b64encode(f.read_bytes()).decode()


content = src.replace("/*@FONTS@*/", font_css(inline=not web))
content = re.sub(r"\{\{img:([a-z0-9\-]+)\}\}", img_ref, content)
if web:
    content = content.replace(
        'id="hero-img" src="/img/tea-baihao.webp"',
        'id="hero-img" src="/catalog-media/current-hero.webp"',
    )

if missing:
    raise SystemExit("НЕТ КАРТИНОК: " + ", ".join(sorted(missing)))

have = {p.stem for p in (root / "img").glob("*.webp")}
if unused := have - used:
    print("не используются:", ", ".join(sorted(unused)))


def document(
    body: str,
    extra_head: str = "",
    *,
    title: str = TITLE,
    html_lang: str = "ru",
) -> str:
    # В исходнике CSS хранится первым блоком, чтобы проект оставался одним
    # редактируемым файлом. В готовом HTML переносим его в <head>: браузер
    # получает стили до body, а документ остаётся валидным.
    style_match = re.match(r"\s*(<style>.*?</style>)\s*", body, flags=re.S)
    style_block = ""
    if style_match:
        style_block = style_match.group(1)
        body = body[style_match.end():]
    return (
        f'<!doctype html>\n<html lang="{html_lang}">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{title}</title>\n"
        f"{extra_head}\n"
        f"{style_block}\n"
        "<style>*{margin:0}</style>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )


def extract_inline_script(document_source: str, asset_path: str) -> tuple[str, str]:
    """Move the one executable inline script into a same-origin build asset."""

    match = re.search(r"<script>(.*?)</script>", document_source, flags=re.DOTALL)
    if not match:
        raise SystemExit(f"НЕТ INLINE SCRIPT ДЛЯ {asset_path}")
    external = f'<script src="{asset_path}" defer></script>'
    html_source = document_source[: match.start()] + external + document_source[match.end() :]
    return html_source, match.group(1).strip() + "\n"


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
        title=f"{code} · Чайня",
    )


if web:
    dist = root / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    (dist / "img").mkdir(parents=True)
    (dist / "fonts").mkdir()
    (dist / "assets").mkdir()
    for name in sorted(used):
        shutil.copy(root / "img" / f"{name}.webp", dist / "img" / f"{name}.webp")
    for f in ("prata-cyr", "prata-lat", "golos-cyr", "golos-lat"):
        shutil.copy(root / "fonts" / f"{f}.woff2", dist / "fonts" / f"{f}.woff2")
    shutil.copy(root / "src-assets" / "favicon.png", dist / "favicon.png")
    # Многие браузеры и поисковые роботы всё ещё запрашивают именно этот путь.
    # PNG корректно распознаётся по сигнатуре даже при историческом расширении.
    shutil.copy(root / "src-assets" / "favicon.png", dist / "favicon.ico")
    shutil.copy(OG_SRC, dist / OG_NAME)
    public_content, public_script = extract_inline_script(content, "/assets/site.js")
    # HTML and JavaScript are deployed together. A content version prevents a
    # browser/service worker from combining fresh markup with an older script.
    site_script_version = hashlib.sha256(public_script.encode("utf-8")).hexdigest()[:12]
    public_content = public_content.replace(
        'src="/assets/site.js"',
        f'src="/assets/site.js?v={site_script_version}"',
    )
    (dist / "assets" / "site.js").write_text(public_script, encoding="utf-8")
    for page_name in ("privacy", "legal", "consent-personal-data"):
        page_html, page_script = extract_inline_script(
            (root / f"{page_name}.html").read_text(encoding="utf-8"),
            f"/assets/{page_name}.js",
        )
        (dist / f"{page_name}.html").write_text(page_html, encoding="utf-8")
        (dist / "assets" / f"{page_name}.js").write_text(page_script, encoding="utf-8")
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
    sitemap_rows = []
    for route, frequency, priority in (
        ("home", "weekly", "1.0"),
        ("shop", "weekly", "0.9"),
        ("teaware", "weekly", "0.8"),
        ("business", "monthly", "0.7"),
        ("booking", "monthly", "0.7"),
    ):
        localized = {code: public_page_url(route, code) for code in PUBLIC_PAGE_META}
        links = "".join(
            f'<xhtml:link rel="alternate" hreflang="{PUBLIC_PAGE_META[code]["hreflang"]}" href="{url}"/>'
            for code, url in localized.items()
        )
        links += f'<xhtml:link rel="alternate" hreflang="x-default" href="{localized["ru"]}"/>'
        for url in localized.values():
            sitemap_rows.append(
                f"  <url><loc>{url}</loc>{links}<changefreq>{frequency}</changefreq><priority>{priority}</priority></url>"
            )
    sitemap_rows.extend((
        f"  <url><loc>{SITE}legal.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>",
        f"  <url><loc>{SITE}privacy.html</loc><changefreq>monthly</changefreq><priority>0.4</priority></url>",
        f"  <url><loc>{SITE}consent-personal-data.html</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>",
    ))
    (dist / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        + "\n".join(sitemap_rows)
        + "\n</urlset>\n",
        encoding="utf-8",
    )
    (dist / "index.html").write_text(
        document(public_content, HEAD_EXTRA),
        encoding="utf-8",
    )

    route_views = {"shop": "shop", "teaware": "shop", "business": "b2b", "booking": "book"}
    for language, locale in PUBLIC_PAGE_META.items():
        language_root = dist if language == "ru" else dist / language
        if language != "ru":
            language_root.mkdir()
            home_title, home_desc, home_label = locale["home"]
            home_url = public_page_url("home", language)
            (language_root / "index.html").write_text(
                document(
                    public_content,
                    seo_head(
                        page_title=home_title,
                        page_desc=home_desc,
                        page_url=home_url,
                        page_label=home_label,
                        route="home",
                        language=language,
                    ),
                    title=home_title,
                    html_lang=locale["html_lang"],
                ),
                encoding="utf-8",
            )
        for route, route_view in route_views.items():
            route_title, route_desc, route_label = locale[route]
            route_url = public_page_url(route, language)
            route_head = seo_head(
                page_title=route_title,
                page_desc=route_desc,
                page_url=route_url,
                page_label=route_label,
                route=route,
                language=language,
                preload_hero=False,
            )
            route_dir = language_root / route
            route_dir.mkdir()
            route_content = public_content.replace(
                '<section class="view is-active" id="view-home">',
                '<section class="view" id="view-home">',
            ).replace(
                f'<section class="view" id="view-{route_view}">',
                f'<section class="view is-active" id="view-{route_view}">',
            )
            critical = CRITICAL_ROUTE_COPY[language]
            if route == "home":
                route_content = re.sub(
                    r'(<p class="lead hero__lead" data-i18n="hero_lead">).*?(</p>)',
                    rf'\1{critical["hero_lead"]}\2',
                    route_content,
                    count=1,
                )
            if route in {"shop", "teaware"}:
                heading_key = "teaware_heading" if route == "teaware" else "shop_heading"
                route_content = re.sub(
                    r'(<h1 class="shop-heading" id="shop-heading" data-i18n="shop_heading">).*?(</h1>)',
                    rf'\1{critical[heading_key]}\2',
                    route_content,
                    count=1,
                )
            if route == "teaware":
                route_content = route_content.replace(
                    '<section class="repair-service" id="repair-service" hidden',
                    '<section class="repair-service" id="repair-service"',
                    1,
                )
            if route == "booking":
                route_content = re.sub(
                    r'(<p class="lead" data-i18n="book_lead">).*?(</p>)',
                    rf'\1{critical["book_lead"]}\2',
                    route_content,
                    count=1,
                )
                route_content = re.sub(
                    r'(<p class="summary__note" data-i18n="sum_note">).*?(</p>)',
                    rf'\1{critical["sum_note"]}\2',
                    route_content,
                    count=1,
                )
            (route_dir / "index.html").write_text(
                document(
                    route_content,
                    route_head,
                    title=route_title,
                    html_lang=locale["html_lang"],
                ),
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

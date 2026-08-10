#!/usr/bin/env python3
"""Обложка /start — дизайнерская, а не фото зала.

Показывает суть: чайная-маркетплейс с десятками сортов. Фон — сетка из фото
сухого листа, поверх тёмная подложка и брендовый блок с призывом выбрать чай.
Фото зала теперь живёт за кнопкой «Наш зал», а старт — витрина.
"""
import json
import pathlib

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = pathlib.Path(__file__).parent
TEA_DIR = HERE / "media" / "teas"
SITE = HERE.resolve().parent if (HERE.resolve().parent / "src.html").is_file() else HERE.resolve().parent / "site"
FONTS = SITE / "src-assets" / "fonts-full"
LOGO = SITE / "src-assets" / "logo-mark.png"
prata = lambda s: ImageFont.truetype(str(FONTS / "prata.ttf"), s)
golos = lambda s: ImageFont.truetype(str(FONTS / "golos.ttf"), s)

INK = (243, 239, 232)
GOLD = (200, 168, 75)
GOLD_TIP = (224, 198, 128)
MUTE = (176, 168, 158)

W, H = 1280, 720
data = json.load(open(HERE / "teas.json", encoding="utf-8"))
n_teas = len(data["teas"])


def plural(n):
    if n % 10 == 1 and n % 100 != 11:
        return "сорт"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "сорта"
    return "сортов"


# ── знак ЧНЯ кремовым ──
_m = Image.open(LOGO).convert("RGBA")
_let = ImageChops.multiply(ImageChops.invert(_m.convert("RGB").convert("L")), _m.split()[3])
_let = _let.point(lambda v: 255 if v > 135 else 0)
_let = _let.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.6))


def mark(h, color=INK):
    a = _let.resize((round(_let.width * h / _let.height), h), Image.LANCZOS)
    g = Image.new("RGBA", a.size, color + (0,))
    g.putalpha(a)
    return g


# ── фон: сетка фото чая 6×3 ──
ids = [t["id"] for t in data["teas"] if (TEA_DIR / f"{t['id']}.jpg").exists()]
cols, rows_n = 6, 3
tw, th = W // cols, H // rows_n
pick = [ids[(i * 7) % len(ids)] for i in range(cols * rows_n)]  # разбросать по карте
bg = Image.new("RGB", (W, H))
for idx, tid in enumerate(pick):
    im = Image.open(TEA_DIR / f"{tid}.jpg").convert("RGB")
    s = max(tw / im.width, th / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    im = im.crop(((im.width - tw) // 2, (im.height - th) // 2, (im.width - tw) // 2 + tw, (im.height - th) // 2 + th))
    bg.paste(im, ((idx % cols) * tw, (idx // cols) * th))
bg = ImageEnhance.Brightness(bg).enhance(0.5)
bg = ImageEnhance.Color(bg).enhance(0.82)

# затемняющая подложка + мягкий центральный фокус под текст
bg = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (W, H), (10, 8, 7, 90)))
focus = Image.new("L", (200, 200), 0)
fd = ImageDraw.Draw(focus)
fd.ellipse([10, 40, 190, 160], fill=180)
focus = focus.filter(ImageFilter.GaussianBlur(40)).resize((W, H))
bg = Image.composite(Image.new("RGBA", (W, H), (8, 6, 5, 255)), bg, focus)
img = bg.convert("RGB")
d = ImageDraw.Draw(img)

cx = W // 2
# бренд
mh = 58
mk = mark(mh)
brand = "Ч А Й Н Я"
bf = prata(38)
bw = bf.getlength(brand)
total = mk.width + 18 + bw
x0 = cx - int(total) // 2
img.paste(mk, (x0, 150), mk)
d.text((x0 + mk.width + 18, 150 + mh // 2), brand, font=bf, fill=INK, anchor="lm")

# заголовок-призыв
d.text((cx, 300), "Выбирайте свой чай", font=prata(82), fill=INK, anchor="mm")

# золотая плашка с числом сортов
badge = f"{n_teas} {plural(n_teas)} китайского чая"
bff = golos(30)
bwid = bff.getlength(badge)
px, py, ph = int(bwid) + 96, 400, 62
d.rounded_rectangle([cx - px // 2, py, cx + px // 2, py + ph], radius=ph // 2, outline=GOLD, width=3)
d.text((cx, py + ph // 2), badge, font=bff, fill=GOLD_TIP, anchor="mm")

# подстрочник
d.text((cx, 512), "Навынос по России · и за столом у метро Аэропорт",
       font=golos(27), fill=MUTE, anchor="mm")

out = HERE / "media" / "start.jpg"
img.save(out, "JPEG", quality=92, progressive=True)
print("start.jpg:", img.size, "| сортов:", n_teas)

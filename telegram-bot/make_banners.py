#!/usr/bin/env python3
"""Баннеры разделов бота — по картинке на каждый экран (не только карточки/старт).

Один шаблон, разный фон+подпись: атмосферное фото, слева тёмный градиент под
текст, надстрочник (золотом) + крупный заголовок (Prata) + знак ЧНЯ. 1280×680.
Экраны раздела делят один баннер (все шаги брони — booking, и т.д.).
Выход: media/banners/<name>.jpg
"""
import json
import pathlib

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = pathlib.Path(__file__).parent
TA = HERE.resolve().parent if (HERE.resolve().parent / "src.html").is_file() else HERE.resolve().parent / "site"
IMG = TA / "img"
TEAS = HERE / "media" / "teas"
OUT = HERE / "media" / "banners"
OUT.mkdir(parents=True, exist_ok=True)
FONTS = TA / "src-assets" / "fonts-full"
LOGO = TA / "src-assets" / "logo-mark.png"
prata = lambda s: ImageFont.truetype(str(FONTS / "prata.ttf"), s)
golos = lambda s: ImageFont.truetype(str(FONTS / "golos.ttf"), s)

INK = (243, 239, 232)
GOLD = (206, 176, 92)
MUTE = (176, 168, 158)

W, H = 1280, 680
n_teas = len(json.load(open(HERE / "teas.json", encoding="utf-8"))["teas"])

_m = Image.open(LOGO).convert("RGBA")
_let = ImageChops.multiply(ImageChops.invert(_m.convert("RGB").convert("L")), _m.split()[3])
_let = _let.point(lambda v: 255 if v > 135 else 0)
_let = _let.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.6))


def mark(h, color=INK):
    a = _let.resize((round(_let.width * h / _let.height), h), Image.LANCZOS)
    g = Image.new("RGBA", a.size, color + (0,))
    g.putalpha(a)
    return g


# горизонтальный градиент «тьма слева → прозрачно справа» под текст
def left_scrim():
    g = Image.new("L", (W, 1))
    gx = g.load()
    for x in range(W):
        t = min(1.0, x / (W * 0.72))
        gx[x, 0] = int(238 * (1 - t) ** 1.7)
    return g.resize((W, H))


SCRIM = left_scrim()


def fit(text, maker, max_w, hi, lo):
    for s in range(hi, lo - 1, -2):
        f = maker(s)
        if f.getlength(text) <= max_w:
            return f
    return maker(lo)


def spaced(s):
    return " ".join(list(s))  # тонкие шпации между буквами надстрочника


def banner(name, eyebrow, title, src):
    photo = Image.open(src).convert("RGB")
    scale = max(W / photo.width, H / photo.height)
    photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    ox, oy = (photo.width - W) // 2, (photo.height - H) // 2
    photo = photo.crop((ox, oy, ox + W, oy + H))
    photo = ImageEnhance.Brightness(photo).enhance(0.62)
    photo = ImageEnhance.Color(photo).enhance(0.92)
    # тёмная подложка слева
    photo.paste(Image.new("RGB", (W, H), (10, 8, 7)), (0, 0), SCRIM)
    d = ImageDraw.Draw(photo)

    x = 74
    mk = mark(46)
    photo.paste(mk, (x, 150), mk)
    d.text((x + mk.width + 16, 150 + 23), "ЧАЙНЯ", font=prata(30), fill=INK, anchor="lm")

    d.text((x, 300), spaced(eyebrow), font=golos(24), fill=GOLD, anchor="lm")
    tf = fit(title, prata, W - x - 130, 96, 52)
    d.text((x, 300 + 78), title, font=tf, fill=INK, anchor="lm")
    # золотая черта
    d.line([(x + 2, 300 + 150), (x + 150, 300 + 150)], fill=GOLD, width=3)

    photo.save(OUT / f"{name}.jpg", "JPEG", quality=90, progressive=True)
    print("  ", name)


print("баннеры:")
banner("menu", "Чайная у метро Аэропорт · 12–22", "Чайня", IMG / "hero-master.webp")
banner("catalog", f"Купить чай · {n_teas} сорта", "Каталог", TEAS / "dahongpao.jpg")
banner("taste", "Подберём под настроение", "По вкусу", TEAS / "osmanthus.jpg")
banner("booking", "Бронь · церемония с мастером", "За столом", HERE / "media" / "table.jpg")

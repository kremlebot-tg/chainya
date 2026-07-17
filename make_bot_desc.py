#!/usr/bin/env python3
"""Картинка описания бота (экран ДО «Запустить») — дизайн-карточка.

Проще и чётче прежней: фраза — герой, радар-«характер» без мелких подписей осей
(они мутнели в 640×360) и с толстыми штрихами. Рисуем в 1920×1080 (×3) и
уменьшаем до 640×360 (супер-сэмплинг = резче). @BotFather требует РОВНО 640×360.
"""
import math
import pathlib

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

root = pathlib.Path(__file__).parent
FONTS = root / "src-assets" / "fonts-full"
prata = lambda s: ImageFont.truetype(str(FONTS / "prata.ttf"), s)

INK = (243, 239, 232)
RED = (214, 96, 88)
GOLD = (206, 176, 92)
BG_IN, BG_OUT = (32, 26, 24), (12, 10, 9)

W, H = 1920, 1080          # рендер ×3 → финал 640×360

_m = Image.open(root / "src-assets" / "logo-mark.png").convert("RGBA")
_let = ImageChops.multiply(ImageChops.invert(_m.convert("RGB").convert("L")), _m.split()[3])
_let = _let.point(lambda v: 255 if v > 135 else 0)
_let = _let.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.6))


def mark(h, color=INK):
    a = _let.resize((round(_let.width * h / _let.height), h), Image.LANCZOS)
    g = Image.new("RGBA", a.size, color + (0,))
    g.putalpha(a)
    return g


def radial(size, inner, outer, power=1.4):
    g = Image.new("RGB", (size, size)); px = g.load(); c = size / 2
    for y in range(size):
        for x in range(size):
            t = (min(1.0, ((x - c) ** 2 + (y - c) ** 2) ** 0.5 / c)) ** power
            px[x, y] = tuple(round(inner[i] * (1 - t) + outer[i] * t) for i in range(3))
    return g


img = radial(420, BG_IN, BG_OUT).resize((W, H), Image.BILINEAR).convert("RGBA")

# ── радар-«характер» справа, БЕЗ подписей, толстыми штрихами ──
SC = [1, 5, 4, 2, 0, 2, 1, 1, 0]     # профиль габы «Рубин» (яркий фруктово-сухофруктовый)
n = len(SC)
cx, cy, Rr = 1430, 560, 300
ang = lambda i: math.radians(-90 + i * 360 / n)
pt = lambda i, r: (cx + r * math.cos(ang(i)), cy + r * math.sin(ang(i)))

lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ld = ImageDraw.Draw(lay)
for f in (0.4, 0.7, 1.0):                                   # кольца
    ld.polygon([pt(i, Rr * f) for i in range(n)], outline=(74, 64, 58, 255), width=4)
for i in range(n):                                          # спицы
    ld.line([(cx, cy), pt(i, Rr)], fill=(60, 52, 47, 255), width=3)
poly = [pt(i, Rr * SC[i] / 5) for i in range(n)]
ld.polygon(poly, fill=RED + (66,), outline=RED + (255,), width=9)
for i in range(n):
    if SC[i]:
        x, y = pt(i, Rr * SC[i] / 5)
        ld.ellipse([x - 12, y - 12, x + 12, y + 12], fill=RED + (255,))
img = Image.alpha_composite(img, lay).convert("RGB")
d = ImageDraw.Draw(img)

# ── левая часть: бренд + фраза-герой ──
X = 150
mh = 74
mk = mark(mh)
img.paste(mk, (X, 150), mk)
d.text((X + mk.width + 26, 150 + mh // 2), "Ч А Й Н Я", font=prata(50), fill=GOLD, anchor="lm")

fh = prata(140)
d.text((X, 402), "У каждого чая", font=fh, fill=INK)
d.text((X, 576), "свой характер", font=fh, fill=RED)
d.line([(X + 4, 772), (X + 250, 772)], fill=GOLD, width=5)

out = root / "src-assets" / "bot-desc.jpg"
img = img.resize((640, 360), Image.LANCZOS)
img.save(out, "JPEG", quality=95, progressive=True)
print("bot-desc.jpg:", img.size)

import shutil
dst = pathlib.Path.home() / "Desktop" / "chainya-bot-ассеты"
dst.mkdir(exist_ok=True)
shutil.copy(out, dst / "description-picture.jpg")
print("копия:", dst / "description-picture.jpg")

#!/usr/bin/env python3
"""Картинка описания бота (экран ДО «Запустить») — не фото, а дизайн-карточка.

Показывает фишку: у каждого чая свой вкусовой профиль (паутинка, как на сайте).
Слева бренд + строка, справа крупная диаграмма реального чая. 1280×720.
"""
import math
import pathlib
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

root = pathlib.Path(__file__).parent
FONTS = root / "src-assets" / "fonts-full"
prata = lambda s: ImageFont.truetype(str(FONTS / "prata.ttf"), s)
golos = lambda s: ImageFont.truetype(str(FONTS / "golos.ttf"), s)

INK = (241, 237, 230)
RED = (219, 106, 98)      # тёплый красный (акцент тёмной темы)
RED_DEEP = (190, 59, 48)
MUTE = (150, 142, 133)
LINE = (60, 52, 49)

# знак ЧНЯ кремовым (как в make_bot_assets)
_m = Image.open(root / "src-assets" / "logo-mark.png").convert("RGBA")
_let = ImageChops.multiply(ImageChops.invert(_m.convert("RGB").convert("L")), _m.split()[3])
_let = _let.point(lambda v: 255 if v > 135 else 0)
_let = _let.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.6))


def mark(h, color=INK):
    a = _let.resize((round(_let.width * h / _let.height), h), Image.LANCZOS)
    g = Image.new("RGBA", a.size, color + (0,))
    g.putalpha(a)
    return g


W, H = 1280, 720


def radial(size, inner, outer, power=1.5):
    g = Image.new("RGB", (size, size)); px = g.load(); c = size / 2
    for y in range(size):
        for x in range(size):
            d = min(1.0, ((x - c) ** 2 + (y - c) ** 2) ** 0.5 / c)
            t = d ** power
            px[x, y] = tuple(round(inner[i] * (1 - t) + outer[i] * t) for i in range(3))
    return g


img = radial(340, (30, 25, 23), (12, 10, 9)).resize((W, H), Image.BILINEAR)

# ── диаграмма вкуса реального чая (Габа «Рубин»: яркий фруктово-сухофруктовый) ──
AX = ["Цветочный", "Фруктовый", "Сухофрукты", "Медовый", "Ореховый",
      "Жареный", "Пряный", "Древесный", "Травяной"]
SC = [1, 5, 4, 2, 0, 2, 1, 1, 0]          # profile gabaruby
n = len(AX)
cx, cy, Rr = 936, 372, 162
ang = lambda i: math.radians(-90 + i * 360 / n)
pt = lambda i, r: (cx + r * math.cos(ang(i)), cy + r * math.sin(ang(i)))

layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ld = ImageDraw.Draw(layer)
# сетка-кольца
for f in (1 / 3, 2 / 3, 1.0):
    ld.polygon([pt(i, Rr * f) for i in range(n)], outline=LINE + (255,), width=2)
# спицы
for i in range(n):
    ld.line([(cx, cy), pt(i, Rr)], fill=LINE + (255,), width=2)
# полигон профиля
poly = [pt(i, Rr * SC[i] / 5) for i in range(n)]
ld.polygon(poly, fill=RED + (70,), outline=RED + (255,), width=4)
for i in range(n):
    if SC[i]:
        x, y = pt(i, Rr * SC[i] / 5)
        ld.ellipse([x - 6, y - 6, x + 6, y + 6], fill=RED + (255,))
img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

# подписи осей
d = ImageDraw.Draw(img)
fa = golos(19)
for i in range(n):
    x, y = pt(i, Rr + 20)
    c = math.cos(ang(i))
    anc = "mm" if abs(c) < 0.3 else ("lm" if c > 0 else "rm")
    d.text((x, y), AX[i], font=fa, fill=MUTE, anchor=anc)

# ── левая часть: бренд + строка ──
X = 82
mh = 60
mk = mark(mh)
img.paste(mk, (X, 90), mk)
d.text((X + mk.width + 20, 90 + mh // 2), "Ч А Й Н Я", font=prata(34), fill=INK, anchor="lm")

# фраза-фокус, крупно, по центру левой части (без мелкой подписи)
fh = prata(70)
d.text((X, 292), "У каждого чая", font=fh, fill=INK)
d.text((X, 378), "свой характер", font=fh, fill=RED)

out = root / "src-assets" / "bot-desc.jpg"
# @BotFather требует картинку описания РОВНО 640×360. Рисуем в 1280×720 и
# уменьшаем вдвое (супер-сэмплинг = чётче, чем рисовать сразу мелко).
img = img.resize((640, 360), Image.LANCZOS)
img.save(out, "JPEG", quality=92, progressive=True)
print("bot-desc.jpg:", img.size)

import shutil
dst = pathlib.Path.home() / "Desktop" / "chainya-bot-ассеты"
dst.mkdir(exist_ok=True)
shutil.copy(out, dst / "description-picture.jpg")
print("копия:", dst / "description-picture.jpg")

#!/usr/bin/env python3
"""Рисует ассеты для телеграм-бота «Чайня»: аватарку и картинку-обложку.

Аватарка — квадрат под кружок телеграма: тёмный фон, знак ЧНЯ, красный ободок.
Обложка (description picture у @BotFather) — 16:9: бренд слева, стена чая справа.

Те же средства, что у сайта и og-карточки: фон #141110, знак из logo-mark.png,
шрифты Prata/Golos, красный акцент. Результат в src-assets/, копии на Desktop.
"""
import pathlib
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFont

root = pathlib.Path(__file__).parent
FONTS = root / "src-assets" / "fonts-full"
prata = lambda s: ImageFont.truetype(str(FONTS / "prata.ttf"), s)
golos = lambda s: ImageFont.truetype(str(FONTS / "golos.ttf"), s)

BG = (20, 17, 16)          # #141110
INK = (241, 237, 230)      # #F1EDE6
RED = (190, 59, 48)        # красный акцент, читается на тёмном
JADE_RED = (219, 106, 98)  # тёплый красный (как акцент тёмной темы)
MUTE = (148, 140, 131)

# logo-mark.png — светлая плашка с ТЁМНЫМИ буквами ЧНЯ. Нам нужны только буквы
# кремовым цветом на прозрачном: инвертируем яркость (буквы становятся яркими,
# плашка тёмной) и режем маской по альфе плашки, чтобы за её краями было пусто.
mark_src = Image.open(root / "src-assets" / "logo-mark.png").convert("RGBA")
_gray = mark_src.convert("RGB").convert("L")
_letters = ImageChops.multiply(ImageChops.invert(_gray), mark_src.split()[3])  # альфа букв, гладкая
# гасим слабые значения — призрак рамки плашки, оставляем только сами буквы
_letters = _letters.point(lambda v: 0 if v < 150 else 255)


def mark(height, color=INK):
    a = _letters.resize((round(_letters.width * height / _letters.height), height), Image.LANCZOS)
    glyph = Image.new("RGBA", a.size, color + (0,))
    glyph.putalpha(a)
    return glyph


# ─────────────  АВАТАРКА 640×640  ─────────────
A = 640
av = Image.new("RGB", (A, A), BG)
ad = ImageDraw.Draw(av)
# красный ободок — после круглого кропа телеграма читается как рамка
ad.ellipse([26, 26, A - 26, A - 26], outline=RED, width=9)
# знак по центру
m = mark(300)
av.paste(m, ((A - m.width) // 2, (A - m.height) // 2 - 6), m)
av.save(root / "src-assets" / "bot-avatar.png")
print("bot-avatar.png:", av.size)


# ─────────────  ОБЛОЖКА 1280×720  ─────────────
W, H = 1280, 720
cov = Image.new("RGB", (W, H), BG)

# стена чая справа (те же кадры, что в герое/og)
TEAS = ["tea-dancong", "tea-baihao", "tea-osmanthus", "tea-gaba-ruby",
        "tea-dianhong", "tea-molisiaobaiya", "tea-laochatou", "tea-longjing",
        "tea-shengchenxiang", "tea-biluochun", "tea-maoxie", "tea-peacock"]
COLS, GAP, CELL = 3, 12, 250
wall_w = COLS * CELL + (COLS - 1) * GAP
WALL_X = W - wall_w
wall = Image.new("RGB", (wall_w, H + CELL), BG)
for i, name in enumerate(TEAS):
    f = root / "img" / f"{name}.webp"
    if not f.exists():
        continue
    im = Image.open(f).convert("RGB").resize((CELL, CELL), Image.LANCZOS)
    im = ImageEnhance.Brightness(im).enhance(1.14)
    col, rw = i % COLS, i // COLS
    wall.paste(im, (col * (CELL + GAP), rw * (CELL + GAP) - (0, 80, 40)[col]))
cov.paste(wall, (WALL_X, -40))

# ширма: гасим стену слева (под текстом), мягко открываем к правому краю
TEXT_R = 640
scrim = Image.new("L", (W, H), 0)
sd = ImageDraw.Draw(scrim)
for x in range(W):
    if x < TEXT_R:
        a = 255
    else:
        t = (x - TEXT_R) / (W - TEXT_R)
        a = max(0, int(255 * (1 - t) ** 1.5))
    sd.line([(x, 0), (x, H)], fill=a)
cov.paste(Image.new("RGB", (W, H), BG), (0, 0), scrim)
# лёгкие затемнения верх/низ
edge = Image.new("L", (W, H), 0)
ed = ImageDraw.Draw(edge)
for y in range(H):
    a = 0
    if y < 70:
        a = int(200 * (1 - y / 70))
    elif y > H - 70:
        a = int(200 * ((y - (H - 70)) / 70))
    ed.line([(0, y), (W, y)], fill=a)
cov.paste(Image.new("RGB", (W, H), BG), (0, 0), edge)

d = ImageDraw.Draw(cov)
X = 84
# знак + слово ЧАЙНЯ
mh = 70
mk = mark(mh)
cov.paste(mk, (X, 96), mk)
d.text((X + mk.width + 22, 96 + mh // 2), "Ч А Й Н Я", font=prata(34), fill=INK, anchor="lm")
# заголовок-оффер, последняя строка красным
f_h = prata(60)
lines = [("Два часа,", INK), ("два чая", INK), ("и мастер напротив", JADE_RED)]
y = 250
for text, color in lines:
    d.text((X, y), text, font=f_h, fill=color)
    y += 74
# подпись
d.text((X, 500), "Камерная чайная у метро Аэропорт", font=golos(24), fill=MUTE)
d.text((X, 540), "Меню · бронь стола · чай навынос — в этом боте", font=golos(24), fill=(182, 175, 166))

cov.save(root / "src-assets" / "bot-cover.jpg", "JPEG", quality=90, progressive=True)
print("bot-cover.jpg:", cov.size)

# копии на Desktop, чтобы удобно залить через @BotFather
import shutil
dst = pathlib.Path.home() / "Desktop" / "chainya-bot-ассеты"
dst.mkdir(exist_ok=True)
shutil.copy(root / "src-assets" / "bot-avatar.png", dst / "avatar.png")
shutil.copy(root / "src-assets" / "bot-cover.jpg", dst / "cover.jpg")
print("копии:", dst)

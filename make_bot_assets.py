#!/usr/bin/env python3
"""Рисует ассеты для телеграм-бота «Чайня»: аватарку и картинку-обложку.

Аватарка — квадрат под кружок телеграма: тёмный фон, знак ЧНЯ, красный ободок.
Обложка (description picture у @BotFather) — 16:9: бренд слева, стена чая справа.

Те же средства, что у сайта и og-карточки: фон #141110, знак из logo-mark.png,
шрифты Prata/Golos, красный акцент. Результат в src-assets/, копии на Desktop.
"""
import pathlib
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

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
_letters = ImageChops.multiply(ImageChops.invert(_gray), mark_src.split()[3])  # альфа букв
_letters = _letters.point(lambda v: 255 if v > 135 else 0)                     # бинаризуем
# морфологическое открытие (эрозия→дилатация): убирает ТОНКУЮ рамку плашки,
# толстые штрихи букв переживают. Так знак остаётся чистым, без призрака.
_letters = _letters.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
# лёгкое размытие вернёт сглаженные края после бинаризации
_letters = _letters.filter(ImageFilter.GaussianBlur(0.6))


def mark(height, color=INK):
    a = _letters.resize((round(_letters.width * height / _letters.height), height), Image.LANCZOS)
    glyph = Image.new("RGBA", a.size, color + (0,))
    glyph.putalpha(a)
    return glyph


# ─────────────  АВАТАРКА 1024×1024  ─────────────
A = 1024


def radial(size, inner, outer, power=1.5):
    g = Image.new("RGB", (size, size))
    px = g.load()
    c = size / 2
    for y in range(size):
        for x in range(size):
            d = min(1.0, ((x - c) ** 2 + (y - c) ** 2) ** 0.5 / c)
            t = d ** power
            px[x, y] = tuple(round(inner[i] * (1 - t) + outer[i] * t) for i in range(3))
    return g


# тёплый радиальный фон: центр чуть светлее краёв — глубина, не плоский квадрат
av = radial(320, (36, 30, 27), (10, 8, 7)).resize((A, A), Image.BILINEAR)
ad = ImageDraw.Draw(av)
# тонкий красный ободок с запасом от края (круглый кроп телеграма его не срежет)
ad.ellipse([72, 72, A - 72, A - 72], outline=RED, width=13)
# знак ЧНЯ по центру: кремовый, крупный, чистый (рамка плашки убрана)
m = mark(478)
av.paste(m, ((A - m.width) // 2, (A - m.height) // 2 - 8), m)
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
TEXT_R = 700
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
X = 92
# знак + слово ЧАЙНЯ — крупнее
mh = 88
mk = mark(mh)
cov.paste(mk, (X, 74), mk)
d.text((X + mk.width + 26, 74 + mh // 2), "Ч А Й Н Я", font=prata(50), fill=INK, anchor="lm")
# заголовок-оффер крупным кеглем, последняя строка красным
f_h = prata(72)
lines = [("Два часа,", INK), ("два чая", INK), ("и мастер напротив", JADE_RED)]
y = 236
for text, color in lines:
    d.text((X, y), text, font=f_h, fill=color)
    y += 94
# одна крупная подпись (мелкие строки убраны — их дублирует подпись под фото)
d.text((X, y + 24), "Чайная · метро Аэропорт", font=golos(35), fill=(198, 191, 182))

cov.save(root / "src-assets" / "bot-cover.jpg", "JPEG", quality=90, progressive=True)
print("bot-cover.jpg:", cov.size)


# ─────────────  КАРТИНКА ОПИСАНИЯ (экран ДО «Запустить»)  ─────────────
# Отдельная от обложки /start, иначе гость видит одно и то же дважды. Здесь —
# тёплое фото «садись и пей», без офферного текста, только маленький логотип.
TEA_SRC = pathlib.Path.home() / "Desktop" / "чайная"
DW, DH = 1280, 720
photo = Image.open(TEA_SRC / "IMG_3094.JPG").convert("RGB")  # накрытый стол на двоих
r = max(DW / photo.width, DH / photo.height)
photo = photo.resize((round(photo.width * r), round(photo.height * r)), Image.LANCZOS)
ox, oy = (photo.width - DW) // 2, (photo.height - DH) // 2
photo = photo.crop((ox, oy, ox + DW, oy + DH))
photo = ImageEnhance.Brightness(photo).enhance(1.12)
photo = ImageEnhance.Color(photo).enhance(1.06)
# нижний градиент, чтобы логотип читался поверх фото
grad = Image.new("L", (DW, DH), 0)
gd = ImageDraw.Draw(grad)
for yy in range(DH):
    gd.line([(0, yy), (DW, yy)], fill=(int(210 * ((yy - (DH - 240)) / 240) ** 1.6) if yy > DH - 240 else 0))
photo.paste(Image.new("RGB", (DW, DH), (8, 6, 5)), (0, 0), grad)
dd = ImageDraw.Draw(photo)
dmh = 58
dmk = mark(dmh)
photo.paste(dmk, (66, DH - 70 - dmh), dmk)
dd.text((66 + dmk.width + 20, DH - 70 - dmh // 2), "Чайня", font=prata(50), fill=INK, anchor="lm")
photo.save(root / "src-assets" / "bot-desc.jpg", "JPEG", quality=90, progressive=True)
print("bot-desc.jpg:", photo.size)

# копии на Desktop, чтобы удобно залить через @BotFather
import shutil
dst = pathlib.Path.home() / "Desktop" / "chainya-bot-ассеты"
dst.mkdir(exist_ok=True)
shutil.copy(root / "src-assets" / "bot-avatar.png", dst / "avatar.png")
shutil.copy(root / "src-assets" / "bot-cover.jpg", dst / "cover.jpg")
shutil.copy(root / "src-assets" / "bot-desc.jpg", dst / "description-picture.jpg")
print("копии:", dst)

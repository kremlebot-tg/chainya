#!/usr/bin/env python3
"""Рисует картинку превью для ссылок (og:image), 1200×630.

Собирает её теми же средствами, что и сайт: фирменный фон, знак «ЧНЯ»,
заголовок в Prata, стена чая справа. Голая фотография зала в превью
не работает — по ней не понять, что это и зачем.

Нужен Pillow. Шрифты берёт из src-assets/fonts-full (полные ttf).
Результат: src-assets/og.jpg
Запускать вручную, только если поменялся оффер или знак.
"""
import os
import pathlib

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

root = pathlib.Path(__file__).parent

W, H = 1200, 630
BG = (28, 14, 16)         # #1C0E10 — винный сумрак, как тёмная тема сайта
INK = (244, 232, 228)     # #F4E8E4
ACCENT = (219, 106, 98)   # #DB6A62 — акцент сайта (тёмная тема), кармин
MUTE = (158, 130, 126)    # #9E827E


# Сайту хватает woff2-сабсетов, но Pillow их не читает и не умеет подменять
# шрифт для отсутствующих глифов. А сабсеты порезаны так, что кириллица лежит
# отдельно от знаков препинания: на них «Два часа,» теряет запятую, а «·»
# превращается в квадрат. Поэтому здесь — полные ttf из src-assets/fonts-full.
FONTS = root / "src-assets" / "fonts-full"
prata = lambda s: ImageFont.truetype(FONTS / "prata.ttf", s)
golos = lambda s: ImageFont.truetype(FONTS / "golos.ttf", s)

card = Image.new("RGB", (W, H), BG)

# ── стена чая справа: та же идея, что в герое сайта ──
TEAS = ["tea-chongshicha", "tea-baihao", "tea-longjing", "tea-biluogold",
        "tea-dancong", "tea-gaba-ruby", "tea-laochatou", "tea-peacock",
        "tea-molimaojian", "tea-dianhong", "tea-maoxie", "tea-bingdao"]
COLS, GAP = 3, 10
CELL = 210
wall_w = COLS * CELL + (COLS - 1) * GAP
WALL_X = W - wall_w
wall = Image.new("RGB", (wall_w, H + CELL * 2), BG)
for i, name in enumerate(TEAS):
    f = root / "img" / f"{name}.webp"
    if not f.exists():
        continue
    im = Image.open(f).convert("RGB").resize((CELL, CELL), Image.LANCZOS)
    # лист снят при тёплой лампе и на тёмном фоне тонет: поднимаем
    im = ImageEnhance.Brightness(im).enhance(1.22)
    im = ImageEnhance.Color(im).enhance(1.08)
    col, row = i % COLS, i // COLS
    # колонки сдвигаем по-разному, чтобы стыки не выстроились в одну линию
    wall.paste(im, (col * (CELL + GAP), row * (CELL + GAP) - (0, 70, 35)[col]))
card.paste(wall, (WALL_X, -50))

# ── ширма: гасит стену там, где идёт текст, и открывает её к правому краю.
#    Ramp начинается не от края стены, а от края текстовой колонки (TEXT_R):
#    иначе либо стена проступает под заголовком, либо на стыке видна полоса.
TEXT_R = 700
scrim = Image.new("L", (W, H), 0)
sd = ImageDraw.Draw(scrim)
for x in range(W):
    if x < TEXT_R:
        a = 255
    else:
        t = (x - TEXT_R) / (W - TEXT_R)      # 0 у края текста → 1 у края картинки
        a = max(0, int(255 * (1 - t) ** 1.5))
    sd.line([(x, 0), (x, H)], fill=a)
card.paste(Image.new("RGB", (W, H), BG), (0, 0), scrim)

# верх и низ притемняем, чтобы стена не упиралась в край
edge = Image.new("L", (W, H), 0)
ed = ImageDraw.Draw(edge)
for y in range(H):
    a = 0
    if y < 90:
        a = int(255 * (1 - y / 90) ** 1.4)
    elif y > H - 90:
        a = int(255 * ((y - (H - 90)) / 90) ** 1.4)
    ed.line([(0, y), (W, y)], fill=a)
card.paste(Image.new("RGB", (W, H), BG), (0, 0), edge)

d = ImageDraw.Draw(card)
X = 72

# ── знак «ЧНЯ» и слово ──
mark = Image.open(root / "src-assets" / "logo-mark.png")
mh = 66
mark = mark.resize((round(mark.width * mh / mark.height), mh), Image.LANCZOS)
card.paste(mark, (X, 66), mark)

f_brand = prata(30)
d.text((X + mark.width + 18, 66 + mh // 2), "Ч А Й Н Я", font=f_brand, fill=INK, anchor="lm")

# ── заголовок: тот же оффер, что в герое ──
f_h = prata(58)
lines = [("Два часа,", INK), ("два чая", INK), ("и мастер напротив", ACCENT)]
y = 214
for text, color in lines:
    d.text((X, y), text, font=f_h, fill=color)
    y += 70

# ── адрес ──
f_meta = golos(20)
ADDR = "Москва · Острякова, 3 · метро Аэропорт"
d.text((X, 476), ADDR, font=f_meta, fill=MUTE)

# ── подпись: не пересказ заголовка, а то, чего в нём нет ──
f_note = golos(22)
NOTE = "Ежедневно 12:00–22:00 · один зал на девять мест"
d.text((X, 520), NOTE, font=f_note, fill=(182, 175, 166))

# Ширма держит текст на чистом фоне только до TEXT_R. Если поменяется оффер
# или адрес, строка может молча заехать на стену — проверяем сразу.
widest = max((d.textlength(t, font=f) for t, f in
              [(l[0], f_h) for l in lines]
              + [(ADDR, f_meta), (NOTE, f_note)]))
assert X + widest < TEXT_R, (
    f"текст ({round(X + widest)}px) заезжает под стену (ширма открывается с {TEXT_R}px)")

out = root / "src-assets" / "og.jpg"
card.save(out, "JPEG", quality=88, optimize=True, progressive=True)
print("og.jpg:", card.size, round(out.stat().st_size / 1024), "KB", "->", out)

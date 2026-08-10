#!/usr/bin/env python3
"""Композитная карточка чая для телеграма: фото + вкусовой профиль на самой картинке.

Проблема: текстовые бары «▓░» в подписи выглядят грязно. Решение — рисуем
профиль прямо на изображении: сверху фото сухого листа, снизу тёмная брендовая
панель с названием и золотыми полосками вкуса. Подпись в боте тогда несёт только
описание и цену.

Вход:  media/teas/<id>.jpg  (квадратные фото 800px) + teas.json
Выход: media/cards/<id>.jpg (1080×1350, готово для answer_photo)

Перезапускать при смене фото/вкусов/названий (после build_data.py).
"""
import json
import pathlib

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

HERE = pathlib.Path(__file__).parent
TEA_DIR = HERE / "media" / "teas"
OUT_DIR = HERE / "media" / "cards"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SITE = HERE.parent if (HERE.parent / "src.html").is_file() else HERE.parent / "site"
FONTS = SITE / "src-assets" / "fonts-full"
LOGO = SITE / "src-assets" / "logo-mark.png"

prata = lambda s: ImageFont.truetype(str(FONTS / "prata.ttf"), s)
golos = lambda s: ImageFont.truetype(str(FONTS / "golos.ttf"), s)

# палитра сайта
INK = (241, 237, 230)
GOLD = (200, 168, 75)
GOLD_SOFT = (176, 152, 96)
GOLD_TIP = (226, 200, 128)
RED = (206, 74, 66)
MUTE = (150, 142, 133)
TRACK = (46, 39, 33)
PANEL_TOP = (23, 19, 16)
PANEL_BOT = (14, 11, 9)

W, H = 1080, 1380
PHOTO_H = 700
PAD = 64

data = json.load(open(HERE / "teas.json", encoding="utf-8"))
AX_NAME = {a["id"]: a["name"] for a in data["axes"]}
AX_ORDER = [a["id"] for a in data["axes"]]

# ── знак ЧНЯ кремовым (та же морфология, что в остальных ассетах) ──
_m = Image.open(LOGO).convert("RGBA")
_let = ImageChops.multiply(ImageChops.invert(_m.convert("RGB").convert("L")), _m.split()[3])
_let = _let.point(lambda v: 255 if v > 135 else 0)
_let = _let.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(0.6))


def mark(h, color=INK):
    a = _let.resize((round(_let.width * h / _let.height), h), Image.LANCZOS)
    g = Image.new("RGBA", a.size, color + (0,))
    g.putalpha(a)
    return g


# ── панель одинакова для всех карточек — считаем один раз ──
def build_panel():
    panel = Image.new("RGB", (W, H - PHOTO_H), PANEL_TOP)
    px = panel.load()
    ph = H - PHOTO_H
    for y in range(ph):
        t = (y / ph) ** 1.2
        col = tuple(round(PANEL_TOP[i] * (1 - t) + PANEL_BOT[i] * t) for i in range(3))
        for x in range(W):
            px[x, y] = col
    return panel


PANEL = build_panel()


def fit_font(text, maker, max_w, hi, lo):
    for s in range(hi, lo - 1, -2):
        f = maker(s)
        if f.getlength(text) <= max_w:
            return f, s
    return maker(lo), lo


def rounded_bar(draw, x0, y0, x1, y1, r, fill):
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)


def diamond(draw, cx, cy, r, fill):
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=fill)


def make_card(t):
    # фото: crop-fill 1080×760
    src = TEA_DIR / f"{t['id']}.jpg"
    photo = Image.open(src).convert("RGB")
    scale = max(W / photo.width, PHOTO_H / photo.height)
    photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    ox = (photo.width - W) // 2
    oy = (photo.height - PHOTO_H) // 2
    photo = photo.crop((ox, oy, ox + W, oy + PHOTO_H))
    photo = ImageEnhance.Brightness(photo).enhance(1.04)
    photo = ImageEnhance.Color(photo).enhance(1.05)

    card = Image.new("RGB", (W, H), PANEL_TOP)
    card.paste(photo, (0, 0))
    card.paste(PANEL, (0, PHOTO_H))

    # мягкий переход фото → панель (последние 190px фото гаснут в цвет панели)
    fade = Image.new("RGBA", (W, 190), PANEL_TOP + (0,))
    fpx = fade.load()
    for y in range(190):
        a = int(255 * (y / 190) ** 1.4)
        for x in range(W):
            fpx[x, y] = PANEL_TOP + (a,)
    card.paste(fade, (0, PHOTO_H - 190), fade)

    d = ImageDraw.Draw(card)

    # плашка «нет в наличии»
    if not t.get("stock", True):
        lab = "нет в наличии"
        f = golos(30)
        tw = f.getlength(lab)
        rounded_bar(d, W - PAD - tw - 44, 34, W - PAD, 34 + 58, 29, RED)
        d.text((W - PAD - tw - 22, 34 + 29), lab, font=f, fill=INK, anchor="lm")

    # заголовок
    y = PHOTO_H + 40
    nf, ns = fit_font(t["name"], prata, W - 2 * PAD, 66, 40)
    d.text((PAD, y), t["name"], font=nf, fill=INK, anchor="la")
    y += ns + 16
    of = golos(28)
    d.text((PAD, y), t["orig"], font=of, fill=GOLD_SOFT, anchor="la")
    y += 40

    # золотой разделитель с ромбом
    ry = y + 22
    d.line([(PAD, ry), (PAD + 150, ry)], fill=(70, 60, 46), width=3)
    diamond(d, PAD + 174, ry, 7, GOLD)
    d.line([(PAD + 198, ry), (PAD + 300, ry)], fill=(70, 60, 46), width=3)
    y = ry + 42

    # вкусовой профиль — топ-5 ненулевых осей, порядок как в axes
    items = [(k, t["taste"].get(k, 0)) for k in AX_ORDER if t["taste"].get(k, 0) > 0]
    items.sort(key=lambda kv: -kv[1])
    items = items[:5]

    d.text((PAD, y), "ВКУСОВОЙ ПРОФИЛЬ", font=golos(24), fill=MUTE, anchor="la")
    y += 46

    lf = golos(31)
    track_x0, track_x1 = 470, W - PAD
    track_w = track_x1 - track_x0
    pitch = 64
    bh = 22
    for k, v in items:
        cy = y + bh // 2 + 4
        d.text((PAD, cy), AX_NAME[k], font=lf, fill=INK, anchor="lm")
        rounded_bar(d, track_x0, cy - bh // 2, track_x1, cy + bh // 2, bh // 2, TRACK)
        fw = max(bh, round(track_w * v / 5))
        # золотая заливка с чуть более светлым кончиком
        rounded_bar(d, track_x0, cy - bh // 2, track_x0 + fw, cy + bh // 2, bh // 2, GOLD)
        d.ellipse([track_x0 + fw - bh, cy - bh // 2, track_x0 + fw, cy + bh // 2], fill=GOLD_TIP)
        y += pitch

    card.save(OUT_DIR / f"{t['id']}.jpg", "JPEG", quality=90, progressive=True)


n = 0
for t in data["teas"]:
    if (TEA_DIR / f"{t['id']}.jpg").exists():
        make_card(t)
        n += 1
    else:
        print("нет фото:", t["id"])
print(f"карточек: {n} → {OUT_DIR}")

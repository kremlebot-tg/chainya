#!/usr/bin/env python3
"""Вырезает знак «ЧНЯ» из логотипа, лежащего на фотографии плантации.

Логотип — белая плашка с чёрным знаком, поверх фотоподложки. Берём самую
большую связную белую область (это и есть плашка), заливкой от краёв
отделяем фон, а всё, куда заливка не дошла, считаем знаком.

Нужен Pillow. Результат: src-assets/logo-mark.png (RGBA).
Запускать только если пришлют новый логотип.
"""
from PIL import Image, ImageFilter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT.parent / 'source-materials' / 'tea-house' / 'photo_2026-07-16_07-59-35.jpg'
DST = ROOT / 'src-assets' / 'logo-mark.png'

im = Image.open(SRC).convert('RGB')
W, H = im.size
px = im.load()


def is_white(x, y):
    r, g, b = px[x, y]
    mx, mn = max(r, g, b), min(r, g, b)
    return mx > 185 and (0 if mx == 0 else (mx - mn) / mx) < 0.18


white = [is_white(x, y) for y in range(H) for x in range(W)]

# самая большая белая область = плашка
lab = [0] * (W * H)
cur, best = 0, (0, 0)
for i in range(W * H):
    if white[i] and not lab[i]:
        cur += 1
        n, st, lab[i] = 0, [i], cur
        while st:
            j = st.pop()
            n += 1
            x, y = j % W, j // W
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H:
                    k = ny * W + nx
                    if white[k] and not lab[k]:
                        lab[k] = cur
                        st.append(k)
        if n > best[0]:
            best = (n, cur)
tile = best[1]

xs = [i % W for i in range(W * H) if lab[i] == tile]
ys = [i // W for i in range(W * H) if lab[i] == tile]
pad = 6
x0, x1 = max(0, min(xs) - pad), min(W - 1, max(xs) + pad)
y0, y1 = max(0, min(ys) - pad), min(H - 1, max(ys) + pad)
w, h = x1 - x0 + 1, y1 - y0 + 1

# заливка от края bbox по не-плашке: куда дошли — фон, куда нет — знак
outside = [False] * (w * h)
st = []
for x in range(w):
    for y in (0, h - 1):
        if lab[(y + y0) * W + (x + x0)] != tile and not outside[y * w + x]:
            outside[y * w + x] = True
            st.append((x, y))
for y in range(h):
    for x in (0, w - 1):
        if lab[(y + y0) * W + (x + x0)] != tile and not outside[y * w + x]:
            outside[y * w + x] = True
            st.append((x, y))
while st:
    x, y = st.pop()
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h and not outside[ny * w + nx] \
                and lab[(ny + y0) * W + (nx + x0)] != tile:
            outside[ny * w + nx] = True
            st.append((nx, ny))

out = Image.new('RGBA', (w, h))
op = out.load()
for y in range(h):
    for x in range(w):
        if outside[y * w + x]:
            op[x, y] = (0, 0, 0, 0)
        elif lab[(y + y0) * W + (x + x0)] == tile:
            op[x, y] = (255, 255, 255, 255)
        else:
            op[x, y] = (24, 22, 20, 255)

# открытие: срезаем тонкий хвостик рамки, прилипший к плашке
a = out.split()[3]
opened = a.filter(ImageFilter.MinFilter(9)).filter(ImageFilter.MaxFilter(9))
m = Image.new('L', out.size)
mp, ap, op2 = m.load(), a.load(), opened.load()
for y in range(h):
    for x in range(w):
        mp[x, y] = min(ap[x, y], op2[x, y])
out.putalpha(m)
out = out.crop(out.getbbox())

os.makedirs(os.path.dirname(DST), exist_ok=True)
out.save(DST)
print('знак:', out.size, '->', DST)

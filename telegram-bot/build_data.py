#!/usr/bin/env python3
"""Вытаскивает данные о чаях из сайта (src.html) в teas.json для бота.

Берёт TEA_META, вкусовые профили TASTE и русские тексты teas (I18N.ru), сшивает
в один JSON, который бот читает для нативного каталога. Парсинг — через node
(он корректно понимает собственный JS: смешанные кавычки, «ёлочки», {{img}}).

Перезапускать, когда на сайте меняются чаи/цены/описания.
"""
import json
import pathlib
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent if (HERE.parent / "src.html").is_file() else HERE.parent / "site"
SRC = REPO / "src.html"
OUT = HERE / "teas.json"
s = SRC.read_text(encoding="utf-8")


def balanced(text, i):
    """Возвращает сбалансированный блок [..] или {..}, начиная с открывающей скобки i.
    Учитывает строки (скобки внутри '...' / \"...\" игнорируются)."""
    open_ch = text[i]
    close_ch = {"[": "]", "{": "}"}[open_ch]
    depth = 0
    instr = None
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == instr:
                instr = None
        else:
            if c in "'\"":
                instr = c
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return text[i:j + 1]
    raise ValueError("не сбалансировано")


def grab(marker, bracket):
    p = s.index(marker)
    b = s.index(bracket, p)
    return balanced(s, b)


meta_txt = grab("const TEA_META = ", "[")
taste_txt = grab("const TASTE = ", "{")
# первый "teas:{" в файле — это блок русского языка (ru идёт первым)
teas_p = s.index("teas:{")
teas_txt = balanced(s, s.index("{", teas_p))

node = f"""
const TEA_META = {meta_txt};
const TASTE = {taste_txt};
const RU = {teas_txt};
console.log(JSON.stringify({{meta: TEA_META, taste: TASTE, ru: RU}}));
"""
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(node)
    tmp = f.name
raw = subprocess.check_output(["node", tmp], text=True)
d = json.loads(raw)

# Категории и порядок — как в кассе чайной (раскладку прислал владелец).
# Должны совпадать с TYPE_IDS/LQ в src.html.
TYPES = [("white", "Белый чай"), ("green", "Зелёный чай"), ("gaba", "Габа"),
         ("fujian", "Улуны Южной Фуцзяни"), ("dancong", "Улуны Гуандуна"),
         ("wuyi", "Улуны Уишаня"), ("red", "Красный чай"), ("sheng", "Шэн Пуэр"),
         ("shu", "Шу Пуэр"), ("heicha", "Хэй Ча"), ("herbs", "Травы и добавки"),
         ("tea-sets", "Чайные наборы"), ("teaware-teapots", "Чайники"),
         ("teaware-gaiwans", "Гайвани"), ("teaware-cups", "Пиалы"),
         ("teaware-chahai", "Чахаи"), ("teaware-chahe", "Чахэ"),
         ("teaware-figurines", "Фигурки"), ("teaware-tools", "Инструменты"),
         ("teaware-sets", "Наборы посуды")]
AXES = [("floral", "Цветочный"), ("fruity", "Фруктовый"), ("driedfruit", "Сухофрукты"),
        ("honey", "Медовый"), ("nutty", "Ореховый"), ("roasted", "Жареный"),
        ("spicy", "Пряный"), ("woody", "Древесный"), ("herbal", "Травяной")]

teas = []
for m in d["meta"]:
    tid = m["id"]
    ru = d["ru"][tid]
    teas.append({
        "id": tid,
        "type": m["t"],
        "name": ru["n"],
        "orig": ru["o"],
        "desc": ru["d"],
        "price": m["p"],
        "unit": m["unit"],
        "stock": m.get("stock", True),
        "img": m["img"].replace("{{img:", "").replace("}}", ""),  # имя webp (у габы с дефисами)
        "taste": d["taste"][tid],
    })

OUT.write_text(json.dumps({
    "types": [{"id": t, "name": n} for t, n in TYPES],
    "axes": [{"id": a, "name": n} for a, n in AXES],
    "packs": [10, 25, 50, 100],
    "teas": teas,
}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"teas.json: {len(teas)} товаров, {len(TYPES)} категорий")

#!/bin/bash
# Готовит webp-ассеты из исходных фото чайной.
# Лист снят вертикально (3750x5000) — для карточек берём квадрат из центра.
set -e
SRC="$HOME/Desktop/чайная"
OUT="$HOME/Desktop/tea-house/img"
mkdir -p "$OUT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# квадратный кроп из центра -> webp
# $5 — угол поворота (для кадров с камеры: cwebp срезает EXIF, sips не запекает,
# поэтому «рыжий» набор снят боком и его надо довернуть на 90 по часовой)
square () {  # $1=файл $2=имя $3=размер $4=качество [$5=градусы]
  local f="$SRC/$1" t="$TMP/$2.jpg"
  sips -Z $(( $3 * 3 )) "$f" --out "$t" >/dev/null 2>&1
  [ -n "$5" ] && sips -r "$5" "$t" >/dev/null 2>&1
  local W H OFF
  W=$(sips -g pixelWidth  "$t" | awk '/pixelWidth/{print $2}')
  H=$(sips -g pixelHeight "$t" | awk '/pixelHeight/{print $2}')
  if [ "$H" -gt "$W" ]; then OFF=$(( (H - W) / 2 )); cwebp -quiet -q $4 -crop 0 $OFF $W $W -resize $3 $3 "$t" -o "$OUT/$2.webp"
  else OFF=$(( (W - H) / 2 )); cwebp -quiet -q $4 -crop $OFF 0 $H $H -resize $3 $3 "$t" -o "$OUT/$2.webp"; fi
}

# по ширине, пропорции сохраняем
# $5 — угол: cwebp выбрасывает EXIF, а sips его не запекает, поэтому кадры
# с камеры (4160x2768 + флаг поворота) надо довернуть руками, иначе лягут на бок
wide () {  # $1=файл $2=имя $3=ширина $4=качество [$5=градусы]
  local t="$TMP/$2.jpg"
  sips -Z $(( $3 * 2 )) "$SRC/$1" --out "$t" >/dev/null 2>&1
  [ -n "$5" ] && sips -r "$5" "$t" >/dev/null 2>&1
  cwebp -quiet -q $4 -resize $3 0 "$t" -o "$OUT/$2.webp"
}

# Квадрат 800/q85: карточка на Retina показывается ~520px, паспорт ~740px —
# 360px давало мыло, особенно в широком паспорте. Исходники 4032px, запас есть.
echo "— чаи (квадрат 800, q85)"
square IMG_3116.JPG tea-chongshicha   800 85
square IMG_3117.JPG tea-baihao        800 85
square IMG_3118.JPG tea-baimudan      800 85
square IMG_3119.JPG tea-longjing      800 85
square IMG_3120.JPG tea-molimaojian   800 85
square IMG_3121.JPG tea-xiaozhong     800 85
square IMG_3122.JPG tea-biluogold     800 85
square IMG_3123.JPG tea-dianhong      800 85
square IMG_3124.JPG tea-longjinghong  800 85
square IMG_3125.JPG tea-herbal        800 85
square IMG_3126.JPG tea-maoxie        800 85
square IMG_3127.JPG tea-huangjingui   800 85
square IMG_3128.JPG tea-ginseng       800 85
square IMG_3104.JPG tea-gaba-ruby     800 85
square IMG_3130.JPG tea-gaba-honey    800 85
square IMG_3131.JPG tea-gaba-maocha   800 85
square IMG_3132.JPG tea-dahongpao     800 85
square IMG_3103.JPG tea-dancong       800 85
square IMG_3134.JPG tea-laochatou     800 85
square IMG_3135.JPG tea-nuomixiang    800 85
square IMG_3136.JPG tea-mandarin      800 85
square IMG_3137.JPG tea-bingdao       800 85
square IMG_3102.JPG tea-peacock       800 85
square IMG_3139.JPG tea-jinhuawang    800 85

echo "— новые чаи (рыжий коврик, поворот 90 по часовой)"
square IMG_3330.JPG  tea-molisiaobaiya   800 85 90
square IMG_3331.JPG  tea-biluochun       800 85 90
square IMG_3326.JPG  tea-dancongmilan    800 85 90
square IMG_3328.JPG  tea-dancongtongtian 800 85 90
square IMG_3332.JPG  tea-yeshenghong     800 85 90
square IMG_3333.JPG  tea-osmanthus       800 85 90
square IMG_3329.JPG  tea-vitamin         800 85 90
square IMG_3335.jpg  tea-shengchenxiang  800 85 90

echo "— мастер, зал, логотип"
wide   IMG_3106.JPG hero-master   1500 85 270   # мастер разливает из чахая (кадр повёрнут)
wide   IMG_3107.JPG ceremony-self 1300 85 270   # самостоятельная церемония, вид сверху
wide   photo_2026-07-16_07-59-34.jpg master-pour 1100 85   # мастер наливает в чахай, вертикальный

# Знак «ЧНЯ» отдельно от задника. Вырезан из photo_..._07-59-35.jpg скриптом
# extract_logo.py (нужен Pillow, у нас он в venv скретчпада), исходник лежит
# в src-assets/logo-mark.png. Пересобирать нужно, только если пришлют новый логотип.
echo "— знак (готовый PNG с альфой)"
cwebp -quiet -q 90 -alpha_q 100 "$HOME/Desktop/tea-house/src-assets/logo-mark.png" -o "$OUT/logo-mark.webp"
wide   IMG_3093.JPG hall-room     1600 85   # зал целиком
wide   IMG_3094.JPG hall-table    1300 85   # стол на двоих
wide   IMG_3098.JPG hall-bar      1300 85   # стойка, сифон, полки
wide   IMG_3100.JPG hall-shelf    1300 85   # полки с чаем

echo
ls -la "$OUT" | awk 'NR>3 {printf "%-24s %6.1f KB\n", $9, $5/1024}'
echo "-----"
echo "итого: $(du -sh "$OUT" | cut -f1)"

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
square () {  # $1=файл $2=имя $3=размер $4=качество
  local f="$SRC/$1" t="$TMP/$2.jpg"
  sips -Z $(( $3 * 3 )) "$f" --out "$t" >/dev/null 2>&1
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

echo "— чаи (квадрат 360, q62)"
square IMG_3116.JPG tea-chongshicha   360 62
square IMG_3117.JPG tea-baihao        360 62
square IMG_3118.JPG tea-baimudan      360 62
square IMG_3119.JPG tea-longjing      360 62
square IMG_3120.JPG tea-molimaojian   360 62
square IMG_3121.JPG tea-xiaozhong     360 62
square IMG_3122.JPG tea-biluogold     360 62
square IMG_3123.JPG tea-dianhong      360 62
square IMG_3124.JPG tea-longjinghong  360 62
square IMG_3125.JPG tea-herbal        360 62
square IMG_3126.JPG tea-maoxie        360 62
square IMG_3127.JPG tea-huangjingui   360 62
square IMG_3128.JPG tea-ginseng       360 62
square IMG_3104.JPG tea-gaba-ruby     360 62
square IMG_3130.JPG tea-gaba-honey    360 62
square IMG_3131.JPG tea-gaba-maocha   360 62
square IMG_3132.JPG tea-dahongpao     360 62
square IMG_3103.JPG tea-dancong       360 62
square IMG_3134.JPG tea-laochatou     360 62
square IMG_3135.JPG tea-nuomixiang    360 62
square IMG_3136.JPG tea-mandarin      360 62
square IMG_3137.JPG tea-bingdao       360 62
square IMG_3102.JPG tea-peacock       360 62
square IMG_3139.JPG tea-jinhuawang    360 62

echo "— мастер, зал, логотип"
wide   IMG_3106.JPG hero-master   1100 74 270   # мастер разливает из чахая (кадр повёрнут)
wide   IMG_3107.JPG ceremony-self  900 70 270   # самостоятельная церемония, вид сверху
wide   photo_2026-07-16_07-59-34.jpg master-pour 760 74   # мастер наливает в чахай, вертикальный

# Знак «ЧНЯ» отдельно от задника. Вырезан из photo_..._07-59-35.jpg скриптом
# extract_logo.py (нужен Pillow, у нас он в venv скретчпада), исходник лежит
# в src-assets/logo-mark.png. Пересобирать нужно, только если пришлют новый логотип.
echo "— знак (готовый PNG с альфой)"
cwebp -quiet -q 90 -alpha_q 100 "$HOME/Desktop/tea-house/src-assets/logo-mark.png" -o "$OUT/logo-mark.webp"
wide   IMG_3093.JPG hall-room     1200 70   # зал целиком
wide   IMG_3094.JPG hall-table    1000 70   # стол на двоих
wide   IMG_3098.JPG hall-bar      1000 70   # стойка, сифон, полки
wide   IMG_3100.JPG hall-shelf    1000 70   # полки с чаем

echo
ls -la "$OUT" | awk 'NR>3 {printf "%-24s %6.1f KB\n", $9, $5/1024}'
echo "-----"
echo "итого: $(du -sh "$OUT" | cut -f1)"

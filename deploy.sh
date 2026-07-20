#!/usr/bin/env bash
# Деплой сайта на свой сервер (liable-copper), а не на GitHub Pages.
#
# Переехали с Pages, потому что их в России периодически режут (сайт не
# открывался). Теперь chainya.ru живёт рядом с ботом на 79.137.194.101.
#
#   ./deploy.sh          собрать и выложить
#
# git push больше НЕ обновляет сайт — обновляет этот скрипт.
set -euo pipefail

HOST="liable-copper"
DIR="/var/www/chainya"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$(dirname "$0")"

echo "→ сборка (build.py --web)"
python3 build.py --web >/dev/null
[ -f dist/index.html ] || { echo "✗ dist/index.html не собрался"; exit 1; }

echo "→ упаковка ($(du -sh dist | cut -f1))"
COPYFILE_DISABLE=1 tar czf "$TMP/site.tgz" -C dist --exclude=CNAME .

echo "→ заливка на $HOST"
scp -q "$TMP/site.tgz" "$HOST:/tmp/site.tgz"

echo "→ раскладка + проверка"
ssh "$HOST" '
  set -e
  tar xzf /tmp/site.tgz -C '"$DIR"'
  rm -f /tmp/site.tgz
  chown -R www-data:www-data '"$DIR"'
  nginx -t >/dev/null 2>&1 || { echo "✗ конфиг nginx сломан — reload не делаю"; exit 1; }
  # проверяем по https: http теперь 301→https, старая проверка на http падала
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -k -H "Host: chainya.ru" https://127.0.0.1/)
  echo "  локальная проверка: HTTPS $code, файлов: $(find '"$DIR"' -type f | wc -l)"
  [ "$code" = "200" ] || exit 1
'

echo "→ проверка снаружи"
curl -s -o /dev/null -w "  https://chainya.ru → %{http_code} за %{time_total}s\n" --max-time 15 https://chainya.ru/ || true
echo "✓ готово"

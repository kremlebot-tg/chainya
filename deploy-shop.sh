#!/usr/bin/env bash
# Разворачивает тестовый checkout backend и статический сайт на liable-copper.
set -euo pipefail

HOST="liable-copper"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BOT_ROOT="$ROOT/../telegram-bot"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

test -f "$BOT_ROOT/teas.json" || { echo "нет каталога $BOT_ROOT/teas.json"; exit 1; }

cd "$ROOT"
python3 build.py --web
COPYFILE_DISABLE=1 tar czf "$TMP/shop.tgz" \
  --exclude='backend/data' --exclude='backend/__pycache__' --exclude='backend/tests/__pycache__' \
  backend ops -C "$BOT_ROOT" teas.json

scp -q "$TMP/shop.tgz" "$HOST:/tmp/chainya-shop.tgz"
scp -q ops/chainya-shop.service "$HOST:/tmp/chainya-shop.service"
scp -q ops/nginx-chainya.ru "$HOST:/tmp/nginx-chainya.ru"

ssh "$HOST" '
  set -e
  sudo mkdir -p /opt/chainya-shop /var/lib/chainya-shop
  sudo tar xzf /tmp/chainya-shop.tgz -C /opt/chainya-shop
  sudo python3 -m venv /opt/chainya-shop/.venv
  sudo /opt/chainya-shop/.venv/bin/pip install -q -r /opt/chainya-shop/backend/requirements.txt
  sudo chown -R root:root /opt/chainya-shop
  sudo chown -R www-data:www-data /var/lib/chainya-shop
  sudo install -m 0644 /tmp/chainya-shop.service /etc/systemd/system/chainya-shop.service
  sudo install -m 0644 /tmp/nginx-chainya.ru /etc/nginx/sites-available/chainya.ru
  sudo systemctl daemon-reload
  sudo systemctl enable --now chainya-shop
  sudo nginx -t
  sudo systemctl reload nginx
  rm -f /tmp/chainya-shop.tgz /tmp/chainya-shop.service /tmp/nginx-chainya.ru
'

./deploy.sh
curl -fsS https://chainya.ru/api/health
echo
echo "✓ сайт и тестовый checkout развёрнуты"

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

rsync -az "$TMP/shop.tgz" "$HOST:/tmp/chainya-shop.tgz"
rsync -az ops/chainya-shop.service "$HOST:/tmp/chainya-shop.service"
rsync -az ops/chainya-backup.service "$HOST:/tmp/chainya-backup.service"
rsync -az ops/chainya-backup.timer "$HOST:/tmp/chainya-backup.timer"
rsync -az ops/nginx-chainya.ru "$HOST:/tmp/nginx-chainya.ru"

ssh "$HOST" '
  set -e
  sudo mkdir -p /opt/chainya-shop /var/lib/chainya-shop /var/backups/chainya-shop
  sudo tar xzf /tmp/chainya-shop.tgz -C /opt/chainya-shop
  sudo python3 -m venv /opt/chainya-shop/.venv
  sudo /opt/chainya-shop/.venv/bin/pip install -q -r /opt/chainya-shop/backend/requirements.txt
  sudo chown -R root:root /opt/chainya-shop
  sudo chown -R www-data:www-data /var/lib/chainya-shop
  sudo chown -R root:root /var/backups/chainya-shop
  sudo chmod 0700 /var/lib/chainya-shop /var/backups/chainya-shop
  if sudo test -e /var/lib/chainya-shop/orders.sqlite3; then
    sudo chmod 0600 /var/lib/chainya-shop/orders.sqlite3
  fi
  sudo install -m 0644 /tmp/chainya-shop.service /etc/systemd/system/chainya-shop.service
  sudo install -m 0644 /tmp/chainya-backup.service /etc/systemd/system/chainya-backup.service
  sudo install -m 0644 /tmp/chainya-backup.timer /etc/systemd/system/chainya-backup.timer
  sudo install -m 0644 /tmp/nginx-chainya.ru /etc/nginx/sites-available/chainya.ru
  sudo grep -E "^(BOT_TOKEN|OWNER_CHAT_ID)=" /opt/chainya-bot/.env | sudo tee /etc/chainya-shop.env >/dev/null
  if ! sudo test -s /etc/chainya-shop-admin.env; then
    printf "ADMIN_TOKEN=%s\n" "$(openssl rand -hex 24)" | sudo tee /etc/chainya-shop-admin.env >/dev/null
  fi
  sudo chmod 600 /etc/chainya-shop.env
  sudo chmod 600 /etc/chainya-shop-admin.env
  sudo systemctl daemon-reload
  sudo systemctl enable chainya-shop
  sudo systemctl enable --now chainya-backup.timer
  sudo systemctl restart chainya-shop
  sudo nginx -t
  sudo systemctl reload nginx
  sudo systemctl start chainya-backup.service
  rm -f /tmp/chainya-shop.tgz /tmp/chainya-shop.service /tmp/chainya-backup.service /tmp/chainya-backup.timer /tmp/nginx-chainya.ru
'

./deploy.sh
curl -fsS https://chainya.ru/api/health
echo
echo "✓ сайт и тестовый checkout развёрнуты"

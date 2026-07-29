#!/usr/bin/env bash
# Разворачивает checkout backend и статический сайт на liable-copper.
set -euo pipefail

HOST="liable-copper"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BOT_ROOT="$ROOT/../telegram-bot"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

test -f "$BOT_ROOT/teas.json" || { echo "нет каталога $BOT_ROOT/teas.json"; exit 1; }

cd "$ROOT"
test -z "$(git status --porcelain)" || {
  echo "рабочая папка не чистая — сначала зафиксируйте release commit"
  exit 1
}
RELEASE_COMMIT="$(git rev-parse HEAD)"
printf '%s\n' "$RELEASE_COMMIT" > "$TMP/RELEASE_COMMIT"
python3 build.py --web
COPYFILE_DISABLE=1 tar czf "$TMP/shop.tgz" \
  --exclude='backend/data' --exclude='backend/__pycache__' --exclude='backend/tests/__pycache__' \
  --exclude='backend/.env' --exclude='backend/.env.*' --exclude='backend/*.env' \
  backend ops -C "$BOT_ROOT" teas.json

rsync -az "$TMP/shop.tgz" "$HOST:/tmp/chainya-shop.tgz"
rsync -az "$TMP/RELEASE_COMMIT" "$HOST:/tmp/chainya-release-commit"
rsync -az ops/chainya-shop.service "$HOST:/tmp/chainya-shop.service"
rsync -az ops/chainya-backup.service "$HOST:/tmp/chainya-backup.service"
rsync -az ops/chainya-backup.timer "$HOST:/tmp/chainya-backup.timer"
rsync -az ops/nginx-chainya.ru "$HOST:/tmp/nginx-chainya.ru"

ssh "$HOST" '
  set -e
  sudo mkdir -p /opt/chainya-shop /var/lib/chainya-shop /var/backups/chainya-shop
  sudo tar xzf /tmp/chainya-shop.tgz -C /opt/chainya-shop
  sudo install -m 0444 /tmp/chainya-release-commit /opt/chainya-shop/RELEASE_COMMIT
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
  sudo cp -a /etc/nginx/sites-available/chainya.ru /tmp/nginx-chainya.ru.previous
  sudo install -m 0644 /tmp/nginx-chainya.ru /etc/nginx/sites-available/chainya.ru
  sudo grep -E "^(BOT_TOKEN|OWNER_CHAT_ID)=" /opt/chainya-bot/.env | sudo tee /etc/chainya-shop.env >/dev/null
  if ! sudo test -s /etc/chainya-shop-admin.env; then
    printf "ADMIN_TOKEN=%s\n" "$(openssl rand -hex 24)" | sudo tee /etc/chainya-shop-admin.env >/dev/null
  fi
  if ! sudo test -e /etc/chainya-shop-integrations.env; then
    printf "CHAINYA_TEST_MODE=1\nTBANK_CHECKOUT_MODE=off\nCDEK_INTEGRATION_MODE=off\nSABY_ORDER_SYNC_MODE=off\n" | sudo tee /etc/chainya-shop-integrations.env >/dev/null
  fi
  sudo chmod 600 /etc/chainya-shop.env
  sudo chmod 600 /etc/chainya-shop-admin.env
  sudo chmod 600 /etc/chainya-shop-integrations.env
  sudo systemctl daemon-reload
  sudo systemctl enable chainya-shop
  sudo systemctl enable --now chainya-backup.timer
  sudo systemctl restart chainya-shop
  if ! sudo nginx -t; then
    sudo cp -a /tmp/nginx-chainya.ru.previous /etc/nginx/sites-available/chainya.ru
    sudo nginx -t
    echo "Новый nginx-конфиг отклонён; прежний восстановлен" >&2
    exit 1
  fi
  sudo systemctl reload nginx
  sudo systemctl start chainya-backup.service
  rm -f /tmp/chainya-shop.tgz /tmp/chainya-release-commit /tmp/chainya-shop.service /tmp/chainya-backup.service /tmp/chainya-backup.timer /tmp/nginx-chainya.ru /tmp/nginx-chainya.ru.previous
'

./deploy.sh
curl -fsS https://chainya.ru/api/health
echo
echo "✓ сайт и checkout развёрнуты"

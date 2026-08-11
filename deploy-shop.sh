#!/usr/bin/env bash
# Согласованный двухсерверный release Chainya.
# Общий Nginx не останавливается и не запускается; maintenance действует только
# внутри выделенного Chainya edge.
set -Eeuo pipefail

HOST="${CHAINYA_ORIGIN_HOST:-liable-copper}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BOT_ROOT="$ROOT/telegram-bot"
EDGE_HOST="${CHAINYA_EDGE_HOST:-$(awk -F'"' '/^EDGE_HOST=/{print $2; exit}' "$ROOT/deploy-edge.sh")}"
MAINTENANCE="$ROOT/ops/timeweb/edge-maintenance.sh"

cd "$ROOT"
test -z "$(git status --porcelain)" || {
  echo "рабочая папка не чистая — сначала зафиксируйте release commit" >&2
  exit 1
}
test -f "$BOT_ROOT/teas.json" || {
  echo "нет каталога $BOT_ROOT/teas.json" >&2
  exit 1
}

RELEASE_COMMIT="$(git rev-parse HEAD)"
STAGE_ID="${RELEASE_COMMIT}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
REMOTE_STAGE="/tmp/chainya-shop-${STAGE_ID}"
edge_previous=""
remote_staged=0
remote_uploaded=0
edge_switched=0
edge_transaction=0
origin_prepared=0
maintenance_enabled=0
TMP="$(mktemp -d)"

remote_transaction() {
  local operation=$1
  ssh "$HOST" \
    "sudo env CHAINYA_STAGE='$REMOTE_STAGE' bash '$REMOTE_STAGE/deploy-shop-remote.sh' '$operation'"
}

restore_edge_release() {
  test -n "$edge_previous"
  ssh "$EDGE_HOST" "bash -se" -- "$edge_previous" <<'REMOTE'
set -Eeuo pipefail
previous=$1
case "$previous" in
  (/var/www/chainya-releases/*) ;;
  (*) echo "некорректный предыдущий edge release" >&2; exit 1 ;;
esac
test -d "$previous"
ln -sfnT "$previous" /var/www/chainya.rollback
mv -Tf /var/www/chainya.rollback /var/www/chainya
test "$(readlink -f /var/www/chainya)" = "$previous"
curl -fsS http://127.0.0.1:8078/__chainya_edge_health >/dev/null
REMOTE
}

open_edge_transaction() {
  ssh "$EDGE_HOST" "bash -se" -- "$edge_previous" <<'REMOTE'
set -Eeuo pipefail
previous=$1
case "$previous" in (/var/www/chainya-releases/*) ;; (*) exit 1 ;; esac
transaction=/run/chainya-edge-deploy.transaction
mkdir "$transaction"
printf '%s\n' "$previous" > "$transaction/previous"
REMOTE
  edge_transaction=1
}

close_edge_transaction() {
  if ! ssh "$EDGE_HOST" \
    "unlink /run/chainya-edge-deploy.transaction/previous && rmdir /run/chainya-edge-deploy.transaction"; then
    return 1
  fi
  edge_transaction=0
}

finish_failed_release() {
  local status=$?
  trap - EXIT ERR HUP INT TERM
  set +e
  [ "$status" -ne 0 ] || status=1
  rollback_failed=0

  if [ "$maintenance_enabled" = 0 ] &&
     { [ "$edge_switched" = 1 ] || [ "$origin_prepared" = 1 ]; }; then
    "$MAINTENANCE" on || rollback_failed=1
    maintenance_enabled=1
  fi
  if [ "$remote_staged" = 1 ]; then
    remote_transaction rollback || rollback_failed=1
  fi
  if [ "$edge_switched" = 1 ]; then
    restore_edge_release || rollback_failed=1
  fi
  if [ "$remote_staged" = 1 ] && [ "$rollback_failed" = 0 ]; then
    if remote_transaction commit; then
      remote_uploaded=0
    else
      rollback_failed=1
    fi
  elif [ "$remote_staged" = 0 ] && [ "$remote_uploaded" = 1 ]; then
    if ssh "$HOST" "rm -rf -- '$REMOTE_STAGE'"; then
      remote_uploaded=0
    else
      rollback_failed=1
    fi
  fi
  if [ "$edge_transaction" = 1 ] && [ "$rollback_failed" = 0 ]; then
    close_edge_transaction || rollback_failed=1
  fi

  if [ "$maintenance_enabled" = 1 ] && [ "$rollback_failed" = 0 ]; then
    ssh "$HOST" \
      "curl -fsS http://127.0.0.1:8077/api/health >/dev/null" || rollback_failed=1
    if [ "$rollback_failed" = 0 ]; then
      "$MAINTENANCE" off || rollback_failed=1
      maintenance_enabled=0
    fi
  fi
  rm -rf -- "$TMP"

  if [ "$rollback_failed" = 1 ]; then
    echo "✗ rollback не завершён; Chainya maintenance оставлен включённым" >&2
  else
    echo "✓ предыдущий Chainya release восстановлен" >&2
  fi
  exit "$status"
}

cleanup_success() {
  rm -rf -- "$TMP"
}

trap finish_failed_release EXIT ERR HUP INT TERM

printf '%s\n' "$RELEASE_COMMIT" > "$TMP/RELEASE_COMMIT"

echo "→ локальная сборка и проверки"
python3 scripts/check-deploy-contract.py
python3 scripts/build-catalog-seed.py --check
python3 build.py --web
python3 scripts/verify-release.py --dist dist

COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TMP/shop.tgz" \
  --exclude='backend/data' --exclude='backend/__pycache__' \
  --exclude='backend/tests/__pycache__' --exclude='backend/.env' \
  --exclude='backend/.env.*' --exclude='backend/*.env' \
  --exclude='telegram-bot/.venv' --exclude='telegram-bot/.env' \
  --exclude='telegram-bot/.env.*' --exclude='telegram-bot/__pycache__' \
  --exclude='telegram-bot/.pytest_cache' --exclude='telegram-bot/favs.json' \
  backend ops telegram-bot scripts/check-deploy-contract.py deploy-edge.sh \
  deploy-shop.sh deploy-bot.sh privacy.html
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TMP/web.tgz" \
  --exclude='._*' --exclude='.DS_Store' -C dist .

echo "→ staging origin без остановки сервисов"
ssh "$HOST" "umask 077; mkdir '$REMOTE_STAGE'"
remote_uploaded=1
rsync -az \
  "$TMP/shop.tgz" \
  "$TMP/web.tgz" \
  "$TMP/RELEASE_COMMIT" \
  ops/deploy-shop-remote.sh \
  ops/chainya-shop.service \
  ops/chainya-backup.service \
  ops/chainya-backup.timer \
  ops/nginx-chainya.ru \
  "$HOST:$REMOTE_STAGE/"
remote_transaction stage
remote_staged=1

edge_previous=$(ssh "$EDGE_HOST" "readlink -f /var/www/chainya")
case "$edge_previous" in
  (/var/www/chainya-releases/*) ;;
  (*) echo "активный edge release не прошёл проверку" >&2; false ;;
esac
open_edge_transaction

echo "→ Chainya-only maintenance"
maintenance_enabled=1
"$MAINTENANCE" on

echo "→ атомарная подготовка frontend за maintenance"
CHAINYA_EDGE_MAINTENANCE=1 "$ROOT/deploy-edge.sh"
edge_switched=1

echo "→ транзакционный cutover origin"
remote_transaction cutover
origin_prepared=1

echo "→ снятие Chainya-only maintenance"
"$MAINTENANCE" off
maintenance_enabled=0

echo "→ публичная проверка согласованного release"
python3 scripts/verify-release.py --dist dist --base-url https://chainya.ru

# После public health транзакция больше не откатывается: commit только удаляет
# временные rollback-артефакты и deploy-lock.
trap - EXIT ERR HUP INT TERM
if ! remote_transaction commit; then
  cleanup_success
  echo "✗ release работает, но remote cleanup не завершён; новый deploy заблокирован до ручной проверки" >&2
  exit 1
fi
remote_staged=0
remote_uploaded=0
if ! close_edge_transaction; then
  cleanup_success
  echo "✗ release работает, но edge transaction-lock не удалён; новый deploy заблокирован до ручной проверки" >&2
  exit 1
fi
cleanup_success
echo "✓ Chainya release опубликован без остановки общего Nginx"

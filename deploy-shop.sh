#!/usr/bin/env bash
# Согласованный деплой checkout backend и статического сайта на liable-copper.
# Оба артефакта готовятся заранее и переключаются как один release.
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
STAGE_ID="${RELEASE_COMMIT}-$(date +%Y%m%d%H%M%S)-$$"
REMOTE_STAGE="/tmp/chainya-shop-${STAGE_ID}"
printf '%s\n' "$RELEASE_COMMIT" > "$TMP/RELEASE_COMMIT"

echo "→ сборка и локальная проверка"
python3 scripts/build-catalog-seed.py --check
python3 build.py --web
python3 scripts/verify-release.py --dist dist

COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TMP/shop.tgz" \
  --exclude='backend/data' --exclude='backend/__pycache__' --exclude='backend/tests/__pycache__' \
  --exclude='backend/.env' --exclude='backend/.env.*' --exclude='backend/*.env' \
  backend ops privacy.html -C "$BOT_ROOT" teas.json
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TMP/web.tgz" \
  --exclude='._*' --exclude='.DS_Store' -C dist .

echo "→ загрузка согласованного release"
ssh "$HOST" "umask 077; mkdir '$REMOTE_STAGE'"
rsync -az \
  "$TMP/shop.tgz" \
  "$TMP/web.tgz" \
  "$TMP/RELEASE_COMMIT" \
  ops/chainya-shop.service \
  ops/chainya-backup.service \
  ops/chainya-backup.timer \
  ops/nginx-chainya.ru \
  scripts/verify-release.py \
  "$HOST:$REMOTE_STAGE/"

echo "→ подготовка, переключение и проверка release"
ssh "$HOST" "CHAINYA_STAGE='$REMOTE_STAGE' bash -se" <<'REMOTE'
set -Eeuo pipefail
umask 077

stage=${CHAINYA_STAGE:?}
backend_active=/opt/chainya-shop
backend_releases=/opt/chainya-shop-releases
web_active=/var/www/chainya
web_releases=/var/www/chainya-releases
rollback_dir="$stage/rollback"

cleanup() {
  sudo rm -rf -- "$stage" || true
}
trap cleanup EXIT

exec 9>"${HOME:?}/.chainya-deploy.lock"
if ! flock -n 9; then
  echo "✗ уже выполняется другой деплой chainya" >&2
  exit 1
fi

release_commit=$(cat "$stage/RELEASE_COMMIT")
case "$release_commit" in
  (*[!0-9a-f]*|'') echo "✗ некорректный release commit" >&2; exit 1 ;;
esac
case "${#release_commit}" in
  (40|64) ;;
  (*) echo "✗ некорректная длина release commit" >&2; exit 1 ;;
esac

release_id="${release_commit}-$(date +%Y%m%d%H%M%S)-$$"
backend_release="${backend_releases}/${release_id}"
web_release="${web_releases}/${release_id}"
backend_previous=""
web_previous=""
backend_legacy=0
web_legacy=0
backend_release_created=0
web_release_created=0
config_snapshot_ready=0
config_mutation_started=0
cutover_started=0
deployment_committed=0
ownership_snapshot_ready=0
ownership_mutation_started=0
marker_mutation_started=0
rollback_failed=0

shop_unit_had_previous=0
backup_unit_had_previous=0
backup_timer_had_previous=0
nginx_had_previous=0
shop_env_had_previous=0
admin_env_had_previous=0
integrations_env_had_previous=0
marker_had_previous=0

shop_was_active=0
nginx_was_active=0
backup_timer_was_active=0
shop_was_enabled=0
backup_timer_was_enabled=0
data_owner_before=""
data_mode_before=""
backup_owner_before=""
backup_mode_before=""

ensure_service_account() {
  local nologin_shell
  if ! getent group chainya-shop >/dev/null; then
    sudo groupadd --system chainya-shop
  fi
  if ! id -u chainya-shop >/dev/null 2>&1; then
    nologin_shell=$(command -v nologin || true)
    nologin_shell=${nologin_shell:-/usr/sbin/nologin}
    sudo useradd --system --gid chainya-shop --home-dir /nonexistent \
      --shell "$nologin_shell" chainya-shop
  fi
  getent group chainya-shop >/dev/null
  id -u chainya-shop >/dev/null
  sudo -u chainya-shop -g chainya-shop true
}

snapshot_file() {
  local path=$1
  local name=$2
  local result_variable=$3
  if sudo test -e "$path" || sudo test -L "$path"; then
    sudo cp -aT -- "$path" "$rollback_dir/$name"
    printf -v "$result_variable" '%s' 1
  fi
}

restore_file() {
  local path=$1
  local name=$2
  local had_previous=$3
  if [ "$had_previous" = 1 ]; then
    sudo cp -aT --remove-destination -- "$rollback_dir/$name" "$path"
  else
    sudo rm -f -- "$path"
  fi
}

restore_active() {
  local active=$1
  local previous=$2
  local was_legacy=$3

  sudo rm -f -- "${active}.next" "${active}.rollback" || return 1
  if [ "$was_legacy" = 1 ]; then
    if sudo test -d "$previous"; then
      if sudo test -L "$active"; then
        sudo unlink "$active" || return 1
      elif sudo test -e "$active"; then
        echo "✗ нельзя вернуть legacy-каталог: $active уже занят" >&2
        return 1
      fi
      sudo mv "$previous" "$active" || return 1
    fi
    if sudo test -d "$active" && ! sudo test -L "$active"; then
      return 0
    fi
    return 1
  fi

  if [ -n "$previous" ]; then
    sudo test -d "$previous" || return 1
    sudo ln -sfnT "$previous" "${active}.rollback" || return 1
    sudo mv -Tf "${active}.rollback" "$active" || return 1
    if [ "$(sudo readlink -f "$active")" = "$previous" ]; then
      return 0
    fi
    return 1
  fi

  if sudo test -L "$active"; then
    sudo unlink "$active" || return 1
  fi
  ! sudo test -e "$active" && ! sudo test -L "$active"
}

restore_enabled_state() {
  local unit=$1
  local was_enabled=$2
  if [ "$was_enabled" = 1 ]; then
    sudo systemctl enable "$unit" >/dev/null &&
      sudo systemctl is-enabled --quiet "$unit"
  else
    sudo systemctl disable "$unit" >/dev/null 2>&1 || true
    ! sudo systemctl is-enabled --quiet "$unit"
  fi
}

rollback_step() {
  local description=$1
  shift
  if "$@"; then
    return 0
  fi
  echo "✗ rollback: $description" >&2
  rollback_failed=1
  return 0
}

wait_backend_health() {
  local _attempt
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -fs --max-time 2 \
      http://127.0.0.1:8077/api/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

require_production_health() {
  python3 -c 'import json, sys
payload = json.load(sys.stdin)
raise SystemExit(0 if payload.get("ok") is True and payload.get("test_mode") is False else 1)'
}

restore_ownership() {
  [ "$ownership_snapshot_ready" = 1 ] || return 0
  sudo chown -R "$data_owner_before" /var/lib/chainya-shop || return 1
  sudo chmod "$data_mode_before" /var/lib/chainya-shop || return 1
  sudo chown -R "$backup_owner_before" /var/backups/chainya-shop || return 1
  sudo chmod "$backup_mode_before" /var/backups/chainya-shop
}

restore_shop_runtime() {
  if [ "$shop_was_active" = 1 ]; then
    sudo systemctl start chainya-shop && wait_backend_health
  else
    sudo systemctl stop chainya-shop >/dev/null 2>&1 || true
    ! sudo systemctl is-active --quiet chainya-shop
  fi
}

restore_backup_timer_runtime() {
  if [ "$backup_timer_was_active" = 1 ]; then
    sudo systemctl start chainya-backup.timer &&
      sudo systemctl is-active --quiet chainya-backup.timer
  else
    sudo systemctl stop chainya-backup.timer >/dev/null 2>&1 || true
    ! sudo systemctl is-active --quiet chainya-backup.timer
  fi
}

restore_nginx_runtime() {
  if [ "$nginx_was_active" != 1 ]; then
    sudo systemctl stop nginx >/dev/null 2>&1 || true
    ! sudo systemctl is-active --quiet nginx
    return
  fi

  sudo nginx -t || return 1
  sudo systemctl start nginx || return 1
  curl -fkSs --max-time 10 \
    --resolve chainya.ru:443:127.0.0.1 \
    https://chainya.ru/ >/dev/null || return 1
  if [ "$shop_was_active" = 1 ]; then
    curl -fkSs --max-time 10 \
      --resolve chainya.ru:443:127.0.0.1 \
      https://chainya.ru/api/health >/dev/null || return 1
  fi
}

remove_release_if_inactive() {
  local active=$1
  local release=$2
  local current
  current=$(sudo readlink -f "$active" 2>/dev/null || true)
  if [ "$current" = "$release" ]; then
    return 1
  fi
  sudo rm -rf -- "$release"
}

rollback_release() {
  local status=${1:-1}
  trap - EXIT ERR HUP INT TERM
  set +e
  [ "$status" -ne 0 ] || status=1
  rollback_failed=0

  echo "✗ release не прошёл проверку; выполняется общий rollback" >&2
  if [ "$cutover_started" = 1 ]; then
    rollback_step "не удалось остановить Nginx" sudo systemctl stop nginx
    rollback_step "не удалось остановить backend" sudo systemctl stop chainya-shop
    rollback_step "не удалось вернуть backend symlink" \
      restore_active "$backend_active" "$backend_previous" "$backend_legacy"
    rollback_step "не удалось вернуть web symlink" \
      restore_active "$web_active" "$web_previous" "$web_legacy"
  fi

  if [ "$config_snapshot_ready" = 1 ] &&
     [ "$config_mutation_started" = 1 ]; then
    rollback_step "не удалось вернуть chainya-shop.service" \
      restore_file /etc/systemd/system/chainya-shop.service \
      chainya-shop.service "$shop_unit_had_previous"
    rollback_step "не удалось вернуть chainya-backup.service" \
      restore_file /etc/systemd/system/chainya-backup.service \
      chainya-backup.service "$backup_unit_had_previous"
    rollback_step "не удалось вернуть chainya-backup.timer" \
      restore_file /etc/systemd/system/chainya-backup.timer \
      chainya-backup.timer "$backup_timer_had_previous"
    rollback_step "не удалось вернуть Nginx-конфиг" \
      restore_file /etc/nginx/sites-available/chainya.ru \
      nginx-chainya.ru "$nginx_had_previous"
    rollback_step "не удалось вернуть основной env" \
      restore_file /etc/chainya-shop.env \
      chainya-shop.env "$shop_env_had_previous"
    rollback_step "не удалось вернуть admin env" \
      restore_file /etc/chainya-shop-admin.env \
      chainya-shop-admin.env "$admin_env_had_previous"
    rollback_step "не удалось вернуть integrations env" \
      restore_file /etc/chainya-shop-integrations.env \
      chainya-shop-integrations.env "$integrations_env_had_previous"
    rollback_step "systemd daemon-reload завершился ошибкой" \
      sudo systemctl daemon-reload
    rollback_step "не удалось вернуть enable-state backend" \
      restore_enabled_state chainya-shop.service "$shop_was_enabled"
    rollback_step "не удалось вернуть enable-state backup timer" \
      restore_enabled_state chainya-backup.timer "$backup_timer_was_enabled"
  fi

  if [ "$ownership_mutation_started" = 1 ]; then
    rollback_step "не удалось вернуть владельцев data/backup" restore_ownership
  fi
  if [ "$marker_mutation_started" = 1 ]; then
    rollback_step "не удалось вернуть release marker" \
      restore_file /var/lib/chainya-shop/web-release-commit \
      web-release-commit "$marker_had_previous"
  fi

  if [ "$cutover_started" = 1 ]; then
    rollback_step "предыдущий backend не прошёл health-check" \
      restore_shop_runtime
    rollback_step "не удалось вернуть runtime-state backup timer" \
      restore_backup_timer_runtime
    rollback_step "предыдущий Nginx/site не прошёл health-check" \
      restore_nginx_runtime
  fi

  if [ "$backend_release_created" = 1 ]; then
    rollback_step "новый backend release всё ещё активен или не удалён" \
      remove_release_if_inactive "$backend_active" "$backend_release"
  fi
  if [ "$web_release_created" = 1 ]; then
    rollback_step "новый web release всё ещё активен или не удалён" \
      remove_release_if_inactive "$web_active" "$web_release"
  fi

  cleanup
  if [ "$rollback_failed" = 1 ]; then
    echo "✗ rollback НЕ завершён; требуется ручное вмешательство" >&2
  elif [ "$cutover_started" = 1 ]; then
    echo "✓ предыдущая версия восстановлена и проверена" >&2
  else
    echo "✓ подготовка отменена; работающая версия не переключалась" >&2
  fi
  exit "$status"
}

finish_remote() {
  local status=$1
  trap - EXIT ERR HUP INT TERM
  if [ "$deployment_committed" = 1 ]; then
    cleanup
    exit "$status"
  fi
  rollback_release "$status"
}

trap 'finish_remote $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

sudo mkdir -p \
  "$backend_releases" \
  "$web_releases" \
  /var/lib/chainya-shop \
  /var/backups/chainya-shop
sudo chmod 0755 "$backend_releases" "$web_releases"
for state_dir in /var/lib/chainya-shop /var/backups/chainya-shop; do
  if sudo test -L "$state_dir" || ! sudo test -d "$state_dir"; then
    echo "✗ state path должен быть обычным каталогом: $state_dir" >&2
    false
  fi
done
ensure_service_account
data_owner_before=$(sudo stat -c '%u:%g' /var/lib/chainya-shop)
data_mode_before=$(sudo stat -c '%a' /var/lib/chainya-shop)
backup_owner_before=$(sudo stat -c '%u:%g' /var/backups/chainya-shop)
backup_mode_before=$(sudo stat -c '%a' /var/backups/chainya-shop)
ownership_snapshot_ready=1

if sudo test -e "$backend_release" || sudo test -e "$web_release"; then
  echo "✗ каталог release уже существует: $release_id" >&2
  false
fi

# Backend и static полностью готовятся до остановки сервисов.
sudo mkdir "$backend_release"
backend_release_created=1
sudo tar xzf "$stage/shop.tgz" -C "$backend_release"
sudo install -m 0444 "$stage/RELEASE_COMMIT" "$backend_release/RELEASE_COMMIT"
sudo chmod -R a+rX "$backend_release"

# Проверки запускаются в одноразовом окружении: pytest/httpx не попадают в
# production-venv и не исполняются от root.
python3 -m venv "$stage/test-venv"
"$stage/test-venv/bin/pip" install -q \
  -r "$backend_release/backend/requirements-dev.txt"
(
  cd "$backend_release"
  CHAINYA_DATA_DIR="$stage/test-data" \
    "$stage/test-venv/bin/python" -m pytest -q backend/tests
)
rm -rf -- "$stage/test-venv" "$stage/test-data"

sudo python3 -m venv "$backend_release/.venv"
sudo "$backend_release/.venv/bin/pip" install -q \
  -r "$backend_release/backend/requirements.txt"
sudo chown -R root:root "$backend_release"
sudo chmod -R a+rX "$backend_release"

sudo mkdir "$web_release"
web_release_created=1
sudo tar xzf "$stage/web.tgz" -C "$web_release"
sudo chown -R root:root "$web_release"
sudo chmod -R a+rX "$web_release"
sudo test -s "$web_release/index.html"

if sudo test -L "$backend_active"; then
  backend_previous=$(sudo readlink -f "$backend_active")
  sudo test -d "$backend_previous"
elif sudo test -d "$backend_active"; then
  backend_previous="${backend_releases}/legacy-${release_id}"
  backend_legacy=1
elif sudo test -e "$backend_active"; then
  echo "✗ $backend_active не является каталогом или symlink" >&2
  false
fi

if sudo test -L "$web_active"; then
  web_previous=$(sudo readlink -f "$web_active")
  sudo test -d "$web_previous"
elif sudo test -d "$web_active"; then
  web_previous="${web_releases}/legacy-${release_id}"
  web_legacy=1
elif sudo test -e "$web_active"; then
  echo "✗ $web_active не является каталогом или symlink" >&2
  false
fi

if sudo systemctl is-active --quiet chainya-shop; then
  shop_was_active=1
fi
if sudo systemctl is-active --quiet nginx; then
  nginx_was_active=1
fi
if sudo systemctl is-active --quiet chainya-backup.timer; then
  backup_timer_was_active=1
fi
if sudo systemctl is-enabled --quiet chainya-shop; then
  shop_was_enabled=1
fi
if sudo systemctl is-enabled --quiet chainya-backup.timer; then
  backup_timer_was_enabled=1
fi

# Снимок всех изменяемых конфигураций и env хранится только во временном
# каталоге с umask 077; содержимое секретов не выводится.
sudo mkdir -p "$rollback_dir"
snapshot_file /etc/systemd/system/chainya-shop.service \
  chainya-shop.service shop_unit_had_previous
snapshot_file /etc/systemd/system/chainya-backup.service \
  chainya-backup.service backup_unit_had_previous
snapshot_file /etc/systemd/system/chainya-backup.timer \
  chainya-backup.timer backup_timer_had_previous
snapshot_file /etc/nginx/sites-available/chainya.ru \
  nginx-chainya.ru nginx_had_previous
snapshot_file /etc/chainya-shop.env \
  chainya-shop.env shop_env_had_previous
snapshot_file /etc/chainya-shop-admin.env \
  chainya-shop-admin.env admin_env_had_previous
snapshot_file /etc/chainya-shop-integrations.env \
  chainya-shop-integrations.env integrations_env_had_previous
snapshot_file /var/lib/chainya-shop/web-release-commit \
  web-release-commit marker_had_previous
config_snapshot_ready=1

sudo grep -E "^(BOT_TOKEN|OWNER_CHAT_ID|BOOKING_BOT_SECRET)=" /etc/chainya-bot.env |
  sudo tee "$stage/chainya-shop.env.next" >/dev/null
sudo chmod 0600 "$stage/chainya-shop.env.next"
for required_key in BOT_TOKEN OWNER_CHAT_ID BOOKING_BOT_SECRET; do
  if ! sudo awk -F= -v key="$required_key" \
    '$1 == key && length(substr($0, index($0, "=") + 1)) > 0 { found++ } END { exit(found == 1 ? 0 : 1) }' \
    "$stage/chainya-shop.env.next"; then
    echo "✗ в /etc/chainya-bot.env отсутствует единственный непустой $required_key" >&2
    false
  fi
done

config_mutation_started=1
sudo install -m 0600 "$stage/chainya-shop.env.next" /etc/chainya-shop.env
if ! sudo test -s /etc/chainya-shop-admin.env; then
  printf "ADMIN_TOKEN=%s\n" "$(openssl rand -hex 24)" \
    > "$stage/chainya-shop-admin.env.next"
  sudo install -m 0600 "$stage/chainya-shop-admin.env.next" \
    /etc/chainya-shop-admin.env
fi
if ! sudo test -s /etc/chainya-shop-integrations.env; then
  # Новый public-host не должен принимать mock-оплату: production-mode включён,
  # а все внешние записи остаются off до явной настройки оператором.
  printf "CHAINYA_TEST_MODE=0\nTBANK_CHECKOUT_MODE=off\nCDEK_INTEGRATION_MODE=off\nSABY_ORDER_SYNC_MODE=off\n" \
    > "$stage/chainya-shop-integrations.env.next"
  sudo install -m 0600 "$stage/chainya-shop-integrations.env.next" \
    /etc/chainya-shop-integrations.env
fi
if ! sudo awk -F= \
  '$1 == "CHAINYA_TEST_MODE" { count++; valid = ($2 == "0") }
   END { exit(count == 1 && valid ? 0 : 1) }' \
  /etc/chainya-shop-integrations.env; then
  echo "✗ public deploy требует единственный CHAINYA_TEST_MODE=0" >&2
  false
fi
sudo chmod 0600 \
  /etc/chainya-shop.env \
  /etc/chainya-shop-admin.env \
  /etc/chainya-shop-integrations.env
sudo install -m 0644 "$stage/chainya-shop.service" \
  /etc/systemd/system/chainya-shop.service
sudo install -m 0644 "$stage/chainya-backup.service" \
  /etc/systemd/system/chainya-backup.service
sudo install -m 0644 "$stage/chainya-backup.timer" \
  /etc/systemd/system/chainya-backup.timer
sudo install -m 0644 "$stage/nginx-chainya.ru" \
  /etc/nginx/sites-available/chainya.ru
sudo systemctl daemon-reload
sudo nginx -t

# .next не влияет на работающую версию и сокращает окно остановки Nginx.
sudo ln -sfnT "$backend_release" "${backend_active}.next"
sudo ln -sfnT "$web_release" "${web_active}.next"

# Пока переключаются два независимых symlink, Nginx остановлен. Так ни один
# запрос не может увидеть новый frontend со старым backend или наоборот.
cutover_started=1
sudo systemctl stop chainya-backup.timer
sudo systemctl stop nginx
sudo systemctl stop chainya-shop

if [ "$backend_legacy" = 1 ]; then
  sudo mv "$backend_active" "$backend_previous"
fi
sudo mv -Tf "${backend_active}.next" "$backend_active"

if [ "$web_legacy" = 1 ]; then
  sudo mv "$web_active" "$web_previous"
fi
sudo mv -Tf "${web_active}.next" "$web_active"

test "$(sudo readlink -f "$backend_active")" = "$backend_release"
test "$(sudo readlink -f "$web_active")" = "$web_release"

ownership_mutation_started=1
sudo chown -R chainya-shop:chainya-shop \
  /var/lib/chainya-shop \
  /var/backups/chainya-shop
sudo chmod 0700 /var/lib/chainya-shop /var/backups/chainya-shop
if sudo test -e /var/lib/chainya-shop/orders.sqlite3; then
  sudo chmod 0600 /var/lib/chainya-shop/orders.sqlite3
fi

sudo systemctl start chainya-shop
if ! wait_backend_health; then
  echo "✗ backend не прошёл health-check" >&2
  false
fi
if ! curl -fsS --max-time 2 http://127.0.0.1:8077/api/health |
  require_production_health; then
  echo "✗ public backend запущен в test mode" >&2
  false
fi

sudo systemctl start nginx
curl -fkSs --max-time 10 \
  --resolve chainya.ru:443:127.0.0.1 \
  https://chainya.ru/ >/dev/null
curl -fkSs --max-time 10 \
  --resolve chainya.ru:443:127.0.0.1 \
  https://chainya.ru/api/health >/dev/null

# Проверка через публичные DNS/TLS остаётся внутри rollback-транзакции.
curl -fsS --max-time 15 https://chainya.ru/ >/dev/null
curl -fsS --max-time 15 https://chainya.ru/api/health |
  require_production_health
python3 "$stage/verify-release.py" \
  --dist "$web_release" \
  --base-url https://chainya.ru

sudo systemctl enable chainya-shop
sudo systemctl enable --now chainya-backup.timer
sudo systemctl start chainya-backup.service
marker_mutation_started=1
sudo install -m 0444 "$stage/RELEASE_COMMIT" \
  /var/lib/chainya-shop/web-release-commit

deployment_committed=1

# Оставляем текущую и предыдущую согласованные версии. Старые каталоги больше
# не участвуют в выдаче, поэтому удаление не может оставить stale-файлы.
for candidate in "$backend_releases"/*; do
  if sudo test -d "$candidate" &&
     [ "$candidate" != "$backend_release" ] &&
     [ "$candidate" != "$backend_previous" ]; then
    sudo rm -rf -- "$candidate"
  fi
done
for candidate in "$web_releases"/*; do
  if sudo test -d "$candidate" &&
     [ "$candidate" != "$web_release" ] &&
     [ "$candidate" != "$web_previous" ]; then
    sudo rm -rf -- "$candidate"
  fi
done
REMOTE

echo "→ внешняя проверка"
if curl -fsS https://chainya.ru/api/health; then
  echo
else
  echo "⚠ локальная внешняя проверка health недоступна; remote-проверка уже пройдена" >&2
fi
if ! python3 scripts/verify-release.py --dist dist --base-url https://chainya.ru; then
  echo "⚠ локальная внешняя release-проверка недоступна; remote-проверка уже пройдена" >&2
fi
echo "✓ сайт и checkout развёрнуты одним release"

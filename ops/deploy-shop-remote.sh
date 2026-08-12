#!/usr/bin/env bash
# Транзакционное переключение Chainya origin. Запускается deploy-shop.sh по SSH.
# Общий Nginx никогда не останавливается и не запускается этим скриптом.
set -Eeuo pipefail

action=${1:-}
stage=${CHAINYA_STAGE:?CHAINYA_STAGE is required}
test_root=${CHAINYA_DEPLOY_TEST_ROOT:-}
failpoint=${CHAINYA_DEPLOY_FAILPOINT:-}

if [ -n "$failpoint" ] && [ -z "$test_root" ]; then
  echo "failpoints разрешены только с CHAINYA_DEPLOY_TEST_ROOT" >&2
  exit 64
fi
if [ -z "$test_root" ] && [ "$(id -u)" -ne 0 ]; then
  echo "remote transaction must run as root" >&2
  exit 77
fi

root_path() { printf '%s%s' "$test_root" "$1"; }

backend_active=$(root_path /opt/chainya-shop)
backend_releases=$(root_path /opt/chainya-shop-releases)
web_active=$(root_path /var/www/chainya)
web_releases=$(root_path /var/www/chainya-releases)
data_dir=$(root_path /var/lib/chainya-shop)
backup_dir=$(root_path /var/backups/chainya-shop)
shop_unit=$(root_path /etc/systemd/system/chainya-shop.service)
backup_unit=$(root_path /etc/systemd/system/chainya-backup.service)
backup_timer_unit=$(root_path /etc/systemd/system/chainya-backup.timer)
nginx_config=$(root_path /etc/nginx/sites-available/chainya.ru)
integrations_env=$(root_path /etc/chainya-shop-integrations.env)
run_dir=$(root_path /run/chainya-deploy.transaction)
transaction=$stage/transaction
meta=$transaction/meta.sh
phase_file=$transaction/phase

phase() { cat "$phase_file" 2>/dev/null || echo none; }
set_phase() { printf '%s\n' "$1" > "$phase_file"; }
resolve_path() {
  python3 - "$1" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
}

service_is_active() {
  if [ -n "$test_root" ]; then
    test -f "$(root_path "/run/$1.active")"
  else
    systemctl is-active --quiet "$1"
  fi
}
service_is_enabled() {
  if [ -n "$test_root" ]; then
    test -f "$(root_path "/run/$1.enabled")"
  else
    systemctl is-enabled --quiet "$1"
  fi
}
service_start() {
  if [ -n "$test_root" ]; then
    : > "$(root_path "/run/$1.active")"
    printf 'start %s\n' "$1" >> "$(root_path /run/service-actions.log)"
  else
    systemctl start "$1"
  fi
}
service_stop() {
  if [ -n "$test_root" ]; then
    rm -f -- "$(root_path "/run/$1.active")"
    printf 'stop %s\n' "$1" >> "$(root_path /run/service-actions.log)"
  else
    systemctl stop "$1"
  fi
}
daemon_reload() {
  if [ -n "$test_root" ]; then
    printf 'daemon-reload\n' >> "$(root_path /run/service-actions.log)"
  else
    systemctl daemon-reload
  fi
}
nginx_test() {
  if [ -n "$test_root" ]; then
    printf 'test nginx\n' >> "$(root_path /run/service-actions.log)"
    ! grep -q 'INVALID_NGINX_TEST' "$nginx_config" 2>/dev/null
  else
    nginx -t
  fi
}
nginx_reload() {
  if [ -n "$test_root" ]; then
    printf 'reload nginx\n' >> "$(root_path /run/service-actions.log)"
  else
    systemctl reload nginx
  fi
}
backend_health() {
  if [ -n "$test_root" ]; then
    service_is_active chainya-shop
  else
    local payload _attempt
    for _attempt in {1..30}; do
      payload=$(curl -fsS --max-time 2 http://127.0.0.1:8077/api/health 2>/dev/null || true)
      if [ -n "$payload" ] && python3 -c \
        'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") is True and d.get("test_mode") is False else 1)' \
        <<<"$payload"; then
        return 0
      fi
      sleep 1
    done
    return 1
  fi
}
origin_health() {
  if [ -n "$test_root" ]; then
    backend_health
  else
    local _attempt
    for _attempt in {1..15}; do
      if curl -fkSs --max-time 3 \
        --resolve chainya.ru:443:127.0.0.1 \
        https://chainya.ru/api/health >/dev/null 2>&1; then
        return 0
      fi
      sleep 1
    done
    return 1
  fi
}
maybe_fail() {
  if [ "$failpoint" = "$1" ]; then
    echo "test failpoint: $1" >&2
    return 97
  fi
}

require_active_symlink() {
  local path=$1
  test -L "$path"
  test -d "$(resolve_path "$path")"
}
atomic_symlink() {
  local target=$1 active=$2 suffix=$3
  rm -f -- "${active}.${suffix}"
  ln -s "$target" "${active}.${suffix}"
  atomic_move "${active}.${suffix}" "$active"
}
atomic_move() {
  python3 - "$1" "$2" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
}
snapshot_file() {
  local source=$1 name=$2
  if [ -e "$source" ] || [ -L "$source" ]; then
    cp -a -- "$source" "$transaction/config/$name"
    printf '1'
  else
    printf '0'
  fi
}
restore_file() {
  local target=$1 name=$2 had=$3
  if [ "$had" = 1 ]; then
    rm -f -- "${target}.rollback"
    cp -a -- "$transaction/config/$name" "${target}.rollback"
    atomic_move "${target}.rollback" "$target"
  else
    rm -f -- "$target"
  fi
}
same_as_snapshot() {
  local current=$1 name=$2 had=$3
  if [ "$had" = 1 ]; then
    cmp -s -- "$current" "$transaction/config/$name"
  else
    test ! -e "$current" && test ! -L "$current"
  fi
}
write_meta() {
  {
    printf 'release_commit=%q\n' "$release_commit"
    printf 'release_id=%q\n' "$release_id"
    printf 'backend_release=%q\n' "$backend_release"
    printf 'web_release=%q\n' "$web_release"
    printf 'backend_previous=%q\n' "$backend_previous"
    printf 'web_previous=%q\n' "$web_previous"
    printf 'shop_was_active=%q\n' "$shop_was_active"
    printf 'timer_was_active=%q\n' "$timer_was_active"
    printf 'timer_was_enabled=%q\n' "$timer_was_enabled"
    printf 'shop_unit_had=%q\n' "$shop_unit_had"
    printf 'backup_unit_had=%q\n' "$backup_unit_had"
    printf 'backup_timer_had=%q\n' "$backup_timer_had"
    printf 'nginx_had=%q\n' "$nginx_had"
    printf 'shop_unit_changed=%q\n' "$shop_unit_changed"
    printf 'backup_unit_changed=%q\n' "$backup_unit_changed"
    printf 'backup_timer_changed=%q\n' "$backup_timer_changed"
    printf 'nginx_changed=%q\n' "$nginx_changed"
    printf 'integrations_env_had=%q\n' "$integrations_env_had"
    printf 'integrations_env_changed=%q\n' "$integrations_env_changed"
  } > "$meta"
  chmod 0600 "$meta"
}
load_meta() {
  test -s "$meta"
  # shellcheck disable=SC1090
  source "$meta"
}
verify_transaction_owner() {
  test -s "$run_dir/stage"
  test "$(cat "$run_dir/stage")" = "$stage"
}

stage_release() {
  test -s "$stage/RELEASE_COMMIT"
  test -s "$stage/shop.tgz"
  test -s "$stage/web.tgz"
  test -s "$stage/chainya-shop.service"
  test -s "$stage/chainya-backup.service"
  test -s "$stage/chainya-backup.timer"
  test -s "$stage/nginx-chainya.ru"
  case "$(cat "$stage/RELEASE_COMMIT")" in (*[!0-9a-f]*|'') return 1;; esac

  mkdir -p "$(dirname "$run_dir")"
  if ! mkdir "$run_dir" 2>/dev/null; then
    echo "другая Chainya deploy-транзакция уже активна" >&2
    return 1
  fi
  printf '%s\n' "$stage" > "$run_dir/stage"
  trap 'code=$?; trap - ERR; set +e; test -z "${backend_release:-}" || rm -rf -- "$backend_release"; test -z "${web_release:-}" || rm -rf -- "$web_release"; rm -rf -- "$run_dir" "$transaction"; exit "$code"' ERR

  mkdir -p "$transaction/config" "$backend_releases" "$web_releases" "$backup_dir"
  require_active_symlink "$backend_active"
  require_active_symlink "$web_active"
  test -d "$data_dir"
  test ! -L "$data_dir"
  test -f "$nginx_config"

  release_commit=$(cat "$stage/RELEASE_COMMIT")
  release_id="${release_commit}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  backend_release="$backend_releases/$release_id"
  web_release="$web_releases/$release_id"
  backend_previous=$(resolve_path "$backend_active")
  web_previous=$(resolve_path "$web_active")
  test ! -e "$backend_release"
  test ! -e "$web_release"

  mkdir "$backend_release" "$web_release"
  tar xzf "$stage/shop.tgz" -C "$backend_release"
  install -m 0444 "$stage/RELEASE_COMMIT" "$backend_release/RELEASE_COMMIT"
  tar xzf "$stage/web.tgz" -C "$web_release"
  test -s "$web_release/index.html"

  if [ -z "$test_root" ]; then
    python3 -m venv "$stage/test-venv"
    "$stage/test-venv/bin/pip" install -q -r "$backend_release/backend/requirements-dev.txt"
    (
      cd "$backend_release"
      CHAINYA_DATA_DIR="$stage/test-data" \
        "$stage/test-venv/bin/python" -m pytest -q backend/tests
    )
    rm -rf -- "$stage/test-venv" "$stage/test-data"
    python3 -m venv "$backend_release/.venv"
    "$backend_release/.venv/bin/pip" install -q -r "$backend_release/backend/requirements.txt"
    chown -R root:root "$backend_release" "$web_release"
    chmod -R a+rX "$backend_release" "$web_release"
  fi

  shop_was_active=0; service_is_active chainya-shop && shop_was_active=1
  if [ "$shop_was_active" != 1 ]; then
    echo "chainya-shop не активен; deploy не будет менять runtime-state" >&2
    return 1
  fi
  timer_was_active=0; service_is_active chainya-backup.timer && timer_was_active=1
  timer_was_enabled=0; service_is_enabled chainya-backup.timer && timer_was_enabled=1
  service_is_active chainya-backup.service && {
    echo "Chainya backup уже выполняется; cutover отменён" >&2
    return 1
  }

  shop_unit_had=$(snapshot_file "$shop_unit" chainya-shop.service)
  backup_unit_had=$(snapshot_file "$backup_unit" chainya-backup.service)
  backup_timer_had=$(snapshot_file "$backup_timer_unit" chainya-backup.timer)
  nginx_had=$(snapshot_file "$nginx_config" nginx-chainya.ru)
  integrations_env_had=0
  if [ -z "$test_root" ]; then
    integrations_env_had=$(snapshot_file "$integrations_env" chainya-shop-integrations.env)
  fi
  shop_unit_changed=1; cmp -s "$stage/chainya-shop.service" "$shop_unit" && shop_unit_changed=0
  backup_unit_changed=1; cmp -s "$stage/chainya-backup.service" "$backup_unit" && backup_unit_changed=0
  backup_timer_changed=1; cmp -s "$stage/chainya-backup.timer" "$backup_timer_unit" && backup_timer_changed=0
  nginx_changed=1; cmp -s "$stage/nginx-chainya.ru" "$nginx_config" && nginx_changed=0
  integrations_env_changed=0
  if [ -z "$test_root" ]; then
    test "$integrations_env_had" = 1
    if ! grep -qx 'SABY_OFD_PAY_METHOD=4' "$integrations_env"; then
      integrations_env_changed=1
    fi
  fi

  # Candidate validation is deliberately fail-closed and happens before the
  # installed Nginx file is touched.
  grep -Fq 'server_name chainya.ru' "$stage/nginx-chainya.ru"
  ! grep -Eq 'systemctl[[:space:]]+(stop|start|restart)[[:space:]]+nginx|service[[:space:]]+nginx[[:space:]]+(stop|start|restart)' "$stage/nginx-chainya.ru"
  if [ "$nginx_changed" = 1 ]; then nginx_test; fi
  write_meta
  set_phase staged
  maybe_fail after_stage
  trap - ERR
}

install_candidate_file() {
  local candidate=$1 target=$2 mode=$3
  install -m "$mode" "$candidate" "${target}.next"
  atomic_move "${target}.next" "$target"
}

rollback_core() {
  load_meta
  local current_phase
  current_phase=$(phase)
  if [ "$current_phase" = staged ]; then
    rm -rf -- "$backend_release" "$web_release"
    set_phase rolled_back
    return 0
  fi
  if [ "$current_phase" != cutting ] && [ "$current_phase" != prepared ]; then
    test "$current_phase" = rolled_back
    return
  fi

  if [ "$timer_was_active" = 1 ]; then service_stop chainya-backup.timer; fi
  service_stop chainya-shop

  if [ -d "$transaction/state" ]; then
    rm -rf -- "$transaction/failed-state"
    mv "$data_dir" "$transaction/failed-state"
    cp -a "$transaction/state" "$data_dir"
  fi
  atomic_symlink "$backend_previous" "$backend_active" rollback
  atomic_symlink "$web_previous" "$web_active" rollback

  restore_file "$shop_unit" chainya-shop.service "$shop_unit_had"
  restore_file "$backup_unit" chainya-backup.service "$backup_unit_had"
  restore_file "$backup_timer_unit" chainya-backup.timer "$backup_timer_had"
  if [ "$integrations_env_changed" = 1 ]; then
    restore_file "$integrations_env" chainya-shop-integrations.env "$integrations_env_had"
  fi
  if [ "$shop_unit_changed" = 1 ] || [ "$backup_unit_changed" = 1 ] || [ "$backup_timer_changed" = 1 ]; then
    daemon_reload
  fi

  if [ "$nginx_changed" = 1 ]; then
    restore_file "$nginx_config" nginx-chainya.ru "$nginx_had"
    nginx_test
    nginx_reload
  fi

  if [ "$timer_was_enabled" = 1 ] && [ -z "$test_root" ]; then
    systemctl enable chainya-backup.timer >/dev/null
  fi
  if [ "$timer_was_active" = 1 ]; then service_start chainya-backup.timer; fi
  if [ "$shop_was_active" = 1 ]; then
    service_start chainya-shop
    backend_health
    origin_health
  fi
  set_phase rolled_back
}

cutover_release() {
  verify_transaction_owner
  load_meta
  test "$(phase)" = staged
  test "$(resolve_path "$backend_active")" = "$backend_previous"
  test "$(resolve_path "$web_active")" = "$web_previous"
  same_as_snapshot "$shop_unit" chainya-shop.service "$shop_unit_had"
  same_as_snapshot "$backup_unit" chainya-backup.service "$backup_unit_had"
  same_as_snapshot "$backup_timer_unit" chainya-backup.timer "$backup_timer_had"
  same_as_snapshot "$nginx_config" nginx-chainya.ru "$nginx_had"
  same_as_snapshot "$integrations_env" chainya-shop-integrations.env "$integrations_env_had"

  set_phase cutting
  trap 'code=$?; trap - ERR; echo "cutover failed; restoring previous Chainya release" >&2; set -e; rollback_core; exit "$code"' ERR
  if [ "$timer_was_active" = 1 ]; then service_stop chainya-backup.timer; fi
  service_is_active chainya-backup.service && false
  if [ "$shop_was_active" = 1 ]; then service_stop chainya-shop; fi
  maybe_fail after_stop

  # Backend уже остановлен: полный state snapshot согласован и не может
  # расходиться с SQLite WAL или каталогом.
  # Копируем сам каталог, а не только его содержимое: rollback обязан вернуть
  # владельца и mode корня data_dir вместе с SQLite/WAL и каталогом.
  cp -a "$data_dir" "$transaction/state"
  maybe_fail after_state_snapshot

  if [ "$shop_unit_changed" = 1 ]; then install_candidate_file "$stage/chainya-shop.service" "$shop_unit" 0644; fi
  if [ "$backup_unit_changed" = 1 ]; then install_candidate_file "$stage/chainya-backup.service" "$backup_unit" 0644; fi
  if [ "$backup_timer_changed" = 1 ]; then install_candidate_file "$stage/chainya-backup.timer" "$backup_timer_unit" 0644; fi
  if [ "$shop_unit_changed" = 1 ] || [ "$backup_unit_changed" = 1 ] || [ "$backup_timer_changed" = 1 ]; then daemon_reload; fi

  if [ "$integrations_env_changed" = 1 ]; then
    # Change only the non-secret receipt-method switch while Chainya is in
    # maintenance and its backend is stopped.  The complete protected file is
    # already in the transaction snapshot and is restored on rollback.
    python3 - "$integrations_env" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
lines = text.splitlines()
matches = [index for index, line in enumerate(lines) if line.startswith("SABY_OFD_PAY_METHOD=")]
if len(matches) != 1:
    raise SystemExit("SABY_OFD_PAY_METHOD must occur exactly once")
lines[matches[0]] = "SABY_OFD_PAY_METHOD=4"
candidate = path.with_name(path.name + ".next")
candidate.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.chmod(candidate, path.stat().st_mode & 0o777)
os.replace(candidate, path)
PY
    grep -qx 'SABY_OFD_PAY_METHOD=4' "$integrations_env"
  fi

  if [ "$nginx_changed" = 1 ]; then
    nginx_test
    install_candidate_file "$stage/nginx-chainya.ru" "$nginx_config" 0644
    nginx_test
    nginx_reload
  fi
  maybe_fail after_config

  atomic_symlink "$backend_release" "$backend_active" next
  atomic_symlink "$web_release" "$web_active" next
  maybe_fail after_symlink

  service_start chainya-shop
  maybe_fail after_start
  backend_health
  origin_health
  maybe_fail after_health

  if [ "$timer_was_enabled" = 1 ] && [ -z "$test_root" ]; then
    systemctl enable chainya-backup.timer >/dev/null
  fi
  if [ "$timer_was_active" = 1 ]; then service_start chainya-backup.timer; fi
  install -m 0444 "$stage/RELEASE_COMMIT" "$data_dir/web-release-commit.next"
  atomic_move "$data_dir/web-release-commit.next" "$data_dir/web-release-commit"
  set_phase prepared
  trap - ERR
}

commit_transaction() {
  verify_transaction_owner
  load_meta
  case "$(phase)" in
    prepared)
      test "$(resolve_path "$backend_active")" = "$backend_release"
      test "$(resolve_path "$web_active")" = "$web_release"
      backend_health
      ;;
    rolled_back)
      rm -rf -- "$backend_release" "$web_release"
      ;;
    *) echo "transaction is not ready to commit" >&2; return 1;;
  esac
  rm -rf -- "$transaction/failed-state"
  rm -rf -- "$run_dir"
  rm -rf -- "$stage"
}

rollback_transaction() {
  verify_transaction_owner
  rollback_core
}

case "$action" in
  stage) stage_release ;;
  cutover) cutover_release ;;
  rollback) rollback_transaction ;;
  commit) commit_transaction ;;
  *) echo "usage: $0 stage|cutover|rollback|commit" >&2; exit 64 ;;
esac

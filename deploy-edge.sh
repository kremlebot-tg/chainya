#!/usr/bin/env bash
# Атомарная публикация статического release на публичный Timeweb edge.
# Backend и интеграции этот скрипт не меняет.
set -Eeuo pipefail

EDGE_HOST="root@5.42.123.182"
ROOT="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
RELEASE_COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
STAGE_ID="${RELEASE_COMMIT}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
REMOTE_STAGE="/tmp/chainya-edge-${STAGE_ID}"
MAINTENANCE_MODE="${CHAINYA_EDGE_MAINTENANCE:-0}"

case "$MAINTENANCE_MODE" in
  (0|1) ;;
  (*) echo "CHAINYA_EDGE_MAINTENANCE должен быть 0 или 1" >&2; exit 64 ;;
esac

cleanup() {
  rm -rf -- "$TMP"
  ssh "$EDGE_HOST" "rm -rf -- '$REMOTE_STAGE'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT"
test -z "$(git status --porcelain)" || {
  echo "рабочая папка не чистая — сначала зафиксируйте release commit" >&2
  exit 1
}

echo "→ сборка и локальная проверка edge"
python3 scripts/build-catalog-seed.py --check
python3 build.py --web
python3 scripts/verify-release.py --dist dist

COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TMP/web.tgz" \
  --exclude='._*' --exclude='.DS_Store' -C dist .
cp scripts/verify-release.py "$TMP/verify-release.py"
cp ops/timeweb/Caddyfile.internal "$TMP/Caddyfile.internal"
printf '%s\n' "$RELEASE_COMMIT" > "$TMP/RELEASE_COMMIT"

echo "→ загрузка edge release"
ssh "$EDGE_HOST" "umask 077; mkdir '$REMOTE_STAGE'"
rsync -az \
  "$TMP/web.tgz" \
  "$TMP/verify-release.py" \
  "$TMP/Caddyfile.internal" \
  "$TMP/RELEASE_COMMIT" \
  "$EDGE_HOST:$REMOTE_STAGE/"

echo "→ атомарное переключение edge"
ssh "$EDGE_HOST" "CHAINYA_EDGE_STAGE='$REMOTE_STAGE' CHAINYA_EDGE_MAINTENANCE='$MAINTENANCE_MODE' bash -se" <<'REMOTE'
set -Eeuo pipefail

stage=${CHAINYA_EDGE_STAGE:?}
maintenance=${CHAINYA_EDGE_MAINTENANCE:?}
releases=/var/www/chainya-releases
active=/var/www/chainya
edge_dir=/opt/chainya-edge
edge_config="$edge_dir/Caddyfile.internal"
edge_compose="$edge_dir/docker-compose.edge.yml"
commit=$(cat "$stage/RELEASE_COMMIT")
case "$commit" in
  (*[!0-9a-f]*|'') echo "✗ некорректный commit" >&2; exit 1 ;;
esac

previous=$(readlink -f "$active")
release="$releases/${commit}-edge-$(date -u +%Y%m%dT%H%M%SZ)"
switched=0
config_changed=0
config_installed=0

wait_for_edge() {
  local attempt
  for attempt in $(seq 1 30); do
    if test "$(docker inspect chainya-edge-edge-1 --format '{{.State.Health.Status}}' 2>/dev/null || true)" = healthy && \
       curl -fsS http://127.0.0.1:8078/__chainya_edge_health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback() {
  local code=${1:-1}
  local failed_line=${2:-unknown}
  trap - ERR
  echo "✗ edge release failed at remote line $failed_line (exit $code)" >&2
  if [ "$switched" = 1 ] && [ -n "$previous" ] && [ -d "$previous" ]; then
    ln -sfnT "$previous" "${active}.rollback"
    mv -Tf "${active}.rollback" "$active"
  fi
  if [ "$config_installed" = 1 ] && [ -s "$stage/Caddyfile.previous" ]; then
    install -m 0644 "$stage/Caddyfile.previous" "${edge_config}.rollback"
    mv -Tf "${edge_config}.rollback" "$edge_config"
    docker compose -f "$edge_compose" up -d --no-deps --force-recreate edge >/dev/null
    wait_for_edge || true
  fi
  rm -rf -- "$stage"
  exit "$code"
}
trap 'rollback "$?" "$LINENO"' ERR

test -f "$edge_config"
test -f "$edge_compose"
cp -p "$edge_config" "$stage/Caddyfile.previous"
if ! cmp -s "$stage/Caddyfile.internal" "$edge_config"; then
  config_changed=1
  test "$maintenance" = 1 || {
    echo "✗ изменение Chainya edge разрешено только за maintenance" >&2
    false
  }
  docker run --rm \
    -v "$stage/Caddyfile.internal:/etc/caddy/Caddyfile:ro" \
    caddy:2.10-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  install -m 0644 "$stage/Caddyfile.internal" "${edge_config}.next"
  mv -Tf "${edge_config}.next" "$edge_config"
  config_installed=1
  docker compose -f "$edge_compose" up -d --no-deps --force-recreate edge >/dev/null
  wait_for_edge
fi

mkdir "$release"
tar -xzf "$stage/web.tgz" -C "$release"
chown -R root:root "$release"
find "$release" -type d -exec chmod 755 {} +
find "$release" -type f -exec chmod 644 {} +

python3 "$stage/verify-release.py" --dist "$release"
ln -sfnT "$release" "${active}.next"
mv -Tf "${active}.next" "$active"
switched=1

test "$(readlink -f "$active")" = "$release"
test "$(docker inspect chainya-edge-edge-1 --format '{{.State.Health.Status}}')" = healthy
curl -fsS http://127.0.0.1:8078/__chainya_edge_health >/dev/null
grep -Fq 'Рекомендуем начать свой чайный путь с этих позиций:' "$release/assets/site.js"
! grep -Eq 'id="ts-taste"|renderRadar' "$release/index.html"
if [ "$maintenance" = 1 ]; then
  test -f /var/www/chainya-maintenance.enabled
  test "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8078/api/health)" = 503
else
  curl -fsS http://127.0.0.1:8078/api/health >/dev/null
fi

rm -rf -- "$stage"
trap - ERR
printf '✓ edge release: %s\n' "$release"
REMOTE

echo "→ публичная проверка edge"
if [ "$MAINTENANCE_MODE" = 1 ]; then
  test "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://chainya.ru/api/health)" = 503
  echo "✓ edge обновлён за Chainya-only maintenance"
  exit 0
fi
curl -fsS https://chainya.ru/shop -o "$TMP/public-shop.html"
grep -Eq '<script src="/assets/site\.js\?v=[0-9a-f]{12}" defer></script>' "$TMP/public-shop.html"
! grep -Eq 'id="ts-taste"|renderRadar' "$TMP/public-shop.html"
curl -fsS https://chainya.ru/assets/site.js -o "$TMP/public-site.js"
grep -Fq 'Рекомендуем начать свой чайный путь с этих позиций:' "$TMP/public-site.js"
curl -fsS https://chainya.ru/business -o "$TMP/public-business.html"
grep -Fq 'Можно начать с небольшой партии и проверить спрос.' "$TMP/public-business.html"
curl -fsS https://chainya.ru/api/health -o "$TMP/public-health.json"
python3 - "$TMP/public-health.json" "${RELEASE_COMMIT:0:12}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    health = json.load(source)
if health.get("ok") is not True or health.get("version") != sys.argv[2]:
    raise SystemExit(f"production health mismatch: {health}")
PY

echo "✓ публичный edge обновлён"

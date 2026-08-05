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
printf '%s\n' "$RELEASE_COMMIT" > "$TMP/RELEASE_COMMIT"

echo "→ загрузка edge release"
ssh "$EDGE_HOST" "umask 077; mkdir '$REMOTE_STAGE'"
rsync -az \
  "$TMP/web.tgz" \
  "$TMP/verify-release.py" \
  "$TMP/RELEASE_COMMIT" \
  "$EDGE_HOST:$REMOTE_STAGE/"

echo "→ атомарное переключение edge"
ssh "$EDGE_HOST" "CHAINYA_EDGE_STAGE='$REMOTE_STAGE' bash -se" <<'REMOTE'
set -Eeuo pipefail

stage=${CHAINYA_EDGE_STAGE:?}
releases=/var/www/chainya-releases
active=/var/www/chainya
commit=$(cat "$stage/RELEASE_COMMIT")
case "$commit" in
  (*[!0-9a-f]*|'') echo "✗ некорректный commit" >&2; exit 1 ;;
esac

previous=$(readlink -f "$active")
release="$releases/${commit}-edge-$(date -u +%Y%m%dT%H%M%SZ)"
switched=0

rollback() {
  local code=$?
  if [ "$switched" = 1 ] && [ -n "$previous" ] && [ -d "$previous" ]; then
    ln -sfnT "$previous" "${active}.rollback"
    mv -Tf "${active}.rollback" "$active"
  fi
  rm -rf -- "$stage"
  exit "$code"
}
trap rollback ERR

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
curl -fsS http://127.0.0.1:8078/shop -o "$stage/internal-shop.html"
grep -Fq 'Рекомендуем начать свой чайный путь с этих позиций:' "$stage/internal-shop.html"
! grep -Eq 'id="ts-taste"|renderRadar' "$stage/internal-shop.html"

rm -rf -- "$stage"
trap - ERR
printf '✓ edge release: %s\n' "$release"
REMOTE

echo "→ публичная проверка edge"
curl -fsS https://chainya.ru/shop -o "$TMP/public-shop.html"
grep -Fq 'Рекомендуем начать свой чайный путь с этих позиций:' "$TMP/public-shop.html"
! grep -Eq 'id="ts-taste"|renderRadar' "$TMP/public-shop.html"
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

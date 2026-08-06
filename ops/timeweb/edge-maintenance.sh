#!/usr/bin/env bash
# Управляет только marker внутреннего Chainya edge. Общий Caddy, Nginx и другие
# vhost этим скриптом не перезагружаются и не изменяются.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EDGE_HOST="${CHAINYA_EDGE_HOST:-$(awk -F'"' '/^EDGE_HOST=/{print $2; exit}' "$ROOT/deploy-edge.sh")}"
MARKER=/var/www/chainya-maintenance.enabled
PUBLIC_HEALTH=https://chainya.ru/api/health

case "${1:-}" in
  on)
    ssh "$EDGE_HOST" "umask 077; : > '$MARKER'"
    expected=503
    ;;
  off)
    ssh "$EDGE_HOST" "rm -f -- '$MARKER'"
    expected=200
    ;;
  status)
    if ssh "$EDGE_HOST" "test -f '$MARKER'"; then
      echo on
    else
      echo off
    fi
    exit 0
    ;;
  *)
    echo "usage: $0 on|off|status" >&2
    exit 64
    ;;
esac

for _attempt in 1 2 3 4 5 6 7 8 9 10; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$PUBLIC_HEALTH" || true)
  if [ "$code" = "$expected" ]; then
    printf 'Chainya maintenance: %s\n' "$1"
    exit 0
  fi
  sleep 1
done

echo "Chainya maintenance не подтвердился ожидаемым HTTP $expected" >&2
if [ "$1" = off ]; then
  # Снятие maintenance fail-closed: при сомнительном public health marker
  # возвращается, чтобы пользователи не попали на несогласованный release.
  ssh "$EDGE_HOST" "umask 077; : > '$MARKER'" || true
fi
exit 1

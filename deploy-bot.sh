#!/usr/bin/env bash
# Атомарный деплой Telegram-бота из этого Git-репозитория.
set -Eeuo pipefail

HOST="liable-copper"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BOT_ROOT="$ROOT/telegram-bot"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

case "$BOT_ROOT" in
  "$ROOT/telegram-bot") ;;
  *) echo "бот должен публиковаться только из Git-репозитория" >&2; exit 1 ;;
esac
for file in bot.py teas.json requirements.txt requirements.lock.txt test_bot_booking.py; do
  test -s "$BOT_ROOT/$file" || { echo "нет $BOT_ROOT/$file" >&2; exit 1; }
  git -C "$ROOT" ls-files --error-unmatch "telegram-bot/$file" >/dev/null || {
    echo "telegram-bot/$file не отслеживается Git" >&2
    exit 1
  }
done
test -s "$ROOT/ops/chainya-bot.service"
test -z "$(git -C "$ROOT" status --porcelain)" || {
  echo "рабочая папка сайта не чистая — сначала зафиксируйте release commit" >&2
  exit 1
}

echo "→ локальная проверка Telegram-бота"
python3 -m venv "$TMP/test-venv"
"$TMP/test-venv/bin/pip" install -q \
  -r "$BOT_ROOT/requirements.lock.txt" 'pytest==9.0.3'
(
  cd "$BOT_ROOT"
  BOT_TOKEN=test-token "$TMP/test-venv/bin/python" -m pytest -q test_bot_booking.py
)
rm -rf -- "$TMP/test-venv"

release_id="$(git -C "$ROOT" rev-parse --short=12 HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"
case "$release_id" in (*[!A-Za-z0-9TZ-]*|'') echo "некорректный release id" >&2; exit 1;; esac
printf '%s\n' "$release_id" >"$TMP/RELEASE_ID"
COPYFILE_DISABLE=1 tar --no-xattrs -czf "$TMP/bot.tgz" \
  --exclude='.env' --exclude='.env.*' --exclude='favs.json' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='._*' \
  -C "$BOT_ROOT" .

remote_stage="/tmp/chainya-bot-$release_id-$$"
ssh "$HOST" "umask 077; mkdir '$remote_stage'"
rsync -az "$TMP/bot.tgz" "$TMP/RELEASE_ID" \
  "$ROOT/ops/chainya-bot.service" "$HOST:$remote_stage/"

echo "→ подготовка и атомарное переключение Telegram-бота"
ssh "$HOST" "CHAINYA_BOT_STAGE='$remote_stage' bash -se" <<'REMOTE'
set -Eeuo pipefail
umask 077

stage=${CHAINYA_BOT_STAGE:?}
active=/opt/chainya-bot
releases=/opt/chainya-bot-releases
config=/etc/chainya-bot.env
unit=/etc/systemd/system/chainya-bot.service
release_id=$(cat "$stage/RELEASE_ID")
release="$releases/$release_id"
previous=""
unit_backup=""
switched=0
committed=0

cleanup() { sudo rm -rf -- "$stage" || true; }
rollback() {
  status=$?
  if [ "$committed" != 1 ]; then
    echo "✗ деплой бота не завершён, возвращаем предыдущую версию" >&2
    if [ "$switched" = 1 ] && [ -n "$previous" ] && sudo test -d "$previous"; then
      sudo ln -sfnT "$previous" "$active.rollback"
      sudo mv -Tf "$active.rollback" "$active"
    fi
    if [ -n "$unit_backup" ] && sudo test -f "$unit_backup"; then
      sudo cp -aT "$unit_backup" "$unit"
    fi
    sudo systemctl daemon-reload || true
    sudo systemctl restart chainya-bot || true
    sudo rm -rf -- "$release" || true
  fi
  cleanup
  exit "$status"
}
trap rollback EXIT

exec 9>"${HOME:?}/.chainya-bot-deploy.lock"
flock -n 9 || { echo "уже выполняется другой деплой бота" >&2; exit 1; }

case "$release" in /opt/chainya-bot-releases/*) ;; *) exit 1;; esac
sudo install -d -m 0755 -o root -g root "$releases"
if ! getent group chainya-bot >/dev/null; then sudo groupadd --system chainya-bot; fi
if ! id -u chainya-bot >/dev/null 2>&1; then
  sudo useradd --system --gid chainya-bot --home-dir /nonexistent \
    --shell /usr/sbin/nologin chainya-bot
fi

if ! sudo test -f "$config"; then
  sudo test -f "$active/.env" || { echo "нет исходного env бота" >&2; exit 1; }
  sudo install -m 0640 -o root -g chainya-bot "$active/.env" "$config"
fi
sudo chown root:chainya-bot "$config"
sudo chmod 0640 "$config"
for key in BOT_TOKEN OWNER_CHAT_ID BOOKING_BOT_SECRET; do
  sudo grep -q "^${key}=..*" "$config" || { echo "в env отсутствует $key" >&2; exit 1; }
done

sudo test ! -e "$release"
sudo mkdir "$release"
sudo tar xzf "$stage/bot.tgz" -C "$release"
sudo python3 -m venv "$release/.venv"
sudo "$release/.venv/bin/pip" install -q -r "$release/requirements.lock.txt"
sudo chown -R root:root "$release"
sudo chmod -R a+rX "$release"
sudo test -s "$release/bot.py"
sudo test -s "$release/teas.json"
sudo systemd-analyze verify "$stage/chainya-bot.service"

sudo install -d -m 0700 -o chainya-bot -g chainya-bot /var/lib/chainya-bot
if ! sudo test -e /var/lib/chainya-bot/favs.json && sudo test -f "$active/favs.json"; then
  sudo install -m 0600 -o chainya-bot -g chainya-bot \
    "$active/favs.json" /var/lib/chainya-bot/favs.json
fi

if sudo test -f "$unit"; then
  unit_backup="/var/backups/chainya-shop/chainya-bot.service.before-$release_id"
  sudo cp -aT "$unit" "$unit_backup"
fi
if sudo test -L "$active"; then
  previous=$(sudo readlink -f "$active")
elif sudo test -d "$active"; then
  previous="$releases/legacy-$release_id"
  sudo mv "$active" "$previous"
else
  echo "некорректный active path бота" >&2
  exit 1
fi
sudo ln -sfnT "$release" "$active.next"
sudo mv -Tf "$active.next" "$active"
switched=1
sudo install -m 0644 "$stage/chainya-bot.service" "$unit"
sudo systemctl daemon-reload
sudo systemctl enable chainya-bot >/dev/null
sudo systemctl restart chainya-bot

for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if sudo systemctl is-active --quiet chainya-bot; then
    pid=$(sudo systemctl show -p MainPID --value chainya-bot)
    if [ "$pid" != 0 ] && [ "$(ps -o user= -p "$pid" | xargs)" = chainya-bot ]; then
      break
    fi
  fi
  sleep 1
done
sudo systemctl is-active --quiet chainya-bot
pid=$(sudo systemctl show -p MainPID --value chainya-bot)
test "$pid" != 0
test "$(ps -o user= -p "$pid" | xargs)" = chainya-bot
sleep 2
sudo systemctl is-active --quiet chainya-bot
if sudo journalctl -u chainya-bot --since '-30 seconds' --no-pager | grep -q 'Traceback'; then
  echo "бот записал Traceback после запуска" >&2
  exit 1
fi

committed=1
trap cleanup EXIT
printf 'release=%s\nuser=%s\n' "$release_id" "$(ps -o user= -p "$pid" | xargs)"
REMOTE

echo "✓ Telegram-бот опубликован: $release_id"

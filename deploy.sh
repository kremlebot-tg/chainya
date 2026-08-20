#!/usr/bin/env bash
# Деплой сайта на свой сервер (liable-copper), а не на GitHub Pages.
#
# Переехали с Pages, потому что их в России периодически режут (сайт не
# открывался). Теперь chainya.ru живёт рядом с ботом на 79.137.194.101.
#
#   ./deploy.sh          собрать и выложить
#
# git push больше НЕ обновляет сайт — обновляет этот скрипт.
set -euo pipefail

HOST="liable-copper"
DIR="/var/www/chainya"
RELEASES_DIR="/var/www/chainya-releases"
ROOT="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$ROOT"
test -z "$(git status --porcelain)" || {
  echo "✗ рабочая папка не чистая — сначала зафиксируйте release commit"
  exit 1
}
RELEASE_COMMIT="$(git rev-parse HEAD)"
STAGE_ID="${RELEASE_COMMIT}-$(date +%Y%m%d%H%M%S)-$$"
REMOTE_STAGE="/tmp/chainya-web-${STAGE_ID}"

echo "→ сборка (build.py --web)"
python3 build.py --web >/dev/null
[ -f dist/index.html ] || { echo "✗ dist/index.html не собрался"; exit 1; }

echo "→ подготовка релиза ($(du -sh dist | cut -f1))"
python3 scripts/verify-release.py --dist dist
printf '%s\n' "$RELEASE_COMMIT" > "$TMP/RELEASE_COMMIT"
COPYFILE_DISABLE=1 tar czf "$TMP/web.tgz" \
  --exclude='._*' --exclude='.DS_Store' -C dist .

ssh "$HOST" "umask 077; mkdir '$REMOTE_STAGE'"
rsync -az \
  "$TMP/web.tgz" \
  "$TMP/RELEASE_COMMIT" \
  scripts/verify-release.py \
  "$HOST:$REMOTE_STAGE/"

echo "→ атомарное переключение static"
ssh "$HOST" \
  "CHAINYA_STAGE='$REMOTE_STAGE' CHAINYA_WEB_ACTIVE='$DIR' CHAINYA_WEB_RELEASES='$RELEASES_DIR' bash -se" \
  <<'REMOTE'
set -Eeuo pipefail
umask 077

stage=${CHAINYA_STAGE:?}
active=${CHAINYA_WEB_ACTIVE:?}
releases=${CHAINYA_WEB_RELEASES:?}
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
release_dir="${releases}/${release_id}"
previous_target=""
legacy_directory=0
release_created=0
cutover_started=0
deployment_committed=0
marker_had_previous=0
marker_mutation_started=0
rollback_failed=0

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
  sudo rm -f -- "${active}.next" "${active}.rollback" || return 1
  if [ "$legacy_directory" = 1 ]; then
    if sudo test -d "$previous_target"; then
      if sudo test -L "$active"; then
        sudo unlink "$active" || return 1
      elif sudo test -e "$active"; then
        echo "✗ нельзя вернуть legacy-каталог: $active уже занят" >&2
        return 1
      fi
      sudo mv "$previous_target" "$active" || return 1
    fi
    sudo test -d "$active" && ! sudo test -L "$active"
    return
  fi

  if [ -n "$previous_target" ]; then
    sudo test -d "$previous_target" || return 1
    sudo ln -sfnT "$previous_target" "${active}.rollback" || return 1
    sudo mv -Tf "${active}.rollback" "$active" || return 1
    [ "$(sudo readlink -f "$active")" = "$previous_target" ]
    return
  fi

  if sudo test -L "$active"; then
    sudo unlink "$active" || return 1
  fi
  ! sudo test -e "$active" && ! sudo test -L "$active"
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

restore_nginx_runtime() {
  sudo nginx -t || return 1
  sudo systemctl is-active --quiet nginx || return 1
  curl -fkSs --max-time 10 \
    --resolve chainya.ru:443:127.0.0.1 \
    https://chainya.ru/ >/dev/null
}

remove_release_if_inactive() {
  local current
  current=$(sudo readlink -f "$active" 2>/dev/null || true)
  if [ "$current" = "$release_dir" ]; then
    return 1
  fi
  sudo rm -rf -- "$release_dir"
}

rollback_release() {
  local status=${1:-1}
  trap - EXIT ERR HUP INT TERM
  set +e
  [ "$status" -ne 0 ] || status=1
  rollback_failed=0

  echo "✗ static-релиз не прошёл проверку; выполняется rollback" >&2
  if [ "$cutover_started" = 1 ]; then
    rollback_step "не удалось вернуть web symlink" restore_active
  fi
  if [ "$marker_mutation_started" = 1 ]; then
    rollback_step "не удалось вернуть release marker" \
      restore_file /var/lib/chainya-shop/web-release-commit \
      web-release-commit "$marker_had_previous"
  fi
  if [ "$cutover_started" = 1 ]; then
    rollback_step "предыдущий Nginx/site не прошёл health-check" \
      restore_nginx_runtime
  fi
  if [ "$release_created" = 1 ]; then
    rollback_step "новый web release всё ещё активен или не удалён" \
      remove_release_if_inactive
  fi
  cleanup
  if [ "$rollback_failed" = 1 ]; then
    echo "✗ rollback НЕ завершён; требуется ручное вмешательство" >&2
  elif [ "$cutover_started" = 1 ]; then
    echo "✓ предыдущая static-версия восстановлена и проверена" >&2
  else
    echo "✓ подготовка static отменена; работающая версия не переключалась" >&2
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

sudo mkdir -p "$releases"
sudo chmod 0755 "$releases"
sudo mkdir -p "$rollback_dir"
snapshot_file /var/lib/chainya-shop/web-release-commit \
  web-release-commit marker_had_previous
if sudo test -e "$release_dir"; then
  echo "✗ каталог релиза уже существует: $release_dir" >&2
  false
fi
sudo mkdir "$release_dir"
release_created=1
sudo tar xzf "$stage/web.tgz" -C "$release_dir"
sudo chown -R root:root "$release_dir"
sudo chmod -R a+rX "$release_dir"
sudo test -s "$release_dir/index.html"
sudo nginx -t >/dev/null

if sudo test -L "$active"; then
  previous_target=$(sudo readlink -f "$active")
  sudo test -d "$previous_target"
elif sudo test -d "$active"; then
  echo "✗ $active является legacy-каталогом; безопасно преобразуйте его в symlink отдельной согласованной процедурой" >&2
  false
elif sudo test -e "$active"; then
  echo "✗ $active существует и не является каталогом или symlink" >&2
  false
fi

if sudo systemctl is-active --quiet nginx; then
  :
else
  echo "✗ Nginx не запущен; static-релиз не переключён" >&2
  false
fi

sudo ln -sfnT "$release_dir" "${active}.next"
cutover_started=1
sudo mv -Tf "${active}.next" "$active"

test "$(sudo readlink -f "$active")" = "$release_dir"
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
  -k --resolve chainya.ru:443:127.0.0.1 https://chainya.ru/)
echo "  локальная проверка: HTTPS $code, файлов: $(sudo find "$release_dir" -type f | wc -l)"
test "$code" = "200"

# Публичная DNS/TLS-проверка проходит до фиксации release.
curl -fsS --max-time 15 https://chainya.ru/ >/dev/null
python3 "$stage/verify-release.py" \
  --dist "$release_dir" \
  --base-url https://chainya.ru

sudo mkdir -p /var/lib/chainya-shop
marker_mutation_started=1
sudo install -m 0444 "$stage/RELEASE_COMMIT" /var/lib/chainya-shop/web-release-commit
deployment_committed=1

# Текущий и предыдущий release остаются для мгновенного rollback; прочие
# каталоги больше не участвуют в выдаче и удаляются целиком.
for candidate in "$releases"/*; do
  if sudo test -d "$candidate" &&
     [ "$candidate" != "$release_dir" ] &&
     [ "$candidate" != "$previous_target" ]; then
    sudo rm -rf -- "$candidate"
  fi
done
REMOTE

echo "→ проверка снаружи"
if ! curl -sS -o /dev/null \
  -w "  https://chainya.ru → %{http_code} за %{time_total}s\n" \
  --max-time 15 https://chainya.ru/; then
  echo "⚠ локальная внешняя проверка недоступна; remote-проверка уже пройдена" >&2
fi
if ! python3 scripts/verify-release.py --dist dist --base-url https://chainya.ru; then
  echo "⚠ локальная внешняя release-проверка недоступна; remote-проверка уже пройдена" >&2
fi
echo "✓ готово"

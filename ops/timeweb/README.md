# Timeweb edge для Chainya

Публичный IP `5.42.123.182` принадлежит VPS Re-dnd Scout. Его внешний Caddy
завершает TLS для обоих проектов и проксирует `chainya.ru` на отдельный
localhost-edge `127.0.0.1:8078`.

Внутренний edge:

- работает отдельным контейнером `chainya-edge-edge-1`;
- видит `/var/www` только для чтения;
- отдаёт статический release из `/var/www/chainya`;
- закрывает test/sensitive paths и повторяет security headers;
- проксирует динамические маршруты на локальный backend `127.0.0.1:8077`.

Важно: оба контейнера Caddy используют `network_mode: host`. Во внутреннем
Chainya Caddy admin API отключён, а `localhost:2019` принадлежит внешнему Caddy
Re-dnd Scout. Поэтому нельзя запускать `caddy reload` из контейнера
`chainya-edge-edge-1`: команда попадёт во внешний admin API. Новый внутренний
конфиг сначала проверяется через stdin, затем применяется только перезапуском
`chainya-edge-edge-1` из его собственного compose-файла.

С 01.09.2026 единственная рабочая SQLite, backend и Telegram-бот размещаются на
этом же Timeweb VPS. Старый origin хранится только как остановленная точка
отката и не должен запускаться одновременно с Timeweb. Telegram API на Timeweb
доступен по IPv6; IPv4-доступ к нему по-прежнему не используется.

## Публикация

Статический релиз запускается из корня проекта:

```bash
./deploy-edge.sh
```

Старый двухсерверный `deploy-shop.sh` выведен из эксплуатации после миграции и
завершается fail-closed. Для статических изменений остаётся атомарный
`deploy-edge.sh`. Backend-релиз требует отдельного Timeweb cutover со snapshot
`/var/lib/chainya-shop`; нельзя снова направлять edge на старый origin или
запускать две копии backend/бота одновременно.

`deploy-bot.sh` также направлен только на этот Timeweb VPS. Старый origin не
должен запускать `chainya-bot`: две polling-копии конфликтуют в Telegram API.

Если изменился `Caddyfile.internal`, candidate сначала проверяется той же версией
Caddy, которая работает в контейнере. Затем за уже включённым Chainya-only
maintenance пересоздаётся только `chainya-edge-edge-1`. При ошибке скрипт
возвращает прежний Caddyfile, прежний frontend symlink и снова поднимает только
этот контейнер; внешний Caddy и соседние проекты не перезапускаются.

Контейнерный healthcheck использует локальный `/__chainya_edge_health`, поэтому
maintenance не делает контейнер unhealthy и больше не создаёт запрос каждые
30 секунд к production-origin. Полная последовательность первой установки,
обычного релиза и аварийного восстановления описана в
[`../SAFE_DEPLOY_RUNBOOK.md`](../SAFE_DEPLOY_RUNBOOK.md).

## Проверка до DNS

```bash
ssh root@5.42.123.182 \
  'curl -fsS http://127.0.0.1:8078/api/health && \
   docker inspect chainya-edge-edge-1 --format "{{.State.Health.Status}}"'
```

`Caddyfile.public-snippet` добавляется в Caddyfile Re-dnd Scout только после
успешной внутренней проверки. Перед изменением сохраняются исходные Caddyfile и
compose; новый публичный конфиг сначала проходит `caddy validate`.

## Откат

При неудачном backend cutover внутренний Caddy возвращается к сохранённому
конфигу со старым origin, Timeweb backend и bot останавливаются, а старые
сервисы запускаются только после проверки целостности сохранённой базы. DNS при
этом не меняется.

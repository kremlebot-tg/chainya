# Timeweb edge для Chainya

Публичный IP `5.42.123.182` принадлежит VPS Re-dnd Scout. Его внешний Caddy
завершает TLS для обоих проектов и проксирует `chainya.ru` на отдельный
localhost-edge `127.0.0.1:8078`.

Внутренний edge:

- работает отдельным контейнером `chainya-edge-edge-1`;
- видит `/var/www` только для чтения;
- отдаёт статический release из `/var/www/chainya`;
- закрывает test/sensitive paths и повторяет security headers;
- проксирует динамические маршруты на HTTPS-origin `79.137.194.101` с SNI
  `chainya.ru`.

Важно: оба контейнера Caddy используют `network_mode: host`. Во внутреннем
Chainya Caddy admin API отключён, а `localhost:2019` принадлежит внешнему Caddy
Re-dnd Scout. Поэтому нельзя запускать `caddy reload` из контейнера
`chainya-edge-edge-1`: команда попадёт во внешний admin API. Новый внутренний
конфиг сначала проверяется через stdin, затем применяется только перезапуском
`chainya-edge-edge-1` из его собственного compose-файла.

Backend временно остаётся на старом origin, потому что 04.08.2026 Timeweb VPS
не устанавливал соединение с Telegram API ни по IPv4, ни по IPv6. Это сохраняет
единую SQLite и уведомления владельца во время смены публичного IP. Снимок базы,
env и готовый backend release на Timeweb являются только резервом и не должны
запускаться одновременно с origin без отдельного плана синхронизации.

## Публикация

Обычный релиз запускается из корня проекта:

```bash
./deploy-shop.sh
```

Скрипт сначала готовит и тестирует origin без остановки сервисов. Затем marker
включает maintenance только на внутреннем Chainya edge, статический release
переключается атомарно, а `chainya-shop` проходит транзакционный cutover с полным
snapshot `/var/lib/chainya-shop`. Общий Nginx не останавливается и не запускается.
Если его конфиг не изменился, Nginx вообще не получает команд. При реальном
изменении допустим только `nginx -t` и graceful reload с автоматическим
восстановлением предыдущего файла при ошибке.

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

## Откат DNS

Если новый edge не проходит public health-check, A-записи `chainya.ru` и
`www.chainya.ru` возвращаются на `79.137.194.101`. Старый nginx/backend во время
переезда не выключаются. После DNS-отката внешний Caddy Re-dnd Scout можно
вернуть из сохранённого файла без удаления данных или контейнеров Chainya.

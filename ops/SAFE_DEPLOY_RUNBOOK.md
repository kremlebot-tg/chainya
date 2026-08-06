# Безопасный релиз Chainya при временном двухсерверном режиме

Этот runbook относится только к схеме «публичный edge → старый origin». Он не
разрешает перенос SQLite, запуск второго backend, изменение Telegram egress,
платежей, CDEK или Saby.

## Гарантии candidate

- maintenance включается marker-файлом только во внутреннем Chainya edge;
- внешний Caddy, Kremlinet, VPN и другие vhost не перезапускаются;
- останавливаются только `chainya-shop` и, если он был активен,
  `chainya-backup.timer`;
- активный `chainya-backup.service` блокирует cutover;
- обычный релиз не вызывает Nginx даже для `nginx -t`, если конфиг не изменён;
- единственная допустимая runtime-команда Nginx — `systemctl reload nginx`;
- snapshot всего `/var/lib/chainya-shop` создаётся после остановки backend;
- deploy-lock запрещает вторую одновременно пишущую транзакцию;
- maintenance снимается только после direct и public health.

## Однократная установка maintenance-механизма

До первого запуска нового `deploy-shop.sh` требуется отдельное согласованное
production-окно.

1. Зафиксировать чистый release commit и результаты локальных проверок.
2. Сохранить активные `Caddyfile.internal` и `docker-compose.edge.yml` edge.
3. Проверить новый Caddyfile версией Caddy из используемого container image.
4. Установить оба файла в каталог только внутреннего Chainya edge.
5. Пересоздать только `chainya-edge-edge-1`; внешний Scout Caddy не трогать.
6. Проверить `/__chainya_edge_health` локально: `200`.
7. Выполнить `edge-maintenance.sh on`: публичные Chainya-маршруты должны дать
   `503`, а локальный edge health — остаться `200`.
8. Выполнить `edge-maintenance.sh off`: `/api/health` должен вернуться к `200`.
9. При любой ошибке вернуть сохранённые два файла и пересоздать только
   `chainya-edge-edge-1`.

## Обычный релиз

Перед окном:

1. `git status --short --branch` — дерево чистое, HEAD опубликован.
2. `bash -n` для четырёх shell-скриптов.
3. `python3 scripts/check-deploy-contract.py`.
4. Полный `pytest`, build и `verify-release.py`.
5. Убедиться, что нет активной старой транзакции в `/run` на обоих серверах.

Запуск из корня проекта:

```bash
./deploy-shop.sh
```

Скрипт самостоятельно:

1. собирает release;
2. готовит и тестирует backend, пока production продолжает работать;
3. фиксирует предыдущий edge release;
4. включает Chainya-only maintenance;
5. переключает edge;
6. останавливает backup timer и `chainya-shop`;
7. снимает согласованный state snapshot;
8. переключает origin и запускает backend;
9. проверяет direct origin health;
10. снимает maintenance и проверяет public release;
11. только после этого удаляет транзакционные snapshot и locks.

## Nginx-конфиг

Если candidate полностью совпадает с активным файлом, Nginx не проверяется и не
получает runtime-команд.

Если файл отличается:

1. активный файл уже сохранён в transaction snapshot;
2. candidate проходит статические fail-closed проверки;
3. выполняется `nginx -t` для активной конфигурации;
4. candidate устанавливается через временный файл и atomic replace;
5. снова выполняется `nginx -t`;
6. выполняется только `systemctl reload nginx`;
7. проверяются backend и origin-vhost;
8. при ошибке восстанавливается snapshot, затем ещё раз `nginx -t` и graceful
   reload для возврата старого конфига.

Команды `stop`, `start` и `restart` общего Nginx запрещены contract-тестом.

## Автоматический rollback

При ошибке до успешного public health:

1. maintenance остаётся или повторно включается;
2. останавливаются backup timer и `chainya-shop`;
3. возвращаются предыдущие backend/web symlink;
4. восстанавливаются полный state snapshot, unit-файлы и изменённый Nginx-файл;
5. старый `chainya-shop` запускается;
6. проверяются direct backend и origin-vhost;
7. edge возвращается на сохранённый symlink;
8. public health проверяется после снятия maintenance;
9. snapshot удаляется только после полного восстановления.

Если локальный процесс или SSH оборвался, maintenance остаётся включённым.
Путь origin-транзакции находится в `/run/chainya-deploy.transaction/stage`, а
предыдущий edge release — в `/run/chainya-edge-deploy.transaction/previous`.
Сначала требуется read-only осмотр phase и health; затем с тем же stage запускают
`deploy-shop-remote.sh rollback`, возвращают edge symlink и только после direct
health снимают maintenance. Нельзя удалять locks до завершения rollback.

## Ожидаемое окно

Сборка, backend-тесты и установка venv идут до maintenance. В успешном случае
недоступность ограничена переключением edge, snapshot небольшого state,
перезапуском одного backend и health-check. Ожидаемый ориентир — 15–45 секунд,
но первое production-окно должно измерить фактическое значение. При ошибке
maintenance остаётся до полного rollback, поэтому окно будет длиннее, но
несогласованный checkout пользователям не показывается.

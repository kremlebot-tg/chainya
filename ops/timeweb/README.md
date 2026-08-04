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

Backend временно остаётся на старом origin, потому что 04.08.2026 Timeweb VPS
не устанавливал соединение с Telegram API ни по IPv4, ни по IPv6. Это сохраняет
единую SQLite и уведомления владельца во время смены публичного IP. Снимок базы,
env и готовый backend release на Timeweb являются только резервом и не должны
запускаться одновременно с origin без отдельного плана синхронизации.

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

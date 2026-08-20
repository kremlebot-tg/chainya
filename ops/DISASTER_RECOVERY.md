# Аварийное восстановление Chainya

Этот документ восстанавливает сайт, backend, каталог, загрузки владельца,
Telegram-бот и конфигурацию после потери production-сервера. Он не разрешает
запуск второго пишущего backend: до переключения новый экземпляр остаётся
остановленным, а старый origin либо подтверждённо недоступен, либо остановлен.

## Что является резервной копией

Полное восстановление требует двух независимых частей:

1. GitHub commit или release tag — код сайта, backend, бота, статические медиа,
   systemd/nginx/Caddy-конфигурации и deploy-скрипты.
2. Зашифрованный recovery bundle — SQLite, фактический каталог, загруженные
   фотографии, production env, release marker и необязательное избранное бота.

Один GitHub checkout не содержит заказов, броней, аккаунтов, актуальных
изменений каталога, пользовательских загрузок и секретов интеграций.

Ежедневная локальная копия проверяется командой:

```bash
sudo /opt/chainya-shop/.venv/bin/python \
  /opt/chainya-shop/ops/verify_chainya_backups.py \
  --directory /var/backups/chainya-shop --max-age-hours 36
```

Проверка fail-closed контролирует свежесть, права доступа, SQLite
`integrity_check`, безопасный состав каталожного архива и валидность JSON.

## Однократная подготовка off-site контура

Эти действия выполняются отдельным согласованным окном. До него новые unit-файлы
только лежат в Git и ничего не запускают.

1. На доверенном компьютере владельца создать отдельный сертификат и
   зашифрованный приватный ключ. Не использовать ключ сайта, SSH или TLS:

   ```bash
   openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 \
     -keyout chainya-recovery-private.pem \
     -out chainya-recovery-recipient.pem \
     -subj /CN=Chainya-Recovery
   ```

2. Приватный ключ и его пароль сохранить минимум в двух независимых местах:
   менеджере паролей владельца и на зашифрованном съёмном носителе. На origin,
   edge и в GitHub приватный ключ не копировать.
3. На origin установить только публичный `chainya-recovery-recipient.pem` с
   правами `0644`, отдельный SSH-ключ отправителя с правами `0600` и отдельный
   pinned `known_hosts`. Сервис не использует пользовательский `~/.ssh`.
4. На независимом хранилище создать отдельного пользователя, которому разрешена
   запись только в каталог Chainya. Не использовать root и не давать этому ключу
   интерактивную shell-сессию; допустим forced-command `rrsync` для одного
   каталога.
5. Создать `/etc/chainya-offsite-backup.env` по примеру
   `ops/chainya-offsite-backup.env.example`, права `0600 root:root`.
6. Установить service/timer, выполнить `systemctl daemon-reload`, но timer пока
   не включать.
7. Вручную запустить service один раз. На независимом компьютере скачать новый
   `.cms` и проверить его командой из следующего раздела.
8. Только после успешной расшифровки, проверки manifest и пробного запуска из
   восстановленной SQLite включить `chainya-offsite-backup.timer`.

## Независимая проверка recovery bundle

Проверка выполняется вне production. Команда не печатает секреты и не запускает
приложение:

```bash
python3 ops/recovery_bundle.py verify \
  --bundle chainya-recovery-DATE-COMMIT.cms \
  --recipient-certificate chainya-recovery-recipient.pem \
  --private-key chainya-recovery-private.pem
```

Для проверенного извлечения добавить новый, ещё не существующий каталог:

```bash
python3 ops/recovery_bundle.py verify \
  --bundle chainya-recovery-DATE-COMMIT.cms \
  --recipient-certificate chainya-recovery-recipient.pem \
  --private-key chainya-recovery-private.pem \
  --extract-directory ./chainya-recovered
```

Скрипт сначала проверяет CMS, manifest, SHA-256 каждого файла, SQLite и каталог
и лишь затем создаёт каталог извлечения с правами `0700/0600`.

## Полная процедура восстановления

1. Зафиксировать инцидент и время последней подтверждённой записи.
2. Не менять DNS и не запускать новый backend. Включить maintenance только для
   Chainya, если edge доступен.
3. Подтвердить, что старый backend остановлен или сервер действительно потерян.
   Два одновременно пишущих экземпляра с разными SQLite запрещены.
4. На чистом сервере клонировать GitHub и checkout commit из manifest bundle.
   Если commit отсутствует в default branch, получить его по сохранённому tag
   или полному SHA; не подменять ближайшей версией.
5. Установить зависимости из репозитория, прогнать полный `pytest`, сборку и
   `scripts/verify-release.py`.
6. На отдельной машине расшифровать и проверить bundle. Передать восстановленные
   файлы на новый сервер только по защищённому каналу.
7. При остановленном `chainya-shop` установить:

   - `data/orders.sqlite3` → `/var/lib/chainya-shop/orders.sqlite3`;
   - распакованные `catalog.json` и `catalog-media/` из
     `data/catalog.tar.gz` → `/var/lib/chainya-shop/`;
   - файлы `config/` → соответствующие `/etc/chainya-*.env`;
   - необязательный `state/favs.json` → `/var/lib/chainya-bot/favs.json`.

8. Вернуть владельцев и права из systemd unit, `0700` каталогам данных и `0600`
   SQLite/env. Не переносить приватный recovery-ключ на сервер.
9. Запустить только backend и проверить local health, версию, число товаров,
   read-only каталог и отсутствие второй активной базы.
10. Запустить Telegram-бот и проверить только безопасные команды/чтение
    каталога. Не создавать тестовый платёж или запись Saby/CDEK.
11. Переключить edge/DNS лишь после direct health. Снять maintenance после
    public health и совпадения release marker.
12. Сохранить прежнее состояние до подтверждения владельца. Затем выполнить
    новый off-site bundle и независимую проверку.

## Контрольная частота

- local SQLite/catalog backup: ежедневно, допустимый возраст до 36 часов;
- зашифрованная off-site копия: ежедневно после local backup;
- автоматическая проверка CMS-структуры: каждый запуск;
- независимая расшифровка и полный restore drill: не реже одного раза в месяц;
- проверка доступа к GitHub, recovery-ключу и DNS: раз в квартал;
- release tag: после каждого подтверждённого production-релиза.

Успешный upload сам по себе не является доказательством восстановления. Контур
считается готовым только после независимой расшифровки и пробного запуска копии
без подключения к production-интеграциям.

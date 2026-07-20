# Тестовый checkout backend

Локальный контур заказов без реальных списаний. Сервер сам считает цены по
`../telegram-bot/teas.json`, сохраняет заказы в SQLite и предоставляет тестовую
страницу оплаты.

```bash
cd /Users/mac/Documents/Сайты/Чайня/site
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
python3 build.py --web
CHAINYA_TEST_MODE=1 .venv/bin/uvicorn backend.app:app --reload --port 8080
```

Открыть `http://127.0.0.1:8080`. База создаётся в `backend/data/` и не попадает
в Git.

## API

- `GET /api/health` — состояние и число позиций каталога.
- `GET /api/delivery/quote?method=...` — тестовая стоимость получения.
- `POST /api/orders` — серверная проверка, расчёт и сохранение заказа.
- `GET /api/orders/{id}` — состояние заказа.
- `POST /api/orders/{id}/test-pay` — имитация webhook успешной оплаты.

Цены СДЭК сейчас демонстрационные. Перед продакшеном mock-расчёт и test-pay
заменяются адаптерами СДЭК, эквайринга и Saby. Секреты передаются только через
переменные окружения.

Если заданы `BOT_TOKEN` и `OWNER_CHAT_ID`, после первого перехода заказа в
`paid` backend отправляет владельцам уведомление через Telegram. Повторный вызов
эндпоинта оплаты не дублирует сообщение.

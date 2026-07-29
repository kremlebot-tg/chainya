"""Подготовительный низкоуровневый клиент CDEK API v2.

Модуль не выполняет запросов при импорте или создании клиента. Регистрация
отправления происходит только при явном вызове :meth:`CdekClient.create_order`.
Секреты читаются из окружения и никогда не включаются в тексты ошибок.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


PRODUCTION_API_ROOT = "https://api.cdek.ru/v2"
TOKEN_SAFETY_WINDOW_SECONDS = 60.0


class CdekError(RuntimeError):
    """Безопасная ошибка CDEK без URL, токенов, ключей и тела запроса."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CdekSettings:
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    api_root: str = PRODUCTION_API_ROOT

    @classmethod
    def from_env(cls) -> "CdekSettings":
        api_root = (
            os.getenv("CDEK_API_ROOT", PRODUCTION_API_ROOT).strip() or PRODUCTION_API_ROOT
        ).rstrip("/")
        parsed = urllib.parse.urlparse(api_root)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"api.cdek.ru", "api.edu.cdek.ru"}
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.path != "/v2"
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise CdekError("Некорректный адрес CDEK API")
        return cls(
            client_id=os.getenv("CDEK_CLIENT_ID", "").strip(),
            client_secret=os.getenv("CDEK_CLIENT_SECRET", "").strip(),
            api_root=api_root,
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


class CdekClient:
    """Минимальная обёртка над OAuth, калькулятором и созданием заказа v2."""

    def __init__(
        self,
        settings: CdekSettings | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings or CdekSettings.from_env()
        self._opener = opener
        self._clock = clock
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    def configuration(self) -> dict[str, Any]:
        """Возвращает только состояние настройки, не значения реквизитов."""
        return {
            "configured": self.settings.configured,
            "api_root": self.settings.api_root,
            "missing": [
                name
                for name, value in (
                    ("CDEK_CLIENT_ID", self.settings.client_id),
                    ("CDEK_CLIENT_SECRET", self.settings.client_secret),
                )
                if not value
            ],
        }

    def _json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)

        try:
            with self._opener(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CdekError(f"CDEK вернул HTTP {exc.code}", status_code=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CdekError("CDEK временно недоступен") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CdekError("CDEK вернул некорректный ответ") from exc

    def access_token(self, force: bool = False) -> str:
        """Получает и кеширует OAuth-токен с запасом до его истечения."""
        if not self.settings.configured:
            raise CdekError("Не заданы параметры интеграции CDEK")

        with self._lock:
            now = self._clock()
            if self._token and not force and now < self._token_expires_at:
                return self._token

            # Актуальная спецификация CDEK v2 описывает OAuth-параметры в query.
            query = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            })
            result = self._json_request(
                f"{self.settings.api_root}/oauth/token?{query}",
                method="POST",
            )
            token = result.get("access_token") if isinstance(result, dict) else None
            expires_in = result.get("expires_in") if isinstance(result, dict) else None
            try:
                ttl = float(expires_in)
            except (TypeError, ValueError) as exc:
                raise CdekError("CDEK не вернул срок действия токена") from exc
            if not token or ttl <= 0:
                raise CdekError("CDEK не вернул корректный токен доступа")

            safety_window = min(TOKEN_SAFETY_WINDOW_SECONDS, ttl * 0.1)
            self._token = str(token)
            self._token_expires_at = self._clock() + max(0.0, ttl - safety_window)
            return self._token

    def api(self, path: str, payload: dict[str, Any]) -> Any:
        """Выполняет авторизованный POST и один раз обновляет токен при HTTP 401."""
        if not path.startswith("/"):
            raise ValueError("Путь CDEK должен начинаться с /")

        headers = {"Authorization": f"Bearer {self.access_token()}"}
        try:
            return self._json_request(
                f"{self.settings.api_root}{path}",
                method="POST",
                payload=payload,
                headers=headers,
            )
        except CdekError as exc:
            if exc.status_code != 401:
                raise
            headers["Authorization"] = f"Bearer {self.access_token(force=True)}"
            return self._json_request(
                f"{self.settings.api_root}{path}",
                method="POST",
                payload=payload,
                headers=headers,
            )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Авторизованный GET к безопасному пути API с одним обновлением токена."""
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("Путь CDEK должен быть абсолютным и не содержать query")
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in (params or {}).items()
                if value is not None and value != ""
            },
            doseq=True,
        )
        url = f"{self.settings.api_root}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        try:
            return self._json_request(url, method="GET", headers=headers)
        except CdekError as exc:
            if exc.status_code != 401:
                raise
            headers["Authorization"] = f"Bearer {self.access_token(force=True)}"
            return self._json_request(url, method="GET", headers=headers)

    def calculate_tariff(self, payload: dict[str, Any]) -> Any:
        """Расчёт по конкретному ``tariff_code``: POST /calculator/tariff."""
        return self.api("/calculator/tariff", payload)

    def quote(self, payload: dict[str, Any]) -> Any:
        """Расчёт всех доступных тарифов: POST /calculator/tarifflist."""
        return self.api("/calculator/tarifflist", payload)

    def create_order(self, payload: dict[str, Any]) -> Any:
        """Явно регистрирует заказ: POST /orders; сам модуль метод не вызывает."""
        return self.api("/orders", payload)

    def cities(self, **params: Any) -> Any:
        """Справочник населённых пунктов CDEK."""
        return self.get("/location/cities", params)

    def delivery_points(self, **params: Any) -> Any:
        """Действующие пункты выдачи CDEK."""
        return self.get("/deliverypoints", params)

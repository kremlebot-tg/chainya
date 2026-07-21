"""Минимальный клиент Saby Retail для каталога и заказов.

Секреты берутся только из окружения. Модуль ничего не синхронизирует сам:
переключение магазина на Saby будет отдельным шагом после проверки доступов.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable


AUTH_URL = "https://online.sbis.ru/oauth/service/"
API_ROOT = "https://api.sbis.ru"


class SabyError(RuntimeError):
    """Безопасная ошибка интеграции без вывода ключей в текст или лог."""


@dataclass(frozen=True)
class SabySettings:
    app_client_id: str = ""
    app_secret: str = ""
    secret_key: str = ""
    point_id: int | None = None
    price_list_id: int | None = None

    @classmethod
    def from_env(cls) -> "SabySettings":
        def optional_int(name: str) -> int | None:
            value = os.getenv(name, "").strip()
            return int(value) if value else None

        return cls(
            app_client_id=os.getenv("SABY_APP_CLIENT_ID", "").strip(),
            app_secret=os.getenv("SABY_APP_SECRET", "").strip(),
            secret_key=os.getenv("SABY_SECRET_KEY", "").strip(),
            point_id=optional_int("SABY_POINT_ID"),
            price_list_id=optional_int("SABY_PRICE_LIST_ID"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.app_client_id and self.app_secret and self.secret_key)


class SabyClient:
    def __init__(
        self,
        settings: SabySettings | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        self.settings = settings or SabySettings.from_env()
        self._opener = opener
        self._token = ""
        self._token_at = 0.0
        self._lock = threading.Lock()

    def configuration(self) -> dict[str, Any]:
        return {
            "configured": self.settings.configured,
            "point_id": self.settings.point_id,
            "price_list_id": self.settings.price_list_id,
            "missing": [
                name for name, value in (
                    ("SABY_APP_CLIENT_ID", self.settings.app_client_id),
                    ("SABY_APP_SECRET", self.settings.app_secret),
                    ("SABY_SECRET_KEY", self.settings.secret_key),
                ) if not value
            ],
        }

    def _json_request(
        self, url: str, *, method: str = "GET", payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with self._opener(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SabyError(f"Saby вернул HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SabyError("Saby временно недоступен") from exc
        except json.JSONDecodeError as exc:
            raise SabyError("Saby вернул некорректный ответ") from exc
        if isinstance(result, dict) and result.get("error"):
            error = result["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise SabyError(f"Ошибка Saby: {message or 'запрос отклонён'}")
        return result

    def access_token(self, force: bool = False) -> str:
        if not self.settings.configured:
            raise SabyError("Не заданы параметры сервисного приложения Saby")
        with self._lock:
            if self._token and not force and time.monotonic() - self._token_at < 3000:
                return self._token
            result = self._json_request(AUTH_URL, method="POST", payload={
                "app_client_id": self.settings.app_client_id,
                "app_secret": self.settings.app_secret,
                "secret_key": self.settings.secret_key,
            })
            token = result.get("token") if isinstance(result, dict) else None
            if not token:
                raise SabyError("Saby не вернул токен доступа")
            self._token, self._token_at = str(token), time.monotonic()
            return self._token

    def api(self, path: str, params: dict | None = None, *, method: str = "GET", payload: dict | None = None) -> Any:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        headers = {"X-SBISAccessToken": self.access_token()}
        try:
            return self._json_request(url, method=method, payload=payload, headers=headers)
        except SabyError as exc:
            if "HTTP 401" not in str(exc):
                raise
            headers["X-SBISAccessToken"] = self.access_token(force=True)
            return self._json_request(url, method=method, payload=payload, headers=headers)

    def sales_points(self, product: str = "retail") -> Any:
        return self.api("/retail/point/list", {"product": product, "withPrices": "true", "pageSize": 500})

    def price_lists(self, point_id: int | None = None) -> Any:
        point = point_id or self.settings.point_id
        if not point:
            raise SabyError("Не выбран идентификатор точки продаж Saby")
        return self.api("/retail/nomenclature/price-list", {
            "pointId": point, "actualDate": date.today().isoformat(), "pageSize": 500,
        })

    def catalog(
        self, point_id: int | None = None, price_list_id: int | None = None,
        *, page: int = 0, page_size: int = 25,
    ) -> Any:
        point = point_id or self.settings.point_id
        price = price_list_id or self.settings.price_list_id
        if not point or not price:
            raise SabyError("Не выбраны точка продаж и прайс-лист Saby")
        return self.api("/retail/v2/nomenclature/list", {
            "pointId": point, "priceListId": price, "noStopList": "true",
            "withBalance": "true", "page": page, "pageSize": min(max(page_size, 1), 25),
        })

    def companies(self) -> Any:
        return self.api("/retail/company/list")

    def warehouses(self, company_id: int) -> Any:
        return self.api("/retail/company/warehouses", {"companyId": company_id})

    def balances(self, company_ids: list[int], warehouse_ids: list[int], price_list_ids: list[int]) -> Any:
        return self.api("/retail/nomenclature/balances", {
            "companies": company_ids, "warehouses": warehouse_ids, "priceListIds": price_list_ids,
        })

    def create_delivery_order(self, payload: dict) -> Any:
        """Низкоуровневый метод; вызывающий код обязан собрать и проверить заказ."""
        return self.api("/retail/order/create", method="POST", payload=payload)

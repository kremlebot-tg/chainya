"""Минимальный клиент Saby Retail для каталога и заказов.

Секреты берутся только из окружения. Модуль ничего не синхронизирует сам:
переключение магазина на Saby будет отдельным шагом после проверки доступов.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable


# Service OAuth returns an application token for ``X-SBISAccessToken``.  Do
# not mix it with the separate login/password OFD session API, which returns a
# ``sid`` and uses another authentication contract.
AUTH_URL = "https://online.sbis.ru/oauth/service/"
API_ROOT = "https://api.sbis.ru"


class SabyError(RuntimeError):
    """Безопасная ошибка интеграции без вывода ключей в текст или лог."""


def unwrap_fiscal_response(result: Any) -> Any:
    """Normalize the legacy wrapper used by Saby fiscal endpoints.

    The fiscal API can return its actual JSON response as a serialized string
    in an uppercase ``Result`` field.  A JSON-RPC-style lowercase ``result``
    wrapper is accepted only when it is the response envelope, so an ordinary
    business field named ``result`` is not accidentally discarded.
    """
    value = result
    for _depth in range(5):
        wrapped = False
        if isinstance(value, dict) and "Result" in value:
            value = value["Result"]
            wrapped = True
        elif (
            isinstance(value, dict)
            and "result" in value
            and set(value).issubset({"jsonrpc", "id", "result"})
        ):
            value = value["result"]
            wrapped = True
        if not wrapped:
            return value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SabyError("Saby вернул некорректный результат регистрации чека") from exc
            if not isinstance(value, (dict, list)):
                raise SabyError("Saby вернул результат регистрации чека в неожиданном формате")
        elif not isinstance(value, (dict, list)):
            raise SabyError("Saby вернул результат регистрации чека в неожиданном формате")
    raise SabyError("Saby вернул слишком глубоко вложенный результат регистрации чека")


@dataclass(frozen=True)
class SabySettings:
    app_client_id: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    point_id: int | None = None
    price_list_id: int | None = None
    shop_url: str = "https://chainya.ru"
    success_url: str = "https://chainya.ru/payment/success"
    error_url: str = "https://chainya.ru/payment/fail"

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
            shop_url=os.getenv("SABY_SHOP_URL", "https://chainya.ru").strip(),
            success_url=os.getenv(
                "SABY_SUCCESS_URL", "https://chainya.ru/payment/success"
            ).strip(),
            error_url=os.getenv(
                "SABY_ERROR_URL", "https://chainya.ru/payment/fail"
            ).strip(),
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
            detail = self._safe_http_error_detail(exc)
            suffix = f": {detail}" if detail else ""
            raise SabyError(f"Saby вернул HTTP {exc.code}{suffix}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SabyError("Saby временно недоступен") from exc
        except json.JSONDecodeError as exc:
            raise SabyError("Saby вернул некорректный ответ") from exc
        if isinstance(result, dict) and result.get("error"):
            error = result["error"]
            raw_code = error.get("code") if isinstance(error, dict) else None
            code = str(raw_code).strip() if raw_code is not None else ""
            suffix = f" (код {code})" if code and code.replace("-", "").replace("_", "").isalnum() and len(code) <= 32 else ""
            raise SabyError(f"Saby отклонил запрос{suffix}")
        return result

    def _safe_http_error_detail(self, exc: urllib.error.HTTPError) -> str:
        """Extract a short vendor explanation without leaking sensitive data."""
        try:
            raw = exc.read(16_384).decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except (AttributeError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return ""

        candidates: list[str] = []
        allowed = {"error", "message", "detail", "description", "errormessage", "reason"}

        def collect(value: Any, depth: int = 0) -> None:
            if depth > 4:
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = re.sub(r"[^a-z]", "", str(key).casefold())
                    if normalized in allowed and isinstance(nested, str):
                        candidates.append(nested)
                    elif isinstance(nested, (dict, list)):
                        collect(nested, depth + 1)
            elif isinstance(value, list):
                for nested in value[:10]:
                    collect(nested, depth + 1)

        collect(payload)
        if not candidates:
            return ""
        message = " ".join(candidates)
        for secret in (
            self.settings.app_client_id,
            self.settings.app_secret,
            self.settings.secret_key,
            self._token,
        ):
            if secret:
                message = message.replace(secret, "[скрыто]")
        message = re.sub(r"https?://\S+", "[ссылка скрыта]", message)
        message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}", "[email скрыт]", message)
        message = re.sub(r"(?<!\d)(?:\+?7|8)[\d ()-]{9,18}\d", "[телефон скрыт]", message)
        message = re.sub(r"\b[A-Za-z0-9_=-]{24,}\b", "[значение скрыто]", message)
        message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
        message = re.sub(r"\s+", " ", message).strip(" .:;,-")
        return message[:360]

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

    def sales_point_enabled(
        self, product: str, point_id: int | None = None
    ) -> bool:
        """Return whether the configured point is enabled for a Saby product."""
        point = point_id or self.settings.point_id
        if not point:
            raise SabyError("Не выбран идентификатор точки продаж Saby")
        result = self.sales_points(product)
        rows = result.get("salesPoints", []) if isinstance(result, dict) else []
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not isinstance(rows, list):
            raise SabyError("Saby вернул список точек в неожиданном формате")
        return any(
            isinstance(row, dict) and str(row.get("id")) == str(point)
            for row in rows
        )

    def delivery_calendar(self, point_id: int | None = None) -> Any:
        """Return delivery availability without creating or changing an order."""
        point = point_id or self.settings.point_id
        if not point:
            raise SabyError("Не выбран идентификатор точки продаж Saby")
        return self.api("/retail/delivery/calendar", {"pointId": point})

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

    def catalog_all(
        self, point_id: int | None = None, price_list_id: int | None = None,
        *, with_balance: bool = False, max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Возвращает выбранный прайс целиком, не теряя вторую и следующие страницы."""
        point = point_id or self.settings.point_id
        price = price_list_id or self.settings.price_list_id
        if not point or not price:
            raise SabyError("Не выбраны точка продаж и прайс-лист Saby")

        items: dict[Any, dict[str, Any]] = {}
        for page in range(max_pages):
            result = self.api("/retail/v2/nomenclature/list", {
                "pointId": point, "priceListId": price, "noStopList": "false",
                "withBalance": str(with_balance).lower(), "page": page, "pageSize": 25,
            })
            rows = result.get("nomenclatures", []) if isinstance(result, dict) else []
            if not isinstance(rows, list):
                raise SabyError("Saby вернул каталог в неожиданном формате")
            for item in rows:
                if isinstance(item, dict):
                    key = item.get("id", item.get("externalId"))
                    items[key if key is not None else (page, len(items))] = item
            has_more = (
                bool((result.get("outcome") or {}).get("hasMore"))
                if isinstance(result, dict) else False
            )
            if not has_more:
                return list(items.values())
        raise SabyError("Каталог Saby содержит слишком много страниц")

    def base_catalog_all(
        self, point_id: int | None = None, *, with_balance: bool = False,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the base catalog without applying a retail price list.

        Saby may expose a sale package through a price list while still naming
        its unit ``г``.  The base catalog is therefore needed by the read-only
        shadow check to infer that package safely.  This method only performs
        GET requests.
        """
        point = point_id or self.settings.point_id
        if not point:
            raise SabyError("Не выбран идентификатор точки продаж Saby")

        items: dict[Any, dict[str, Any]] = {}
        for page in range(max_pages):
            result = self.api("/retail/v2/nomenclature/list", {
                "pointId": point, "noStopList": "false",
                "withBalance": str(with_balance).lower(), "page": page,
                "pageSize": 25,
            })
            rows = result.get("nomenclatures", []) if isinstance(result, dict) else []
            if not isinstance(rows, list):
                raise SabyError("Saby вернул каталог в неожиданном формате")
            for item in rows:
                if isinstance(item, dict):
                    key = item.get("id", item.get("externalId"))
                    items[key if key is not None else (page, len(items))] = item
            has_more = bool((result.get("outcome") or {}).get("hasMore")) if isinstance(result, dict) else False
            if not has_more:
                return list(items.values())
        raise SabyError("Каталог Saby содержит слишком много страниц")

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

    def create_fiscal_sale(self, payload: dict) -> Any:
        """Register one fiscal sale/refund; policy is enforced by the caller."""
        result = self.api("/retail/sale/create", method="POST", payload=payload)
        return unwrap_fiscal_response(result)

    def fiscal_receipt(self, receipt_id: str) -> Any:
        """Read receipt state returned by ``create_fiscal_sale``."""
        value = str(receipt_id or "").strip()
        if not value or len(value) > 120:
            raise SabyError("Некорректный идентификатор чека Saby")
        result = self.api("/retail/pay/list", {"ids[]": value})
        return unwrap_fiscal_response(result)

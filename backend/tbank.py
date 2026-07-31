"""Low-level T-Bank internet acquiring client.

The adapter follows the current official ``/v2`` API documentation:

* token: https://developer.tbank.ru/eacq/intro/developer/token
* Init: https://developer.tbank.ru/eacq/api/init
* GetState: https://developer.tbank.ru/eacq/api/get-state
* refunds/cancellations: https://developer.tbank.ru/eacq/api/cancel

It deliberately has no payment workflow of its own.  Every public operation
performs exactly one requested API call; persistence, retries and order state
transitions belong to the calling application.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


API_ROOT = "https://securepay.tinkoff.ru/v2"
OFFICIAL_PAYMENT_HOSTS = frozenset(
    {
        "pay.tbank.ru",
        "securepayments.tinkoff.ru",
    }
)


class TBankError(RuntimeError):
    """Safe integration error that never includes credentials or payloads."""


@dataclass(frozen=True)
class TBankSettings:
    terminal_key: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    notification_url: str = ""
    success_url: str = ""
    fail_url: str = ""

    @classmethod
    def from_env(cls) -> "TBankSettings":
        return cls(
            terminal_key=os.getenv("TBANK_TERMINAL_KEY", "").strip(),
            password=os.getenv("TBANK_PASSWORD", "").strip(),
            notification_url=os.getenv("TBANK_NOTIFICATION_URL", "").strip(),
            success_url=os.getenv("TBANK_SUCCESS_URL", "").strip(),
            fail_url=os.getenv("TBANK_FAIL_URL", "").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.terminal_key and self.password)

    @property
    def is_demo(self) -> bool:
        """Whether this is an explicitly named T-Bank test terminal."""
        return self.terminal_key.endswith("DEMO")


def _token_value(value: Any) -> str | None:
    """Return the exact textual value used by the official token algorithm."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)) and not isinstance(value, complex):
        return str(value)
    raise TBankError("Неподдерживаемый тип параметра для подписи Т-Банка")


def generate_token(payload: Mapping[str, Any], password: str) -> str:
    """Generate a T-Bank request token from root scalar fields.

    ``Token`` and an accidentally supplied ``Password`` are ignored. Nested
    objects and arrays do not participate in the signature, as required by the
    official API. The real password is added exactly once by this function.
    """
    if not password:
        raise TBankError("Не задан пароль терминала Т-Банка")

    values: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise TBankError("Некорректное имя параметра Т-Банка")
        if key in {"Token", "Password"}:
            continue
        token_value = _token_value(value)
        if token_value is not None:
            values[key] = token_value
    values["Password"] = password
    source = "".join(values[key] for key in sorted(values))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def verify_notification_token(
    payload: Mapping[str, Any], settings: TBankSettings
) -> bool:
    """Verify an HTTP notification using T-Bank's root-scalar algorithm.

    The notification must belong to the configured terminal. Signature and
    terminal comparisons are constant-time and malformed untrusted payloads
    are rejected without including credentials or received values in errors.
    """
    if not settings.configured:
        raise TBankError("Не заданы параметры терминала Т-Банка")
    if not isinstance(payload, Mapping):
        return False

    received_token = payload.get("Token")
    received_terminal = payload.get("TerminalKey")
    if not isinstance(received_token, str) or not received_token:
        return False
    if not isinstance(received_terminal, str):
        return False

    try:
        expected_token = generate_token(payload, settings.password)
    except TBankError:
        return False

    terminal_matches = hmac.compare_digest(
        received_terminal.encode("utf-8"), settings.terminal_key.encode("utf-8")
    )
    token_matches = hmac.compare_digest(
        received_token.encode("utf-8"), expected_token.encode("ascii")
    )
    return terminal_matches and token_matches


def validate_payment_url(value: Any) -> str:
    """Return a safe T-Bank hosted payment URL or raise a sanitized error."""
    error_message = "Т-Банк вернул небезопасную ссылку оплаты"
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise TBankError(error_message)

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise TBankError(error_message) from None

    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or hostname.lower() not in OFFICIAL_PAYMENT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise TBankError(error_message)
    return value


class TBankClient:
    def __init__(
        self,
        settings: TBankSettings | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        *,
        timeout: float = 15.0,
    ):
        self.settings = settings or TBankSettings.from_env()
        self._opener = opener
        self._timeout = timeout

    def configuration(self) -> dict[str, Any]:
        """Return readiness information without terminal credentials."""
        return {
            "configured": self.settings.configured,
            "missing": [
                name
                for name, value in (
                    ("TBANK_TERMINAL_KEY", self.settings.terminal_key),
                    ("TBANK_PASSWORD", self.settings.password),
                )
                if not value
            ],
            "notification_url_configured": bool(self.settings.notification_url),
            "success_url_configured": bool(self.settings.success_url),
            "fail_url_configured": bool(self.settings.fail_url),
        }

    def _post(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.settings.configured:
            raise TBankError("Не заданы параметры терминала Т-Банка")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", method):
            raise TBankError("Некорректный метод Т-Банка")
        if any(key in payload for key in ("TerminalKey", "Token", "Password")):
            raise TBankError("Служебные параметры Т-Банка задаются адаптером")

        body = {"TerminalKey": self.settings.terminal_key, **payload}
        body["Token"] = generate_token(body, self.settings.password)
        request = urllib.request.Request(
            f"{API_ROOT}/{method}",
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TBankError(f"Т-Банк вернул HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TBankError("Т-Банк временно недоступен") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TBankError("Т-Банк вернул некорректный ответ") from exc

        if not isinstance(result, dict):
            raise TBankError("Т-Банк вернул некорректный ответ")
        if result.get("Success") is False:
            raw_code = str(result.get("ErrorCode", "")).strip()
            code = raw_code if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", raw_code) else ""
            suffix = f" (код {code})" if code else ""
            raise TBankError(f"Т-Банк отклонил запрос{suffix}")
        return result

    @staticmethod
    def _required_id(value: str | int, name: str, maximum: int) -> str:
        result = str(value)
        if not result or result != result.strip() or len(result) > maximum:
            raise TBankError(f"Некорректный {name}")
        return result

    @staticmethod
    def _amount(value: int, *, optional: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 9_999_999_999:
            label = "сумма возврата" if optional else "сумма платежа"
            raise TBankError(f"Некорректная {label}")
        return value

    def create_payment(
        self,
        local_order_id: str,
        amount: int,
        *,
        description: str = "",
        customer_key: str | None = None,
        pay_type: str = "O",
        language: str = "ru",
        notification_url: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
        data: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call ``Init`` using the local order id as the stable ``OrderId``.

        The caller must persist the returned ``PaymentId`` and must not create a
        second payment for the same local order after a successful response.
        """
        order_id = self._required_id(local_order_id, "идентификатор заказа", 50)
        if len(description) > 140:
            raise TBankError("Описание платежа длиннее 140 символов")
        if pay_type not in {"O", "T"}:
            raise TBankError("Некорректный тип платежа")
        if language not in {"ru", "en"}:
            raise TBankError("Некорректный язык платежной формы")

        payload: dict[str, Any] = {
            "Amount": self._amount(amount),
            "OrderId": order_id,
            "PayType": pay_type,
            "Language": language,
        }
        if description:
            payload["Description"] = description
        if customer_key is not None:
            payload["CustomerKey"] = self._required_id(customer_key, "идентификатор покупателя", 255)
        for key, explicit, configured in (
            ("NotificationURL", notification_url, self.settings.notification_url),
            ("SuccessURL", success_url, self.settings.success_url),
            ("FailURL", fail_url, self.settings.fail_url),
        ):
            value = configured if explicit is None else explicit
            if value:
                payload[key] = value
        if data is not None:
            payload["DATA"] = dict(data)
        if receipt is not None:
            payload["Receipt"] = dict(receipt)
        result = self._post("Init", payload)
        if "PaymentURL" in result:
            result["PaymentURL"] = validate_payment_url(result["PaymentURL"])
        return result

    # Name used by the official API while keeping a descriptive application API.
    init_payment = create_payment

    def check_order(self, local_order_id: str) -> dict[str, Any]:
        """Return all provider payments registered for the merchant OrderId.

        This read is the safe recovery primitive after an ambiguous ``Init``:
        callers must inspect ``Payments`` instead of blindly repeating Init
        with an OrderId that the provider requires to be unique.
        """
        order_id = self._required_id(
            local_order_id, "идентификатор заказа", 50
        )
        return self._post("CheckOrder", {"OrderId": order_id})

    def get_state(
        self,
        payment_id: str | int,
        *,
        ip: str | None = None,
        get_phone: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "PaymentId": self._required_id(payment_id, "идентификатор платежа", 20),
        }
        if ip:
            payload["IP"] = ip
        if get_phone is not None:
            payload["GetPhone"] = bool(get_phone)
        return self._post("GetState", payload)

    def refund(
        self,
        payment_id: str | int,
        *,
        amount: int | None = None,
        receipt: Mapping[str, Any] | None = None,
        external_request_id: str | None = None,
        qr_member_id: str | None = None,
        ip: str | None = None,
    ) -> dict[str, Any]:
        """Call ``Cancel`` for a full/partial cancellation or refund.

        T-Bank uses the same endpoint for releasing an authorization and for a
        refund of a confirmed payment. ``external_request_id`` is exposed for
        payment methods for which the bank documents request deduplication.
        """
        payload: dict[str, Any] = {
            "PaymentId": self._required_id(payment_id, "идентификатор платежа", 20),
        }
        if amount is not None:
            payload["Amount"] = self._amount(amount, optional=True)
        if receipt is not None:
            payload["Receipt"] = dict(receipt)
        if external_request_id is not None:
            payload["ExternalRequestId"] = self._required_id(
                external_request_id, "идентификатор возврата", 255
            )
        if qr_member_id:
            payload["QrMemberId"] = qr_member_id
        if ip:
            payload["IP"] = ip
        return self._post("Cancel", payload)

    cancel_payment = refund

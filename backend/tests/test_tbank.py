import hashlib
import json
import urllib.error

import pytest

from backend.tbank import (
    TBankClient,
    TBankError,
    TBankSettings,
    generate_token,
    validate_payment_url,
    verify_notification_token,
)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def settings(**changes):
    values = {
        "terminal_key": "MerchantTerminalKey",
        "password": "11111111111111",
        "notification_url": "https://chainya.ru/api/payments/tbank/notification",
        "success_url": "https://chainya.ru/payment/success",
        "fail_url": "https://chainya.ru/payment/fail",
    }
    values.update(changes)
    return TBankSettings(**values)


def test_official_token_vector_and_nested_values_are_excluded():
    payload = {
        "TerminalKey": "MerchantTerminalKey",
        "Amount": 19200,
        "OrderId": "00000",
        "Description": "Подарочная карта на 1000 рублей",
        "DATA": {"Email": "not-signed@example.test"},
        "Receipt": {"Items": [{"Amount": 19200}]},
        "Token": "must-be-ignored",
        "Password": "must-also-be-ignored",
    }
    assert generate_token(payload, "11111111111111") == (
        "72dd466f8ace0a37a1f740ce5fb78101712bc0665d91a8108c7c8a0ccd426db2"
    )


def test_bool_token_value_uses_json_spelling():
    expected = hashlib.sha256("truepasswordterminal".encode()).hexdigest()
    assert generate_token({"Enabled": True, "TerminalKey": "terminal"}, "password") == expected


@pytest.mark.parametrize(
    ("terminal_key", "expected"),
    [
        ("1784764931730DEMO", True),
        ("DEMO", True),
        ("1784764931730demo", False),
        ("1784764931730DEMO ", False),
        ("1784764931730DEMOPROD", False),
        ("", False),
    ],
)
def test_is_demo_requires_exact_demo_suffix(terminal_key, expected):
    assert TBankSettings(terminal_key=terminal_key, password="secret").is_demo is expected


def test_notification_token_matches_official_vector_and_ignores_nested_values():
    payload = {
        "TerminalKey": "1234567890DEMO",
        "OrderId": "000000",
        "Success": True,
        "Status": "AUTHORIZED",
        "PaymentId": "0000000",
        "ErrorCode": "0",
        "Amount": 1111,
        "CardId": "000000",
        "Pan": "200000******0000",
        "ExpDate": "1111",
        "RebillId": "000000",
        "Data": {"untrusted": "nested"},
        "Receipt": {"Items": [{"Amount": 1111}]},
        "Token": "1c0964277d0213349243065a0d5b838b8e90d2d25f740d0f2767836e710e80c8",
    }
    configured = TBankSettings(
        terminal_key="1234567890DEMO",
        password="11111111111",
    )
    assert verify_notification_token(payload, configured) is True


@pytest.mark.parametrize("token", ["", None, 123, "0" * 64, "неверный-токен"])
def test_notification_token_rejects_missing_or_invalid_signature(token):
    payload = {"TerminalKey": "terminal", "Status": "CONFIRMED", "Token": token}
    assert verify_notification_token(payload, settings(terminal_key="terminal")) is False


def test_notification_token_rejects_other_terminal_even_with_valid_signature():
    payload = {"TerminalKey": "attacker-terminal", "Status": "CONFIRMED"}
    payload["Token"] = generate_token(payload, "11111111111111")
    assert verify_notification_token(payload, settings(terminal_key="terminal")) is False


def test_notification_verification_errors_do_not_leak_credentials():
    secret = "must-not-leak"
    with pytest.raises(TBankError) as failed:
        verify_notification_token(
            {"TerminalKey": "received-terminal", "Token": "received-token"},
            TBankSettings(terminal_key=secret, password=""),
        )
    message = str(failed.value)
    assert secret not in message
    assert "received-terminal" not in message
    assert "received-token" not in message


@pytest.mark.parametrize(
    "url",
    [
        "https://pay.tbank.ru/01234567-89ab-cdef",
        "https://securepayments.tinkoff.ru/01234567-89ab-cdef",
        "https://securepayments.tinkoff.ru:443/payment?theme=dark",
    ],
)
def test_validate_payment_url_accepts_only_official_https_payment_host(url):
    assert validate_payment_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://securepayments.tinkoff.ru/payment",
        "https://securepayments.tinkoff.ru.evil.test/payment",
        "https://securepayments.tinkoff.ru@evil.test/payment",
        "https://evil.test/payment",
        "https://securepayments.tinkoff.ru:444/payment",
        "https://securepayments.tinkoff.ru/payment#redirect",
        "javascript:alert(1)",
        " //securepayments.tinkoff.ru/payment",
        "https://securepayments.tinkoff.ru\\@evil.test/payment",
        "",
        None,
    ],
)
def test_validate_payment_url_rejects_untrusted_redirects_without_echoing_them(url):
    with pytest.raises(TBankError) as failed:
        validate_payment_url(url)
    if url:
        assert str(url) not in str(failed.value)


def test_configuration_from_env_does_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("TBANK_TERMINAL_KEY", "terminal-secret-ish")
    monkeypatch.setenv("TBANK_PASSWORD", "very-secret-password")
    monkeypatch.setenv("TBANK_SUCCESS_URL", "https://chainya.ru/ok")
    client = TBankClient(TBankSettings.from_env(), opener=lambda *_args, **_kwargs: None)
    result = client.configuration()
    serialized = json.dumps(result)
    assert result["configured"] is True
    assert result["success_url_configured"] is True
    assert "terminal-secret-ish" not in serialized
    assert "very-secret-password" not in serialized
    assert "very-secret-password" not in repr(client.settings)


def test_create_payment_calls_init_once_and_uses_local_order_id():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        body = json.loads(request.data)
        assert request.method == "POST"
        assert request.full_url == "https://securepay.tinkoff.ru/v2/Init"
        assert body["OrderId"] == "2ADB47E53336"
        assert body["Amount"] == 88_000
        assert body["PayType"] == "O"
        assert body["NotificationURL"].endswith("/notification")
        assert body["DATA"] == {"Phone": "+79991234567"}
        assert body["Receipt"] == {"Items": [{"Amount": 88_000}]}
        assert "Password" not in body
        signed = dict(body)
        token = signed.pop("Token")
        assert token == generate_token(signed, "11111111111111")
        return Response({
            "Success": True,
            "PaymentId": "12345",
            "PaymentURL": "https://securepayments.tinkoff.ru/12345",
        })

    client = TBankClient(settings(), opener=opener)
    result = client.create_payment(
        "2ADB47E53336",
        88_000,
        description="Заказ Чайни 2ADB47E53336",
        data={"Phone": "+79991234567"},
        receipt={"Items": [{"Amount": 88_000}]},
    )
    assert result["PaymentId"] == "12345"
    assert len(calls) == 1


def test_create_payment_rejects_untrusted_payment_url_from_success_response():
    client = TBankClient(
        settings(),
        opener=lambda *_args, **_kwargs: Response({
            "Success": True,
            "PaymentId": "12345",
            "PaymentURL": "https://phishing.example/payment",
        }),
    )
    with pytest.raises(TBankError, match="небезопасную ссылку"):
        client.create_payment("LOCAL-ORDER-1", 10_000)


def test_same_local_order_id_produces_same_bank_order_id_and_signature():
    bodies = []

    def opener(request, timeout):
        bodies.append(json.loads(request.data))
        return Response({"Success": True, "PaymentId": str(len(bodies))})

    client = TBankClient(settings(), opener=opener)
    client.create_payment("LOCAL-ORDER-1", 10_000)
    client.create_payment("LOCAL-ORDER-1", 10_000)
    assert [body["OrderId"] for body in bodies] == ["LOCAL-ORDER-1", "LOCAL-ORDER-1"]
    assert bodies[0]["Token"] == bodies[1]["Token"]


def test_get_state_uses_signed_get_state_request():
    seen = {}

    def opener(request, timeout):
        seen.update(json.loads(request.data))
        assert request.full_url.endswith("/GetState")
        return Response({"Success": True, "Status": "CONFIRMED", "Amount": 10_000})

    result = TBankClient(settings(), opener=opener).get_state("7654321", get_phone=True)
    assert result["Status"] == "CONFIRMED"
    assert seen["PaymentId"] == "7654321"
    assert seen["GetPhone"] is True
    assert seen["Token"]


def test_refund_uses_cancel_and_supports_external_request_id():
    seen = {}

    def opener(request, timeout):
        seen.update(json.loads(request.data))
        assert request.full_url.endswith("/Cancel")
        return Response({"Success": True, "Status": "PARTIAL_REFUNDED"})

    result = TBankClient(settings(), opener=opener).refund(
        "7654321",
        amount=5_000,
        external_request_id="LOCAL-ORDER-1-REFUND-1",
        receipt={"Items": [{"Amount": 5_000}]},
    )
    assert result["Status"] == "PARTIAL_REFUNDED"
    assert seen["Amount"] == 5_000
    assert seen["ExternalRequestId"] == "LOCAL-ORDER-1-REFUND-1"
    assert seen["Receipt"]["Items"][0]["Amount"] == 5_000


def test_unconfigured_client_never_reaches_network():
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    client = TBankClient(TBankSettings(), opener=opener)
    with pytest.raises(TBankError, match="Не заданы параметры"):
        client.get_state("123")
    assert called is False


def test_bank_rejection_and_transport_errors_do_not_leak_secrets():
    secret = "password-that-must-not-leak"

    rejecting = TBankClient(
        settings(password=secret),
        opener=lambda *_args, **_kwargs: Response({
            "Success": False,
            "ErrorCode": "7",
            "Message": f"bad {secret}",
        }),
    )
    with pytest.raises(TBankError) as rejected:
        rejecting.get_state("123")
    assert secret not in str(rejected.value)
    assert "код 7" in str(rejected.value)

    def http_error(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, secret, None, None)

    failing = TBankClient(settings(password=secret), opener=http_error)
    with pytest.raises(TBankError) as failed:
        failing.get_state("123")
    assert str(failed.value) == "Т-Банк вернул HTTP 403"
    assert secret not in str(failed.value)


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.create_payment("", 100),
        lambda client: client.create_payment("order", 0),
        lambda client: client.create_payment("order", 100, pay_type="invalid"),
        lambda client: client.get_state("x" * 21),
        lambda client: client.refund("123", amount=-1),
    ],
)
def test_invalid_inputs_fail_before_network(operation):
    def opener(*_args, **_kwargs):
        raise AssertionError("network must not be called")

    with pytest.raises(TBankError):
        operation(TBankClient(settings(), opener=opener))

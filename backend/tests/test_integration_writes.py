import pytest

from backend.integration_guard import ExternalWriteBlocked
from backend.integration_writes import IntegrationWriter


class FakeClient:
    class Settings:
        is_demo = False

    def __init__(self, *, demo=False):
        self.calls = []
        self.settings = self.Settings()
        self.settings.is_demo = demo

    def create_payment(self, *args, **kwargs):
        self.calls.append(("tbank-create", args, kwargs))

    def refund(self, *args, **kwargs):
        self.calls.append(("tbank-refund", args, kwargs))

    def create_delivery_order(self, *args, **kwargs):
        self.calls.append(("saby-create", args, kwargs))

    def create_order(self, *args, **kwargs):
        self.calls.append(("cdek-create", args, kwargs))


def test_test_mode_performs_zero_calls_for_every_provider_write():
    client = FakeClient()
    writer = IntegrationWriter(test_mode=True, workflow_exposed=True)
    operations = [
        lambda: writer.create_tbank_payment(client, "ORDER", 100, mode="auto"),
        lambda: writer.refund_tbank_payment(client, "PAYMENT", mode="auto"),
        lambda: writer.create_saby_order(client, {"order": 1}, mode="auto"),
        lambda: writer.create_cdek_order(client, {"order": 1}, mode="auto"),
    ]
    for operation in operations:
        with pytest.raises(ExternalWriteBlocked, match="тестовым режимом"):
            operation()
    assert client.calls == []


def test_unpublished_application_workflow_performs_zero_calls():
    client = FakeClient()
    writer = IntegrationWriter(test_mode=False, workflow_exposed=False)
    with pytest.raises(ExternalWriteBlocked, match="ещё не опубликован"):
        writer.create_tbank_payment(client, "ORDER", 100, mode="auto")
    assert client.calls == []


def test_guarded_writer_calls_provider_only_after_every_gate_passes():
    client = FakeClient()
    writer = IntegrationWriter(test_mode=False, workflow_exposed=True)
    writer.create_cdek_order(client, {"number": "ORDER"}, mode="manual", manual_approved=True)
    assert client.calls == [("cdek-create", ({"number": "ORDER"},), {})]


def test_provider_specific_demo_workflow_calls_only_demo_tbank():
    writer = IntegrationWriter(
        test_mode=True, exposed_providers=frozenset({"tbank"})
    )
    demo = FakeClient(demo=True)
    writer.create_tbank_payment(demo, "ORDER", 100, mode="demo")
    assert demo.calls == [("tbank-create", ("ORDER", 100), {})]

    non_demo = FakeClient(demo=False)
    with pytest.raises(ExternalWriteBlocked):
        writer.create_tbank_payment(non_demo, "ORDER", 100, mode="demo")
    assert non_demo.calls == []

"""Application boundary for every future provider mutation.

Routes must depend on :class:`IntegrationWriter`, never invoke low-level write
methods directly. The guard runs immediately before the provider method, so a
misconfigured test deployment still performs zero external write calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet

from .integration_guard import require_external_write


@dataclass(frozen=True)
class IntegrationWriter:
    test_mode: bool
    workflow_exposed: bool = False
    exposed_providers: FrozenSet[str] = frozenset()

    def provider_exposed(self, provider: str) -> bool:
        return self.workflow_exposed or provider in self.exposed_providers

    def _authorize(
        self,
        provider: str,
        mode: str,
        *,
        manual_approved: bool,
        demo_credentials: bool = False,
    ) -> None:
        require_external_write(
            provider,
            mode,
            test_mode=self.test_mode,
            workflow_exposed=self.provider_exposed(provider),
            manual_approved=manual_approved,
            demo_credentials=demo_credentials,
        )

    def create_tbank_payment(
        self, client: Any, local_order_id: str, amount: int, *, mode: str, manual_approved: bool = False,
        **kwargs: Any,
    ) -> Any:
        self._authorize(
            "tbank",
            mode,
            manual_approved=manual_approved,
            demo_credentials=bool(getattr(getattr(client, "settings", None), "is_demo", False)),
        )
        return client.create_payment(local_order_id, amount, **kwargs)

    def refund_tbank_payment(
        self, client: Any, payment_id: str, *, mode: str, manual_approved: bool = False,
        **kwargs: Any,
    ) -> Any:
        self._authorize(
            "tbank",
            mode,
            manual_approved=manual_approved,
            demo_credentials=bool(getattr(getattr(client, "settings", None), "is_demo", False)),
        )
        return client.refund(payment_id, **kwargs)

    def create_saby_order(
        self, client: Any, payload: dict, *, mode: str, manual_approved: bool = False,
    ) -> Any:
        self._authorize("saby", mode, manual_approved=manual_approved)
        return client.create_delivery_order(payload)

    def create_cdek_order(
        self, client: Any, payload: dict, *, mode: str, manual_approved: bool = False,
    ) -> Any:
        self._authorize("cdek", mode, manual_approved=manual_approved)
        return client.create_order(payload)


__all__ = ["IntegrationWriter"]

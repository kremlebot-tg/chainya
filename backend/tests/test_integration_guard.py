import pytest

from backend.integration_guard import ExternalWriteBlocked, require_external_write


@pytest.mark.parametrize("provider", ["tbank", "saby", "cdek"])
@pytest.mark.parametrize("mode", ["off", "shadow", "manual", "auto"])
def test_test_mode_blocks_every_provider_and_rollout_mode(provider, mode):
    with pytest.raises(ExternalWriteBlocked, match="тестовым режимом"):
        require_external_write(
            provider, mode, test_mode=True, workflow_exposed=True, manual_approved=True
        )


def test_unpublished_workflow_is_blocked_even_outside_test_mode():
    with pytest.raises(ExternalWriteBlocked, match="ещё не опубликован"):
        require_external_write("tbank", "auto", test_mode=False, workflow_exposed=False)


def test_manual_mode_requires_per_order_approval():
    with pytest.raises(ExternalWriteBlocked, match="явное подтверждение"):
        require_external_write("saby", "manual", test_mode=False, workflow_exposed=True)
    require_external_write(
        "saby", "manual", test_mode=False, workflow_exposed=True, manual_approved=True
    )


def test_auto_is_allowed_only_after_all_guards_pass():
    require_external_write("cdek", "auto", test_mode=False, workflow_exposed=True)


def test_demo_allows_only_exposed_tbank_demo_in_test_contour():
    require_external_write(
        "tbank", "demo", test_mode=True, workflow_exposed=True, demo_credentials=True
    )
    for changes in (
        {"provider": "cdek"},
        {"test_mode": False},
        {"workflow_exposed": False},
        {"demo_credentials": False},
    ):
        values = {
            "provider": "tbank", "mode": "demo", "test_mode": True,
            "workflow_exposed": True, "demo_credentials": True,
        }
        values.update(changes)
        with pytest.raises(ExternalWriteBlocked):
            require_external_write(**values)


@pytest.mark.parametrize("provider,mode", [("unknown", "auto"), ("cdek", "live")])
def test_unknown_provider_or_mode_fails_closed(provider, mode):
    with pytest.raises(ExternalWriteBlocked):
        require_external_write(provider, mode, test_mode=False, workflow_exposed=True)

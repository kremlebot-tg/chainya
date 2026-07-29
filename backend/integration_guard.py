"""Single safety gate for future external write workflows.

Low-level provider clients deliberately know nothing about the web application's
test mode. Any application workflow that calls a write method must pass this
gate immediately before that call.
"""

from __future__ import annotations


class ExternalWriteBlocked(PermissionError):
    """An external mutation is not explicitly allowed by the rollout policy."""


PROVIDERS = {"tbank", "saby", "cdek"}
MODES = {"off", "shadow", "demo", "manual", "auto"}


def require_external_write(
    provider: str,
    mode: str,
    *,
    test_mode: bool,
    workflow_exposed: bool,
    manual_approved: bool = False,
    demo_credentials: bool = False,
) -> None:
    """Fail closed unless every application-level write condition is satisfied."""

    if provider not in PROVIDERS:
        raise ExternalWriteBlocked("Неизвестный внешний провайдер")
    if mode not in MODES:
        raise ExternalWriteBlocked(f"Некорректный режим интеграции {provider}")
    # DEMO-терминалы Т-Банка используют тот же официальный API endpoint, но
    # физически не умеют списывать реальные деньги. Разрешаем этот единственный
    # внешний вызов в общем тестовом контуре только при всех трёх явных
    # признаках: провайдер T-Bank, опубликованный workflow и ключ ...DEMO.
    if mode == "demo":
        if provider != "tbank":
            raise ExternalWriteBlocked("Режим demo разрешён только для Т-Банка")
        if not test_mode:
            raise ExternalWriteBlocked("DEMO-терминал разрешён только в тестовом контуре")
        if not workflow_exposed:
            raise ExternalWriteBlocked("Рабочий процесс внешней записи ещё не опубликован")
        if not demo_credentials:
            raise ExternalWriteBlocked("Терминал Т-Банка не является DEMO-терминалом")
        return
    if test_mode:
        raise ExternalWriteBlocked("Внешние записи заблокированы тестовым режимом")
    if not workflow_exposed:
        raise ExternalWriteBlocked("Рабочий процесс внешней записи ещё не опубликован")
    if mode in {"off", "shadow"}:
        raise ExternalWriteBlocked(f"Интеграция {provider} работает без внешних записей")
    if mode == "manual" and not manual_approved:
        raise ExternalWriteBlocked(f"Для записи в {provider} требуется явное подтверждение")


__all__ = ["ExternalWriteBlocked", "require_external_write"]

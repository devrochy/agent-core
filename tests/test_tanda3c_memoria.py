"""Tanda 3C — memoria: redacción de PII y retención (ADR-0053)."""

from __future__ import annotations

import pytest


def test_redact_pii_enmascara_email_telefono() -> None:
    from core.memory.vector import redact_pii

    assert redact_pii("mi correo es juan@example.com") == "mi correo es [email]"
    out = redact_pii("llamame al +57 300 123 4567 mañana")
    assert "[teléfono]" in out and "300" not in out
    assert redact_pii("") == ""
    assert redact_pii("sin datos") == "sin datos"


def test_redact_pii_tarjeta() -> None:
    from core.memory.vector import redact_pii

    out = redact_pii("tarjeta 4111 1111 1111 1111 exp")
    assert "[número]" in out or "[teléfono]" in out
    assert "4111" not in out


async def test_purge_memory_exige_criterio() -> None:
    from core.memory.vector import purge_memory

    with pytest.raises(ValueError):
        await purge_memory()  # sin customer ni conversation


def test_memory_retention_noop_si_desactivado() -> None:
    """MEMORY_RETENTION_DAYS=0 (default) no purga nada."""
    import asyncio

    from core.tasks.memory_retention import purge_expired_memory

    result = asyncio.run(purge_expired_memory())
    assert result == {"purged": 0, "tenants": 0}

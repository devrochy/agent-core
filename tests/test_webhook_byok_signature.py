"""El webhook genérico debe validar la firma con el secret del negocio BYOK.

Bug real (2026-09-29): la ruta `POST /webhooks/whatsapp` validaba solo con el App
Secret del framework; con una conexión BYOK Meta firma con la app del negocio y
todas las entregas terminaban en 401 (el mensaje nunca llegaba al agente).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from core.gateway import webhooks


def _payload(phone_number_id: str = "1394240483765458", waba_id: str = "waba-1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": waba_id,
                "changes": [{"value": {"metadata": {"phone_number_id": phone_number_id}}}],
            }
        ],
    }


async def test_secretos_priorizan_el_del_negocio(monkeypatch) -> None:
    tenant_id = uuid.uuid4()

    async def fake_resolve_phone(phone: str):
        assert phone == "1394240483765458"
        return tenant_id

    async def fake_secret(tid: uuid.UUID) -> str:
        from core.shared.tenant import get_current_tenant_id

        assert tid == tenant_id
        assert get_current_tenant_id() == tenant_id  # el token vive bajo RLS de tenant
        return "byok-secret"

    monkeypatch.setattr("core.shared.tenant.resolve_tenant_for_phone", fake_resolve_phone, raising=True)
    monkeypatch.setattr("core.gateway.connections.resolve_effective_app_secret", fake_secret, raising=True)
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(
            whatsapp_app_secret="framework-secret", meta_app_secret="framework-secret-2"
        ),
    )

    encontrado, secretos = await webhooks._tenant_y_secretos(_payload())
    assert encontrado == tenant_id
    assert secretos == ["byok-secret", "framework-secret"]


async def test_sin_negocio_queda_solo_el_del_framework(monkeypatch) -> None:
    async def sin_tenant(_phone: str):
        return None

    async def sin_waba(_waba: str):
        return None

    monkeypatch.setattr("core.shared.tenant.resolve_tenant_for_phone", sin_tenant, raising=True)
    import pytest

    monkeypatch.setattr(
        "core.gateway.webhook_route_tenant._resolve_tenant_for_waba", sin_waba, raising=True
    )
    assert pytest is not None
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret="solo-framework"),
    )

    encontrado, secretos = await webhooks._tenant_y_secretos(_payload(phone_number_id="", waba_id=""))
    assert encontrado is None
    assert secretos == ["solo-framework"]

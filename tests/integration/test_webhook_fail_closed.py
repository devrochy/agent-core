"""HU-ARCH-05: webhook fail-closed sin firma no se procesa.

Escenarios Gherkin:
1. Sin secreto en producción → 401, no se encola y se alerta.
2. Firma inválida → 401.
3. Alta manual exige `app_secret` (schema del tipo de conexión).
4. `WEBHOOK_ALLOW_UNSIGNED=true` (test) permite sin firma, con aviso.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _sign(body: bytes, secret: str) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def _meta_payload(phone_id: str, wa_id: str, message_id: str, body: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "e1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": phone_id},
                    "contacts": [{"wa_id": wa_id}],
                    "messages": [{
                        "from": wa_id, "id": message_id, "timestamp": "1700000000",
                        "type": "text", "text": {"body": body},
                    }],
                },
            }],
        }],
    }


async def _onboard(slug: str, phone: str, *, app_secret: str = "") -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"FailClosed {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        whatsapp={
            "label": slug,
            "phone_number_id": phone,
            "app_secret": app_secret,
            "access_token": "",
            "verify_token": "tok",
        },
    )
    return tid


async def test_sin_secreto_fail_closed_en_produccion(app_client, monkeypatch) -> None:
    """Sin app_secret y sin el flag de test, el POST se rechaza con 401."""
    slug = f"fc-{uuid.uuid4().hex[:6]}"
    await _onboard(slug, f"5553{uuid.uuid4().hex[:6]}", app_secret="")

    # El `.env` del entorno puede tener un secret global: lo vaciamos para
    # simular "conexión de producción sin app_secret resoluble".
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_app_secret", "", raising=False)
    monkeypatch.setattr(settings, "meta_app_secret", "", raising=False)
    monkeypatch.setattr(settings, "webhook_allow_unsigned", False, raising=False)

    body = json.dumps(_meta_payload("cualquiera", "wa-fc", f"wamid.fc.{uuid.uuid4().hex[:6]}", "hola")).encode()
    r = await app_client.post(f"/webhooks/whatsapp/{slug}", content=body, headers={"Content-Type": "application/json"})

    assert r.status_code == 401, r.text


async def test_firma_invalida_401(app_client) -> None:
    """Una firma que no coincide devuelve 401."""
    slug = f"fc-{uuid.uuid4().hex[:6]}"
    secret = "secreto-propio-del-tenant"
    await _onboard(slug, f"5554{uuid.uuid4().hex[:6]}", app_secret=secret)

    body = json.dumps(_meta_payload("cualquiera", "wa-fc", f"wamid.fc.{uuid.uuid4().hex[:6]}", "hola")).encode()
    headers = {"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"}
    r = await app_client.post(f"/webhooks/whatsapp/{slug}", content=body, headers=headers)

    assert r.status_code == 401, r.text


async def test_firma_valida_procesa(app_client) -> None:
    """Con la firma correcta del app_secret del tenant, se procesa."""
    slug = f"fc-{uuid.uuid4().hex[:6]}"
    secret = "secreto-propio-del-tenant"
    await _onboard(slug, f"5555{uuid.uuid4().hex[:6]}", app_secret=secret)

    body = json.dumps(_meta_payload("cualquiera", "wa-fc", f"wamid.fc.{uuid.uuid4().hex[:6]}", "hola")).encode()
    r = await app_client.post(f"/webhooks/whatsapp/{slug}", content=body, headers=_sign(body, secret))

    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"


async def test_allow_unsigned_permite_en_test(app_client, monkeypatch) -> None:
    """El flag `WEBHOOK_ALLOW_UNSIGNED` permite sin firma (solo fuera de producción)."""
    slug = f"fc-{uuid.uuid4().hex[:6]}"
    await _onboard(slug, f"5556{uuid.uuid4().hex[:6]}", app_secret="")

    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_app_secret", "", raising=False)
    monkeypatch.setattr(settings, "meta_app_secret", "", raising=False)
    monkeypatch.setattr(settings, "webhook_allow_unsigned", True, raising=False)
    monkeypatch.setattr(settings, "app_env", "development", raising=False)

    body = json.dumps(_meta_payload("cualquiera", "wa-fc", f"wamid.fc.{uuid.uuid4().hex[:6]}", "hola")).encode()
    r = await app_client.post(f"/webhooks/whatsapp/{slug}", content=body, headers={"Content-Type": "application/json"})

    assert r.status_code == 200, r.text


async def test_alta_manual_exige_app_secret() -> None:
    """El schema de la conexión manual de WhatsApp exige `app_secret`."""
    from core.gateway.connection_types import get_connection_type

    ctype = get_connection_type("whatsapp_meta")
    assert ctype is not None
    assert "app_secret" in (ctype.credentials_schema.get("required") or [])

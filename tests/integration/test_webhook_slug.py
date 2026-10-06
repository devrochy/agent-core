"""Webhook por tenant: `/webhooks/whatsapp/<slug>` (cada app su propio webhook).

Genérico: se crea un tenant desechable, se registra su webhook (handshake GET) y
se verifica que los mensajes POST se procesan con el tenant resuelto por slug.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _sign(body: bytes, secret: str) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


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


async def _onboard(slug: str, phone: str, verify_token: str) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Webhook {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        whatsapp={
            "label": slug,
            "phone_number_id": phone,
            "app_secret": "",
            "access_token": "",
            "verify_token": verify_token,
        },
    )
    return tid


async def test_handshake_por_slug(app_client, recorded) -> None:
    """GET /webhooks/whatsapp/<slug> devuelve el challenge si el verify_token coincide."""
    slug = f"wh-{uuid.uuid4().hex[:6]}"
    token = f"miToken{slug}"
    await _onboard(slug, f"5551{uuid.uuid4().hex[:6]}", token)

    # token correcto → 200 + challenge
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "CHAL123"},
    )
    assert r.status_code == 200 and r.text == "CHAL123"

    # token incorrecto → 403
    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "malo", "hub.challenge": "CHAL123"},
    )
    assert r2.status_code == 403

    # slug inexistente → 403
    r3 = await app_client.get(
        "/webhooks/whatsapp/no-existe",
        params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "CHAL123"},
    )
    assert r3.status_code == 403


async def test_post_por_slug_procesa(app_client, recorded) -> None:
    """POST /webhooks/whatsapp/<slug> encola y el consumer responde con el tenant correcto."""
    from sqlalchemy import select as sa_select

    from core.orchestrator.consumer import consume_loop

    slug = f"wh-{uuid.uuid4().hex[:6]}"
    tid = await _onboard(slug, f"5552{uuid.uuid4().hex[:6]}", "tok")
    # modo inmediato (sin debounce de 15s) para que el turno se dispare ya.
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            row = (
                await session.execute(sa_select(AppSettings).where(AppSettings.tenant_id == tid))
            ).scalar_one_or_none()
            behavior = dict(row.behavior_config or {})
            behavior["debounce_mode"] = "inmediato"
            row.behavior_config = behavior
            await session.commit()

    recorded.clear()

    # Firmar como Meta (HMAC del App Secret efectivo del tenant) -- si no, el
    # test depende de que `WHATSAPP_APP_SECRET` esté vacío en el entorno.
    from core.config import get_settings

    body = json.dumps(
        _meta_payload("cualquiera", "wa-slug", f"wamid.slug.{uuid.uuid4().hex[:6]}", "hola slug")
    ).encode()
    secret = get_settings().whatsapp_app_secret or get_settings().meta_app_secret
    r = await app_client.post(
        f"/webhooks/whatsapp/{slug}",
        content=body,
        headers={**_sign(body, secret), "Content-Type": "application/json"} if secret else {},
    )
    assert r.json().get("status") == "ok" and r.json().get("enqueued") == 1

    consumer = asyncio.create_task(consume_loop())
    for _ in range(40):
        if recorded:
            break
        await asyncio.sleep(0.2)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert recorded, "el webhook por slug debe procesar el mensaje del tenant"
    # La conversación queda asociada al tenant (no hay fuga).
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            from core.db.models import Conversation

            n = (
                await session.execute(select(__import__("sqlalchemy").func.count()).select_from(Conversation))
            ).scalar()
    assert n == 1, "la conversación se crea bajo el tenant del slug"


async def test_handshake_por_slug_usa_verify_token_byok_sin_channel_connection(app_client, monkeypatch) -> None:
    """Si el tenant no tiene `ChannelConnection` (caso BYOK vía
    `MetaConnection`) pero sí guardó un `verify_token` propio en el
    wizard BYOK, el handshake lo prioriza antes de caer al `.env` global."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = f"wh-byok-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"Webhook BYOK {slug}", slug=slug, persona={"tone": "cercano"})

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id="app-byok-slug",
                status="connected",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id

        await meta_byok.meta_token_store.put_token(
            connection_id, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, "app-byok-slug", "mi-token-byok", tenant_id=tid,
        )

    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "mi-token-byok", "hub.challenge": "CHAL-BYOK"},
    )
    assert r.status_code == 200 and r.text == "CHAL-BYOK"

    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "otro-token", "hub.challenge": "CHAL-BYOK"},
    )
    assert r2.status_code == 403


async def test_handshake_por_slug_prioriza_byok_sobre_token_global(app_client, monkeypatch) -> None:
    """Regresión: con `WHATSAPP_VERIFY_TOKEN` global NO vacío, el token BYOK del
    tenant debe seguir ganando. Antes `effective_credentials()` (que cae al
    `.env`) tapaba la rama BYOK y el self-check del wizard respondía 403."""
    from core.config import get_settings
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    # El token global del .env es justo la condición que disparaba el bug.
    monkeypatch.setattr(get_settings(), "whatsapp_verify_token", "token-global-env")

    slug = f"wh-byok-global-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(
        name=f"Webhook BYOK global {slug}", slug=slug, persona={"tone": "cercano"}
    )

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id="app-byok-global",
                # "pending" es el estado real durante el self-check del wizard BYOK.
                status="pending",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id

        await meta_byok.meta_token_store.put_token(
            connection_id, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, "app-byok-global", "mi-token-byok",
            tenant_id=tid,
        )

    # El token BYOK gana sobre el global del .env.
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "mi-token-byok", "hub.challenge": "CHAL-BYOK"},
    )
    assert r.status_code == 200 and r.text == "CHAL-BYOK"

    # El token global del .env NO debe aceptarse para este tenant.
    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-global-env", "hub.challenge": "CHAL-BYOK"},
    )
    assert r2.status_code == 403


async def test_handshake_por_slug_rechaza_token_de_otro_tenant(app_client) -> None:
    """Aislamiento: el token BYOK de un tenant NO debe validar el slug de otro."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug_a = f"whct-a-{uuid.uuid4().hex[:6]}"
    slug_b = f"whct-b-{uuid.uuid4().hex[:6]}"
    tid_a, _ = await onboard_tenant(name=f"A {slug_a}", slug=slug_a, persona={"tone": "cercano"})
    tid_b, _ = await onboard_tenant(name=f"B {slug_b}", slug=slug_b, persona={"tone": "cercano"})

    async def _seed(tid, app_id: str, token: str) -> None:
        async with tenant_scope(tid):
            async with get_session() as session:
                connection = MetaConnection(
                    tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_id,
                    status="connected",
                )
                session.add(connection)
                await session.flush()
                connection_id = connection.id
            await meta_byok.meta_token_store.put_token(
                connection_id, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, app_id, token, tenant_id=tid
            )

    await _seed(tid_a, "app-ct-a", "token-ct-a")
    await _seed(tid_b, "app-ct-b", "token-ct-b")

    # Token de A contra el slug de B -> 403 (no hay fuga entre tenants).
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug_b,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-ct-a", "hub.challenge": "X"},
    )
    assert r.status_code == 403

    # Cada tenant con su propio token -> 200.
    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug_b,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-ct-b", "hub.challenge": "X"},
    )
    assert r2.status_code == 200 and r2.text == "X"


async def test_handshake_por_slug_precedencia_manual_sobre_byok(app_client) -> None:
    """Contrato de precedencia: conexión manual (ChannelConnection) > BYOK > .env."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.gateway.connections import save_connection
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = f"whprec-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"Prec {slug}", slug=slug, persona={"tone": "cercano"})

    async with tenant_scope(tid):
        # Conexión manual primaria, con su propio verify_token.
        await save_connection(
            label=slug,
            credentials={"phone_number_id": f"55{uuid.uuid4().hex[:8]}", "verify_token": "token-manual"},
        )
        # Y ADEMÁS una conexión BYOK con otro token.
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id="app-prec",
                status="connected",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
        await meta_byok.meta_token_store.put_token(
            connection_id, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, "app-prec", "token-byok", tenant_id=tid
        )

    # Gana el token de la conexión manual.
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-manual", "hub.challenge": "M"},
    )
    assert r.status_code == 200 and r.text == "M"

    # El token BYOK queda tapado por el manual.
    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-byok", "hub.challenge": "M"},
    )
    assert r2.status_code == 403


async def test_tone_invalido_rechazado() -> None:
    """El onboarding NO persiste un spec fuera del contrato (evita el 500 en /admin/spec)."""
    from core.onboarding import onboard_tenant

    with pytest.raises(ValueError):
        await onboard_tenant(
            name="Tone malo",
            slug=f"tone-{uuid.uuid4().hex[:6]}",
            persona={"tone": "calido"},
        )


async def test_post_por_slug_valida_firma_con_app_secret_byok(app_client) -> None:
    """Regresión: la firma HMAC del POST se valida con el App Secret del BYOK.

    Antes se usaba `effective_credentials()` (que cae al `.env`), así que con
    `WHATSAPP_APP_SECRET` global seteado toda entrega de Meta daba 401 y el
    agente nunca respondía."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = f"whsig-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"Sig {slug}", slug=slug, persona={"tone": "cercano"})

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id="app-sig",
                status="connected",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
        await meta_token_store.put_token(
            connection_id, "app_access", "app-sig", "byok-secret", tenant_id=tid
        )

    body = json.dumps(
        _meta_payload("phone-sig", "wa-sig", f"wamid.sig.{uuid.uuid4().hex[:6]}", "hola")
    ).encode()

    # Firma con el App Secret del BYOK -> aceptada (no 401).
    r = await app_client.post(
        "/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "byok-secret")
    )
    assert r.status_code != 401, r.text

    # Firma con un secreto que NO es el del tenant -> 401.
    r2 = await app_client.post(
        "/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-equivocado")
    )
    assert r2.status_code == 401


async def test_post_por_slug_precedencia_app_secret_manual_sobre_byok(app_client) -> None:
    """Contrato: conexión manual (ChannelConnection) > BYOK > .env, también
    para el App Secret con el que se valida la firma."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.connections import save_connection
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = f"whsigp-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"SigP {slug}", slug=slug, persona={"tone": "cercano"})

    async with tenant_scope(tid):
        await save_connection(
            label=slug,
            credentials={
                "phone_number_id": f"55{uuid.uuid4().hex[:8]}",
                "app_secret": "manual-secret",
                "verify_token": "vt",
            },
        )
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id="app-sigp",
                status="connected",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
        await meta_token_store.put_token(
            connection_id, "app_access", "app-sigp", "byok-secret", tenant_id=tid
        )

    body = json.dumps(
        _meta_payload("phone-sigp", "wa-sigp", f"wamid.sigp.{uuid.uuid4().hex[:6]}", "hola")
    ).encode()

    # Gana el secreto de la conexión manual.
    r = await app_client.post(
        "/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "manual-secret")
    )
    assert r.status_code != 401, r.text

    # El secreto BYOK queda tapado por el manual.
    r2 = await app_client.post(
        "/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "byok-secret")
    )
    assert r2.status_code == 401

async def test_post_por_slug_rechaza_firma_de_otro_tenant(app_client) -> None:
    """Aislamiento: el App Secret de un tenant NO valida la firma del webhook
    de otro tenant."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug_a = f"whsigct-a-{uuid.uuid4().hex[:6]}"
    slug_b = f"whsigct-b-{uuid.uuid4().hex[:6]}"
    tid_a, _ = await onboard_tenant(name=f"A {slug_a}", slug=slug_a, persona={"tone": "cercano"})
    tid_b, _ = await onboard_tenant(name=f"B {slug_b}", slug=slug_b, persona={"tone": "cercano"})

    async def _seed(tid, app_id: str, secret: str) -> None:
        async with tenant_scope(tid):
            async with get_session() as session:
                connection = MetaConnection(
                    tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_id,
                    status="connected",
                )
                session.add(connection)
                await session.flush()
                connection_id = connection.id
            await meta_token_store.put_token(
                connection_id, "app_access", app_id, secret, tenant_id=tid
            )

    await _seed(tid_a, "app-sigct-a", "secreto-a")
    await _seed(tid_b, "app-sigct-b", "secreto-b")

    body = json.dumps(
        _meta_payload("phone-ct", "wa-ct", f"wamid.ct.{uuid.uuid4().hex[:6]}", "hola")
    ).encode()

    # Secreto de A contra el slug de B -> 401.
    r = await app_client.post(
        "/webhooks/whatsapp/" + slug_b, content=body, headers=_sign(body, "secreto-a")
    )
    assert r.status_code == 401

    # El secreto propio de B -> aceptado.
    r2 = await app_client.post(
        "/webhooks/whatsapp/" + slug_b, content=body, headers=_sign(body, "secreto-b")
    )
    assert r2.status_code != 401, r2.text


# ------------------------------------------------------------------ ADR-0101: BYOK degradado
async def _tenant_con_byok(
    prefix: str, *, status: str, app_id: str, app_secret: str | None = None, verify_token: str | None = None,
    connected_at=None,
) -> tuple[uuid.UUID, str]:
    from core.onboarding import onboard_tenant

    slug = f"{prefix}-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"BYOK {slug}", slug=slug, persona={"tone": "cercano"})
    await _agregar_byok(tid, status=status, app_id=app_id, app_secret=app_secret, verify_token=verify_token,
                        connected_at=connected_at)
    return tid, slug


async def _agregar_byok(
    tid: uuid.UUID, *, status: str, app_id: str, app_secret: str | None = None, verify_token: str | None = None,
    connected_at=None, origin: str = "byok",
) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin=origin, meta_app_id=app_id,
                status=status, connected_at=connected_at,
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
        if app_secret:
            await meta_byok.meta_token_store.put_token(
                connection_id, meta_byok.APP_SECRET_KIND, app_id, app_secret, tenant_id=tid
            )
        if verify_token:
            await meta_byok.meta_token_store.put_token(
                connection_id, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, app_id, verify_token, tenant_id=tid
            )


def _cuerpo(prefix: str) -> bytes:
    return json.dumps(
        _meta_payload(f"phone-{prefix}", f"wa-{prefix}", f"wamid.{prefix}.{uuid.uuid4().hex[:6]}", "hola")
    ).encode()


@pytest.mark.parametrize("status", ["degraded", "error"])
async def test_post_por_slug_acepta_la_firma_de_un_byok_degradado(app_client, status) -> None:
    """Un check de salud fallido no le quita el secreto al negocio: antes, todo su inbound
    daba 401 y Meta lo reintentaba en vano (ADR-0101)."""
    _, slug = await _tenant_con_byok("whdeg", status=status, app_id="app-deg", app_secret="secreto-deg")
    body = _cuerpo("deg")

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-deg"))

    assert r.status_code == 200, r.text


async def test_post_por_slug_no_acepta_la_firma_de_un_byok_revocado(app_client) -> None:
    """Desactivada a mano o borrada: cortar es intencional, el secreto ya no vale."""
    _, slug = await _tenant_con_byok("whrev", status="revoked", app_id="app-rev", app_secret="secreto-rev")
    body = _cuerpo("rev")

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-rev"))

    assert r.status_code == 401


async def test_handshake_por_slug_con_byok_degradado(app_client) -> None:
    """Meta re-verifica la suscripción con este handshake: con la conexión `degraded`, el
    `verify_token` BYOK caía al del `.env` y la verificación fallaba."""
    _, slug = await _tenant_con_byok("whdegv", status="degraded", app_id="app-degv", verify_token="token-degv")

    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-degv", "hub.challenge": "CHAL-DEG"},
    )

    assert r.status_code == 200 and r.text == "CHAL-DEG"


async def test_una_conexion_vieja_sana_le_gana_a_una_nueva_en_error(app_client) -> None:
    """Con dos conexiones BYOK, el secreto sale de la sana aunque la de `error` sea más nueva."""
    from datetime import UTC, datetime, timedelta

    ahora = datetime.now(UTC)
    tid, slug = await _tenant_con_byok(
        "whdos", status="connected", app_id="app-sana", app_secret="secreto-sano",
        connected_at=ahora - timedelta(days=30),
    )
    await _agregar_byok(tid, status="error", app_id="app-rota", app_secret="secreto-roto", connected_at=ahora)
    body = _cuerpo("dos")

    sana = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-sano"))
    rota = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-roto"))

    assert sana.status_code == 200, sana.text
    assert rota.status_code == 401


async def test_handshake_por_slug_con_byok_revocado_da_403(app_client) -> None:
    _, slug = await _tenant_con_byok("whrevv", status="revoked", app_id="app-revv", verify_token="token-revv")

    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-revv", "hub.challenge": "CHAL-REV"},
    )

    assert r.status_code == 403


async def test_una_conexion_vieja_conectada_le_gana_a_una_nueva_degradada(app_client) -> None:
    """`degraded` es solo un respaldo: si hay una `connected`, se usa esa aunque sea más vieja."""
    from datetime import UTC, datetime, timedelta

    ahora = datetime.now(UTC)
    tid, slug = await _tenant_con_byok(
        "whdeg2", status="connected", app_id="app-ok", app_secret="secreto-ok", connected_at=ahora - timedelta(days=30),
    )
    await _agregar_byok(tid, status="degraded", app_id="app-degr", app_secret="secreto-degr", connected_at=ahora)
    body = _cuerpo("deg2")

    ok = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-ok"))
    degr = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-degr"))

    assert ok.status_code == 200, ok.text
    assert degr.status_code == 401


async def test_una_conexion_de_embedded_signup_no_le_quita_el_secreto_a_la_byok(app_client) -> None:
    """Una conexión de ES más nueva no guarda secreto propio: antes podía ganar el orden y dejar
    al negocio sin el secreto de su app BYOK (todo su inbound daba 401)."""
    from datetime import UTC, datetime, timedelta

    ahora = datetime.now(UTC)
    tid, slug = await _tenant_con_byok(
        "whes", status="connected", app_id="app-byok-es", app_secret="secreto-byok-es",
        connected_at=ahora - timedelta(days=30),
    )
    await _agregar_byok(tid, status="degraded", app_id="app-es", origin="embedded_signup", connected_at=ahora)
    body = _cuerpo("es")

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-byok-es"))

    assert r.status_code == 200, r.text

"""Webhook por tenant: `/webhooks/whatsapp/<slug>` (cada app su propio webhook).

Genérico: se crea un tenant desechable, se registra su webhook (handshake GET) y
se verifica que los mensajes POST se procesan con el tenant resuelto por slug.

Desde HU-CONN-05 (ADR-0125) la firma de cada número se verifica con el secreto de SU
conexión, y un número que no es de ninguna conexión del negocio no se procesa: los
payloads de estos tests usan números registrados.
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
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
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
    from core.alta_negocio import onboard_tenant

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
    phone = f"5552{uuid.uuid4().hex[:6]}"
    tid = await _onboard(slug, phone, "tok")
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
        _meta_payload(phone, "wa-slug", f"wamid.slug.{uuid.uuid4().hex[:6]}", "hola slug")
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
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
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
    from core.alta_negocio import onboard_tenant
    from core.config import get_settings
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
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
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
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


async def test_handshake_por_slug_acepta_el_token_de_cada_conexion(app_client) -> None:
    """HU-CONN-05: cada conexión que apunta a la URL del negocio re-verifica su suscripción
    con su propio token (antes la manual tapaba a la BYOK y su handshake daba 403)."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.gateway.connections import save_connection
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

    # El token de la conexión manual.
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-manual", "hub.challenge": "M"},
    )
    assert r.status_code == 200 and r.text == "M"

    # Y el de la conexión BYOK.
    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-byok", "hub.challenge": "M"},
    )
    assert r2.status_code == 200 and r2.text == "M"

    # Uno que no registró ninguna conexión, no.
    r3 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-de-nadie", "hub.challenge": "M"},
    )
    assert r3.status_code == 403


async def test_tone_invalido_rechazado() -> None:
    """El onboarding NO persiste un spec fuera del contrato (evita el 500 en /admin/spec)."""
    from core.alta_negocio import onboard_tenant

    with pytest.raises(ValueError):
        await onboard_tenant(
            name="Tone malo",
            slug=f"tone-{uuid.uuid4().hex[:6]}",
            persona={"tone": "calido"},
        )


# ------------------------------------------------------------------ firma por conexión (ADR-0125)
async def _tenant_con_byok(
    prefix: str, *, status: str, app_id: str, app_secret: str | None = None, verify_token: str | None = None,
    connected_at=None, origin: str = "byok", phone: str | None = None,
) -> tuple[uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant

    slug = f"{prefix}-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"BYOK {slug}", slug=slug, persona={"tone": "cercano"})
    await _agregar_byok(tid, status=status, app_id=app_id, app_secret=app_secret, verify_token=verify_token,
                        connected_at=connected_at, origin=origin, phone=phone)
    return tid, slug


async def _agregar_byok(
    tid: uuid.UUID, *, status: str, app_id: str, app_secret: str | None = None, verify_token: str | None = None,
    connected_at=None, origin: str = "byok", phone: str | None = None,
) -> uuid.UUID:
    """Conexión de Meta del negocio. Con `phone`, también su WABA y ese número: la firma de
    sus mensajes se verifica con el `app_secret` de esta conexión."""
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
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
            if phone:
                waba = WabaAccount(tenant_id=tid, connection_id=connection_id, waba_id=f"waba-{uuid.uuid4().hex[:10]}")
                session.add(waba)
                await session.flush()
                session.add(
                    PhoneNumber(
                        tenant_id=tid, connection_id=connection_id, waba_account_id=waba.id, phone_number_id=phone
                    )
                )
        if app_secret:
            await meta_byok.meta_token_store.put_token(
                connection_id, meta_byok.APP_SECRET_KIND, app_id, app_secret, tenant_id=tid
            )
        if verify_token:
            await meta_byok.meta_token_store.put_token(
                connection_id, meta_byok.WEBHOOK_VERIFY_TOKEN_KIND, app_id, verify_token, tenant_id=tid
            )
    return connection_id


def _phone(prefix: str) -> str:
    return f"phone-{prefix}-{uuid.uuid4().hex[:8]}"


def _cuerpo(phone: str) -> bytes:
    return json.dumps(
        _meta_payload(phone, f"wa-{phone}", f"wamid.{phone}.{uuid.uuid4().hex[:6]}", "hola")
    ).encode()


async def test_post_por_slug_valida_firma_con_app_secret_byok(app_client) -> None:
    """Regresión: la firma HMAC del POST se valida con el App Secret del BYOK.

    Antes se usaba `effective_credentials()` (que cae al `.env`), así que con
    `WHATSAPP_APP_SECRET` global seteado toda entrega de Meta daba 401 y el
    agente nunca respondía."""
    phone = _phone("sig")
    _, slug = await _tenant_con_byok("whsig", status="connected", app_id="app-sig", app_secret="byok-secret", phone=phone)
    body = _cuerpo(phone)

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "byok-secret"))
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"

    r2 = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-equivocado"))
    assert r2.status_code == 401


async def test_post_por_slug_un_numero_sin_conexion_no_se_procesa(app_client) -> None:
    """Antes, el endpoint por slug le atribuía a su negocio cualquier número firmado con su
    secreto. Ahora un número que no es de ninguna conexión del negocio no tiene secreto con
    el que verificarse: 401, nunca se procesa (fail-closed, HU-CONN-05)."""
    _, slug = await _tenant_con_byok(
        "whsin", status="connected", app_id="app-sin", app_secret="secreto-sin", phone=_phone("sin")
    )
    body = _cuerpo(_phone("desconocido"))

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-sin"))

    assert r.status_code == 401


async def test_post_por_slug_cada_numero_con_el_secreto_de_su_conexion_manual_o_byok(app_client) -> None:
    """Un negocio con una conexión manual (`ChannelConnection`) y otra BYOK: cada número se
    verifica con el secreto de la suya, y el de una no sirve para el número de la otra
    (antes la manual tapaba a la BYOK y todos sus mensajes daban 401)."""
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    phone_byok, phone_manual = _phone("sigp-byok"), f"55{uuid.uuid4().hex[:8]}"
    tid, slug = await _tenant_con_byok(
        "whsigp", status="connected", app_id="app-sigp", app_secret="byok-secret", phone=phone_byok
    )
    async with tenant_scope(tid):
        await save_connection(
            label=slug,
            credentials={"phone_number_id": phone_manual, "app_secret": "manual-secret", "verify_token": "vt"},
        )
    body_byok, body_manual = _cuerpo(phone_byok), _cuerpo(phone_manual)

    ok_byok = await app_client.post("/webhooks/whatsapp/" + slug, content=body_byok, headers=_sign(body_byok, "byok-secret"))
    ok_manual = await app_client.post(
        "/webhooks/whatsapp/" + slug, content=body_manual, headers=_sign(body_manual, "manual-secret")
    )
    cruzado = await app_client.post(
        "/webhooks/whatsapp/" + slug, content=body_byok, headers=_sign(body_byok, "manual-secret")
    )

    assert ok_byok.status_code == 200 and ok_byok.json().get("status") == "ok", ok_byok.text
    assert ok_manual.status_code == 200 and ok_manual.json().get("status") == "ok", ok_manual.text
    assert cruzado.status_code == 401


async def test_post_por_slug_rechaza_firma_de_otro_tenant(app_client) -> None:
    """Aislamiento: el App Secret de un tenant NO valida la firma del webhook
    de otro tenant."""
    phone_b = _phone("ct-b")
    await _tenant_con_byok("whsigct-a", status="connected", app_id="app-sigct-a", app_secret="secreto-a",
                           phone=_phone("ct-a"))
    _, slug_b = await _tenant_con_byok("whsigct-b", status="connected", app_id="app-sigct-b", app_secret="secreto-b",
                                       phone=phone_b)
    body = _cuerpo(phone_b)

    r = await app_client.post("/webhooks/whatsapp/" + slug_b, content=body, headers=_sign(body, "secreto-a"))
    assert r.status_code == 401

    r2 = await app_client.post("/webhooks/whatsapp/" + slug_b, content=body, headers=_sign(body, "secreto-b"))
    assert r2.status_code == 200, r2.text


@pytest.mark.parametrize("status", ["degraded", "error"])
async def test_post_por_slug_acepta_la_firma_de_un_byok_degradado(app_client, status) -> None:
    """Un check de salud fallido no le quita el secreto a la conexión: antes, todo su inbound
    daba 401 y Meta lo reintentaba en vano (ADR-0101)."""
    phone = _phone("deg")
    _, slug = await _tenant_con_byok("whdeg", status=status, app_id="app-deg", app_secret="secreto-deg", phone=phone)
    body = _cuerpo(phone)

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-deg"))

    assert r.status_code == 200, r.text


async def test_post_por_slug_acepta_la_firma_de_un_byok_pausado(app_client) -> None:
    """Pausada a mano (ADR-0122): la entrada se verifica con su secreto y se guarda sin IA. Antes
    daba 401: el mensaje se perdía y Meta lo reintentaba."""
    phone = _phone("rev")
    _, slug = await _tenant_con_byok("whrev", status="revoked", app_id="app-rev", app_secret="secreto-rev", phone=phone)
    body = _cuerpo(phone)

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-rev"))

    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("estado_1", "estado_2", "origen_1"),
    [
        ("connected", "revoked", "embedded_signup"),  # una BYOK tomada por ES y otra pausada
        ("connected", "error", "byok"),
        ("connected", "degraded", "byok"),
        ("degraded", "error", "byok"),
    ],
)
async def test_dos_conexiones_cada_una_verifica_sus_numeros(app_client, estado_1, estado_2, origen_1) -> None:
    """HU-CONN-05: con dos apps en el negocio ya no "gana" una (ADR-0101 elegía la más sana y
    la otra daba 401). Cada número se verifica con el secreto de su conexión, sea cual sea su
    estado, y el secreto de una no sirve para los números de la otra."""
    from datetime import UTC, datetime, timedelta

    ahora = datetime.now(UTC)
    phone_1, phone_2 = _phone("uno"), _phone("dos")
    tid, slug = await _tenant_con_byok(
        "whdos", status=estado_1, app_id="app-uno", app_secret="secreto-uno",
        connected_at=ahora - timedelta(days=30), origin=origen_1, phone=phone_1,
    )
    await _agregar_byok(tid, status=estado_2, app_id="app-dos", app_secret="secreto-dos", connected_at=ahora,
                        phone=phone_2)
    body_1, body_2 = _cuerpo(phone_1), _cuerpo(phone_2)

    uno = await app_client.post("/webhooks/whatsapp/" + slug, content=body_1, headers=_sign(body_1, "secreto-uno"))
    dos = await app_client.post("/webhooks/whatsapp/" + slug, content=body_2, headers=_sign(body_2, "secreto-dos"))
    cruzado = await app_client.post("/webhooks/whatsapp/" + slug, content=body_1, headers=_sign(body_1, "secreto-dos"))

    assert uno.status_code == 200, uno.text
    assert dos.status_code == 200, dos.text
    assert cruzado.status_code == 401


async def test_handshake_por_slug_con_byok_degradado(app_client) -> None:
    """Meta re-verifica la suscripción con este handshake: con la conexión `degraded`, el
    `verify_token` BYOK caía al del `.env` y la verificación fallaba."""
    _, slug = await _tenant_con_byok("whdegv", status="degraded", app_id="app-degv", verify_token="token-degv")

    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-degv", "hub.challenge": "CHAL-DEG"},
    )

    assert r.status_code == 200 and r.text == "CHAL-DEG"


async def test_handshake_por_slug_con_byok_pausado_responde(app_client) -> None:
    """Una pausada sigue siendo dueña de su número (ADR-0095) y Meta puede re-verificar la
    suscripción mientras tanto (ADR-0122)."""
    _, slug = await _tenant_con_byok("whrevv", status="revoked", app_id="app-revv", verify_token="token-revv")

    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "token-revv", "hub.challenge": "CHAL-REV"},
    )

    assert r.status_code == 200 and r.text == "CHAL-REV"


async def test_handshake_por_slug_con_dos_apps_acepta_el_token_de_cada_una(app_client) -> None:
    """Antes solo se aceptaba el token de la conexión que ganaba el orden: la otra app no
    podía re-verificar su suscripción."""
    tid, slug = await _tenant_con_byok("whdosv", status="connected", app_id="app-v1", verify_token="token-v1")
    await _agregar_byok(tid, status="connected", app_id="app-v2", verify_token="token-v2")

    for token in ("token-v1", "token-v2"):
        r = await app_client.get(
            "/webhooks/whatsapp/" + slug,
            params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "C"},
        )
        assert r.status_code == 200 and r.text == "C", token


async def test_una_conexion_de_embedded_signup_no_le_quita_el_secreto_a_la_byok(app_client) -> None:
    """Una conexión de ES más nueva, sana y sin secreto propio no le quita nada a la BYOK."""
    from datetime import UTC, datetime, timedelta

    ahora = datetime.now(UTC)
    phone = _phone("es")
    tid, slug = await _tenant_con_byok(
        "whes", status="connected", app_id="app-byok-es", app_secret="secreto-byok-es",
        connected_at=ahora - timedelta(days=30), phone=phone,
    )
    await _agregar_byok(tid, status="connected", app_id="app-es", origin="embedded_signup", connected_at=ahora,
                        phone=_phone("es-plataforma"))
    body = _cuerpo(phone)

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-byok-es"))

    assert r.status_code == 200, r.text


async def test_una_conexion_byok_tomada_por_embedded_signup_conserva_su_secreto(app_client) -> None:
    """El wizard de ES cambia el `origin` de una conexión BYOK pero no borra su secreto, y la app
    BYOK puede seguir suscrita a la WABA: sus eventos se siguen aceptando, como antes de ADR-0101."""
    from sqlalchemy import update

    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    phone = _phone("toma")
    tid, slug = await _tenant_con_byok(
        "whtoma", status="connected", app_id="app-toma", app_secret="secreto-toma", phone=phone
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.tenant_id == tid).values(origin="embedded_signup")
            )
    body = _cuerpo(phone)

    r = await app_client.post("/webhooks/whatsapp/" + slug, content=body, headers=_sign(body, "secreto-toma"))

    assert r.status_code == 200, r.text

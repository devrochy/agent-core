"""Un número o una WABA de Meta pertenecen a un solo negocio (ADR-0095), con BD real.

Antes de este arreglo un negocio podía registrar la WABA o el número de otro y recibir
sus mensajes: el ruteo miraba primero la WABA (`LIMIT 1` sin orden), el resolvedor de
número no veía `phone_numbers` y las altas guardaban IDs sin verificarlos (el Embedded
Signup, incluso antes de canjear el `code`). Estos tests corren las funciones SQL de la
migración 0052 como `app_runtime` (sin bypass de RLS), igual que el runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, stub_meta_pair_ok, uniq
from sqlalchemy import select, update

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ------------------------------------------------------------------ helpers
async def _new_tenant(prefix: str, *, phone_in_channel_connections: str | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefix)
    whatsapp = {"label": slug, "phone_number_id": phone_in_channel_connections} if phone_in_channel_connections else None
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de reclamo de activos de Meta.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL, whatsapp=whatsapp,
    )
    return tid


async def _seed_meta_assets(
    tid: uuid.UUID, *, waba_id: str | None = None, phone_number_id: str | None = None
) -> uuid.UUID:
    """Conexión de Meta viva con su WABA y su número, escrita directo (sin pasar por las altas)."""
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin="manual", meta_app_id=uniq("app"), status="connected",
            )
            session.add(connection)
            await session.flush()
            waba = None
            if waba_id:
                waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
                session.add(waba)
                await session.flush()
            if phone_number_id:
                session.add(
                    PhoneNumber(
                        tenant_id=tid, connection_id=connection.id, phone_number_id=phone_number_id,
                        waba_account_id=waba.id if waba else None,
                    )
                )
            return connection.id


async def _soft_delete(tid: uuid.UUID, connection_id: uuid.UUID) -> None:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.id == connection_id).values(deleted_at=utcnow())
            )


async def _resolve(fn: str, value: str) -> uuid.UUID | None:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(text(f"select {fn}(:v)"), {"v": value})).scalar_one_or_none()


async def _rows_for(tid: uuid.UUID, *, waba_id: str, phone_number_id: str) -> tuple[int, int]:
    from core.db.models import PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            wabas = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalars().all()
            phones = (
                await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == phone_number_id))
            ).scalars().all()
    return len(wabas), len(phones)


@pytest.fixture
def platform_app(monkeypatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "platform-app-test")
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_config_id", "platform-config")
    return settings


# ------------------------------------------------------------------ ruteo
def _cambio(phone_number_id: str, wa_id: str, msg_id: str) -> dict:
    return {
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_number_id},
            "contacts": [{"wa_id": wa_id, "profile": {"name": "Cliente"}}],
            "messages": [{"from": wa_id, "id": msg_id, "type": "text", "text": {"body": "hola"}}],
        },
    }


async def _post_webhook(app_client, monkeypatch, waba_id: str, cambios: list[dict]):
    """POST firmado al endpoint genérico, en `production` (sin el `default_tenant()` de desarrollo)."""
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": waba_id, "changes": cambios}]}).encode()
    secret = settings.whatsapp_app_secret or settings.meta_app_secret
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = await app_client.post("/webhooks/whatsapp", content=body, headers=headers)
    assert r.status_code == 200, r.text
    return r


async def _evento_registrado(msg_id: str):
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == msg_id))).scalar_one()


async def test_el_numero_de_a_con_la_waba_registrada_por_b_no_va_a_b(app_client, monkeypatch) -> None:
    """El caso del hallazgo: A tiene el número; B registró la WABA de A. Antes el evento
    iba a B (la WABA mandaba). Número y WABA apuntan a negocios distintos: una de las dos
    filas es ajena, así que no va a ninguno y queda registrado para corregirlo."""
    waba_id, phone_a = uniq("waba-de-a"), uniq("phone-de-a")
    tenant_a = await _new_tenant("ruteo-a", phone_in_channel_connections=phone_a)
    tenant_b = await _new_tenant("ruteo-b")
    await _seed_meta_assets(tenant_b, waba_id=waba_id, phone_number_id=uniq("phone-de-b"))
    assert await _resolve("resolve_tenant_for_waba", waba_id) == tenant_b
    assert await _resolve("resolve_tenant_for_phone", phone_a) == tenant_a

    msg_id = f"wamid.{uniq('a')}"
    await _post_webhook(app_client, monkeypatch, waba_id, [_cambio(phone_a, "wa-cliente-a", msg_id)])

    row = await _evento_registrado(msg_id)
    assert row.tenant_id is None
    assert row.process_error == "tenant_conflict"


async def test_un_numero_con_dos_duenos_no_cae_a_la_waba(app_client, monkeypatch) -> None:
    """X tiene el número en `channel_connections`; Z lo tiene en `phone_numbers` y además
    la WABA. Si un número ambiguo cayera a la WABA, Z se quedaría con los mensajes de X."""
    waba_id, phone = uniq("waba-de-z"), uniq("phone-doble")
    await _new_tenant("doble-x", phone_in_channel_connections=phone)
    tenant_z = await _new_tenant("doble-z")
    await _seed_meta_assets(tenant_z, waba_id=waba_id, phone_number_id=phone)
    assert await _resolve("resolve_tenant_for_phone", phone) is None
    assert await _resolve("resolve_tenant_for_waba", waba_id) == tenant_z

    msg_id = f"wamid.{uniq('d')}"
    await _post_webhook(app_client, monkeypatch, waba_id, [_cambio(phone, "wa-cliente-x", msg_id)])

    row = await _evento_registrado(msg_id)
    assert (row.tenant_id, row.process_error) == (None, "tenant_conflict")


async def test_un_conflicto_no_va_al_negocio_del_endpoint_por_slug() -> None:
    """En el endpoint por slug, un evento sin negocio resuelto va al de la URL. Un
    conflicto no es "sin negocio": no puede caer ahí."""
    from core.gateway.webhooks import Firmantes, _process_payload

    waba_id, phone_x = uniq("waba-de-z"), uniq("phone-de-x")
    await _new_tenant("slug-x", phone_in_channel_connections=phone_x)
    tenant_z = await _new_tenant("slug-z")
    await _seed_meta_assets(tenant_z, waba_id=waba_id)
    tenant_slug = await _new_tenant("slug-s")

    msg_id = f"wamid.{uniq('s')}"
    payload = {"object": "whatsapp_business_account", "entry": [
        {"id": waba_id, "changes": [_cambio(phone_x, "wa-cliente-x", msg_id)]},
    ]}
    await _process_payload(
        payload, known_tenant_id=tenant_slug, firmantes=Firmantes.de_negocio(tenant_slug), dev_fallback=False
    )

    row = await _evento_registrado(msg_id)
    assert (row.tenant_id, row.process_error) == (None, "tenant_conflict")


async def test_numero_y_waba_del_mismo_negocio_rutean_a_el(app_client, monkeypatch) -> None:
    waba_id, phone = uniq("waba-propia"), uniq("phone-propio")
    tid = await _new_tenant("ruteo-propio")
    await _seed_meta_assets(tid, waba_id=waba_id, phone_number_id=phone)

    msg_id = f"wamid.{uniq('p')}"
    await _post_webhook(app_client, monkeypatch, waba_id, [_cambio(phone, "wa-cliente-p", msg_id)])

    assert (await _evento_registrado(msg_id)).tenant_id == tid


async def test_dos_numeros_de_negocios_distintos_en_una_entry_van_cada_uno_al_suyo(app_client, monkeypatch) -> None:
    """Antes la entry entera iba al negocio de su primer evento."""
    phone_a, phone_b = uniq("phone-entry-a"), uniq("phone-entry-b")
    tenant_a = await _new_tenant("entry-a", phone_in_channel_connections=phone_a)
    tenant_b = await _new_tenant("entry-b", phone_in_channel_connections=phone_b)

    msg_a, msg_b = f"wamid.{uniq('ea')}", f"wamid.{uniq('eb')}"
    await _post_webhook(
        app_client, monkeypatch, uniq("waba-sin-registrar"),
        [_cambio(phone_a, "wa-entry-a", msg_a), _cambio(phone_b, "wa-entry-b", msg_b)],
    )

    assert (await _evento_registrado(msg_a)).tenant_id == tenant_a
    assert (await _evento_registrado(msg_b)).tenant_id == tenant_b


async def test_el_resolvedor_de_numero_ve_phone_numbers_y_solo_conexiones_vivas() -> None:
    phone = uniq("phone-pn")
    tid = await _new_tenant("ruteo-pn")
    connection_id = await _seed_meta_assets(tid, waba_id=uniq("waba-pn"), phone_number_id=phone)

    assert await _resolve("resolve_tenant_for_phone", phone) == tid

    await _soft_delete(tid, connection_id)
    assert await _resolve("resolve_tenant_for_phone", phone) is None


async def test_una_waba_que_reclaman_dos_negocios_no_rutea_a_ninguno() -> None:
    from core.gateway.webhook_flatten import FlattenedEvent
    from core.gateway.webhook_route_tenant import resolve_tenant_for_event

    waba_id = uniq("waba-doble")
    tenant_a, tenant_b = await _new_tenant("doble-a"), await _new_tenant("doble-b")
    await _seed_meta_assets(tenant_a, waba_id=waba_id)
    conn_b = await _seed_meta_assets(tenant_b, waba_id=waba_id)

    assert await _resolve("resolve_tenant_for_waba", waba_id) is None
    evento = FlattenedEvent(
        object_type="whatsapp_business_account", field="message_template_status_update",
        external_id=uniq("evt"), entry_id=waba_id, sub_id=None, value={},
    )
    assert await resolve_tenant_for_event(evento) is None

    # Al borrar la conexión que sobra, la WABA vuelve a rutear a su único dueño vivo.
    await _soft_delete(tenant_b, conn_b)
    assert await _resolve("resolve_tenant_for_waba", waba_id) == tenant_a


# ------------------------------------------------------------------ reclamo
async def test_reclamo_con_tenant_nulo_mismo_tenant_y_otro_tenant() -> None:
    """Corre como `app_runtime`: también prueba el GRANT de la función."""
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant

    waba_id, phone = uniq("waba-claim"), uniq("phone-claim")
    duena, otra = await _new_tenant("claim-a"), await _new_tenant("claim-b")
    connection_id = await _seed_meta_assets(duena, waba_id=waba_id, phone_number_id=phone)

    # Alta de un tenant nuevo (`tenant_id=None`): cualquier dueño cuenta como otro.
    assert await asset_claimed_by_other_tenant(None, phone_number_id=phone) is True
    assert await asset_claimed_by_other_tenant(None, waba_id=waba_id) is True
    assert await asset_claimed_by_other_tenant(duena, waba_id=waba_id, phone_number_id=phone) is False
    assert await asset_claimed_by_other_tenant(otra, phone_number_id=phone) is True
    assert await asset_claimed_by_other_tenant(otra, waba_id=waba_id) is True
    assert await asset_claimed_by_other_tenant(otra, waba_id=uniq("waba-libre"), phone_number_id=uniq("p")) is False

    # Una conexión borrada ya no reclama nada.
    await _soft_delete(duena, connection_id)
    assert await asset_claimed_by_other_tenant(otra, waba_id=waba_id, phone_number_id=phone) is False


async def test_onboard_tenant_rechaza_un_numero_que_otro_tiene_en_phone_numbers() -> None:
    """Antes `_phone_owner` solo miraba `channel_connections`: este número pasaba."""
    phone = uniq("phone-onb")
    duena = await _new_tenant("onb-a")
    await _seed_meta_assets(duena, phone_number_id=phone)

    with pytest.raises(ValueError, match="ya pertenece a otro tenant"):
        await _new_tenant("onb-b", phone_in_channel_connections=phone)


async def test_save_connection_rechaza_el_numero_de_otro_negocio() -> None:
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    phone = uniq("phone-cc")
    duena, otra = await _new_tenant("cc-a"), await _new_tenant("cc-b")
    await _seed_meta_assets(duena, phone_number_id=phone)

    with pytest.raises(ValueError, match="otro negocio"):
        async with tenant_scope(otra):
            await save_connection(
                "Número ajeno", {"phone_number_id": phone, "access_token": "tok", "app_secret": "sec"}
            )


async def test_las_funciones_se_prueban_como_app_runtime_sin_bypass() -> None:
    """Las funciones SQL de estos tests corren con el rol del runtime: sin este rol, la
    RLS forzada no aplicaría y un test podría pasar por el bypass del superusuario."""
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        rol, bypass = (
            await session.execute(
                text("select current_user, rolsuper or rolbypassrls from pg_roles where rolname = current_user")
            )
        ).one()
    assert (rol, bypass) == ("app_runtime", False)


async def test_las_filas_de_una_conexion_en_draft_no_cuentan() -> None:
    """Un Embedded Signup que nunca canjeó su `code` (las filas las escribía el navegador)."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant
    from core.shared.tenant import tenant_scope

    waba_id, phone = uniq("waba-draft"), uniq("phone-draft")
    tid = await _new_tenant("draft")
    connection_id = await _seed_meta_assets(tid, waba_id=waba_id, phone_number_id=phone)
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.id == connection_id).values(status="draft")
            )

    assert await _resolve("resolve_tenant_for_waba", waba_id) is None
    assert await _resolve("resolve_tenant_for_phone", phone) is None
    assert await asset_claimed_by_other_tenant(None, waba_id=waba_id, phone_number_id=phone) is False


async def test_dos_altas_simultaneas_de_la_misma_waba_solo_una_gana() -> None:
    """El candado por activo serializa las dos transacciones: la segunda ve la fila de la
    primera y falla. Sin candado, las dos comprobarían antes de que ninguna confirme."""
    import asyncio

    from core.db.models import WabaAccount
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import MetaAssetError, claim_assets
    from core.shared.tenant import tenant_scope

    waba_id = uniq("waba-carrera")
    tenants = [await _new_tenant("carrera-a"), await _new_tenant("carrera-b")]
    conexiones = [await _seed_meta_assets(tid) for tid in tenants]

    async def alta(tid: uuid.UUID, connection_id: uuid.UUID) -> None:
        async with tenant_scope(tid):
            async with get_session() as session:
                await claim_assets(session, tid, waba_id=waba_id, phone_number_id=None)
                session.add(WabaAccount(tenant_id=tid, connection_id=connection_id, waba_id=waba_id))
                await session.flush()
                await asyncio.sleep(0.3)  # la transacción sigue abierta: la otra alta espera

    resultados = await asyncio.gather(
        *(alta(tid, cid) for tid, cid in zip(tenants, conexiones, strict=True)), return_exceptions=True
    )

    errores = [r for r in resultados if isinstance(r, BaseException)]
    assert len(errores) == 1, resultados
    assert isinstance(errores[0], MetaAssetError) and errores[0].code == "asset_claimed"
    assert await _resolve("resolve_tenant_for_waba", waba_id) in tenants


async def test_las_funciones_no_las_ejecuta_public() -> None:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        for firma in (
            "resolve_tenant_for_phone(text)",
            "resolve_tenant_for_waba(text)",
            "meta_asset_claimed_by_other_tenant(text, text, uuid)",
        ):
            puede = (
                await session.execute(
                    text("select has_function_privilege('public', :f, 'execute')"), {"f": firma}
                )
            ).scalar_one()
            assert puede is False, firma


async def test_activar_una_conexion_en_draft_se_rechaza() -> None:
    """Activarla sacaría de `draft` filas que nadie verificó (ADR-0095)."""
    from core.admin.meta_health_service import set_connection_status
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("activar-draft")
    connection_id = await _seed_meta_assets(tid, waba_id=uniq("waba-draft"))
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.id == connection_id).values(status="draft")
            )
        error = await set_connection_status(tid, str(connection_id), active=True)
        async with get_session() as session:
            status = (await session.get(MetaConnection, connection_id)).status

    assert error is not None and "no terminó el alta" in error
    assert status == "draft"


async def test_la_migracion_retira_las_conexiones_en_draft_con_filas() -> None:
    """El SQL de 0052, corrido como `app_runtime` dentro del scope del negocio (en la
    migración corre con el dueño, sin RLS): retira el `draft` con filas y cierra su wizard;
    no toca un `draft` sin filas ni una conexión canjeada."""
    import importlib.util
    from pathlib import Path

    from sqlalchemy import text

    from core.db.models import ConnectionWizard, MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    spec = importlib.util.spec_from_file_location(
        "m0052", Path(__file__).resolve().parents[2] / "alembic/versions/0052_reclamo_de_numero_y_waba.py"
    )
    assert spec and spec.loader
    m0052 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0052)

    tid = await _new_tenant("retiro")
    con_filas = await _seed_meta_assets(tid, waba_id=uniq("waba-r"), phone_number_id=uniq("phone-r"))
    sin_filas = await _seed_meta_assets(tid)
    canjeada = await _seed_meta_assets(tid, waba_id=uniq("waba-ok"))
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.id.in_([con_filas, sin_filas])).values(status="draft")
            )
            wizard = ConnectionWizard(
                tenant_id=tid, connection_id=con_filas, channel="whatsapp", environment="production",
                current_step="exchange_code",
            )
            session.add(wizard)
            await session.flush()
            wizard_id = wizard.id

        async with get_session() as session:
            await session.execute(text(m0052._RETIRAR_DRAFT_CON_FILAS), {"marca": m0052._MARCA_RETIRO})
            await session.execute(text(m0052._CERRAR_SUS_WIZARDS), {"marca": m0052._MARCA_RETIRO})

        async with get_session() as session:
            filas = {
                c.id: c for c in (await session.execute(select(MetaConnection).where(
                    MetaConnection.id.in_([con_filas, sin_filas, canjeada])
                ))).scalars()
            }
            wizard_row = await session.get(ConnectionWizard, wizard_id)

    assert filas[con_filas].deleted_at is not None and filas[con_filas].status == "revoked"
    assert filas[con_filas].status_reason == m0052._MARCA_RETIRO
    assert filas[sin_filas].deleted_at is None and filas[sin_filas].status == "draft"
    assert filas[canjeada].deleted_at is None and filas[canjeada].status == "connected"
    assert wizard_row.current_step == "done"


async def test_sincronizar_numeros_no_guarda_los_de_otro_negocio(monkeypatch) -> None:
    """Graph lista tres números en la WABA del negocio; uno lo tiene otro negocio en
    `channel_connections` y otro en `phone_numbers`. Se guarda solo el libre, sin
    `IntegrityError` por el único global de `phone_number_id`."""
    import core.gateway.meta_phone_numbers as phones_mod
    from core.db.models import PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    libre, en_cc, en_pn = uniq("phone-libre"), uniq("phone-cc"), uniq("phone-pn")
    await _new_tenant("sync-otro-cc", phone_in_channel_connections=en_cc)
    await _seed_meta_assets(await _new_tenant("sync-otro-pn"), phone_number_id=en_pn)
    tid = await _new_tenant("sync")
    waba_id = uniq("waba-sync")
    connection_id = await _seed_meta_assets(tid, waba_id=waba_id)

    async def numeros_de_la_waba(*_a):
        return [{"id": libre}, {"id": en_cc}, {"id": en_pn}]

    monkeypatch.setattr(phones_mod, "list_phone_numbers", numeros_de_la_waba)
    async with tenant_scope(tid):
        await phones_mod.sync_phone_numbers(waba_id, "tok", "sec", "v21.0", connection_id, tid)
        async with get_session() as session:
            guardados = {
                p.phone_number_id
                for p in (await session.execute(
                    select(PhoneNumber).where(PhoneNumber.connection_id == connection_id)
                )).scalars()
            }

    assert guardados == {libre}


async def test_dos_embedded_signup_simultaneos_de_la_misma_waba_solo_uno_gana(monkeypatch, platform_app) -> None:
    """Las dos conexiones parten en `draft`. Si el alta guardara las filas sin sacar la
    conexión de `draft` en la misma transacción, la segunda no vería las de la primera
    (no cuentan) y las dos se quedarían con la WABA."""
    import asyncio

    import core.gateway.meta_onboarding as onboarding
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    stub_meta_pair_ok(monkeypatch)
    waba_id = uniq("waba-es-carrera")
    tenants = [await _new_tenant("es-carrera-a"), await _new_tenant("es-carrera-b")]
    wizards = []
    for tid in tenants:
        async with tenant_scope(tid):
            wizards.append(await onboarding.start_or_resume_wizard(tid))

    async def alta(tid: uuid.UUID, wizard) -> None:
        async with tenant_scope(tid):
            await onboarding._attach_finish_assets(
                wizard.id, tid, wizard.connection_id, "tok", waba_id=waba_id, phone_number_id=None
            )

    resultados = await asyncio.gather(
        *(alta(tid, w) for tid, w in zip(tenants, wizards, strict=True)), return_exceptions=True
    )

    errores = [r for r in resultados if isinstance(r, BaseException)]
    assert len(errores) == 1, resultados
    assert isinstance(errores[0], onboarding.WizardError)
    ganador = tenants[resultados.index(None)]
    assert await _resolve("resolve_tenant_for_waba", waba_id) == ganador
    async with tenant_scope(ganador):
        async with get_session() as session:
            conexion = await session.get(MetaConnection, wizards[resultados.index(None)].connection_id)
    assert conexion.status == "pending"


# ------------------------------------------------------------------ altas
async def test_alta_manual_de_una_waba_ajena_se_rechaza_sin_crear_nada(app_client, monkeypatch, platform_app) -> None:
    import core.gateway.meta_health as health_mod
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    class _Ok:
        ok = True
        detail = "ok"

    async def fake_check(*_a, **_k):
        return _Ok()

    monkeypatch.setattr(health_mod, "check_token_valid", fake_check)
    stub_meta_pair_ok(monkeypatch)  # el token SÍ ve la WABA (un tenant tipo agencia)

    waba_id = uniq("waba-manual")
    duena, intrusa = await _new_tenant("manual-a"), await _new_tenant("manual-b")
    await _seed_meta_assets(duena, waba_id=waba_id)

    r = await app_client.post(
        "/api/v1/admin/meta/manual", headers=await api_headers(app_client, intrusa),
        json={"waba_id": waba_id, "phone_number_id": uniq("phone-manual"), "token": "tok-manual"},
    )

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "asset_claimed"
    async with tenant_scope(intrusa):
        async with get_session() as session:
            assert (await session.execute(select(MetaConnection))).scalars().all() == []


async def test_alta_manual_con_un_numero_que_no_es_de_la_waba_se_rechaza(app_client, monkeypatch, platform_app) -> None:
    import core.gateway.meta_asset_claims as claims_mod
    import core.gateway.meta_health as health_mod

    class _Ok:
        ok = True
        detail = "ok"

    async def fake_check(*_a, **_k):
        return _Ok()

    async def numeros_de_la_waba(*_a):
        return [{"id": "otro-numero"}]

    monkeypatch.setattr(health_mod, "check_token_valid", fake_check)
    monkeypatch.setattr(claims_mod, "list_phone_numbers", numeros_de_la_waba)

    tid = await _new_tenant("manual-par")
    waba_id, phone = uniq("waba-par"), uniq("phone-par")
    r = await app_client.post(
        "/api/v1/admin/meta/manual", headers=await api_headers(app_client, tid),
        json={"waba_id": waba_id, "phone_number_id": phone, "token": "tok-manual"},
    )

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "phone_not_in_waba"
    assert await _rows_for(tid, waba_id=waba_id, phone_number_id=phone) == (0, 0)


async def test_canje_con_code_invalido_no_deja_filas(app_client, monkeypatch, platform_app) -> None:
    """El endpoint `/wizard/exchange` guardaba la WABA y el número del popup ANTES del
    canje: un `code` inventado dejaba filas que ocupaban los activos de otro negocio.
    Ahora, si Meta rechaza el `code`, responde 400 y no queda nada."""
    import core.gateway.meta_onboarding as onboarding
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant
    from core.gateway.meta_errors import GraphError
    from core.shared.tenant import tenant_scope

    async def code_invalido(version, params, settings):
        raise GraphError("Invalid verification code format.", category="PERMANENT", http_status=400, code=100)

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", code_invalido)

    tid = await _new_tenant("es-code")
    async with tenant_scope(tid):
        wizard = await onboarding.start_or_resume_wizard(tid)

    waba_id, phone = uniq("waba-es"), uniq("phone-es")
    r = await app_client.post(
        "/api/v1/admin/meta/wizard/exchange", headers=await api_headers(app_client, tid),
        json={"wizard_id": str(wizard.id), "code": "code-inventado", "waba_id": waba_id,
              "phone_number_id": phone, "business_id": uniq("biz")},
    )

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "meta_error"
    assert await _rows_for(tid, waba_id=waba_id, phone_number_id=phone) == (0, 0)
    assert await asset_claimed_by_other_tenant(None, waba_id=waba_id, phone_number_id=phone) is False


async def test_canje_valido_de_una_waba_ajena_se_rechaza_por_el_endpoint(app_client, monkeypatch, platform_app) -> None:
    import core.gateway.meta_onboarding as onboarding
    from core.db.models import ConnectionWizard
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async def code_ok(version, params, settings):
        return "tok-system-user", False

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", code_ok)
    stub_meta_pair_ok(monkeypatch)

    waba_id = uniq("waba-es-ajena")
    duena, intrusa = await _new_tenant("es-a"), await _new_tenant("es-b")
    await _seed_meta_assets(duena, waba_id=waba_id)
    async with tenant_scope(intrusa):
        wizard = await onboarding.start_or_resume_wizard(intrusa)

    phone = uniq("phone-es-ajena")
    r = await app_client.post(
        "/api/v1/admin/meta/wizard/exchange", headers=await api_headers(app_client, intrusa),
        json={"wizard_id": str(wizard.id), "code": "code-real", "waba_id": waba_id, "phone_number_id": phone,
              "business_id": uniq("biz")},
    )

    assert r.status_code == 400, r.text
    assert await _rows_for(intrusa, waba_id=waba_id, phone_number_id=phone) == (0, 0)
    async with tenant_scope(intrusa):
        async with get_session() as session:
            row = await session.get(ConnectionWizard, wizard.id)
    assert row.current_step != "discover_assets"
    assert (row.last_error or {}).get("code") == "asset_claimed"


async def test_canje_valido_guarda_la_waba_y_el_numero_verificados(app_client, monkeypatch, platform_app) -> None:
    import core.gateway.meta_onboarding as onboarding
    from core.db.models import PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async def code_ok(version, params, settings):
        return "tok-system-user", False

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", code_ok)
    stub_meta_pair_ok(monkeypatch)

    tid = await _new_tenant("es-ok")
    async with tenant_scope(tid):
        wizard = await onboarding.start_or_resume_wizard(tid)

    waba_id, phone = uniq("waba-es-ok"), uniq("phone-es-ok")
    r = await app_client.post(
        "/api/v1/admin/meta/wizard/exchange", headers=await api_headers(app_client, tid),
        json={"wizard_id": str(wizard.id), "code": "code-real", "waba_id": waba_id, "phone_number_id": phone,
              "business_id": uniq("biz")},
    )

    assert r.status_code == 200, r.text
    assert r.json()["nextStep"] == "discover_assets"
    async with tenant_scope(tid):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            numero = (
                await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == phone))
            ).scalar_one()
    assert waba.connection_id == wizard.connection_id
    assert numero.waba_account_id == waba.id
    assert await _resolve("resolve_tenant_for_phone", phone) == tid

"""HU-AOS-15 · Toda conversación nace con conexión (requisito del contract, ADR-0115 adenda 2026-10-24).

- **Centro de pruebas:** la conversación de prueba va por la conexión interna `playground` del
  negocio (una por negocio, creada al primer uso), que no se ve como conexión manual, ni en la
  consola de conexiones, ni cuenta como conversacional.
- **Backfill de conexiones:** liga las conversaciones de prueba (también las cerradas) y no crea la
  conexión interna donde no hay nada que ligar.
- **Consumer:** un número de la cuenta sin fila canónica la recibe al llegar el primer mensaje
  (con `AOS_CONNECTION_ROUTING` encendida) y la conversación nace con conexión.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text
from test_api_registrar_numero import _negocio_con_byok

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("pruebas")
    tid, _ = await onboard_tenant(name=f"Pruebas {slug}", slug=slug, persona={"tone": "cercano"})
    return tid


async def _conversacion_de_prueba(tid: uuid.UUID) -> Any:
    from core.admin.playground import get_or_create_test_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await get_or_create_test_conversation(tid)


async def _conexiones_de_pruebas(tid: uuid.UUID) -> list[Any]:
    return await _sql(
        tid,
        "select id, channel, provider, type_id, category, active, is_primary, credentials_encrypted, external_id "
        "from channel_connections where tenant_id = :t and type_id = 'playground' and deleted_at is null",
        t=tid,
    )


# ---------------------------------------------------------------- Centro de pruebas
async def test_la_conversacion_de_prueba_va_por_la_conexion_interna_del_negocio() -> None:
    tid = await _negocio()
    primera = await _conversacion_de_prueba(tid)
    segunda = await _conversacion_de_prueba(tid)
    assert primera.id == segunda.id and primera.connection_id is not None

    [conexion] = await _conexiones_de_pruebas(tid)
    assert conexion.id == primera.connection_id
    # Sin credenciales, no primaria, de la categoría de mensajería y con su clave propia.
    assert (conexion.channel, conexion.provider, conexion.category) == ("test", "internal", "comunicacion")
    assert conexion.credentials_encrypted is None and conexion.is_primary is False
    assert conexion.external_id == f"playground-{tid}"


async def test_cada_negocio_tiene_su_propia_conexion_de_pruebas() -> None:
    a, b = await _negocio(), await _negocio()
    ca, cb = await _conversacion_de_prueba(a), await _conversacion_de_prueba(b)
    assert ca.connection_id != cb.connection_id
    # B no ve la conexión de A (RLS) ni al revés.
    assert await _sql(b, "select 1 from channel_connections where id = :c", c=ca.connection_id) == []
    assert await _sql(a, "select 1 from channel_connections where id = :c", c=cb.connection_id) == []


async def test_una_conversacion_de_prueba_de_antes_sin_conexion_se_liga_al_usarla() -> None:
    from core.admin.playground import _get_or_create_customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = await _get_or_create_customer(session, tid)
            conv_id = uuid.uuid4()
            await session.execute(
                text(
                    "insert into conversations (id, tenant_id, customer_id, channel, status, state) "
                    "values (:i, :t, :c, 'test', 'open', '{\"is_test\": true}')"
                ),
                {"i": conv_id, "t": tid, "c": cliente.id},
            )
    conversacion = await _conversacion_de_prueba(tid)
    assert conversacion.id == conv_id and conversacion.connection_id is not None


async def test_la_conexion_de_pruebas_no_se_ve_como_conexion_ni_cuenta_como_conversacional() -> None:
    from core.gateway.connections import ES_MANUAL, administrada_aparte, list_connections
    from core.gateway.consola_conexiones import consola
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    conversacion = await _conversacion_de_prueba(tid)
    async with tenant_scope(tid):
        manuales = await list_connections()
    assert conversacion.connection_id not in {c.id for c in manuales}
    assert ES_MANUAL is not None

    async with tenant_scope(tid):
        vista = await consola(tid, offset=0, limite=50)
    ids = {fila.id for fila in vista.items}
    assert conversacion.connection_id not in ids and vista.total == len(vista.items)

    [(produce,)] = await _sql(
        tid,
        "select ct.produces_conversations from channel_connections cc "
        "join connection_types ct on ct.id = cc.type_id where cc.id = :c",
        c=conversacion.connection_id,
    ) or [(None,)]
    # El tipo se sincroniza con el catálogo; si todavía no está en la base de test, no cuenta.
    assert produce in (False, None)
    from core.db.models import ChannelConnection

    assert administrada_aparte(ChannelConnection(type_id="playground"))


# ---------------------------------------------------------------- backfill
async def test_el_backfill_liga_las_conversaciones_de_prueba_y_no_crea_conexion_si_no_hay() -> None:
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    vacio = await _negocio()
    await backfill_conexiones(dry_run=False, tenant_id=vacio)
    assert await _conexiones_de_pruebas(vacio) == []

    tid = await _negocio()
    cliente = (
        await _sql(
            tid,
            "insert into customers (id, tenant_id, channel, external_id) values (gen_random_uuid(), :t, 'test', :e) "
            "returning id",
            t=tid, e=uniq("c"),
        )
    )[0][0]
    for estado in ("open", "closed", "closed"):
        await _sql(
            tid,
            "insert into conversations (id, tenant_id, customer_id, channel, status) values (gen_random_uuid(), :t, :c, 'test', :s)",
            t=tid, c=cliente, s=estado,
        )
    en_seco = await backfill_conexiones(dry_run=True, tenant_id=tid)
    assert en_seco.conversaciones_prueba_ligadas == 3 and await _conexiones_de_pruebas(tid) == []

    real = await backfill_conexiones(dry_run=False, tenant_id=tid, lote=2)
    assert real.conversaciones_prueba_ligadas == 3
    [conexion] = await _conexiones_de_pruebas(tid)
    sin = await _sql(tid, "select count(*) from conversations where tenant_id = :t and connection_id is null", t=tid)
    assert sin == [(0,)]
    assert {f[0] for f in await _sql(tid, "select connection_id from conversations where tenant_id = :t", t=tid)} == {
        conexion.id
    }
    # Idempotente.
    otra = await backfill_conexiones(dry_run=False, tenant_id=tid)
    assert otra.conversaciones_prueba_ligadas == 0 and len(await _conexiones_de_pruebas(tid)) == 1


# ---------------------------------------------------------------- consumer
async def _entrada(tid: uuid.UUID, cliente: str, phone_id: str) -> Any:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        conversacion, _, _ = await consumer._resolve_conversation(
            {"channel": "whatsapp", "customer_external_id": cliente, "connection_external_id": phone_id}
        )
    return conversacion


async def test_un_numero_sin_fila_canonica_la_recibe_con_el_primer_mensaje(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    tid, cuenta, _waba, phone = await _negocio_con_byok()
    assert await _sql(tid, "select 1 from channel_connections where external_id = :p", p=phone) == []

    conversacion = await _entrada(tid, uniq("cli"), phone)
    assert conversacion.connection_id is not None
    [fila] = await _sql(
        tid, "select id, meta_connection_id, type_id, credentials_encrypted from channel_connections "
             "where tenant_id = :t and external_id = :p", t=tid, p=phone,
    )
    assert fila.id == conversacion.connection_id and fila.meta_connection_id == cuenta
    assert fila.type_id == "whatsapp_meta" and fila.credentials_encrypted is None

    # Otro cliente al mismo número reutiliza la fila, no crea otra.
    otra = await _entrada(tid, uniq("cli2"), phone)
    assert otra.connection_id == conversacion.connection_id
    assert len(await _sql(tid, "select 1 from channel_connections where tenant_id = :t and external_id = :p", t=tid, p=phone)) == 1


async def test_con_el_ruteo_apagado_el_consumer_no_crea_conexiones(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", False)
    tid, _cuenta, _waba, phone = await _negocio_con_byok()
    conversacion = await _entrada(tid, uniq("cli"), phone)
    assert await _sql(tid, "select 1 from channel_connections where external_id = :p", p=phone) == []
    assert conversacion.connection_id is None


# ---------------------------------------------------------------- revisión del auditor
async def test_en_vuelo_una_fila_viva_de_otra_alta_gana_y_no_se_religa() -> None:
    """Una alta concurrente ya dejó la fila viva con la cuenta nueva: el consumer, con el dato
    viejo, la devuelve tal cual (religar es de las altas, no del consumer)."""
    from test_conexion_canonica import _alta_numero, _otra_cuenta

    from core.db.session import get_session
    from core.gateway.conexion_canonica import asegurar_conexion_en_vuelo
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-race")
    nueva = await _otra_cuenta(tid)
    await _alta_numero(tid, nueva, numero)  # la alta ganó la carrera
    async with tenant_scope(tid):
        async with get_session() as session:
            conexion = await asegurar_conexion_en_vuelo(session, tid, numero, cuenta)  # dato viejo
    assert conexion is not None and conexion.meta_connection_id == nueva


async def test_en_vuelo_no_se_crea_para_un_numero_liberado_ni_de_una_cuenta_dada_de_baja() -> None:
    from core.db.session import get_session
    from core.gateway.conexion_canonica import asegurar_conexion_en_vuelo
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, phone = await _negocio_con_byok()
    await _sql(
        tid, "update phone_numbers set deleted_at = now() where tenant_id = :t and phone_number_id = :p", t=tid, p=phone
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            assert await asegurar_conexion_en_vuelo(session, tid, phone, cuenta) is None
    assert await _sql(tid, "select 1 from channel_connections where external_id = :p", p=phone) == []

    tid2, cuenta2, _w, phone2 = await _negocio_con_byok()
    await _sql(tid2, "update meta_connections set deleted_at = now() where id = :c", c=cuenta2)
    async with tenant_scope(tid2):
        async with get_session() as session:
            assert await asegurar_conexion_en_vuelo(session, tid2, phone2, cuenta2) is None
    assert await _sql(tid2, "select 1 from channel_connections where external_id = :p", p=phone2) == []


async def test_en_vuelo_solo_para_whatsapp(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    tid, _cuenta, _waba, phone = await _negocio_con_byok()
    async with tenant_scope(tid):
        conversacion, _, _ = await consumer._resolve_conversation(
            {"channel": "messenger", "customer_external_id": uniq("psid"), "connection_external_id": phone}
        )
    assert conversacion.connection_id is None
    assert await _sql(tid, "select 1 from channel_connections where external_id = :p", p=phone) == []


async def test_el_turno_de_prueba_no_es_un_release_missing_en_un_negocio_v3(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from _api_helpers import banderas_como_dueno

    from core.config import get_settings
    from core.kernel import release_del_turno
    from core.shared import release_vigente
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    tid = await _negocio()
    conversacion = await _conversacion_de_prueba(tid)
    await banderas_como_dueno(tid, aos_runtime="v3")
    token = release_vigente.abrir()
    try:
        async with tenant_scope(tid):
            assert await release_del_turno.fijar_release_del_turno(conversacion) is None
            # No se marca «v3 sin release»: eso dejaría el turno de prueba sin escrituras de memoria.
            assert release_vigente.write_namespaces_del_turno() is None
    finally:
        release_vigente.cerrar(token)


async def test_la_conexion_de_pruebas_no_se_guarda_a_mano_ni_envia_por_nadie() -> None:
    from core.gateway.connections import save_connection
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    conversacion = await _conversacion_de_prueba(tid)
    async with tenant_scope(tid):
        with pytest.raises(ValueError, match="Centro de pruebas"):
            await save_connection("x", {}, type_id="playground")
        # Sin credenciales: no cae a la primaria ni al `.env`.
        assert await resolve_credentials(conversacion.connection_id, None, tid) == ("internal", {})

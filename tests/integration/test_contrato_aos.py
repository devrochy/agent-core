"""HU-AOS-15 · Contract del Agent OS (C1): precondiciones, deduplicación y DDL.

Las precondiciones y la deduplicación corren contra el stack real, acotadas a negocios de prueba
(`tenant_ids`, solo en seco): el contract es global y NO se aplica sobre la base compartida de los
tests (dejaría el NOT NULL y el índice único para siempre). El DDL se prueba en un esquema
descartable con las mismas definiciones. Sin LLM ni red.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import banderas_como_dueno, uniq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ---------------------------------------------------------------- helpers
def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte


@pytest.fixture
def ruteo_encendido(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _negocio() -> tuple[uuid.UUID, str, uuid.UUID]:
    from core.alta_negocio import onboard_tenant

    slug = uniq("contrato")
    tid, _ = await onboard_tenant(name=f"Contrato {slug}", slug=slug, persona={"tone": "cercano"})
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid,
        t=tid,
        ext=uniq("ig"),
    )
    return tid, slug, cid


async def _con_dos_releases(tid: uuid.UUID, cid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    from core.releases.build import construir
    from core.releases.promocion import promover
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    (uno,) = (await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid))[0]
    await _sql(tid, "update app_settings set assistant_name = 'Dos' where tenant_id = :t", t=tid)
    async with tenant_scope(tid):
        dos = (await construir(tid, cid, actor="test", modules=_modules())).release_id
        await promover(tid, cid, dos, actor="test")
    return uno, dos


async def _conversacion(
    tid: uuid.UUID, cid: uuid.UUID | None, cliente: uuid.UUID | None = None, *, hace: timedelta = timedelta(0),
    estado: str = "open",
) -> tuple[uuid.UUID, uuid.UUID]:
    cliente = cliente or uuid.uuid4()
    if not await _sql(tid, "select 1 from customers where id = :i", i=cliente):
        await _sql(
            tid,
            "insert into customers (id, tenant_id, channel, external_id) values (:id, :t, 'instagram', :e)",
            id=cliente, t=tid, e=uniq("cli"),
        )
    conv = uuid.uuid4()
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id, status, started_at) "
        "values (:id, :t, :cli, 'instagram', :c, :st, :w)",
        id=conv, t=tid, cli=cliente, c=cid, st=estado, w=datetime.now(UTC) - hace,
    )
    return conv, cliente


async def _mensaje(tid: uuid.UUID, conv: uuid.UUID) -> uuid.UUID:
    mid = uuid.uuid4()
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content) "
        "values (:id, :t, :c, 'inbound', 'customer', 'hola')",
        id=mid, t=tid, c=conv,
    )
    return mid


async def _baja_logica(tid: uuid.UUID) -> None:
    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set deleted_at = now() where id = :t"), {"t": tid})
    finally:
        await engine.dispose()


async def _revisar(tid: uuid.UUID) -> Any:
    from core.releases.contrato import contraer

    return await contraer(dry_run=True, tenant_ids=[tid])


# ---------------------------------------------------------------- precondiciones
async def test_un_negocio_listo_no_tiene_bloqueos(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    tid, _slug, cid = await _negocio()
    await _con_dos_releases(tid, cid)
    await banderas_como_dueno(tid, aos_runtime="v3")
    reporte = await _revisar(tid)
    assert reporte.bloqueos == [] and reporte.dry_run and not reporte.aplicado


async def test_bloquea_si_el_ruteo_esta_apagado_o_el_negocio_sigue_en_legacy(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    from core.config import get_settings

    tid, slug, cid = await _negocio()
    await _con_dos_releases(tid, cid)
    monkeypatch.setattr(get_settings(), "aos_connection_routing", False)
    reporte = await _revisar(tid)
    assert any("AOS_CONNECTION_ROUTING" in b for b in reporte.bloqueos)
    assert any(slug in b and "legacy" in b and "migrar-a-v3" in b for b in reporte.bloqueos)


async def test_bloquea_una_conversacion_sin_conexion_aunque_este_cerrada(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    tid, slug, cid = await _negocio()
    await _con_dos_releases(tid, cid)
    await banderas_como_dueno(tid, aos_runtime="v3")
    await _conversacion(tid, None, estado="closed")
    reporte = await _revisar(tid)
    assert reporte.sin_conexion == 1
    assert any(slug in b and "sin conexión" in b and "backfill-conexiones" in b for b in reporte.bloqueos)


async def test_un_negocio_dado_de_baja_no_pide_releases_pero_sus_conversaciones_cuentan(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    tid, _slug, cid = await _negocio()
    await _conversacion(tid, None, estado="closed")  # sin conexión
    await _conversacion(tid, cid, cliente := uuid.uuid4())
    await _conversacion(tid, cid, cliente)  # repetida
    await banderas_como_dueno(tid, aos_runtime="legacy")
    await _baja_logica(tid)
    reporte = await _revisar(tid)
    # Ni runtime ni releases (ya no atiende), pero sí la conversación sin conexión y la repetida.
    assert reporte.sin_conexion == 1 and reporte.duplicadas == 1
    assert all("baja" in b for b in reporte.bloqueos) and len(reporte.bloqueos) == 1, reporte.bloqueos


async def test_pide_dos_releases_estables_sin_rollback(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    from core.releases.promocion import rollback
    from core.shared.tenant import tenant_scope

    tid, _slug, cid = await _negocio()
    await banderas_como_dueno(tid, aos_runtime="v3")
    # Sin release activa.
    assert any("no tiene release activa" in b for b in (await _revisar(tid)).bloqueos)

    from core.releases.release_uno import backfill_releases

    await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    # Una sola release: no alcanza.
    assert any("se piden 2 estables" in b for b in (await _revisar(tid)).bloqueos)

    await _con_dos_releases(tid, cid)
    assert (await _revisar(tid)).bloqueos == []

    # Un rollback después de la penúltima promoción la deja fuera.
    async with tenant_scope(tid):
        await rollback(tid, cid, actor="test")
    assert any("rollback" in b for b in (await _revisar(tid)).bloqueos)


# ---------------------------------------------------------------- deduplicación
async def test_deduplica_sin_cerrar_conserva_la_mas_reciente_y_pasa_los_mensajes(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    from core.releases.contrato import _deduplicar, _duplicadas
    from core.shared.tenant import tenant_scope

    tid, _slug, cid = await _negocio()
    vieja, cliente = await _conversacion(tid, cid, hace=timedelta(days=2))
    media, _ = await _conversacion(tid, cid, cliente, hace=timedelta(days=1))
    nueva, _ = await _conversacion(tid, cid, cliente)
    otro_cliente, _ = await _conversacion(tid, cid)
    cerrada, _ = await _conversacion(tid, cid, cliente, hace=timedelta(days=9), estado="closed")
    m_vieja, m_media = await _mensaje(tid, vieja), await _mensaje(tid, media)

    async with tenant_scope(tid):
        trios = await _duplicadas(tid)
    assert sorted(trios) == sorted([(vieja, nueva, cliente), (media, nueva, cliente)])
    assert (await _revisar(tid)).duplicadas == 2

    async with tenant_scope(tid):
        assert await _deduplicar(tid, trios) == 2
        assert await _duplicadas(tid) == []

    filas = {f[0]: f for f in await _sql(
        tid, "select id, status, deleted_at is not null from conversations where tenant_id = :t", t=tid
    )}
    # No se cierra ninguna: solo se marcan borradas (lógico) las sobrantes.
    assert filas[vieja][1:] == ("open", True) and filas[media][1:] == ("open", True)
    assert filas[nueva][1:] == ("open", False) and filas[otro_cliente][1:] == ("open", False)
    assert filas[cerrada][1:] == ("closed", False)
    movidos = {f[0]: f[1] for f in await _sql(tid, "select id, conversation_id from messages where tenant_id = :t", t=tid)}
    assert movidos[m_vieja] == nueva and movidos[m_media] == nueva


async def test_la_escalada_o_con_el_bot_pausado_gana_aunque_sea_mas_vieja(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    """Una persona la atiende: perderla devolvería el cliente al bot (hallazgo del auditor)."""
    from core.releases.contrato import _deduplicar, _duplicadas
    from core.shared.tenant import tenant_scope

    tid, _slug, cid = await _negocio()
    escalada, cliente = await _conversacion(tid, cid, hace=timedelta(days=3), estado="escalated")
    pausada, cli2 = await _conversacion(tid, cid, hace=timedelta(days=3))
    await _sql(tid, "update conversations set bot_paused = true where id = :i", i=pausada)
    nueva_1, _ = await _conversacion(tid, cid, cliente)
    nueva_2, _ = await _conversacion(tid, cid, cli2)
    await _sql(tid, "update conversations set service_window_expires_at = now() + interval '1 hour' where id = :i", i=nueva_2)
    await _sql(tid, "update conversations set service_window_expires_at = now() + interval '9 hours' where id = :i", i=pausada)

    async with tenant_scope(tid):
        trios = await _duplicadas(tid)
        assert sorted(trios) == sorted([(nueva_1, escalada, cliente), (nueva_2, pausada, cli2)])
        assert await _deduplicar(tid, trios) == 2
    filas = {f[0]: f for f in await _sql(
        tid, "select id, deleted_at is not null, service_window_expires_at - now() > interval '8 hours' "
             "from conversations where tenant_id = :t", t=tid)}
    assert filas[escalada][1] is False and filas[nueva_1][1] is True
    assert filas[pausada][1] is False and filas[nueva_2][1] is True
    assert filas[pausada][2] is True  # la ventana de servicio más larga se conserva


async def test_la_deduplicacion_de_un_negocio_no_toca_a_otro(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    from core.releases.contrato import _deduplicar, _duplicadas
    from core.shared.tenant import tenant_scope

    a, _sa, ca = await _negocio()
    b, _sb, cb = await _negocio()
    va, cli_a = await _conversacion(a, ca, hace=timedelta(days=1))
    na, _ = await _conversacion(a, ca, cli_a)
    vb, cli_b = await _conversacion(b, cb, hace=timedelta(days=1))
    nb, _ = await _conversacion(b, cb, cli_b)
    mb = await _mensaje(b, vb)

    async with tenant_scope(a):
        assert await _deduplicar(a, await _duplicadas(a)) == 1
    # B queda intacto (sus duplicadas siguen ahí y su mensaje no se movió).
    assert (await _sql(b, "select conversation_id from messages where id = :m", m=mb))[0][0] == vb
    assert (await _sql(b, "select deleted_at is null from conversations where id = :i", i=vb))[0][0] is True
    async with tenant_scope(b):
        assert await _duplicadas(b) == [(vb, nb, cli_b)]
    assert (await _sql(a, "select deleted_at is not null from conversations where id = :i", i=va))[0][0] is True
    assert na


async def test_un_par_que_cambio_desde_el_calculo_se_salta(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    from core.releases.contrato import _deduplicar, _duplicadas
    from core.shared.tenant import tenant_scope

    tid, _slug, cid = await _negocio()
    vieja, cliente = await _conversacion(tid, cid, hace=timedelta(days=1))
    nueva, _ = await _conversacion(tid, cid, cliente)
    async with tenant_scope(tid):
        trios = await _duplicadas(tid)
    await _sql(tid, "update conversations set status = 'closed' where id = :i", i=nueva)  # a mitad de camino
    async with tenant_scope(tid):
        assert await _deduplicar(tid, trios) == 0
    assert (await _sql(tid, "select deleted_at is null from conversations where id = :i", i=vieja))[0][0] is True


async def test_aplicar_exige_confirmacion_y_el_contract_es_global(ruteo_encendido) -> None:  # type: ignore[no-untyped-def]
    from core.releases.contrato import contraer

    tid, _slug, _cid = await _negocio()
    with pytest.raises(ValueError):
        await contraer(dry_run=False, confirmado=True, tenant_ids=[tid])
    from core.config import get_settings

    # Sin confirmación explícita no aplica, aunque todo lo demás estuviera bien.
    reporte = await contraer(dry_run=False, confirmado=False)
    assert any("--confirmo-sin-vuelta-atras" in b for b in reporte.bloqueos) and not reporte.aplicado
    assert get_settings().aos_connection_routing


# ---------------------------------------------------------------- DDL en un esquema descartable
@pytest.fixture
async def esquema():  # type: ignore[no-untyped-def]
    """Esquema con las tablas mínimas del contract y un constraint `NOT VALID` pendiente."""
    from core.config import get_settings

    settings = get_settings()
    nombre = f"contrato_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(settings.migrations_database_url or settings.database_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        for sentencia in (
            f"CREATE SCHEMA {nombre}",
            f"""CREATE TABLE {nombre}.conexiones (tenant_id uuid NOT NULL, id uuid NOT NULL,
                  PRIMARY KEY (id), UNIQUE (tenant_id, id))""",
            f"""CREATE TABLE {nombre}.conversations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
                  customer_id uuid NOT NULL, status text NOT NULL DEFAULT 'open', deleted_at timestamptz,
                  started_at timestamptz NOT NULL DEFAULT now(), connection_id uuid,
                  CONSTRAINT conversations_connection_id_fkey FOREIGN KEY (connection_id)
                    REFERENCES {nombre}.conexiones (id))""",
            f"""ALTER TABLE {nombre}.conversations ADD CONSTRAINT fk_conv_connection
                  FOREIGN KEY (tenant_id, connection_id) REFERENCES {nombre}.conexiones (tenant_id, id) NOT VALID""",
            f"""CREATE TABLE {nombre}.core_decisions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL,
                  connection_id uuid, release_id uuid, outcome text NOT NULL DEFAULT 'routed',
                  created_at timestamptz NOT NULL DEFAULT now())""",
        ):
            await conn.execute(text(sentencia))
    try:
        yield nombre, engine
    finally:
        async with engine.connect() as conn:
            await conn.execute(text(f"DROP SCHEMA {nombre} CASCADE"))
        await engine.dispose()


async def test_el_ddl_deja_indice_unico_not_null_check_y_valida_lo_pendiente(esquema) -> None:  # type: ignore[no-untyped-def]
    from core.releases import contrato

    nombre, engine = esquema
    t, c = uuid.uuid4(), uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(text(f"INSERT INTO {nombre}.conexiones VALUES (:t, :c)"), {"t": t, "c": c})
        await conn.execute(
            text(f"INSERT INTO {nombre}.conversations (id, tenant_id, customer_id, connection_id) VALUES (gen_random_uuid(), :t, :u, :c)"),
            {"t": t, "u": uuid.uuid4(), "c": c},
        )
        # Una decisión vieja (legacy, sin release ni conexión) no impide validar el CHECK.
        await conn.execute(
            text(f"INSERT INTO {nombre}.core_decisions (id, tenant_id, created_at) VALUES (gen_random_uuid(), :t, now() - interval '5 days')"),
            {"t": t},
        )

        assert "creado" in await contrato._crear_indice(conn, nombre)
        assert "ya existía" in await contrato._crear_indice(conn, nombre)
        assert "NOT NULL" in await contrato._connection_id_obligatorio(conn, nombre)
        assert "ya era NOT NULL" in await contrato._connection_id_obligatorio(conn, nombre)
        assert "validado" in await contrato._check_de_decisiones(conn, nombre)
        assert "ya existía" in await contrato._check_de_decisiones(conn, nombre)
        assert await contrato._validar_pendientes(conn, nombre) == [f"{nombre}.conversations.fk_conv_connection"]
        assert "quitada" in await contrato._quitar_fk_simple(conn, nombre)
        assert "ya no existía" in await contrato._quitar_fk_simple(conn, nombre)

        # Efectos: un segundo cliente abierto en la misma conexión choca; connection_id es obligatorio.
        cliente = uuid.uuid4()
        insertar = (
            f"INSERT INTO {nombre}.conversations (id, tenant_id, customer_id, connection_id, status) "
            "VALUES (gen_random_uuid(), :t, :u, :c, :s)"
        )
        await conn.execute(text(insertar), {"t": t, "u": cliente, "c": c, "s": "open"})
        for params, error in (
            ({"t": t, "u": cliente, "c": c, "s": "escalated"}, "uq_conversations_open_per_connection"),
            ({"t": t, "u": uuid.uuid4(), "c": None, "s": "open"}, "connection_id"),
        ):
            with pytest.raises(Exception, match=error):
                await conn.execute(text(insertar), params)
        await conn.execute(text(insertar), {"t": t, "u": cliente, "c": c, "s": "closed"})  # cerrada: sí
        # core_decisions: desde el contract, conexión obligatoria y, si fue enrutada, release.
        decision = (
            f"INSERT INTO {nombre}.core_decisions (id, tenant_id, connection_id, release_id, outcome) "
            "VALUES (gen_random_uuid(), :t, :c, :r, :o)"
        )
        await conn.execute(text(decision), {"t": t, "c": c, "r": uuid.uuid4(), "o": "routed"})
        await conn.execute(text(decision), {"t": t, "c": c, "r": None, "o": "blocked"})
        for params in ({"t": t, "c": None, "r": None, "o": "blocked"}, {"t": t, "c": c, "r": None, "o": "routed"}):
            with pytest.raises(Exception, match="ck_core_decisions_contract"):
                await conn.execute(text(decision), params)


async def test_un_indice_invalido_de_un_intento_cortado_se_rehace(esquema) -> None:  # type: ignore[no-untyped-def]
    from core.releases import contrato

    nombre, engine = esquema
    t, c, u = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(text(f"INSERT INTO {nombre}.conexiones VALUES (:t, :c)"), {"t": t, "c": c})
        insertar = (
            f"INSERT INTO {nombre}.conversations (id, tenant_id, customer_id, connection_id) "
            "VALUES (gen_random_uuid(), :t, :u, :c)"
        )
        await conn.execute(text(insertar), {"t": t, "u": u, "c": c})
        await conn.execute(text(insertar), {"t": t, "u": u, "c": c})  # duplicada sin deduplicar
        with pytest.raises(Exception, match="duplicate"):
            await contrato._crear_indice(conn, nombre)  # CONCURRENTLY deja el índice inválido
        invalido = (await conn.execute(text(
            "SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class k ON k.oid = i.indexrelid "
            "WHERE k.relname = :n AND k.relnamespace = cast(:s AS regnamespace)"), {"n": contrato.INDICE, "s": nombre})).scalar_one()
        assert invalido
        await conn.execute(text(f"UPDATE {nombre}.conversations SET status = 'closed' WHERE ctid IN (SELECT ctid FROM {nombre}.conversations LIMIT 1)"))
        assert "creado" in await contrato._crear_indice(conn, nombre)

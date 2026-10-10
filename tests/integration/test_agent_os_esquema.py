"""Esquema aditivo del Agent OS (HU-AOS-02, migraciones 0067-0071) contra el stack real.

Criterios de la HU:
- cada tabla nueva tiene RLS habilitada y FORZADA con la policy de aislamiento, y otro negocio
  (o una sesión sin negocio) ve 0 filas;
- la FK compuesta `(tenant_id, …)` rechaza un permiso o una instancia que apunte al agente de otro
  negocio;
- `purge_tenant` deja en 0 las tablas nuevas con datos;
- triggers: rol de la instancia, conexión conversacional, Coordinador obligatorio con release
  activa, inmutabilidad (`prevent_mutation`) y `pack_versions` con solo `yanked` mutable.

El `downgrade` se prueba aparte, sobre el stack efímero (ver el PR).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Las 21 tablas de negocio de 0069. Append-only: solo SELECT e INSERT para app_runtime.
APPEND_ONLY = {
    "agent_versions",
    "a2a_graph_versions",
    "agent_links",
    "connection_releases",
    "release_components",
    "release_events",
    "task_events",
    "core_decisions",
    "turn_outcomes",
    "a2a_events",
    "turn_feedback",
}
TABLAS_NUEVAS = sorted(
    APPEND_ONLY
    | {
        "agents",
        "agent_drafts",
        "agent_instances",
        "a2a_graphs",
        "pack_installations",
        "conversation_tasks",
        "a2a_conversations",
        "memory_facts",
        "trigger_embeddings",
        "improvement_proposals",
    }
)
HASH = "a" * 64
PACK = "pack_commerce"


def _uniq(prefijo: str) -> str:
    return f"{prefijo}-{uuid.uuid4().hex[:8]}"


@asynccontextmanager
async def _owner() -> AsyncIterator[Any]:
    """Conexión con el rol de migraciones (dueño): para probar triggers por debajo de los GRANT."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.connect() as conn:
            # Si el rol no se salta RLS, los conteos "vacíos" de abajo no probarían nada.
            puede = (
                await conn.execute(
                    text(
                        "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"
                    )
                )
            ).scalar_one()
            assert puede, "el rol de migraciones debe ser superusuario o BYPASSRLS para estos tests"
            await conn.rollback()
            yield conn
    finally:
        await engine.dispose()


@asynccontextmanager
async def _scope(tid: uuid.UUID | None) -> AsyncIterator[Any]:
    """Sesión del runtime (`app_runtime`, RLS) dentro del negocio `tid` (o sin negocio)."""
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


async def _crear_negocio(prefijo: str) -> tuple[uuid.UUID, str]:
    from core.db.session import get_session

    tid, slug = uuid.uuid4(), _uniq(prefijo)
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, :name, :slug)"),
            {"id": tid, "name": f"Agent OS {slug}", "slug": slug},
        )
        # Catálogo de sistema: la fila del pack la crea el arranque de la app; aquí puede faltar.
        await session.execute(
            text(
                "insert into plugins (id, name, version, status) values (:id, :id, '0.0.0', 'detected') "
                "on conflict (id) do nothing"
            ),
            {"id": PACK},
        )
        await session.commit()
    return tid, slug


async def _conexion(session: Any, tid: uuid.UUID, tipo: str = "whatsapp_meta") -> uuid.UUID:
    cc = uuid.uuid4()
    await session.execute(
        text(
            "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id, category) "
            "values (:id, :t, 'whatsapp', 'meta', :ext, :tipo, 'comunicacion')"
        ),
        {"id": cc, "t": tid, "ext": _uniq("ext"), "tipo": tipo},
    )
    return cc


async def _agente(session: Any, tid: uuid.UUID, key: str, kind: str) -> uuid.UUID:
    aid = uuid.uuid4()
    await session.execute(
        text("insert into agents (id, tenant_id, key, kind, name) values (:id, :t, :k, :kind, :k)"),
        {"id": aid, "t": tid, "k": key, "kind": kind},
    )
    return aid


async def _sembrar(tid: uuid.UUID) -> dict[str, uuid.UUID]:
    """Una fila (o más) en CADA tabla nueva del negocio, en una sola transacción."""
    from core.config import get_settings

    ids: dict[str, uuid.UUID] = {
        k: uuid.uuid4()
        for k in (
            "customer",
            "conv",
            "core_v",
            "ventas_v",
            "core_i",
            "ventas_i",
            "graph",
            "graph_v",
            "release",
            "task",
            "turn",
            "a2a",
            "call",
        )
    }
    vector = "[" + ",".join(["0"] * get_settings().embedding_dimensions) + "]"
    async with _scope(tid) as s:
        p = {**ids, "t": tid, "h": HASH}
        cc = await _conexion(s, tid)
        p["cc"] = ids["cc"] = cc
        await s.execute(
            text(
                "insert into customers (id, tenant_id, channel, external_id) values (:customer, :t, 'whatsapp', :ext)"
            ),
            {**p, "ext": _uniq("wa")},
        )
        await s.execute(
            text(
                "insert into conversations (id, tenant_id, customer_id, channel, connection_id, status) "
                "values (:conv, :t, :customer, 'whatsapp', :cc, 'open')"
            ),
            p,
        )
        p["core_a"] = ids["core_a"] = await _agente(s, tid, "coordinador", "core")
        p["ventas_a"] = ids["ventas_a"] = await _agente(s, tid, "ventas", "specialist")
        p["general_a"] = ids["general_a"] = await _agente(s, tid, "general", "specialist")
        for sql in (
            "insert into agent_drafts (tenant_id, agent_id, content) values (:t, :ventas_a, '{}')",
            "insert into agent_versions (id, tenant_id, agent_id, version, label, contract, content, content_hash) "
            "values (:core_v, :t, :core_a, 1, '1.0.0', 'kernelspec/1', '{}', :h)",
            "insert into agent_versions (id, tenant_id, agent_id, version, label, contract, content, content_hash) "
            "values (:ventas_v, :t, :ventas_a, 1, '1.0.0', 'agentspec/3', '{}', :h)",
            "insert into agent_instances (id, tenant_id, connection_id, agent_id, role) "
            "values (:core_i, :t, :cc, :core_a, 'core')",
            "insert into agent_instances (id, tenant_id, connection_id, agent_id, role) "
            "values (:ventas_i, :t, :cc, :ventas_a, 'specialist')",
            "insert into a2a_graphs (id, tenant_id, name) values (:graph, :t, 'grafo')",
            "insert into a2a_graph_versions (id, tenant_id, graph_id, version, content_hash) "
            "values (:graph_v, :t, :graph, 1, :h)",
            "insert into agent_links (tenant_id, graph_version_id, from_agent_id, to_agent_id, modes) "
            "values (:t, :graph_v, :ventas_a, :general_a, ARRAY['handoff'])",
            "insert into pack_installations (tenant_id, pack_id, scope) values (:t, 'pack_commerce', 'tenant')",
            "insert into connection_releases (id, tenant_id, connection_id, number, graph_version_id, manifest, manifest_hash) "
            "values (:release, :t, :cc, 1, :graph_v, '{}', :h)",
            "insert into release_components (tenant_id, connection_id, release_id, agent_instance_id, "
            "agent_id, agent_version_id) values (:t, :cc, :release, :core_i, :core_a, :core_v)",
            "insert into release_components (tenant_id, connection_id, release_id, agent_instance_id, "
            "agent_id, agent_version_id) values (:t, :cc, :release, :ventas_i, :ventas_a, :ventas_v)",
            "insert into release_events (tenant_id, connection_id, release_id, type, actor) "
            "values (:t, :cc, :release, 'built', 'system:test')",
            "update channel_connections set active_release_id = :release where id = :cc",
            "update conversations set release_id = :release, release_lane = 'stable', "
            "active_agent_instance_id = :ventas_i where id = :conv",
            "insert into conversation_tasks (id, tenant_id, conversation_id, procedure_id, procedure_version, "
            "owner_instance_id, stage) values (:task, :t, :conv, 'pack_commerce.contratar_plan', '1.0.0', :ventas_i, 'inicio')",
            "insert into task_events (tenant_id, task_id, seq, type, actor, turn_id) "
            "values (:t, :task, 1, 'started', 'kernel', :turn)",
            "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, outcome, "
            "route_source, active_instance_id, focus_task_id, latency_ms) "
            "values (:turn, :t, :cc, :conv, :release, 'routed', 'task', :ventas_i, :task, 12)",
            "insert into turn_outcomes (turn_id, tenant_id, status, final_instance_id, latency_ms) "
            "values (:turn, :t, 'replied', :ventas_i, 30)",
            "insert into a2a_conversations (id, tenant_id, connection_id, conversation_id, turn_id, root_mode, "
            "initiator_instance_id, max_depth, budget_usd, budget_tokens, deadline_at) "
            "values (:a2a, :t, :cc, :conv, :turn, 'handoff', :ventas_i, 3, 0.02, 20000, now() + interval '15 seconds')",
            "insert into a2a_events (tenant_id, a2a_conversation_id, seq, call_id, type, mode, depth, "
            "from_instance_id, to_instance_id, intent) "
            "values (:t, :a2a, 1, :call, 'requested', 'handoff', 1, :ventas_i, :core_i, 'soporte')",
            "insert into memory_facts (tenant_id, customer_id, namespace, key, value, source, written_by_instance_id) "
            "values (:t, :customer, 'perfil', 'ciudad', '\"Bogotá\"', 'agent', :ventas_i)",
            "insert into turn_feedback (tenant_id, turn_id, conversation_id, source, signal, created_by) "
            "values (:t, :turn, :conv, 'owner', 'thumbs_down', 'system')",
            "insert into improvement_proposals (tenant_id, scope, connection_id, agent_id, kind, evidence, change) "
            "values (:t, 'connection', :cc, :ventas_a, 'trigger_examples', '{}', '{}')",
            # Columnas nuevas de tablas existentes, con sus FKs compuestas (la del turno, diferida).
            "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, "
            "turn_id, agent_instance_id) values (gen_random_uuid(), :t, :conv, 'outbound', 'bot', 'hola', "
            ":turn, :ventas_i)",
            "insert into usage_ledger (id, tenant_id, conversation_id, model, kind, turn_id, connection_id, "
            "agent_instance_id, agent_version_id, release_id) values (gen_random_uuid(), :t, :conv, 'test', "
            "'agent_run', :turn, :cc, :ventas_i, :ventas_v, :release)",
            "insert into audit_log (id, tenant_id, conversation_id, actor, action, turn_id, connection_id, "
            "agent_instance_id) values (gen_random_uuid(), :t, :conv, 'agent', 'test', :turn, :cc, :ventas_i)",
        ):
            await s.execute(text(sql), p)
        await s.execute(
            text(
                "insert into trigger_embeddings (tenant_id, release_id, procedure_id, exemplar_hash, embedding_model, embedding) "
                "values (:t, :release, 'pack_commerce.contratar_plan', :h, 'test', CAST(:v AS vector))"
            ),
            {**p, "v": vector},
        )
        await s.commit()  # los triggers diferidos (Coordinador) se evalúan aquí
    return ids


async def _contar(tabla: str, tid: uuid.UUID | None, filtro: uuid.UUID | None = None) -> int:
    async with _scope(tid) as s:
        sql = f"select count(*) from {tabla}" + (" where tenant_id = :f" if filtro else "")
        return (await s.execute(text(sql), {"f": filtro})).scalar_one()


@pytest.fixture(scope="module")
async def dos_negocios() -> AsyncIterator[dict[str, Any]]:
    """Negocio A con datos en todas las tablas nuevas y negocio B con los suyos; se purgan al final."""
    from core.tenants import purge_tenant

    a, slug_a = await _crear_negocio("aos-a")
    b, slug_b = await _crear_negocio("aos-b")
    datos = {"a": a, "b": b, "ids_a": await _sembrar(a), "ids_b": await _sembrar(b)}
    yield datos
    await purge_tenant(str(a), f"ELIMINAR {slug_a}")
    await purge_tenant(str(b), f"ELIMINAR {slug_b}")


# ---------------------------------------------------------------- RLS y permisos


async def test_tablas_nuevas_con_rls_forzada_policy_y_permisos_minimos() -> None:
    from core.db.session import get_session

    async with get_session() as s:
        filas = (
            await s.execute(
                text(
                    "select c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "  (select string_agg(pg_get_expr(p.polqual, p.polrelid) || '|' || pg_get_expr(p.polwithcheck, p.polrelid), ';') "
                    "     from pg_policy p where p.polrelid = c.oid), "
                    "  has_table_privilege('app_runtime', c.oid, 'UPDATE'), "
                    "  has_table_privilege('app_runtime', c.oid, 'DELETE'), "
                    "  has_table_privilege('app_runtime', c.oid, 'TRUNCATE') "
                    "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'public' and c.relname = any(:tablas)"
                ),
                {"tablas": TABLAS_NUEVAS},
            )
        ).all()
    assert sorted(f[0] for f in filas) == TABLAS_NUEVAS, "existen todas las tablas nuevas"
    for nombre, rls, force, policy, update, delete, truncate in filas:
        assert rls and force, f"{nombre}: RLS habilitada y FORZADA"
        assert policy and policy.count("NULLIF(current_setting('app.tenant_id'") == 2, (
            f"{nombre}: policy de aislamiento con nullif en USING y WITH CHECK ({policy})"
        )
        assert not delete and not truncate, (
            f"{nombre}: app_runtime sin borrado físico (invariante 8)"
        )
        assert update is (nombre not in APPEND_ONLY), (
            f"{nombre}: UPDATE solo en las tablas mutables"
        )


async def test_pack_versions_es_catalogo_de_solo_lectura_para_el_runtime() -> None:
    from core.db.session import get_session

    async with get_session() as s:
        for objeto in ("pack_versions", "agent_templates"):
            privilegios = (
                await s.execute(
                    text(
                        "select has_table_privilege('app_runtime', :o, 'SELECT'), "
                        "has_table_privilege('app_runtime', :o, 'INSERT'), "
                        "has_table_privilege('app_runtime', :o, 'UPDATE'), "
                        "has_table_privilege('app_runtime', :o, 'DELETE')"
                    ),
                    {"o": objeto},
                )
            ).one()
            assert tuple(privilegios) == (True, False, False, False), f"{objeto}: solo SELECT"


async def test_ninguna_vista_de_public_tiene_tenant_id() -> None:
    """`purge_tenant` (0011) trataría una vista con `tenant_id` como tabla y fallaría para todos."""
    from core.db.session import get_session

    async with get_session() as s:
        vistas = (
            (
                await s.execute(
                    text(
                        "select c.table_name from information_schema.columns c "
                        "join information_schema.views v using (table_schema, table_name) "
                        "where c.table_schema = 'public' and c.column_name = 'tenant_id'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert vistas == []


async def test_otro_negocio_y_sin_negocio_ven_cero_filas(dos_negocios: dict[str, Any]) -> None:
    a, b = dos_negocios["a"], dos_negocios["b"]
    for tabla in TABLAS_NUEVAS:
        assert await _contar(tabla, a, a) >= 1, f"{tabla}: el negocio A ve sus filas"
        assert await _contar(tabla, b, a) == 0, f"{tabla}: el negocio B no ve las de A"
        assert await _contar(tabla, None) == 0, f"{tabla}: sin negocio en contexto no se ve nada"


async def test_rls_rechaza_escribir_filas_de_otro_negocio(dos_negocios: dict[str, Any]) -> None:
    a, b = dos_negocios["a"], dos_negocios["b"]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with _scope(b) as s:
            await s.execute(
                text(
                    "insert into agents (tenant_id, key, kind, name) values (:a, 'intruso', 'specialist', 'x')"
                ),
                {"a": a},
            )


# ---------------------------------------------------------------- FKs compuestas


async def test_fk_compuesta_rechaza_permiso_hacia_agente_de_otro_negocio(
    dos_negocios: dict[str, Any],
) -> None:
    b, ids_a, ids_b = dos_negocios["b"], dos_negocios["ids_a"], dos_negocios["ids_b"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(b) as s:
            await s.execute(
                text(
                    "insert into agent_links (tenant_id, graph_version_id, from_agent_id, to_agent_id, modes) "
                    "values (:t, :gv, :desde, :hacia, ARRAY['delegate'])"
                ),
                {
                    "t": b,
                    "gv": ids_b["graph_v"],
                    "desde": ids_b["general_a"],
                    "hacia": ids_a["ventas_a"],
                },
            )


async def test_fk_compuesta_rechaza_instancia_de_agente_de_otro_negocio(
    dos_negocios: dict[str, Any],
) -> None:
    b, ids_a, ids_b = dos_negocios["b"], dos_negocios["ids_a"], dos_negocios["ids_b"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(b) as s:
            await s.execute(
                text(
                    "insert into agent_instances (tenant_id, connection_id, agent_id, role) "
                    "values (:t, :cc, :agente, 'specialist')"
                ),
                {"t": b, "cc": ids_b["cc"], "agente": ids_a["general_a"]},
            )


async def test_fk_compuesta_rechaza_tarea_en_conversacion_de_otro_negocio(
    dos_negocios: dict[str, Any],
) -> None:
    b, ids_a = dos_negocios["b"], dos_negocios["ids_a"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(b) as s:
            await s.execute(
                text(
                    "insert into conversation_tasks (tenant_id, conversation_id, procedure_id, procedure_version, stage) "
                    "values (:t, :conv, 'x.y', '1', 'inicio')"
                ),
                {"t": b, "conv": ids_a["conv"]},
            )


async def test_puntero_de_release_solo_apunta_a_releases_de_su_conexion(
    dos_negocios: dict[str, Any],
) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(a) as s:
            otra = await _conexion(s, a)
            await s.execute(
                text(
                    "update channel_connections set canary_release_id = :r, canary_percent = 5 where id = :cc"
                ),
                {"r": ids_a["release"], "cc": otra},
            )


async def test_el_kind_del_agente_no_cambia_si_tiene_instancias(
    dos_negocios: dict[str, Any],
) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(a) as s:
            await s.execute(
                text("update agents set kind = 'backoffice' where id = :ag"),
                {"ag": ids_a["ventas_a"]},
            )


async def test_componente_de_release_es_de_su_conexion_y_de_su_agente(
    dos_negocios: dict[str, Any],
) -> None:
    """La versión de otro agente o la instancia de otra conexión no entran en la release."""
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(a) as s:
            nueva = uuid.uuid4()  # release 2 (se revierte con la transacción)
            await s.execute(
                text(
                    "insert into connection_releases (id, tenant_id, connection_id, number, "
                    "manifest, manifest_hash) values (:r, :t, :cc, 2, '{}', :h)"
                ),
                {"r": nueva, "t": a, "cc": ids_a["cc"], "h": "b" * 64},
            )
            await s.execute(
                text(
                    "insert into release_components (tenant_id, connection_id, release_id, "
                    "agent_instance_id, agent_id, agent_version_id) "
                    "values (:t, :cc, :nueva, :ventas_i, :ventas_a, :core_v)"
                ),
                {**ids_a, "t": a, "nueva": nueva},
            )
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(a) as s:
            otra = await _conexion(s, a)
            instancia = uuid.uuid4()
            await s.execute(
                text(
                    "insert into agent_instances (id, tenant_id, connection_id, agent_id, role) "
                    "values (:i, :t, :cc, :ag, 'specialist')"
                ),
                {"i": instancia, "t": a, "cc": otra, "ag": ids_a["general_a"]},
            )
            await s.execute(
                text(
                    "insert into release_components (tenant_id, connection_id, release_id, "
                    "agent_instance_id, agent_id, agent_version_id) "
                    "values (:t, :cc, :release, :i, :ag, :v)"
                ),
                {
                    "t": a,
                    "cc": ids_a["cc"],
                    "release": ids_a["release"],
                    "i": instancia,
                    "ag": ids_a["general_a"],
                    "v": ids_a["ventas_v"],
                },
            )


async def test_el_duenio_de_la_conversacion_es_una_instancia_de_su_conexion(
    dos_negocios: dict[str, Any],
) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(a) as s:
            otra = await _conexion(s, a)
            instancia = uuid.uuid4()
            await s.execute(
                text(
                    "insert into agent_instances (id, tenant_id, connection_id, agent_id, role) "
                    "values (:i, :t, :cc, :ag, 'specialist')"
                ),
                {"i": instancia, "t": a, "cc": otra, "ag": ids_a["general_a"]},
            )
            await s.execute(
                text("update conversations set active_agent_instance_id = :i where id = :conv"),
                {"i": instancia, "conv": ids_a["conv"]},
            )


# ---------------------------------------------------------------- triggers


async def test_rol_de_la_instancia_coincide_con_el_agente(dos_negocios: dict[str, Any]) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(a) as s:
            otra = await _conexion(s, a)
            await s.execute(
                text(
                    "insert into agent_instances (tenant_id, connection_id, agent_id, role) values (:t, :cc, :ag, 'core')"
                ),
                {"t": a, "cc": otra, "ag": ids_a["ventas_a"]},
            )


async def test_solo_las_conexiones_conversacionales_tienen_agentes(
    dos_negocios: dict[str, Any],
) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(DBAPIError, match="no es conversacional"):
        async with _scope(a) as s:
            calendario = await _conexion(s, a, tipo="google_calendar")
            await s.execute(
                text(
                    "insert into agent_instances (tenant_id, connection_id, agent_id, role) values (:t, :cc, :ag, 'specialist')"
                ),
                {"t": a, "cc": calendario, "ag": ids_a["ventas_a"]},
            )


async def test_la_instancia_no_cambia_de_conexion_ni_de_agente(
    dos_negocios: dict[str, Any],
) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(DBAPIError, match="no cambian"):
        async with _scope(a) as s:
            await s.execute(
                text("update agent_instances set agent_id = :ag where id = :i"),
                {"ag": ids_a["general_a"], "i": ids_a["ventas_i"]},
            )


async def test_release_activa_exige_coordinador_al_confirmar(dos_negocios: dict[str, Any]) -> None:
    a = dos_negocios["a"]
    rel = uuid.uuid4()
    with pytest.raises(DBAPIError, match="release activa sin Coordinador"):
        async with _scope(a) as s:
            cc = await _conexion(s, a)
            await s.execute(
                text(
                    "insert into connection_releases (id, tenant_id, connection_id, number, manifest, manifest_hash) "
                    "values (:r, :t, :cc, 1, '{}', :h)"
                ),
                {"r": rel, "t": a, "cc": cc, "h": HASH},
            )
            await s.execute(
                text("update channel_connections set active_release_id = :r where id = :cc"),
                {"r": rel, "cc": cc},
            )
            await s.commit()  # constraint trigger diferido


async def test_no_se_deshabilita_el_unico_coordinador_con_release_activa(
    dos_negocios: dict[str, Any],
) -> None:
    a, ids_a = dos_negocios["a"], dos_negocios["ids_a"]
    with pytest.raises(DBAPIError, match="sin Coordinador"):
        async with _scope(a) as s:
            await s.execute(
                text("update agent_instances set enabled = false where id = :i"),
                {"i": ids_a["core_i"]},
            )
            await s.commit()
    async with _scope(a) as s:  # el especialista sí se deshabilita
        await s.execute(
            text("update agent_instances set enabled = false where id = :i"),
            {"i": ids_a["ventas_i"]},
        )
        await s.execute(
            text("update agent_instances set enabled = true where id = :i"),
            {"i": ids_a["ventas_i"]},
        )
        await s.commit()


async def test_tablas_append_only_rechazan_update_y_delete(dos_negocios: dict[str, Any]) -> None:
    """Debajo de los GRANT (el runtime ni siquiera tiene UPDATE/DELETE), el trigger también frena."""
    ids_a = dos_negocios["ids_a"]
    async with _owner() as conn:
        for sql in (
            "update task_events set actor = 'human' where task_id = :task",
            "delete from task_events where task_id = :task",
            "update agent_versions set notes = 'x' where id = :core_v",
            "delete from connection_releases where id = :release",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                async with conn.begin():
                    await conn.execute(text(sql), ids_a)


async def test_pack_versions_solo_cambia_yanked() -> None:
    """Todo en una transacción que se revierte: la tabla es inmutable y no se ensucia la base."""
    version = _uniq("0.0.0-test")
    async with _owner() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(
                text(
                    "insert into plugins (id, name, version, status) values (:p, :p, '0.0.0', 'detected') "
                    "on conflict (id) do nothing"
                ),
                {"p": PACK},
            )
            await conn.execute(
                text(
                    "insert into pack_versions (pack_id, version, manifest, manifest_hash) values (:p, :v, :m, :h)"
                ),
                {
                    "p": PACK,
                    "v": version,
                    "h": HASH,
                    "m": '{"provides": {"agent_templates": [{"key": "ventas", "name": "Ventas"}]}}',
                },
            )
            plantillas = (
                (
                    await conn.execute(
                        text(
                            "select template_key from agent_templates where pack_id = :p and version = :v"
                        ),
                        {"p": PACK, "v": version},
                    )
                )
                .scalars()
                .all()
            )
            assert plantillas == ["ventas"]
            for sql in (
                "update pack_versions set manifest = '{}' where version = :v",
                "delete from pack_versions where version = :v",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with conn.begin_nested():
                        await conn.execute(text(sql), {"v": version})
            await conn.execute(
                text("update pack_versions set yanked = true where version = :v"), {"v": version}
            )
            assert (
                await conn.execute(
                    text("select count(*) from agent_templates where version = :v"), {"v": version}
                )
            ).scalar_one() == 0, "una versión retirada no ofrece plantillas"
            with pytest.raises(DBAPIError, match="no se restaura"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("update pack_versions set yanked = false where version = :v"),
                        {"v": version},
                    )
        finally:
            await tx.rollback()


async def test_banderas_por_negocio_respetan_el_orden(dos_negocios: dict[str, Any]) -> None:
    a = dos_negocios["a"]
    async with _owner() as conn:
        async with conn.begin():
            fila = (
                await conn.execute(
                    text("select aos_runtime, aos_procedures from tenants where id = :t"), {"t": a}
                )
            ).one()
            assert tuple(fila) == ("legacy", False), "todo negocio nace en legacy"
        with pytest.raises(IntegrityError, match="ck_tenants_aos_order"):
            async with conn.begin():
                await conn.execute(
                    text("update tenants set aos_procedures = true where id = :t"), {"t": a}
                )


async def test_turno_de_otro_negocio_se_rechaza_al_confirmar(dos_negocios: dict[str, Any]) -> None:
    """La FK al turno es diferida: el rechazo llega en el COMMIT."""
    b, ids_a, ids_b = dos_negocios["b"], dos_negocios["ids_a"], dos_negocios["ids_b"]
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"):
        async with _scope(b) as s:
            await s.execute(
                text(
                    "insert into messages (id, tenant_id, conversation_id, direction, sender_type, "
                    "content, turn_id) values (gen_random_uuid(), :t, :conv, 'outbound', 'bot', 'x', :turn)"
                ),
                {"t": b, "conv": ids_b["conv"], "turn": ids_a["turn"]},
            )
            await s.commit()


async def test_plugin_privado_de_otro_negocio_no_se_instala(dos_negocios: dict[str, Any]) -> None:
    a, b = dos_negocios["a"], dos_negocios["b"]
    plugin = _uniq("plugin_privado").replace("-", "_")
    async with _owner() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "insert into plugins (id, name, version, status, owner_tenant_id) "
                    "values (:p, :p, '0.0.0', 'detected', :a)"
                ),
                {"p": plugin, "a": a},
            )
    try:
        with pytest.raises(DBAPIError, match="privado de otro negocio"):
            async with _scope(b) as s:
                await s.execute(
                    text(
                        "insert into pack_installations (tenant_id, pack_id, scope) values (:t, :p, 'tenant')"
                    ),
                    {"t": b, "p": plugin},
                )
        async with _scope(a) as s:  # su dueño sí lo instala
            await s.execute(
                text(
                    "insert into pack_installations (tenant_id, pack_id, scope) values (:t, :p, 'tenant')"
                ),
                {"t": a, "p": plugin},
            )
            await s.rollback()
    finally:
        async with _owner() as conn:
            async with conn.begin():
                await conn.execute(text("delete from plugins where id = :p"), {"p": plugin})


async def test_la_configuracion_de_una_instalacion_va_cifrada(dos_negocios: dict[str, Any]) -> None:
    a = dos_negocios["a"]
    with pytest.raises(IntegrityError, match="ck_pack_installations_config_cifrada"):
        async with _scope(a) as s:
            await s.execute(
                text(
                    "insert into pack_installations (tenant_id, pack_id, scope, connection_id, config_encrypted) "
                    "values (:t, 'pack_commerce', 'connection', :cc, '{\"api_key\": \"en claro\"}')"
                ),
                {"t": a, "cc": dos_negocios["ids_a"]["cc"]},
            )


async def test_clave_de_webchat_no_se_reutiliza_ni_borrada(dos_negocios: dict[str, Any]) -> None:
    """El índice global incluye las filas borradas: una clave pública no pasa a otro negocio."""
    a, b = dos_negocios["a"], dos_negocios["b"]
    clave = _uniq("widget")
    async with _scope(a) as s:
        await s.execute(
            text(
                "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id, "
                "category, deleted_at) values (gen_random_uuid(), :t, 'web', 'web', :k, 'webchat', "
                "'comunicacion', now())"
            ),
            {"t": a, "k": clave},
        )
        await s.commit()
    with pytest.raises(IntegrityError, match="uq_cc_webchat_key"):
        async with _scope(b) as s:
            await s.execute(
                text(
                    "insert into channel_connections (id, tenant_id, channel, provider, external_id, "
                    "type_id, category) values (gen_random_uuid(), :t, 'web', 'web', :k, 'webchat', "
                    "'comunicacion')"
                ),
                {"t": b, "k": clave},
            )


async def test_truncate_tambien_se_rechaza_en_las_append_only() -> None:
    """Siempre en una transacción que se revierte: si el trigger faltara, no se borra nada."""
    async with _owner() as conn:
        for tabla in ("task_events", "a2a_events", "pack_versions"):
            tx = await conn.begin()
            try:
                with pytest.raises(DBAPIError, match="append-only"):
                    await conn.execute(text(f"truncate {tabla}"))
            finally:
                await tx.rollback()


async def test_funciones_de_trigger_con_search_path_y_vista_security_invoker() -> None:
    from core.db.session import get_session

    funciones = (
        "prevent_mutation",
        "agent_instance_checks",
        "connection_requires_core",
        "core_instance_required",
        "pack_versions_yank_irreversible",
        "pack_installation_owner_check",
    )
    async with get_session() as s:
        filas = (
            await s.execute(
                text("select proname, proconfig from pg_proc where proname = any(:f)"),
                {"f": list(funciones)},
            )
        ).all()
        opciones = (
            await s.execute(
                text("select reloptions from pg_class where relname = 'agent_templates'")
            )
        ).scalar_one()
    assert sorted(f[0] for f in filas) == sorted(funciones)
    for nombre, config in filas:
        assert config and "search_path=pg_catalog, public, pg_temp" in config, nombre
    assert opciones and "security_invoker=true" in opciones


# ---------------------------------------------------------------- purge_tenant


async def test_purge_tenant_borra_las_tablas_nuevas_con_datos() -> None:
    from core.tenants import purge_tenant

    tid, slug = await _crear_negocio("aos-purge")
    await _sembrar(tid)
    for tabla in TABLAS_NUEVAS:
        assert await _contar(tabla, tid, tid) >= 1, f"{tabla}: hay datos antes de purgar"

    await purge_tenant(str(tid), f"ELIMINAR {slug}")

    async with _owner() as conn:  # el dueño ve todo: confirma que no quedó nada
        async with conn.begin():
            for tabla in [*TABLAS_NUEVAS, "channel_connections", "conversations", "customers"]:
                quedan = (
                    await conn.execute(
                        text(f"select count(*) from {tabla} where tenant_id = :t"), {"t": tid}
                    )
                ).scalar_one()
                assert quedan == 0, f"{tabla}: purge_tenant no dejó filas"
            assert (
                await conn.execute(text("select count(*) from tenants where id = :t"), {"t": tid})
            ).scalar_one() == 0

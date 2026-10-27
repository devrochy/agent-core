"""SPEC-40 · modelo de las opciones directas con Postgres real: RLS, unicidad, inmutabilidad, purga y bandera.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

HASH = "a" * 64


@asynccontextmanager
async def _scope(tid: uuid.UUID | None) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


@asynccontextmanager
async def _owner(tid: uuid.UUID) -> AsyncIterator[Any]:
    """Conexión del rol dueño (migraciones): salta los GRANT, no el trigger ni la RLS forzada."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
            yield conn
    finally:
        await engine.dispose()


async def _negocio(prefijo: str) -> tuple[uuid.UUID, str, uuid.UUID]:
    """(tenant, slug, conversación) de un negocio nuevo."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import Conversation, Customer

    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Opciones {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    async with _scope(tid) as s:
        cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"opc:{uuid.uuid4().hex[:8]}")
        s.add(cliente)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
        s.add(conv)
        await s.flush()
        return tid, slug, conv.id


async def _limpiar(tid: uuid.UUID, slug: str) -> None:
    from core.tenants import deactivate_tenant, purge_tenant

    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _plantilla_publicada(tid: uuid.UUID, conv_id: uuid.UUID, clave: str = "aviso") -> dict[str, uuid.UUID]:
    """Una plantilla, una versión publicada, un envío y una activación."""
    from core.db.models import OptionActivation, OptionSend, OptionTemplate, OptionTemplateVersion

    async with _scope(tid) as s:
        plantilla = OptionTemplate(tenant_id=tid, key=clave, name="Aviso", status="published", current_version_n=1)
        s.add(plantilla)
        await s.flush()
        version = OptionTemplateVersion(
            tenant_id=tid, option_template_id=plantilla.id, version_n=1, definition={"body": "x"}, definition_hash=HASH
        )
        s.add(version)
        await s.flush()
        envio = OptionSend(
            tenant_id=tid, option_template_id=plantilla.id, version_id=version.id, conversation_id=conv_id,
            via="interactive", expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        s.add(envio)
        await s.flush()
        activacion = OptionActivation(tenant_id=tid, send_id=envio.id, option_id="estado", dedupe_key=f"{envio.id}:estado")
        s.add(activacion)
        await s.flush()
        return {"plantilla": plantilla.id, "version": version.id, "envio": envio.id, "activacion": activacion.id}


async def test_rls_aisla_las_cuatro_tablas_entre_negocios() -> None:
    a = await _negocio("opc-rls-a")
    b = await _negocio("opc-rls-b")
    try:
        ids = await _plantilla_publicada(a[0], a[2])
        tablas = ("option_templates", "option_template_versions", "option_sends", "option_activations")
        async with _scope(b[0]) as s:
            for t in tablas:
                assert (await s.execute(text(f"select count(*) from {t}"))).scalar_one() == 0, t
        async with _scope(a[0]) as s:
            for t in tablas:
                assert (await s.execute(text(f"select count(*) from {t}"))).scalar_one() == 1, t
        # sin negocio en contexto falla cerrado: 0 filas, no error
        async with _scope(None) as s:
            assert (await s.execute(text("select count(*) from option_sends"))).scalar_one() == 0
        # escribir una fila de otro negocio lo rechaza el WITH CHECK
        from core.db.models import OptionTemplate

        with pytest.raises((DBAPIError, IntegrityError)):
            async with _scope(b[0]) as s:
                s.add(OptionTemplate(tenant_id=a[0], key="intruso"))
                await s.flush()
        assert ids
    finally:
        await _limpiar(a[0], a[1])
        await _limpiar(b[0], b[1])


async def test_las_fk_compuestas_impiden_cruzar_negocios() -> None:
    from core.db.models import OptionSend

    a = await _negocio("opc-fk-a")
    b = await _negocio("opc-fk-b")
    try:
        ids = await _plantilla_publicada(a[0], a[2])
        # un envío del negocio B que apunta a la versión del negocio A (con la conversación de B)
        with pytest.raises(IntegrityError):
            async with _scope(b[0]) as s:
                s.add(
                    OptionSend(
                        tenant_id=b[0], option_template_id=ids["plantilla"], version_id=ids["version"], conversation_id=b[2],
                        via="text", expires_at=datetime.now(UTC),
                    )
                )
                await s.flush()
        # y uno del negocio A con la conversación del B
        with pytest.raises(IntegrityError):
            async with _scope(a[0]) as s:
                s.add(
                    OptionSend(
                        tenant_id=a[0], option_template_id=ids["plantilla"], version_id=ids["version"], conversation_id=b[2],
                        via="text", expires_at=datetime.now(UTC),
                    )
                )
                await s.flush()
    finally:
        await _limpiar(a[0], a[1])
        await _limpiar(b[0], b[1])


async def test_unicidad_de_clave_de_dedupe_wamid_y_pendiente() -> None:
    from core.db.models import OptionActivation, OptionTemplate

    tid, slug, conv = await _negocio("opc-uq")
    try:
        ids = await _plantilla_publicada(tid, conv)

        async def activacion(**kw: Any) -> None:
            async with _scope(tid) as s:
                s.add(OptionActivation(tenant_id=tid, send_id=ids["envio"], **kw))
                await s.flush()

        # mismo dedupe_key
        with pytest.raises(IntegrityError):
            await activacion(option_id="otra", dedupe_key=f"{ids['envio']}:estado")
        # una sola pendiente por (envío, opción), aunque cambie el dedupe_key
        with pytest.raises(IntegrityError):
            await activacion(option_id="estado", dedupe_key="distinta")
        # terminada la primera, otra activación de la misma opción sí cabe
        async with _scope(tid) as s:
            await s.execute(text("update option_activations set state='done' where id=:i"), {"i": ids["activacion"]})
        await activacion(option_id="estado", dedupe_key="segunda", inbound_wamid="wamid.1")
        # el mismo wamid entrante no se repite (reintento del webhook de Meta)
        with pytest.raises(IntegrityError):
            await activacion(option_id="otra", dedupe_key="tercera", inbound_wamid="wamid.1")
        # estado fuera del CHECK
        with pytest.raises(IntegrityError):
            await activacion(option_id="otra", dedupe_key="cuarta", state="inventado")
        # clave de plantilla única entre las vivas, reutilizable tras el borrado lógico
        with pytest.raises(IntegrityError):
            async with _scope(tid) as s:
                s.add(OptionTemplate(tenant_id=tid, key="aviso"))
                await s.flush()
        async with _scope(tid) as s:
            await s.execute(text("update option_templates set deleted_at=now() where id=:i"), {"i": ids["plantilla"]})
            s.add(OptionTemplate(tenant_id=tid, key="aviso"))
    finally:
        await _limpiar(tid, slug)


async def test_una_version_publicada_es_inmutable_salvo_meta_y_retiro() -> None:
    tid, slug, conv = await _negocio("opc-inm")
    try:
        ids = await _plantilla_publicada(tid, conv)
        v = {"i": ids["version"]}
        for sql in (
            "update option_template_versions set definition = '{\"body\":\"otro\"}' where id=:i",
            f"update option_template_versions set definition_hash = '{'b' * 64}' where id=:i",
            "update option_template_versions set version_n = 2 where id=:i",
            "update option_template_versions set published_at = now() - interval '1 day' where id=:i",
            "delete from option_template_versions where id=:i",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                async with _owner(tid) as conn:  # ni el dueño de la tabla puede
                    await conn.execute(text(sql), v)
        with pytest.raises(DBAPIError, match="append-only"):
            async with _owner(tid) as conn:
                await conn.execute(text("truncate option_template_versions cascade"))  # sin cascade lo frena la FK
        # el rol de la app ni siquiera tiene DELETE
        with pytest.raises(DBAPIError, match="permission denied"):
            async with _scope(tid) as s:
                await s.execute(text("delete from option_template_versions where id=:i"), v)
        # lo que sí cambia: el retiro (y el vínculo con Meta, que se fija DESPUÉS de congelar)
        async with _scope(tid) as s:
            await s.execute(text("update option_template_versions set retired_at = now() where id=:i"), v)
        async with _scope(tid) as s:
            fila = (await s.execute(text("select retired_at, definition->>'body' from option_template_versions where id=:i"), v)).one()
        assert fila[0] is not None and fila[1] == "x"
    finally:
        await _limpiar(tid, slug)


async def test_purge_tenant_elimina_las_cuatro_tablas_y_solo_las_de_ese_negocio() -> None:
    a = await _negocio("opc-purga-a")
    b = await _negocio("opc-purga-b")
    try:
        await _plantilla_publicada(a[0], a[2])
        await _plantilla_publicada(b[0], b[2])
        await _limpiar(a[0], a[1])  # purga con filas inmutables y FKs compuestas: no se atasca
        tablas = ("option_templates", "option_template_versions", "option_sends", "option_activations")
        async with _scope(a[0]) as s:
            for t in tablas:
                assert (await s.execute(text(f"select count(*) from {t} where tenant_id = :t"), {"t": a[0]})).scalar_one() == 0
        async with _scope(b[0]) as s:
            for t in tablas:
                assert (await s.execute(text(f"select count(*) from {t}"))).scalar_one() == 1, t
    finally:
        await _limpiar(b[0], b[1])


async def test_el_runtime_no_puede_borrar_ninguna_tabla_del_modulo() -> None:
    tid, slug, conv = await _negocio("opc-grant")
    try:
        await _plantilla_publicada(tid, conv)
        for t in ("option_templates", "option_sends", "option_activations"):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with _scope(tid) as s:
                    await s.execute(text(f"delete from {t}"))
    finally:
        await _limpiar(tid, slug)


async def test_la_bandera_nace_apagada_y_solo_fijar_la_enciende() -> None:
    from core.kernel.opciones import bandera

    tid, slug, _ = await _negocio("opc-bandera")
    try:
        async with _scope(tid) as s:
            assert await bandera.habilitada(s, tid) is False
        with pytest.raises(PermissionError):  # fuera del tenant_scope del negocio
            await bandera.fijar(tid, True, actor="admin:master@test")
        async with _scope(tid):
            assert await bandera.fijar(tid, True, actor="admin:master@test") is True
        async with _scope(tid) as s:
            assert await bandera.habilitada(s, tid) is True
            # no pisa el resto de `settings` ni las banderas del Agent OS
            ajustes = (await s.execute(text("select settings from tenants where id=:t"), {"t": tid})).scalar_one()
            assert ajustes["opciones_directas"] == {"habilitada": True}
            auditoria = (
                await s.execute(text("select count(*) from audit_log where action='opciones_directas_bandera'"))
            ).scalar_one()
            assert auditoria == 1
        async with _scope(tid):
            await bandera.fijar(tid, False, actor="admin:master@test")
        async with _scope(tid) as s:
            assert await bandera.habilitada(s, tid) is False
    finally:
        await _limpiar(tid, slug)

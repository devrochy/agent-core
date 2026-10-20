"""Pack de conocimiento, parte 1 (ADR-0145 §1, §3 y §7, HU-AOS-49) contra el stack real.

Tests de aislamiento que exige la parte 49-1:
- las dos tablas tienen RLS habilitada y FORZADA, con la policy en `USING` y `WITH CHECK`;
- como `app_runtime`, otro negocio o una sesión sin negocio ve 0 filas, y sin negocio no inserta;
- la FK compuesta rechaza un fragmento que apunte al documento de otro negocio;
- los índices btree empiezan por `tenant_id`;
- la fila `embedding` del ledger toma el tenant del scope;
- `tsv` usa `public.es_unaccent` («envio» encuentra «envío») y el vector se ordena por coseno.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TABLAS = ("pack_knowledge_documents", "pack_knowledge_chunks")


@asynccontextmanager
async def _scope(tid: uuid.UUID | None) -> AsyncIterator[Any]:
    """Sesión del runtime (`app_runtime`, RLS) dentro del negocio `tid` (o sin negocio)."""
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


async def _negocio() -> uuid.UUID:
    from core.db.session import get_session

    tid, slug = uuid.uuid4(), f"kb-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, :name, :slug)"),
            {"id": tid, "name": f"Conocimiento {slug}", "slug": slug},
        )
        await session.commit()
    return tid


async def _documento(tid: uuid.UUID, texto: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Un documento `listo` con un fragmento embebido con `hash`, en el negocio `tid`."""
    from pack_knowledge.models import KnowledgeChunk, KnowledgeDocument

    from core.llm.embeddings import vector_hash

    doc_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    async with _scope(tid) as s:
        s.add(
            KnowledgeDocument(
                id=doc_id,
                agent_id=uuid.uuid4(),
                titulo="Preguntas frecuentes",
                mime="text/plain",
                bytes=len(texto),
                sha256=hashlib.sha256(texto.encode()).hexdigest(),
                estado="listo",
            )
        )
        await s.flush()
        s.add(
            KnowledgeChunk(
                id=chunk_id, document_id=doc_id, ordinal=0, contenido=texto, embedding=vector_hash(texto)
            )
        )
        await s.commit()
    return doc_id, chunk_id


async def test_rls_habilitada_y_forzada_con_policy() -> None:
    async with _scope(None) as s:
        filas = (
            await s.execute(
                text(
                    "select c.relname, c.relrowsecurity, c.relforcerowsecurity, p.qual, p.with_check "
                    "from pg_class c left join pg_policies p "
                    "  on p.tablename = c.relname and p.policyname = 'tenant_isolation' "
                    "where c.relname = any(:t) and c.relkind = 'r'"
                ),
                {"t": list(TABLAS)},
            )
        ).all()
    assert {f[0] for f in filas} == set(TABLAS)
    for nombre, habilitada, forzada, qual, with_check in filas:
        assert habilitada and forzada, nombre
        for expr in (qual, with_check):
            assert expr is not None, nombre
            plano = " ".join(expr.split())
            assert "tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid" in plano, (
                nombre,
                plano,
            )


async def test_app_runtime_sin_delete() -> None:
    # Borrado lógico (invariante nº8): la purga va por `purge_tenant`, no por el runtime.
    async with _scope(None) as s:
        for tabla in TABLAS:
            permisos = (
                await s.execute(
                    text(
                        "select has_table_privilege('app_runtime', :t, 'SELECT'), "
                        "has_table_privilege('app_runtime', :t, 'INSERT'), "
                        "has_table_privilege('app_runtime', :t, 'UPDATE'), "
                        "has_table_privilege('app_runtime', :t, 'DELETE'), "
                        "has_table_privilege('app_runtime', :t, 'TRUNCATE')"
                    ),
                    {"t": tabla},
                )
            ).one()
            assert tuple(permisos) == (True, True, True, False, False), tabla


async def test_indices_btree_empiezan_por_tenant() -> None:
    async with _scope(None) as s:
        filas = (
            await s.execute(
                text(
                    "select i.relname, a.attname from pg_index x "
                    "join pg_class t on t.oid = x.indrelid "
                    "join pg_class i on i.oid = x.indexrelid "
                    "join pg_am am on am.oid = i.relam "
                    "join pg_attribute a on a.attrelid = t.oid and a.attnum = x.indkey[0] "
                    "where t.relname = any(:t) and am.amname = 'btree' and not x.indisprimary"
                ),
                {"t": list(TABLAS)},
            )
        ).all()
    assert filas, "se esperaban índices btree"
    assert all(col == "tenant_id" for _, col in filas), filas


async def test_otro_negocio_y_sin_negocio_no_ven_nada() -> None:
    from pack_knowledge.models import KnowledgeChunk, KnowledgeDocument

    a, b = await _negocio(), await _negocio()
    await _documento(a, "Hacemos envíos a todo el país")

    for tid, esperado in ((a, 1), (b, 0), (None, 0)):
        async with _scope(tid) as s:
            for modelo in (KnowledgeDocument, KnowledgeChunk):
                n = (await s.execute(select(func.count()).select_from(modelo))).scalar_one()
                assert n == esperado, (tid, modelo.__tablename__)


async def test_sin_negocio_no_inserta() -> None:
    async with _scope(None) as s:
        with pytest.raises(DBAPIError):
            await s.execute(
                text(
                    "insert into pack_knowledge_documents (id, tenant_id, agent_id, titulo, mime, bytes, sha256) "
                    "values (:id, :t, :a, 'x', 'text/plain', 1, :h)"
                ),
                {"id": uuid.uuid4(), "t": uuid.uuid4(), "a": uuid.uuid4(), "h": "a" * 64},
            )


async def test_with_check_rechaza_escribir_en_otro_negocio() -> None:
    a, b = await _negocio(), await _negocio()
    doc_de_a, _ = await _documento(a, "Horario de atención")
    with pytest.raises(DBAPIError):
        async with _scope(a) as s:
            await s.execute(
                text(
                    "insert into pack_knowledge_documents (id, tenant_id, agent_id, titulo, mime, bytes, sha256) "
                    "values (:id, :t, :a, 'x', 'text/plain', 1, :h)"
                ),
                {"id": uuid.uuid4(), "t": b, "a": uuid.uuid4(), "h": "b" * 64},
            )
    with pytest.raises(DBAPIError):
        async with _scope(a) as s:
            await s.execute(
                text("update pack_knowledge_documents set tenant_id = :b where id = :d"), {"b": b, "d": doc_de_a}
            )


async def test_fk_compuesta_rechaza_el_documento_de_otro_negocio() -> None:
    from pack_knowledge.models import KnowledgeChunk

    a, b = await _negocio(), await _negocio()
    doc_de_a, _ = await _documento(a, "Política de cambios: 30 días")
    with pytest.raises(IntegrityError, match="fk_pack_knowledge_chunks_document"):
        async with _scope(b) as s:
            s.add(KnowledgeChunk(document_id=doc_de_a, ordinal=0, contenido="intruso"))
            await s.flush()


async def test_tsv_sin_tildes_y_orden_por_coseno() -> None:
    from pack_knowledge.models import KnowledgeChunk

    from core.llm.embeddings import vector_hash

    tid = await _negocio()
    _, envio = await _documento(tid, "Política de envío: gratis desde 100.000 pesos")
    _, horario = await _documento(tid, "Atendemos de lunes a viernes de 8 a 5")
    async with _scope(tid) as s:
        lexico = (
            await s.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.tsv.op("@@")(func.websearch_to_tsquery(text("'public.es_unaccent'"), "envio"))
                )
            )
        ).scalars().all()
        assert lexico == [envio]
        cercano = (
            await s.execute(
                select(KnowledgeChunk.id)
                .order_by(KnowledgeChunk.embedding.cosine_distance(vector_hash("¿cuánto cuesta el envío?")))
                .limit(1)
            )
        ).scalar_one()
        assert cercano == envio != horario


async def test_fila_embedding_del_ledger_toma_el_tenant_del_scope() -> None:
    from core.db.models import UsageLedger
    from core.llm.embeddings import embeber_conocimiento
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    doc = uuid.uuid4()
    async with tenant_scope(a):
        await embeber_conocimiento(["uno", "dos"], proposito="ingesta", document_id=doc)
        await embeber_conocimiento(["una consulta"], proposito="consulta")
    async with _scope(a) as s:
        filas = (await s.execute(select(UsageLedger).where(UsageLedger.kind == "embedding"))).scalars().all()
    assert len(filas) == 1
    fila = filas[0]
    assert fila.tenant_id == a
    assert fila.conversation_id is None
    assert fila.result == {"proposito": "ingesta", "document_id": str(doc)}
    assert fila.credits == 0
    async with _scope(b) as s:
        assert (await s.execute(select(func.count()).select_from(UsageLedger))).scalar_one() == 0

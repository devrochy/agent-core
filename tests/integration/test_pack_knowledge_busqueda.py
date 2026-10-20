"""Búsqueda del pack de conocimiento contra el stack real (ADR-0145 §1, §4 y §7; HU-AOS-49 parte 3).

Tests de aislamiento que exige la parte 49-3:
- dos negocios con **el mismo texto**: cero fragmentos cruzados, en los dos modos;
- volumen desigual: un negocio chico sigue recibiendo sus fragmentos aunque otro tenga miles;
- los documentos de otro agente del mismo negocio, los borrados y los que no están `listo` no
  aparecen.

Golden set de recuperación (`evals/conocimiento.py`): con la grabación de `text-embedding-3-small`
exige `recall@4` ≥ 0,90, MRR ≥ 0,70, ≥ 8 de 10 preguntas sin respuesta en cero, y la híbrida no
por debajo de la semántica. Sin grabación se salta con el comando para grabarla (con OK del dueño).
Con `hash` (el entorno de test) la parte léxica sola tiene que recuperar ≥ 0,85.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]


def _eval() -> Any:
    spec = importlib.util.spec_from_file_location("eval_conocimiento", RAIZ / "evals" / "conocimiento.py")
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    # `@dataclass` resuelve las anotaciones por `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def raiz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from core.config import get_settings

    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _negocio_con_agentes(n: int = 1) -> tuple[uuid.UUID, list[uuid.UUID]]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, slug = uuid.uuid4(), f"kb-b-{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, :n, :s)"), {"id": tid, "n": slug, "s": slug}
        )
    agentes = [uuid.uuid4() for _ in range(n)]
    async with tenant_scope(tid):
        async with get_session() as session:
            for i, aid in enumerate(agentes):
                await session.execute(
                    text("insert into agents (id, tenant_id, key, kind, name) values (:id, :t, :k, 'specialist', :k)"),
                    {"id": aid, "t": tid, "k": f"agente_{i}"},
                )
    return tid, agentes


async def _documento(tid: uuid.UUID, aid: uuid.UUID, textos: list[str], *, estado: str = "listo", borrado: bool = False) -> uuid.UUID:
    """Un documento con sus fragmentos ya embebidos con `hash`, directo en la base."""
    from datetime import UTC, datetime

    from pack_knowledge.models import KnowledgeChunk, KnowledgeDocument

    from core.db.session import get_session
    from core.llm.embeddings import vector_hash
    from core.shared.tenant import tenant_scope

    doc = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                KnowledgeDocument(
                    id=doc,
                    agent_id=aid,
                    titulo=f"Doc {doc.hex[:4]}",
                    mime="text/plain",
                    bytes=1,
                    sha256=hashlib.sha256(doc.bytes).hexdigest(),
                    estado=estado,
                    deleted_at=datetime.now(UTC) if borrado else None,
                )
            )
            await session.flush()
            session.add_all(
                KnowledgeChunk(document_id=doc, ordinal=i, contenido=t, embedding=vector_hash(t))
                for i, t in enumerate(textos)
            )
    return doc


async def _buscar(tid: uuid.UUID, aid: uuid.UUID, consulta: str, modo: str = "hibrida") -> list[Any]:
    from pack_knowledge.busqueda import Config, buscar

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return (await buscar(aid, consulta, Config(modo=modo, top_k=8, min_score=0.0))).fragmentos  # type: ignore[arg-type]


TEXTO = "El envío es gratis en compras desde 120.000 pesos y llega en 2 días hábiles."


@pytest.mark.parametrize("modo", ["hibrida", "semantica"])
async def test_mismo_texto_en_dos_negocios_no_se_cruza(modo: str) -> None:
    a, [agente_a] = await _negocio_con_agentes()
    b, [agente_b] = await _negocio_con_agentes()
    doc_a = await _documento(a, agente_a, [TEXTO])
    doc_b = await _documento(b, agente_b, [TEXTO])
    de_a = await _buscar(a, agente_a, "envío gratis", modo)
    assert {f.document_id for f in de_a} == {doc_a}
    # El agente de B desde el negocio A no ve nada (RLS + filtro explícito).
    assert await _buscar(a, agente_b, "envío gratis", modo) == []
    assert {f.document_id for f in await _buscar(b, agente_b, "envío gratis", modo)} == {doc_b}


async def test_otro_agente_borrados_y_no_listos_no_aparecen() -> None:
    tid, [ventas, agenda] = await _negocio_con_agentes(2)
    vivo = await _documento(tid, ventas, [TEXTO])
    await _documento(tid, agenda, [TEXTO])
    await _documento(tid, ventas, [TEXTO], borrado=True)
    await _documento(tid, ventas, [TEXTO], estado="procesando")
    assert {f.document_id for f in await _buscar(tid, ventas, "envío gratis")} == {vivo}


async def test_volumen_desigual_no_le_quita_resultados_al_chico() -> None:
    """B con 50.000 fragmentos y A con 2: A recibe sus dos, y el plan como `app_runtime` prefiltra
    los fragmentos por el btree de `tenant_id` (sin HNSW que recorra los vectores de todos)."""
    from pack_knowledge import busqueda

    from core.db.session import get_session
    from core.llm.embeddings import vector_hash
    from core.shared.tenant import tenant_scope

    grande, [agente_grande] = await _negocio_con_agentes()
    chico, [agente_chico] = await _negocio_con_agentes()
    doc = await _documento(grande, agente_grande, [])
    vector = "[" + ",".join(f"{v:.6f}" for v in vector_hash(TEXTO)) + "]"
    async with tenant_scope(grande):
        async with get_session() as session:
            await session.execute(
                text(
                    "insert into pack_knowledge_chunks (id, tenant_id, document_id, ordinal, contenido, embedding) "
                    "select gen_random_uuid(), :t, :d, g, :c, CAST(:v AS vector) from generate_series(1, 50000) g"
                ),
                {"t": grande, "d": doc, "c": TEXTO, "v": vector},
            )
            await session.execute(text("analyze pack_knowledge_chunks"))
    propios = await _documento(chico, agente_chico, [TEXTO, "Atendemos de lunes a viernes."])
    fragmentos = await _buscar(chico, agente_chico, "envío gratis", "semantica")
    assert len(fragmentos) == 2 and {f.document_id for f in fragmentos} == {propios}

    async with tenant_scope(chico):
        async with get_session() as session:
            usuario = (await session.execute(text("select current_user"))).scalar_one()
            plan = (
                await session.execute(
                    text("EXPLAIN (FORMAT JSON) " + busqueda._SEMANTICA),
                    {"texto": "envío", "q": vector, "t": chico, "a": agente_chico, "min": 0.0, "k": 8},
                )
            ).scalar_one()
    assert usuario == "app_runtime"
    nodos: list[dict[str, Any]] = []

    def _recorrer(nodo: dict[str, Any]) -> None:
        nodos.append(nodo)
        for hijo in nodo.get("Plans", []):
            _recorrer(hijo)

    _recorrer(plan[0]["Plan"])
    sobre_chunks = [n for n in nodos if n.get("Relation Name") == "pack_knowledge_chunks"]
    assert sobre_chunks, plan
    assert all(n["Node Type"] != "Seq Scan" for n in sobre_chunks), [n["Node Type"] for n in sobre_chunks]


# ------------------------------------------------------------------ golden set
async def _golden(raiz: Path) -> tuple[Any, uuid.UUID, uuid.UUID]:
    modulo = _eval()
    tid, aid = await modulo._negocio_de_prueba()
    return modulo, tid, aid


async def test_golden_set_con_grabacion(raiz) -> None:
    from core.llm.embeddings import grabacion

    modulo = _eval()
    if not modulo.GRABACION.is_file():
        pytest.skip(
            "Falta la grabación del golden set: con OK del dueño, "
            "`OPENAI_API_KEY=… python evals/conocimiento.py --grabar` (≈ US$0,001)."
        )
    modulo, tid, aid = await _golden(raiz)
    with grabacion(modulo.GRABACION, "replay"):
        await modulo.ingerir(tid, aid)
        hibrida = await modulo.medir(tid, aid, modo="hibrida")
        semantica = await modulo.medir(tid, aid, modo="semantica")
    assert hibrida.cumple(), hibrida.como_dict()
    assert hibrida.recall >= semantica.recall, (hibrida.como_dict(), semantica.como_dict())


async def test_golden_set_con_hash_recupera_por_la_parte_lexica(raiz) -> None:
    modulo, tid, aid = await _golden(raiz)
    await modulo.ingerir(tid, aid)
    hibrida = await modulo.medir(tid, aid, modo="hibrida")
    assert hibrida.recall >= 0.85, hibrida.como_dict()

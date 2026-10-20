"""Búsqueda en los documentos de un agente (ADR-0145 §1 y §4, HU-AOS-49 parte 3).

- **Exacta y prefiltrada:** un CTE `MATERIALIZED` toma solo los fragmentos vivos de los
  documentos `listo` del agente (btree por `tenant_id`, más la RLS del scope) y sobre ellos calcula
  la similitud coseno. Sin HNSW: un índice que recorre los vectores de todos los negocios le daría
  a uno chico menos de `k` resultados según el volumen de los demás.
- **`semantica`:** los `topK` más similares con similitud ≥ `minScore`.
- **`hibrida`** (por defecto): fusiona por RRF (`1/(60 + rango)`) los 20 primeros por vector y los
  20 primeros por `ts_rank_cd` sobre `public.es_unaccent`. La consulta léxica une los términos con
  OR: una pregunta en lenguaje natural casi nunca trae todas sus palabras en un fragmento. Un
  fragmento entra si su similitud llega a `minScore` o si su rango léxico pasa `PISO_LEXICO`. Así
  una pregunta que no está en los documentos devuelve cero fragmentos.

La consulta del cliente se embebe con `proposito="consulta"`: no escribe fila en el ledger.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agentcore_contracts.agentspec import KnowledgeConfigV3
from pydantic import ValidationError
from sqlalchemy import text

from core.db.session import get_session
from core.llm.embeddings import embeber_conocimiento
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Candidatos por cada lista antes de fusionar, y constante de RRF.
CANDIDATOS_POR_LISTA = 20
RRF_K = 60
# `ts_rank_cd` mínimo para que un fragmento entre solo por la parte léxica. Calibrado con el golden
# set de recuperación (`evals/conocimiento/`): un término suelto y común no alcanza.
PISO_LEXICO = 0.1
MAX_CONSULTA = 300

Modo = Literal["semantica", "hibrida"]


@dataclass(frozen=True)
class Config:
    modo: Modo = "hibrida"
    top_k: int = 4
    min_score: float = 0.35

    @classmethod
    def del_agente(cls, bloque: Mapping[str, Any] | None) -> Config:
        """El bloque `knowledge` congelado de la versión (contracts 3.1.0). Sin bloque, o con uno
        que no valida, los valores por defecto del contrato."""
        try:
            cfg = KnowledgeConfigV3.model_validate(dict(bloque or {}))
        except ValidationError:
            log.warning("knowledge_config_invalida_usa_defaults")
            cfg = KnowledgeConfigV3()
        return cls(modo=cfg.mode, top_k=cfg.topK, min_score=cfg.minScore)


@dataclass(frozen=True)
class Fragmento:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    documento: str
    seccion: str | None
    pagina_desde: int | None
    pagina_hasta: int | None
    texto: str
    similitud: float
    lexico: float
    puntaje: float


@dataclass(frozen=True)
class Resultado:
    fragmentos: list[Fragmento]
    tokens_consulta: int


_CANDIDATOS = """
WITH consulta AS (
    SELECT replace(plainto_tsquery('public.es_unaccent', :texto)::text, ' & ', ' | ')::tsquery AS tq
),
candidatos AS MATERIALIZED (
    SELECT c.id, c.document_id, d.titulo, c.seccion, c.pagina_desde, c.pagina_hasta, c.contenido,
           1 - (c.embedding <=> CAST(:q AS vector)) AS similitud,
           ts_rank_cd(c.tsv, consulta.tq) AS lexico
    FROM pack_knowledge_chunks c
    JOIN pack_knowledge_documents d ON d.tenant_id = c.tenant_id AND d.id = c.document_id
    CROSS JOIN consulta
    WHERE c.tenant_id = :t AND d.tenant_id = :t
      AND d.agent_id = :a AND d.estado = 'listo' AND d.deleted_at IS NULL
      AND c.deleted_at IS NULL AND c.embedding IS NOT NULL AND c.contenido IS NOT NULL
),
rangos AS (
    SELECT *,
           row_number() OVER (ORDER BY similitud DESC, id) AS r_vec,
           row_number() OVER (ORDER BY lexico DESC, id) AS r_lex
    FROM candidatos
)
"""

_HIBRIDA = (
    _CANDIDATOS
    + """
SELECT id, document_id, titulo, seccion, pagina_desde, pagina_hasta, contenido, similitud, lexico,
       (CASE WHEN r_vec <= :n THEN 1.0 / (:rrf + r_vec) ELSE 0 END
        + CASE WHEN lexico > 0 AND r_lex <= :n THEN 1.0 / (:rrf + r_lex) ELSE 0 END) AS puntaje
FROM rangos
WHERE (r_vec <= :n AND similitud >= :min) OR (r_lex <= :n AND lexico >= :piso)
ORDER BY puntaje DESC, similitud DESC, id
LIMIT :k
"""
)

_SEMANTICA = (
    _CANDIDATOS
    + """
SELECT id, document_id, titulo, seccion, pagina_desde, pagina_hasta, contenido, similitud, lexico,
       similitud AS puntaje
FROM rangos
WHERE similitud >= :min
ORDER BY similitud DESC, id
LIMIT :k
"""
)


def _vector(valores: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in valores) + "]"


async def buscar(agent_id: uuid.UUID, consulta: str, config: Config) -> Resultado:
    """Fragmentos de los documentos del agente para `consulta`, en el negocio del scope."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("buscar requiere un tenant_scope")
    consulta = " ".join(consulta.split())[:MAX_CONSULTA]
    if not consulta:
        return Resultado(fragmentos=[], tokens_consulta=0)
    embebido = await embeber_conocimiento([consulta], proposito="consulta")
    sql = _HIBRIDA if config.modo == "hibrida" else _SEMANTICA
    async with get_session() as session:
        filas = (
            await session.execute(
                text(sql),
                {
                    "texto": consulta,
                    "q": _vector(embebido.vectores[0]),
                    "t": tenant_id,
                    "a": agent_id,
                    "min": config.min_score,
                    "k": config.top_k,
                    "n": CANDIDATOS_POR_LISTA,
                    "rrf": RRF_K,
                    "piso": PISO_LEXICO,
                },
            )
        ).all()
    fragmentos = [
        Fragmento(
            chunk_id=f.id,
            document_id=f.document_id,
            documento=f.titulo,
            seccion=f.seccion,
            pagina_desde=f.pagina_desde,
            pagina_hasta=f.pagina_hasta,
            texto=f.contenido,
            similitud=float(f.similitud),
            lexico=float(f.lexico),
            puntaje=float(f.puntaje),
        )
        for f in filas
    ]
    return Resultado(fragmentos=fragmentos, tokens_consulta=embebido.tokens)


async def hay_documentos_listos(agent_id: uuid.UUID) -> bool:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return False
    async with get_session() as session:
        return bool(
            (
                await session.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pack_knowledge_documents WHERE tenant_id = :t "
                        "AND agent_id = :a AND estado = 'listo' AND deleted_at IS NULL)"
                    ),
                    {"t": tenant_id, "a": agent_id},
                )
            ).scalar_one()
        )

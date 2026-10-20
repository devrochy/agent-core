"""Embeddings del pack de conocimiento como servicio del core (ADR-0145 §3, HU-AOS-49).

Lo usan el pack (la tool y las tareas del worker, que no tienen `ToolContext`) y, en un cambio
aparte, la memoria. Un pack no puede importar de otro, así que vive acá.

- **Proveedores** (`KNOWLEDGE_EMBEDDING_PROVIDER`, lo lee este módulo; el pack nunca lee el
  entorno):
  - `hash` (por defecto): vector determinista derivado de las palabras y sus trigramas
    normalizados. Sin red ni costo. Es el de develop, CI y el entorno de test: la parte
    semántica no significa nada, pero la búsqueda híbrida sigue encontrando por la parte léxica.
  - `openai`: `text-embedding-3-small` (1536 dimensiones), solo con `APP_ENV=production`. Fuera
    de producción se usa `hash` con un aviso: nada de llamadas reales en develop.
- **Ledger:** cada lote de **ingesta** escribe una fila `usage_ledger` con `kind='embedding'`,
  el tenant del `tenant_scope` vigente (nunca de un argumento), `cost_usd` real y `credits = 0`
  (el dueño decidió absorber los embeddings, ADR-0145 §3). Las consultas no escriben fila: su
  costo se redondea a 0 y los tokens van en la auditoría del turno.
- Nunca se registra el texto embebido (logs ni ledger): solo modelo, conteos e ids.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

import httpx

from core.config import get_settings
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

Proposito = Literal["ingesta", "consulta"]

DIMENSION = 1536
MODELO_OPENAI = "text-embedding-3-small"
MODELO_HASH = "hash-v1"
# USD por millón de tokens de entrada. Aparte de `PRICING` (modelos de chat): el editor arma su
# catálogo de modelos desde ahí y un modelo de embeddings no es elegible para un agente.
PRECIOS_EMBEDDINGS: dict[str, float] = {MODELO_OPENAI: 0.02, MODELO_HASH: 0.0}
# Fragmentos por llamada (ADR-0145 §2: lotes de 100) y tope por texto (~600 tokens con margen).
MAX_LOTE = 100
MAX_CARACTERES = 32_000
_OPENAI_URL = "https://api.openai.com/v1/embeddings"
_TIMEOUT_S = 30.0


class EmbeddingsError(AgentCoreError):
    """El proveedor de embeddings falló. `codigo` es corto y sin texto del documento."""

    def __init__(self, codigo: str) -> None:
        self.codigo = codigo
        super().__init__(f"embeddings: {codigo}")


@dataclass(frozen=True)
class Embebidos:
    vectores: list[list[float]]
    tokens: int
    modelo: str
    dimension: int
    cost_usd: float


class _Proveedor(Protocol):
    modelo: str

    async def embeber(self, textos: Sequence[str]) -> tuple[list[list[float]], int]: ...


# ------------------------------------------------------------------ hash (sin red)
_PALABRA = re.compile(r"\w+", re.UNICODE)


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto.casefold())
    return "".join(c for c in sin_tildes if not unicodedata.combining(c))


def vector_hash(texto: str, dimension: int = DIMENSION) -> list[float]:
    """Vector unitario determinista: cada palabra y cada trigrama (`#pa`, `pal`, …) suma ±1 en
    un balde elegido por su hash. Textos con palabras en común quedan cerca en coseno."""
    vector = [0.0] * dimension
    for palabra in _PALABRA.findall(_normalizar(texto)):
        marcada = f"#{palabra}#"
        rasgos = [palabra, *(marcada[i : i + 3] for i in range(len(marcada) - 2))]
        for rasgo in rasgos:
            digest = hashlib.blake2b(rasgo.encode("utf-8"), digest_size=8).digest()
            balde = int.from_bytes(digest[:4], "big") % dimension
            vector[balde] += 1.0 if digest[4] & 1 else -1.0
    norma = math.sqrt(sum(x * x for x in vector))
    if norma == 0:
        # Sin palabras: un vector fijo (el coseno contra el vector cero no está definido).
        vector[0] = 1.0
        return vector
    return [x / norma for x in vector]


def _tokens_estimados(texto: str) -> int:
    return max(1, math.ceil(len(texto) / 4))


class _Hash:
    modelo = MODELO_HASH

    async def embeber(self, textos: Sequence[str]) -> tuple[list[list[float]], int]:
        return [vector_hash(t) for t in textos], sum(_tokens_estimados(t) for t in textos)


# ------------------------------------------------------------------ OpenAI
class _OpenAI:
    modelo = MODELO_OPENAI

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def embeber(self, textos: Sequence[str]) -> tuple[list[list[float]], int]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                respuesta = await client.post(
                    _OPENAI_URL,
                    json={"model": self.modelo, "input": list(textos)},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as exc:
            raise EmbeddingsError(f"red_{type(exc).__name__}") from None
        if respuesta.status_code != 200:
            # Sin el cuerpo: podría traer parte de la entrada.
            raise EmbeddingsError(f"http_{respuesta.status_code}")
        cuerpo = respuesta.json()
        datos = sorted(cuerpo.get("data", []), key=lambda d: d.get("index", 0))
        if len(datos) != len(textos):
            raise EmbeddingsError("respuesta_incompleta")
        vectores = [[float(x) for x in d["embedding"]] for d in datos]
        if any(len(v) != DIMENSION for v in vectores):
            raise EmbeddingsError("dimension")
        tokens = int((cuerpo.get("usage") or {}).get("prompt_tokens") or 0)
        return vectores, tokens


def _proveedor() -> _Proveedor:
    settings = get_settings()
    if settings.knowledge_embedding_provider == "openai":
        if settings.app_env != "production":
            log.warning("knowledge_embeddings_openai_fuera_de_produccion", usa=MODELO_HASH)
            return _Hash()
        if not settings.openai_api_key:
            raise EmbeddingsError("sin_llave")
        return _OpenAI(settings.openai_api_key)
    return _Hash()


def costo_usd(modelo: str, tokens: int) -> float:
    return round(PRECIOS_EMBEDDINGS.get(modelo, 0.0) * tokens / 1_000_000, 6)


async def embeber_conocimiento(
    textos: Sequence[str],
    *,
    proposito: Proposito,
    document_id: uuid.UUID | None = None,
) -> Embebidos:
    """Embebe un lote de textos del negocio vigente. Con `proposito="ingesta"` escribe la fila
    del ledger (best-effort: si falla, se avisa y el lote sigue)."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("embeber_conocimiento requiere un tenant_scope")
    if not textos:
        raise ValueError("lote vacío")
    if len(textos) > MAX_LOTE:
        raise ValueError(f"lote de {len(textos)} textos: el máximo es {MAX_LOTE}")
    if any(len(t) > MAX_CARACTERES for t in textos):
        raise ValueError(f"un texto pasa de {MAX_CARACTERES} caracteres")

    proveedor = _proveedor()
    vectores, tokens = await proveedor.embeber(textos)
    embebidos = Embebidos(
        vectores=vectores,
        tokens=tokens,
        modelo=proveedor.modelo,
        dimension=DIMENSION,
        cost_usd=costo_usd(proveedor.modelo, tokens),
    )
    if proposito == "ingesta":
        await _registrar_ingesta(tenant_id, embebidos, document_id)
    return embebidos


async def _registrar_ingesta(tenant_id: uuid.UUID, embebidos: Embebidos, document_id: uuid.UUID | None) -> None:
    from core.billing.tarifario import TARIFARIO_VIGENTE, trm_vigente
    from core.db.models import UsageLedger
    from core.db.session import get_session

    try:
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=tenant_id,
                    conversation_id=None,
                    turn_id=None,
                    kind="embedding",
                    model=embebidos.modelo,
                    input_tokens=embebidos.tokens,
                    output_tokens=0,
                    cost_usd=embebidos.cost_usd,
                    result={
                        "proposito": "ingesta",
                        **({"document_id": str(document_id)} if document_id is not None else {}),
                    },
                    # Absorbidos por la plataforma (decisión del dueño, ADR-0145 §3). Tarifario y
                    # TRM van igual: `ck_usage_credits_completos` (ADR-0144) los exige con `credits`.
                    credits=Decimal("0.0"),
                    key_mode="platform",
                    tarifario_version=TARIFARIO_VIGENTE.version,
                    trm_cop=trm_vigente(),
                )
            )
    except Exception as exc:
        log.warning("usage_ledger_embedding_write_failed", error=type(exc).__name__)


__all__ = [
    "DIMENSION",
    "MAX_LOTE",
    "MODELO_HASH",
    "MODELO_OPENAI",
    "PRECIOS_EMBEDDINGS",
    "Embebidos",
    "EmbeddingsError",
    "costo_usd",
    "embeber_conocimiento",
    "vector_hash",
]

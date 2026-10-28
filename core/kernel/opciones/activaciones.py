"""Acceso a `option_activations` y a lo que el toque necesita del envío (SPEC-40, ADR-0172 §4).

Cada función abre su PROPIA sesión corta (una transacción, un commit): el resolver nunca mantiene una
transacción ni un lock mientras corre la tool. El reclamo de la fila lo gana un solo worker con
`UPDATE … WHERE state = 'pending_confirm' … RETURNING`; el alta, con `INSERT … ON CONFLICT DO NOTHING`
(cubre `UNIQUE (tenant_id, dedupe_key)`, el índice parcial de una pendiente por (envío, opción) y el
`wamid` único). Todo filtra además por `tenant_id` explícito (defensa en profundidad sobre la RLS).

Cancelar o vencer una confirmación LIBERA su `dedupe_key` (se le agrega `#<id>`): esa activación nunca
ejecutó, así que el cliente puede volver a tocar la opción. Las que ejecutaron (`running`, `done`,
`failed`) conservan la clave: ahí está la garantía de una sola ejecución.

No guarda argumentos ni resultados: solo `args_hash` y un `error_code`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text

from core.db.session import get_session

_COLUMNAS = "id, send_id, option_id, state, dedupe_key, args_hash, error_code, claimed_at, finished_at, created_at"


@dataclass(frozen=True)
class Activacion:
    id: uuid.UUID
    send_id: uuid.UUID
    option_id: str
    state: str
    dedupe_key: str
    args_hash: str | None
    error_code: str | None
    claimed_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class Envio:
    """El envío tocado, su versión y su plantilla, leídos de un golpe (por PK, bajo RLS)."""

    send_id: uuid.UUID
    conversation_id: uuid.UUID
    template_id: uuid.UUID
    template_key: str
    template_retirada: bool
    version_id: uuid.UUID
    version_n: int
    version_retirada: bool
    definition: dict[str, Any]
    context: dict[str, Any]
    expires_at: datetime
    via: str = "interactive"  # `interactive` | `template` | `text` (CHECK `ck_option_sends_via`)


def _activacion(fila: Any) -> Activacion:
    return Activacion(*tuple(fila))


async def cargar_envio(tenant_id: uuid.UUID, send_id: uuid.UUID) -> Envio | None:
    """None si el envío no existe PARA ESTE NEGOCIO (otro negocio también cuenta como inexistente: la RLS
    y el filtro explícito lo ocultan)."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    "SELECT s.id, s.conversation_id, s.option_template_id, t.key, "
                    "(t.status = 'retired' OR t.deleted_at IS NOT NULL), s.version_id, v.version_n, "
                    "(v.retired_at IS NOT NULL), v.definition, s.context, s.expires_at, s.via "
                    "FROM option_sends s "
                    "JOIN option_template_versions v ON v.tenant_id = s.tenant_id AND v.id = s.version_id "
                    "JOIN option_templates t ON t.tenant_id = s.tenant_id AND t.id = s.option_template_id "
                    "WHERE s.tenant_id = :t AND s.id = :id"
                ),
                {"t": tenant_id, "id": send_id},
            )
        ).first()
    if fila is None:
        return None
    contexto = fila[9] if isinstance(fila[9], dict) else {}
    definicion = fila[8] if isinstance(fila[8], dict) else {}
    return Envio(*fila[:8], definition=definicion, context=contexto, expires_at=fila[10], via=fila[11])


# Los mensajes de un envío salen con `client_message_id = opt_send:<send_id>[:<burbuja>]` (`opciones_envio`): el menú
# de texto puede partirse en varias burbujas y la última es la que el cliente tiene delante.
PREFIJO_ID_MENSAJE = "opt_send"  # lo escribe `gateway/opciones_envio` (que lo importa de aquí) y lo lee `envio_del_ultimo_saliente`
_MENSAJE_DE_ENVIO = re.compile(PREFIJO_ID_MENSAJE + r":([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?::[0-9]+)?")


async def envio_del_ultimo_saliente(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> uuid.UUID | None:
    """El `send_id` del ÚLTIMO mensaje saliente de la conversación si ese mensaje es (una burbuja de) un envío de
    opciones que no falló; `None` si el último saliente es cualquier otra cosa (la respuesta del agente, un mensaje
    del operador, una respuesta fija, uno fallido) o no hay. Es la guarda de la respuesta numérica (pista C2): el
    menú solo se contesta mientras sea lo último que el cliente recibió. Cualquier tipo de remitente cuenta (un
    humano que escribe después también cierra el menú). Una sola consulta por el índice de la conversación."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    "SELECT client_message_id, status FROM messages "
                    "WHERE tenant_id = :t AND conversation_id = :c AND direction = 'outbound' AND deleted_at IS NULL "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"t": tenant_id, "c": conversation_id},
            )
        ).first()
    if fila is None or fila[1] == "failed" or not fila[0]:
        return None
    m = _MENSAJE_DE_ENVIO.fullmatch(str(fila[0]))
    return uuid.UUID(m.group(1)) if m else None


async def por_dedupe(tenant_id: uuid.UUID, dedupe_key: str) -> Activacion | None:
    async with get_session() as s:
        fila = (
            await s.execute(
                text(f"SELECT {_COLUMNAS} FROM option_activations WHERE tenant_id = :t AND dedupe_key = :k"),
                {"t": tenant_id, "k": dedupe_key},
            )
        ).first()
    return _activacion(fila) if fila else None


async def ultima_de(tenant_id: uuid.UUID, send_id: uuid.UUID, option_id: str) -> Activacion | None:
    """La activación más reciente de (envío, opción), de cualquier estado."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    f"SELECT {_COLUMNAS} FROM option_activations "
                    "WHERE tenant_id = :t AND send_id = :s AND option_id = :o ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": tenant_id, "s": send_id, "o": option_id},
            )
        ).first()
    return _activacion(fila) if fila else None


async def insertar(
    tenant_id: uuid.UUID,
    *,
    id: uuid.UUID,
    send_id: uuid.UUID,
    option_id: str,
    state: Literal["pending_confirm", "running"],
    dedupe_key: str,
    wamid: str | None,
    args_hash: str | None,
    ahora: datetime,
) -> bool:
    """Alta atómica. True si ESTE llamador ganó; False si ya había una activación que choca (doble toque,
    reintento del webhook, otra opción de un envío exclusivo). `running` nace reclamada: ganar el INSERT es
    ganar el reclamo."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    "INSERT INTO option_activations (id, tenant_id, send_id, option_id, state, dedupe_key, "
                    "inbound_wamid, args_hash, claimed_at, created_at, updated_at) "
                    "VALUES (:id, :t, :s, :o, :st, :k, :w, :h, :c, :now, :now) "
                    "ON CONFLICT DO NOTHING RETURNING id"
                ),
                {
                    "id": id,
                    "t": tenant_id,
                    "s": send_id,
                    "o": option_id,
                    "st": state,
                    "k": dedupe_key,
                    "w": wamid,
                    "h": args_hash,
                    "c": ahora if state == "running" else None,
                    "now": ahora,
                },
            )
        ).first()
    return fila is not None


async def reclamar_confirmada(
    tenant_id: uuid.UUID,
    send_id: uuid.UUID,
    option_id: str,
    args_hash: str,
    *,
    creada_desde: datetime,
    ahora: datetime,
) -> uuid.UUID | None:
    """`optc:` → la pendiente pasa a `running` SI sigue pendiente, vigente y con los mismos argumentos que
    vio el cliente. Gana un solo worker: el resto recibe None."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    "UPDATE option_activations SET state = 'running', claimed_at = :now, updated_at = :now "
                    "WHERE tenant_id = :t AND send_id = :s AND option_id = :o AND state = 'pending_confirm' "
                    "AND args_hash = :h AND created_at >= :desde RETURNING id"
                ),
                {"t": tenant_id, "s": send_id, "o": option_id, "h": args_hash, "desde": creada_desde, "now": ahora},
            )
        ).first()
    return fila[0] if fila else None


async def cerrar_pendiente(
    tenant_id: uuid.UUID,
    send_id: uuid.UUID,
    option_id: str,
    estado: Literal["cancelled", "expired"],
    *,
    ahora: datetime,
    creada_antes_de: datetime | None = None,
    error_code: str | None = None,
) -> uuid.UUID | None:
    """Cierra SIN ejecutar la confirmación pendiente de (envío, opción) y libera su `dedupe_key`.
    `creada_antes_de`: solo si ya venció (el vencimiento no cierra una confirmación vigente)."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    "UPDATE option_activations SET state = :e, dedupe_key = dedupe_key || '#' || id::text, "
                    "finished_at = :now, updated_at = :now, error_code = coalesce(:code, error_code) "
                    "WHERE tenant_id = :t AND send_id = :s AND option_id = :o AND state = 'pending_confirm' "
                    "AND (cast(:antes as timestamptz) IS NULL OR created_at < :antes) RETURNING id"
                ),
                {
                    "e": estado,
                    "t": tenant_id,
                    "s": send_id,
                    "o": option_id,
                    "now": ahora,
                    "antes": creada_antes_de,
                    "code": error_code,
                },
            )
        ).first()
    return fila[0] if fila else None


async def terminar(
    tenant_id: uuid.UUID,
    activation_id: uuid.UUID,
    estado: Literal["done", "failed"],
    *,
    ahora: datetime,
    error_code: str | None = None,
) -> bool:
    """`running` → `done`/`failed`. False si la fila ya no estaba en `running` (no se pisa)."""
    async with get_session() as s:
        fila = (
            await s.execute(
                text(
                    "UPDATE option_activations SET state = :e, finished_at = :now, updated_at = :now, "
                    "error_code = :code WHERE tenant_id = :t AND id = :id AND state = 'running' RETURNING id"
                ),
                {"e": estado, "t": tenant_id, "id": activation_id, "now": ahora, "code": error_code},
            )
        ).first()
    return fila is not None

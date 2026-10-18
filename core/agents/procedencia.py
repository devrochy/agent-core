"""Procedencia de los registros que crea un agente (HU-AOS-47, ADR-0140).

Cuando una tool que declara `MCPTool.crea_registro` termina bien en un turno del runtime v3, el
loop deja aquí qué instancia, versión, conexión, conversación y turno crearon el
registro (`agent_record_origins`). El pack decide si el resultado es un registro nuevo
(`RegistroCreado.es_nuevo`); el core solo descarta antes lo que trae `error` u `ok: false`.

- **Detrás del runtime v3:** sin release del turno (`aos_runtime = 'legacy'`) no se escribe nada,
  igual que el ledger no atribuye agente. Con el runtime v3 apagado el turno no cambia.
- **Best-effort:** un fallo nunca interrumpe la respuesta al cliente; se loguea sin ids ni datos.
- **El primero que lo crea gana:** `ON CONFLICT DO NOTHING` sobre `(tenant_id, pack_id, kind,
  record_ref)`.
- `label` solo con la referencia pública que declara el pack (número de pedido), truncada.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from core.db.models import AgentRecordOrigin
from core.db.session import get_session
from core.mcp.types import MCPTool, RegistroCreado
from core.shared import release_vigente
from core.shared import turno as turno_vigente
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

_MAX_REF = 200
_MAX_LABEL = 120


def referencia_creada(declaracion: RegistroCreado, resultado: Any) -> tuple[str, str | None] | None:
    """`(record_ref, label)` si el resultado es un registro recién creado; None si no.

    Pura: la regla genérica (sin `error`, sin `ok: false`, con el id) y después el predicado
    del pack. Un predicado que falla cuenta como «no creado» (no se atribuye a ciegas)."""
    if not isinstance(resultado, dict) or resultado.get("error") or resultado.get("ok") is False:
        return None
    ref = resultado.get(declaracion.id_key)
    if ref is None or isinstance(ref, bool) or not isinstance(ref, (str, int, uuid.UUID)):
        return None
    ref = str(ref).strip()
    if not ref or len(ref) > _MAX_REF:
        return None
    try:
        if not declaracion.es_nuevo(resultado):
            return None
    except Exception:
        return None
    label: str | None = None
    if declaracion.label_key is not None:
        valor = resultado.get(declaracion.label_key)
        if isinstance(valor, (str, int)) and not isinstance(valor, bool) and str(valor).strip():
            label = str(valor).strip()[:_MAX_LABEL]
    return ref, label


async def registrar(tool: MCPTool | None, resultado: Any, conversation_id: uuid.UUID | None) -> None:
    """Guarda la procedencia si la tool la declara, el turno es del runtime v3 y el registro es
    nuevo. Best-effort."""
    if tool is None or tool.crea_registro is None:
        return
    release = release_vigente.vigente()
    if release is None:
        return
    referencia = referencia_creada(tool.crea_registro, resultado)
    if referencia is None:
        return
    ref, label = referencia
    agente = release.foco
    try:
        async with get_session() as session:
            await session.execute(
                insert(AgentRecordOrigin)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=get_current_tenant_id(),
                    pack_id=tool.plugin_id or "core",
                    kind=tool.crea_registro.kind,
                    record_ref=ref,
                    label=label,
                    tool=tool.name,
                    agent_instance_id=agente.instance_id if agente is not None else None,
                    agent_version_id=agente.version_id if agente is not None else None,
                    connection_id=release.connection_id,
                    conversation_id=conversation_id,
                    turn_id=turno_vigente.turno_id(),
                )
                .on_conflict_do_nothing(constraint="uq_agent_record_origins_ref")
            )
    except Exception as exc:
        log.warning("record_origin_write_failed", tool=tool.name, error=type(exc).__name__)

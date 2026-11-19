"""Confirmación de una acción con efecto fuera del gobierno del motor (HU-ARCH-36, adenda de ADR-0050).

La primera llamada no ejecuta: deja el pendiente en `conversations.state` y el modelo le pregunta
al cliente. La repetición con los mismos argumentos solo ejecuta si el cliente contestó esa pregunta:

- Hay un mensaje del cliente posterior al que abrió el pendiente: el modelo no se confirma solo, ni
  en el mismo lote, ni en otra vuelta del turno, ni en un reintento sin mensaje nuevo.
- La pregunta salió, y es la del turno inmediatamente anterior: el mensaje que abrió el pendiente es
  el último del cliente que ya tuvo respuesta. Un pendiente de hace varios turnos se vuelve a pedir.

El historial que relee el modelo es solo texto (ADR-0130): el pendiente vigente entra al bloque
dinámico para que pueda repetir los argumentos. Los rótulos viven en el playbook del kernel
`confirmacion-pendiente` (invariante 3) y cada valor se escapa como dato (invariante 7).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Conversation
from core.db.session import get_session
from core.kernel.procedures.textos import secciones
from core.kernel.tarjeta import MAX_VALOR_CHARS, RotulosInvalidos, escapar

CLAVE = "pending_confirmation"
PLAYBOOK = "confirmacion-pendiente"
CLAVES = ("confirmacion_accion", "encabezado", "aviso", "linea", "sin_argumentos")
_NOMBRE = re.compile(r"^[a-zA-Z0-9_]{1,80}$")


@dataclass(frozen=True)
class Entrantes:
    """Últimos mensajes del cliente: el más reciente y el más reciente que ya tuvo respuesta."""

    ultimo: uuid.UUID | None
    respondido: uuid.UUID | None


def parsear(contenido: str) -> dict[str, str]:
    crudas = secciones(contenido)
    faltan = [c for c in CLAVES if len(crudas.get(c, [])) != 1]
    if faltan:
        raise RotulosInvalidos(f"textos de la confirmación pendiente incompletos o con varias líneas: {faltan}")
    return {c: crudas[c][0] for c in CLAVES}


@cache
def textos() -> dict[str, str]:
    """Textos del playbook del kernel (fail-closed: si el archivo está roto, lanza)."""
    from core.agents.playbooks import load_core_playbook

    return parsear(load_core_playbook(PLAYBOOK))


def mensaje_al_modelo(accion: str) -> str:
    return textos()["confirmacion_accion"].format(accion=accion)


async def leer_entrantes(session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> Entrantes:
    fila = (
        await session.execute(
            text(
                "SELECT"
                " (SELECT id FROM messages WHERE tenant_id = :t AND conversation_id = :c AND deleted_at IS NULL"
                "  AND direction = 'inbound' ORDER BY created_at DESC, id DESC LIMIT 1) AS ultimo,"
                " (SELECT i.id FROM messages i WHERE i.tenant_id = :t AND i.conversation_id = :c"
                "  AND i.deleted_at IS NULL AND i.direction = 'inbound' AND i.created_at < ("
                "    SELECT max(o.created_at) FROM messages o WHERE o.tenant_id = :t AND o.conversation_id = :c"
                "    AND o.deleted_at IS NULL AND o.direction = 'outbound' AND o.status IS DISTINCT FROM 'failed')"
                "  ORDER BY i.created_at DESC, i.id DESC LIMIT 1) AS respondido"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).one()
    return Entrantes(ultimo=fila.ultimo, respondido=fila.respondido)


def vigente(pendiente: Mapping[str, Any] | None, entrantes: Entrantes) -> bool:
    """¿El último mensaje del cliente contesta la pregunta de este pendiente? Puro."""
    abierto_en = (pendiente or {}).get("inbound_id")
    if not abierto_en or entrantes.ultimo is None or entrantes.respondido is None:
        return False
    return str(entrantes.ultimo) != abierto_en and str(entrantes.respondido) == abierto_en


async def resolver_llamada(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, tool: str, argumentos: Mapping[str, Any]
) -> bool:
    """True si esta llamada ya está confirmada (consume el pendiente). Si no, deja el pendiente al día.

    Con la fila bloqueada y tocando solo su clave: dos turnos a la vez no ejecutan la acción dos
    veces ni pisan el resto del estado de la conversación."""
    async with get_session() as session:
        conversacion = (
            await session.execute(
                select(Conversation)
                .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversacion is None:
            return False
        entrantes = await leer_entrantes(session, tenant_id, conversation_id)
        estado = dict(conversacion.state)
        pendiente = estado.get(CLAVE)
        if (
            isinstance(pendiente, Mapping)
            and pendiente.get("tool") == tool
            and pendiente.get("arguments") == argumentos
            and vigente(pendiente, entrantes)
        ):
            estado.pop(CLAVE)
            conversacion.state = estado
            return True
        estado[CLAVE] = {
            "tool": tool,
            "arguments": dict(argumentos),
            "inbound_id": str(entrantes.ultimo) if entrantes.ultimo is not None else None,
        }
        conversacion.state = estado
        return False


def serializar(pendiente: Mapping[str, Any], propiedades: Collection[str]) -> str:
    """El bloque del prompt, o "" si algo no se puede mostrar tal cual.

    Todo o nada: el modelo tiene que repetir los argumentos completos, así que una clave oculta o
    un valor truncado harían que la repetición nunca coincida."""
    from core.agents.credenciales import es_sensible

    accion = pendiente.get("tool")
    argumentos = pendiente.get("arguments")
    if not isinstance(accion, str) or not _NOMBRE.fullmatch(accion) or not isinstance(argumentos, Mapping):
        return ""
    partes: list[str] = []
    for clave, valor in argumentos.items():
        if clave not in propiedades or clave.startswith("_") or not _NOMBRE.fullmatch(clave) or es_sensible(clave):
            return ""
        medible = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)
        if len(medible) > MAX_VALOR_CHARS:
            return ""
        partes.append(f"{clave}={escapar(valor)}")
    t = textos()
    linea = t["linea"].format(accion=accion, argumentos=", ".join(partes) or t["sin_argumentos"])
    return "\n".join((t["encabezado"], t["aviso"], linea))


async def para_prompt(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, pendiente: Any, propiedades: Collection[str]
) -> str:
    """Bloque del pendiente que el cliente acaba de contestar, o "" (el prompt queda como siempre)."""
    if not isinstance(pendiente, Mapping):
        return ""
    async with get_session() as session:
        entrantes = await leer_entrantes(session, tenant_id, conversation_id)
    return serializar(pendiente, propiedades) if vigente(pendiente, entrantes) else ""

"""Referencias vigentes de la conversación (ADR-0130).

El historial que relee el modelo es solo texto: los ids que devolvieron las tools en
turnos anteriores (el `quote_id` de una cotización) no llegan al turno siguiente, y el
modelo los adivina (un SKU, el número de otro pedido). Cada pack aporta el estado
actual de lo que el cliente está negociando, leído de sus tablas; el core lo sanea y
lo pone en el bloque dinámico del prompt como dato.

- Solo packs habilitados (ADR-0019) y sanos para el negocio (ADR-0024).
- Fail-open: una fuente que falla o tarda más de `TIMEOUT_FUENTE_S` se salta con un log.
- Saneo (invariante 7): claves en minúsculas y valores tipados o textos cortos sin
  espacios. Lo que no cumple se descarta; la línea la arma el core, nunca el pack.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal
from typing import Any

from core.mcp.types import Referencia
from core.shared.logger import get_logger

log = get_logger(__name__)

TIMEOUT_FUENTE_S = 0.5
MAX_REFERENCIAS = 6
MAX_DATOS = 8
_MAX_TEXTO = 40

_CLAVE = re.compile(r"[a-z_]{1,40}")
_TEXTO = re.compile(rf"[\w\-.]{{1,{_MAX_TEXTO}}}")


def _valor(valor: Any) -> str | None:
    """El valor como texto seguro para el prompt, o None si no es un dato tipado."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, uuid.UUID):
        return str(valor)
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, float | Decimal):
        return f"{float(valor):.2f}"
    if isinstance(valor, str) and _TEXTO.fullmatch(valor):
        return valor
    return None


def renderizar(referencia: Referencia) -> str | None:
    """`- tipo: clave=valor, …`, o None si no queda ningún dato válido."""
    if not isinstance(referencia, Referencia) or not _CLAVE.fullmatch(referencia.tipo or ""):
        log.warning("referencia_descartada", motivo="tipo")
        return None
    partes: list[str] = []
    for clave, valor in referencia.datos[:MAX_DATOS]:
        if valor is None:
            continue
        texto = _valor(valor)
        if not isinstance(clave, str) or not _CLAVE.fullmatch(clave) or texto is None:
            log.warning("referencia_dato_descartado", tipo=referencia.tipo, clave=str(clave)[:40])
            continue
        partes.append(f"{clave}={texto}")
    if not partes:
        return None
    return f"- {referencia.tipo}: " + ", ".join(partes)


async def cargar_referencias(
    habilitados: set[str],
    desactivados: set[str],
    conversation_id: uuid.UUID,
    customer_id: uuid.UUID,
) -> list[str]:
    """Líneas de referencias de los packs habilitados y sanos, ya saneadas."""
    from core.mcp.host import mcp_host

    lineas: list[str] = []
    for plugin_id, fuente in mcp_host.get_referencias().items():
        if plugin_id not in habilitados or plugin_id in desactivados:
            continue
        try:
            referencias = await asyncio.wait_for(fuente(conversation_id, customer_id), TIMEOUT_FUENTE_S)
        except TimeoutError:
            log.warning("referencias_timeout", plugin=plugin_id)
            continue
        except Exception:
            log.exception("referencias_fallaron", plugin=plugin_id)
            continue
        for referencia in referencias or []:
            linea = renderizar(referencia)
            if linea is not None:
                lineas.append(linea)
    if len(lineas) > MAX_REFERENCIAS:
        log.warning("referencias_truncadas", total=len(lineas), limite=MAX_REFERENCIAS)
    return lineas[:MAX_REFERENCIAS]

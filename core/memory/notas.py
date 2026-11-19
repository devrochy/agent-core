"""Notas del equipo sobre el cliente en el prompt (HU-CRM-04, ADR-0147).

Las últimas notas vivas del cliente entran al bloque dinámico, después de lo que sabemos de él.
Los rótulos viven en el playbook del kernel `notas-del-equipo` (invariante 3); aquí solo se
serializa, con cada texto escapado y delimitado como dato (invariante 7, `tarjeta.escapar`).
Sin notas no hay bloque: el prompt queda igual que antes.
"""

from __future__ import annotations

import re
import uuid
from functools import cache

from sqlalchemy import select

from core.db.models import CustomerNote
from core.db.session import get_session
from core.kernel.tarjeta import escapar

PLAYBOOK = "notas-del-equipo"
MAX_NOTAS = 5
MAX_BLOQUE_CHARS = 1500
CLAVES = {"encabezado": (), "aviso": (), "nota": ("fecha", "texto")}
_SECCION = re.compile(r"^## ([a-z_]+)$")


class RotulosInvalidos(ValueError):
    """El playbook de notas no tiene todas sus claves o una plantilla no encaja."""


def parsear_rotulos(contenido: str) -> dict[str, str]:
    secciones: dict[str, list[str]] = {}
    actual: str | None = None
    for linea in contenido.splitlines():
        m = _SECCION.match(linea)
        if m:
            actual = m.group(1)
            secciones[actual] = []
        elif actual is not None:
            secciones[actual].append(linea)
    plantillas = {k: "\n".join(v).strip("\n") for k, v in secciones.items()}
    faltan = sorted(set(CLAVES) - set(plantillas))
    if faltan:
        raise RotulosInvalidos(f"faltan rótulos: {faltan}")
    for clave, campos in CLAVES.items():
        plantilla = plantillas[clave]
        if not plantilla or "\n" in plantilla:
            raise RotulosInvalidos(f"el rótulo {clave} debe ser una sola línea")
        try:
            plantilla.format(**{c: "x" for c in campos})
        except (KeyError, IndexError, ValueError) as exc:
            raise RotulosInvalidos(f"el rótulo {clave} usa un campo desconocido: {exc}") from exc
    return plantillas


@cache
def rotulos() -> dict[str, str]:
    """Rótulos del playbook del kernel (fail-closed: si el archivo está roto, lanza)."""
    from core.agents.playbooks import load_core_playbook

    return parsear_rotulos(load_core_playbook(PLAYBOOK))


def serializar(notas: list[tuple[str, str]]) -> str:
    """(fecha, texto) de la más nueva a la más vieja → bloque del prompt, o "" sin notas."""
    if not notas:
        return ""
    r = rotulos()
    lineas = [r["encabezado"], r["aviso"]]
    largo = sum(len(x) for x in lineas)
    for fecha, texto in notas:
        linea = r["nota"].format(fecha=fecha, texto=escapar(texto))
        if largo + len(linea) > MAX_BLOQUE_CHARS:
            break
        lineas.append(linea)
        largo += len(linea)
    return "\n".join(lineas) if len(lineas) > 2 else ""


async def para_prompt(tenant_id: uuid.UUID | str | None, customer_id: uuid.UUID) -> str:
    """Bloque de las últimas notas del cliente, o "" si no tiene."""
    if tenant_id is None:
        return ""
    async with get_session() as session:
        filas = (
            await session.execute(
                select(CustomerNote.created_at, CustomerNote.text)
                .where(
                    CustomerNote.tenant_id == tenant_id,
                    CustomerNote.customer_id == customer_id,
                    CustomerNote.deleted_at.is_(None),
                )
                .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
                .limit(MAX_NOTAS)
            )
        ).all()
    return serializar([(creada.strftime("%Y-%m-%d"), texto) for creada, texto in filas])

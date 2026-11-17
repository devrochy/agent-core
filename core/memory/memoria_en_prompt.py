"""Perfil del cliente y recuerdos de conversaciones pasadas en el prompt (HU-ARCH-35).

Los dos entran al bloque dinámico como dato y nunca como instrucción (invariante 7): cada clave y
cada valor se escapan y delimitan con `tarjeta.escapar`, como las notas del equipo y la relación con
el negocio. El escape se hace al leer, así que protege también lo que ya estaba guardado. Los rótulos
viven en el playbook del kernel `memoria-del-cliente` (invariante 3). Sin datos no hay bloque.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import cache
from typing import Any

from core.kernel.tarjeta import escapar

PLAYBOOK = "memoria-del-cliente"
MAX_BLOQUE_CHARS = 2000
CLAVES = {"perfil": (), "recuerdos": (), "aviso": (), "dato": ("clave", "valor"), "recuerdo": ("texto",)}
_SECCION = re.compile(r"^## ([a-z_]+)$")


class RotulosInvalidos(ValueError):
    """El playbook no tiene todas sus claves o una plantilla no encaja."""


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


def _bloque(encabezado: str, lineas: Iterable[str]) -> str:
    r = rotulos()
    salida = [r[encabezado], r["aviso"]]
    largo = sum(len(x) for x in salida)
    for linea in lineas:
        if largo + len(linea) > MAX_BLOQUE_CHARS:
            break
        salida.append(linea)
        largo += len(linea)
    return "\n".join(salida) if len(salida) > 2 else ""


def serializar_perfil(perfil: Mapping[str, Any]) -> str:
    """Perfil del cliente → bloque del prompt, o "" si no hay datos. Clave y valor van escapados."""
    if not perfil:
        return ""
    plantilla = rotulos()["dato"]
    return _bloque(
        "perfil",
        (plantilla.format(clave=escapar(str(k)), valor=escapar(v)) for k, v in perfil.items() if v),
    )


def serializar_recuerdos(recuerdos: Iterable[str]) -> str:
    """Recuerdos de `recall` → bloque del prompt, o "" si no hay. Cada uno va escapado."""
    plantilla = rotulos()["recuerdo"]
    return _bloque("recuerdos", (plantilla.format(texto=escapar(t)) for t in recuerdos if t))

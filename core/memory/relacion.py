"""Ficha corta de la relación del cliente con cada módulo, para el prompt (HU-CRM-14, ADR-0179 decisión 3).

Cada módulo habilitado puede aportar, vía `LeadRelacion.ficha`, una línea corta de lo que ya sabe del
cliente (p. ej. «2 pedidos, el último entregado»). Aquí se junta bajo topes duros: pocos módulos, líneas
cortas y un tiempo máximo por módulo; un módulo que tarda o falla se omite y el turno sigue. Las líneas
no llevan montos, documento, correo ni dirección (los importes solo salen por la tool de historial, para
no chocar con el verificador de montos). Los rótulos viven en el playbook del kernel
`relacion-con-el-negocio` (invariante 3); cada línea se escapa y delimita como dato (invariante 7).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from functools import cache

from core.admin.lead_registry import LeadProvider, lead_registry
from core.kernel.tarjeta import escapar
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "relacion-con-el-negocio"
MAX_MODULOS = 4
MAX_LINEA_CHARS = 240  # ≈ 60 tokens
MAX_BLOQUE_CHARS = 1600  # ≈ 400 tokens
TIMEOUT_POR_MODULO_S = 0.5
CLAVES = {"encabezado": (), "aviso": (), "modulo": ("modulo", "resumen")}
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


def serializar(lineas: list[tuple[str, str]]) -> str:
    """[(módulo, resumen)] → bloque del prompt, o "" si no hay nada. Aplica los topes."""
    if not lineas:
        return ""
    r = rotulos()
    salida = [r["encabezado"], r["aviso"]]
    largo = sum(len(x) for x in salida)
    for modulo, resumen in lineas[:MAX_MODULOS]:
        texto = " ".join(str(resumen).split())[:MAX_LINEA_CHARS]
        if not texto:
            continue
        linea = r["modulo"].format(modulo=escapar(modulo), resumen=escapar(texto))
        if largo + len(linea) > MAX_BLOQUE_CHARS:
            break
        salida.append(linea)
        largo += len(linea)
    return "\n".join(salida) if len(salida) > 2 else ""


async def _ficha_de(proveedor: LeadProvider, customer_id: uuid.UUID) -> tuple[str, str] | None:
    ficha = proveedor.relacion.ficha if proveedor.relacion else None
    if ficha is None:
        return None
    try:
        por_cliente = await asyncio.wait_for(ficha([customer_id]), timeout=TIMEOUT_POR_MODULO_S)
    except Exception:  # incluye el timeout: la ficha es una ayuda, nunca tumba el turno
        log.warning("ficha_modulo_omitida", modulo=proveedor.plugin_id, exc_info=True)
        return None
    resumen = (por_cliente or {}).get(customer_id)
    if not resumen:
        return None
    return proveedor.agent_name or proveedor.plugin_id, resumen


async def para_prompt(customer_id: uuid.UUID | None, habilitados: set[str]) -> str:
    """Bloque de la relación del cliente con los módulos habilitados, o "" si no hay datos."""
    if customer_id is None:
        return ""
    proveedores = [p for p in lead_registry.providers_for(habilitados) if p.relacion and p.relacion.ficha]
    if not proveedores:
        return ""
    # Como máximo MAX_MODULOS consultas, en paralelo: el peor caso es un solo timeout, no la suma.
    candidatos = sorted(proveedores, key=lambda p: p.plugin_id)[:MAX_MODULOS]
    resultados = await asyncio.gather(*(_ficha_de(p, customer_id) for p in candidatos))
    return serializar([r for r in resultados if r])

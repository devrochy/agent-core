"""Qué le falta configurar al negocio para que un agente de plantilla funcione (HU-AOS-50, ADR-0155).

Sale de dos hechos que el core ya sabe, sin lógica por habilidad:

- `habilidad_sin_configurar`: una habilidad que el agente necesita y el negocio todavía no habilitó
  (sus claves o su formulario): las opcionales de la plantilla (cobro en línea para Ventas) y las
  habilidades que otra habilidad declara en `required_connections` (Suscripciones pide cobro en línea).
- `sin_contenido`: una habilidad habilitada que aún no tiene nada que ofrecer (productos, tipos de
  cita, cartera de clientes), según el hook `BaseModule.contenido_cargado` de cada pack.

Es un aviso, no una puerta: publicar no se bloquea (lo que bloquea sigue siendo D-35 y el tope del
plan). Los textos de cara al dueño los escribe el pack (`display_name`, `setup_guide`); el panel traduce
`tipo`. Una opcional acotada a algunas acciones (`acciones_opcionales`, p. ej. Agenda con `crear_lead`)
no se avisa: el agente no depende de que esa habilidad esté completa.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from core.presets import PlantillaAgente
from core.primeros_pasos.contrato import ContenidoCargado
from core.primeros_pasos.reglas import SECCION_HABILIDADES
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

TipoDeRequisito = Literal["habilidad_sin_configurar", "sin_contenido"]


@dataclass(frozen=True)
class InfoDeHabilidad:
    """Lo que el pack dice de sí mismo para el aviso (catálogo de habilidades, HU-FWK-04)."""

    nombre: str
    guia: str = ""
    # Habilidades que necesita (`required_connections` que son otro pack, p. ej. `pack_payments`).
    requiere: tuple[str, ...] = ()


@dataclass(frozen=True)
class Requisito:
    pack: str
    tipo: TipoDeRequisito
    titulo: str
    explicacion: str
    # Sección del RBAC con la que se arregla (el panel decide a qué pantalla lleva).
    seccion: str
    # Solo `sin_contenido`: "catalogo" | "servicios" | "cartera".
    contenido: str | None = None


def packs_con_contenido(plantilla: PlantillaAgente, habilitados: Collection[str]) -> list[str]:
    """Las habilidades de la plantilla cuyo contenido importa: las propias y las opcionales
    habilitadas que no están acotadas a unas pocas acciones."""
    acotadas = set(plantilla.acciones_opcionales)
    packs = [*plantilla.habilidades, *(p for p in plantilla.opcionales if p not in acotadas)]
    return [p for p in dict.fromkeys(packs) if p in habilitados]


def calcular(
    plantilla: PlantillaAgente,
    habilitados: Collection[str],
    contenidos: Mapping[str, ContenidoCargado],
    info: Mapping[str, InfoDeHabilidad],
) -> list[Requisito]:
    """Requisitos de la plantilla para ese negocio. Función pura: lo leído viene por argumento."""
    acotadas = set(plantilla.acciones_opcionales)
    necesarias = [p for p in plantilla.opcionales if p not in acotadas]
    for pack in [*plantilla.habilidades, *necesarias]:
        if pack in habilitados:
            necesarias.extend(info[pack].requiere if pack in info else ())

    requisitos: list[Requisito] = []
    for pack in dict.fromkeys(necesarias):
        if pack in habilitados or pack in plantilla.habilidades:
            continue  # las obligatorias faltantes ya las dice `missing_skills`
        datos = info.get(pack)
        requisitos.append(
            Requisito(
                pack=pack,
                tipo="habilidad_sin_configurar",
                titulo=datos.nombre if datos else pack,
                explicacion=datos.guia if datos else "",
                seccion=SECCION_HABILIDADES,
            )
        )
    for pack in packs_con_contenido(plantilla, habilitados):
        contenido = contenidos.get(pack)
        if contenido is None or contenido.cantidad > 0:
            continue
        datos = info.get(pack)
        requisitos.append(
            Requisito(
                pack=pack,
                tipo="sin_contenido",
                titulo=datos.nombre if datos else pack,
                explicacion=datos.guia if datos else "",
                seccion=contenido.seccion,
                contenido=contenido.tipo,
            )
        )
    return requisitos


def _info(packs: Iterable[str]) -> dict[str, InfoDeHabilidad]:
    from core.plugin_manager import plugin_manager

    salida: dict[str, InfoDeHabilidad] = {}
    for pack in packs:
        modulo = plugin_manager.get(pack)
        if modulo is None:
            continue
        salida[pack] = InfoDeHabilidad(
            nombre=modulo.display_name or modulo.name or pack,
            guia=modulo.setup_guide or modulo.description or "",
            requiere=tuple(c for c in modulo.required_connections if c.startswith("pack_")),
        )
    return salida


async def de_plantillas(
    tenant_id: uuid.UUID, habilitados: Collection[str], plantillas: Iterable[PlantillaAgente]
) -> dict[str, list[Requisito]]:
    """Requisitos de cada plantilla (por su key) para el negocio. Corre bajo su `tenant_scope`;
    cada pack lee solo lo suyo y filtra por `tenant_id`. Lo que no se pueda leer no tumba la lista."""
    from core.primeros_pasos.calculo import contenidos_de_habilidades

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("requisitos de plantilla: fuera del tenant_scope del negocio")
    plantillas = list(plantillas)
    packs = {p for pl in plantillas for p in (*pl.habilidades, *pl.opcionales)}
    info = _info(packs)
    info |= _info({r for datos in info.values() for r in datos.requiere} - set(info))
    a_leer = sorted({p for pl in plantillas for p in packs_con_contenido(pl, habilitados)})
    contenidos = dict(await contenidos_de_habilidades(tenant_id, a_leer))
    return {pl.key: calcular(pl, habilitados, contenidos, info) for pl in plantillas}


__all__ = ["InfoDeHabilidad", "Requisito", "calcular", "de_plantillas", "packs_con_contenido"]

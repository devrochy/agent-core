"""Ajustes del negocio sobre la política de datos (HU-CRM-13, ADR-0179 decisión 1).

Viven en `AppSettings.behavior_config["datos_cliente"]["politica"]` como
`{accion_id: {clave: nivel}}`. Solo se guarda lo que difiere del valor por defecto del módulo;
cualquier clave, acción o nivel desconocido se descarta al leer y al escribir (dato del dueño,
nunca instrucción: invariante 7). Es el único módulo, junto con el router de settings, que toca
esa clave de la configuración.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.datos_cliente.modelo import AccionDatos, Nivel
from core.datos_cliente.registro import registro_datos
from core.db.models import AppSettings
from core.db.session import get_session

CLAVE = "datos_cliente"
_NIVELES = {n.value for n in Nivel}


def limpiar(crudo: Any, acciones: list[AccionDatos] | None = None) -> dict[str, dict[str, str]]:
    """Política saneada: solo acciones registradas, claves declaradas y ajustables, niveles válidos."""
    if not isinstance(crudo, dict):
        return {}
    registradas = {a.id: a for a in (acciones if acciones is not None else registro_datos.de_packs(None))}
    salida: dict[str, dict[str, str]] = {}
    for accion_id, claves in crudo.items():
        accion = registradas.get(accion_id)
        if accion is None or not isinstance(claves, dict):
            continue
        limpias: dict[str, str] = {}
        for clave, nivel in claves.items():
            campo = accion.campo(str(clave))
            if campo is None or not campo.ajustable or nivel not in _NIVELES:
                continue
            limpias[campo.clave] = str(nivel)
        if limpias:
            salida[accion_id] = limpias
    return salida


def politica_de(behavior: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Ajustes guardados en el `behavior_config` del negocio, saneados."""
    bloque = (behavior or {}).get(CLAVE)
    return limpiar(bloque.get("politica") if isinstance(bloque, dict) else None)


def con_politica(behavior: dict[str, Any] | None, politica: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Copia del `behavior_config` con la política reemplazada (vacía = se quita el bloque)."""
    nuevo = dict(behavior or {})
    bloque = dict(nuevo.get(CLAVE) or {}) if isinstance(nuevo.get(CLAVE), dict) else {}
    if politica:
        bloque["politica"] = politica
    else:
        bloque.pop("politica", None)
    if bloque:
        nuevo[CLAVE] = bloque
    else:
        nuevo.pop(CLAVE, None)
    return nuevo


async def cargar_ajustes(tenant_id: uuid.UUID) -> dict[str, dict[str, str]]:
    async with get_session() as session:
        behavior = (
            await session.execute(
                select(AppSettings.behavior_config).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    return politica_de(behavior)


def herramientas_de(behavior: dict[str, Any] | None) -> bool:
    """¿El negocio dejó que el agente use los datos del cliente (tools y ficha)? Apagado por defecto."""
    bloque = (behavior or {}).get(CLAVE)
    return bool(bloque.get("herramientas")) if isinstance(bloque, dict) else False


def con_herramientas(behavior: dict[str, Any] | None, activas: bool) -> dict[str, Any]:
    """Copia del `behavior_config` con la bandera de las herramientas del agente puesta o quitada."""
    nuevo = dict(behavior or {})
    bloque = dict(nuevo.get(CLAVE) or {}) if isinstance(nuevo.get(CLAVE), dict) else {}
    if activas:
        bloque["herramientas"] = True
    else:
        bloque.pop("herramientas", None)
    if bloque:
        nuevo[CLAVE] = bloque
    else:
        nuevo.pop(CLAVE, None)
    return nuevo


async def herramientas_activas(tenant_id: uuid.UUID | None) -> bool:
    if tenant_id is None:
        return False
    async with get_session() as session:
        behavior = (
            await session.execute(
                select(AppSettings.behavior_config).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    return herramientas_de(behavior)


def aviso_de(behavior: dict[str, Any] | None) -> dict[str, Any]:
    """Aviso de tratamiento de datos guardado: `{activo: bool, texto: str}` (texto vacío = el borrador)."""
    bloque = (behavior or {}).get(CLAVE)
    crudo = bloque.get("aviso") if isinstance(bloque, dict) else None
    if not isinstance(crudo, dict):
        return {"activo": False, "texto": ""}
    return {"activo": bool(crudo.get("activo")), "texto": str(crudo.get("texto") or "")[:1000]}


def con_aviso(behavior: dict[str, Any] | None, activo: bool, texto: str) -> dict[str, Any]:
    nuevo = dict(behavior or {})
    bloque = dict(nuevo.get(CLAVE) or {}) if isinstance(nuevo.get(CLAVE), dict) else {}
    if activo or texto.strip():
        bloque["aviso"] = {"activo": bool(activo), "texto": texto.strip()[:1000]}
    else:
        bloque.pop("aviso", None)
    if bloque:
        nuevo[CLAVE] = bloque
    else:
        nuevo.pop(CLAVE, None)
    return nuevo

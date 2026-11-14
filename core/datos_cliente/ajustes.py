"""Política de datos del cliente de cada agente (HU-CRM-28, enmienda de ADR-0179).

La política vive en `agents.data_policy` como `{politica: {accion_id: {clave: nivel}},
herramientas: bool}`. Solo se guarda lo que difiere del valor por defecto del módulo; cualquier
clave, acción o nivel desconocido se descarta al leer y al escribir (dato del dueño, nunca
instrucción: invariante 7). El aviso de tratamiento de datos sigue siendo del negocio y vive en
`AppSettings.behavior_config["datos_cliente"]["aviso"]`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.datos_cliente.modelo import AccionDatos, Nivel
from core.datos_cliente.registro import registro_datos
from core.db.models import AgentRecord
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

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


def politica_de(data_policy: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Ajustes guardados en la política del agente, saneados."""
    bloque = (data_policy or {}).get("politica")
    return limpiar(bloque)


def con_politica(
    data_policy: dict[str, Any] | None, politica: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Copia de `data_policy` con la política reemplazada (vacía = se quita)."""
    nuevo = dict(data_policy or {})
    if politica:
        nuevo["politica"] = politica
    else:
        nuevo.pop("politica", None)
    return nuevo


def herramientas_de(data_policy: dict[str, Any] | None) -> bool:
    """¿El agente puede consultar los datos del cliente (tools y ficha)? Apagado por defecto."""
    return bool((data_policy or {}).get("herramientas"))


def con_herramientas(data_policy: dict[str, Any] | None, activas: bool) -> dict[str, Any]:
    """Copia de `data_policy` con la bandera de las herramientas del agente puesta o quitada."""
    nuevo = dict(data_policy or {})
    if activas:
        nuevo["herramientas"] = True
    else:
        nuevo.pop("herramientas", None)
    return nuevo


async def cargar_data_policy(agent_id: uuid.UUID | None) -> dict[str, Any]:
    """Política del agente del negocio activo; `{}` si no hay agente o no existe."""
    tenant_id = get_current_tenant_id()
    if agent_id is None or tenant_id is None:
        return {}
    async with get_session() as session:
        crudo = (
            await session.execute(
                select(AgentRecord.data_policy).where(
                    AgentRecord.tenant_id == tenant_id,
                    AgentRecord.id == agent_id,
                    AgentRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    return crudo if isinstance(crudo, dict) else {}


async def cargar_ajustes(agent_id: uuid.UUID | None) -> dict[str, dict[str, str]]:
    return politica_de(await cargar_data_policy(agent_id))


async def herramientas_activas(agent_id: uuid.UUID | None) -> bool:
    return herramientas_de(await cargar_data_policy(agent_id))


def _bloque_del_negocio(behavior: dict[str, Any] | None) -> dict[str, Any]:
    bloque = (behavior or {}).get(CLAVE)
    return bloque if isinstance(bloque, dict) else {}


def aviso_de(behavior: dict[str, Any] | None) -> dict[str, Any]:
    """Aviso de tratamiento de datos guardado: `{activo: bool, texto: str}` (texto vacío = el borrador)."""
    crudo = _bloque_del_negocio(behavior).get("aviso")
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

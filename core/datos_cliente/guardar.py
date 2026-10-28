"""Única puerta por la que un agente escribe el perfil del cliente (HU-CRM-16, ADR-0179 decisión 8).

Sin aviso activo (el valor por defecto) escribe todo, igual que antes. Con aviso activo, lo operativo
se guarda siempre y lo demás espera la aceptación del cliente:
- operativo = las claves `requerido` de la acción en curso (según la política del negocio) y las que
  el módulo declara en `operativos` (p. ej. el contacto de un lead que el cliente dejó para que lo llamen);
- el resto solo se guarda si el cliente aceptó la versión vigente del aviso.
Las ediciones del operador desde el panel no pasan por aquí y nunca se bloquean.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from core import consentimiento
from core.datos_cliente import aviso as aviso_mod
from core.datos_cliente.modelo import Nivel
from core.datos_cliente.registro import registro_datos
from core.datos_cliente.servicio import niveles_del_negocio
from core.db.models import AppSettings
from core.db.session import get_session
from core.memory import entity as entity_memory
from core.memory.perfil import normalizar_clave
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


@dataclass
class Guardado:
    guardados: list[str] = field(default_factory=list)
    diferidos: list[str] = field(default_factory=list)  # esperan la aceptación del cliente
    estado_aviso: str = consentimiento.SIN_AVISO
    requiere_aviso: bool = False  # el aviso está activo y el cliente aún no aceptó

    @property
    def requiere_aceptacion(self) -> bool:
        return bool(self.diferidos)


async def _estado(tenant_id: uuid.UUID, customer_id: uuid.UUID) -> tuple[aviso_mod.Aviso, str]:
    from sqlalchemy import select

    async with get_session() as session:
        behavior = (
            await session.execute(
                select(AppSettings.behavior_config).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        aviso = aviso_mod.desde(behavior)
        if not aviso.activo:
            return aviso, consentimiento.SIN_AVISO
        return aviso, await consentimiento.estado_aviso(session, tenant_id, customer_id, aviso.version)


async def _claves_operativas(accion: str | None, operativos: Collection[str]) -> set[str]:
    claves = {normalizar_clave(k) for k in operativos}
    definicion = registro_datos.obtener(accion) if accion else None
    if definicion is not None:
        try:
            niveles = await niveles_del_negocio(definicion)
        except Exception:
            log.warning("datos_cliente_niveles_no_disponibles", accion=accion, exc_info=True)
            niveles = {c.clave: c.nivel for c in definicion.campos}
        claves |= {k for k, n in niveles.items() if n is Nivel.REQUERIDO}
    return claves


async def guardar(
    customer_id: uuid.UUID,
    atributos: Mapping[str, Any],
    *,
    accion: str | None = None,
    operativos: Collection[str] = (),
    source: str = "agent",
    concedidos: Collection[str] | None = None,
) -> Guardado:
    """Guarda en el perfil lo que corresponde. Propaga `NamespaceNoConcedido` y `HechoInvalido`."""
    tenant_id = get_current_tenant_id()
    resultado = Guardado()
    if tenant_id is None or not atributos:
        return resultado
    aviso, estado = await _estado(tenant_id, customer_id)
    resultado.estado_aviso = estado
    permitidos = dict(atributos)
    if aviso.activo and estado != consentimiento.OTORGADO:
        resultado.requiere_aviso = True
        operativas = await _claves_operativas(accion, operativos)
        permitidos = {k: v for k, v in atributos.items() if normalizar_clave(str(k)) in operativas}
        resultado.diferidos = [k for k in atributos if k not in permitidos]
    if permitidos:
        await entity_memory.upsert_attributes(customer_id, permitidos, source=source, concedidos=concedidos)
        resultado.guardados = list(permitidos)
    if resultado.diferidos:
        log.info("datos_cliente_guardado_diferido", n=len(resultado.diferidos), estado=estado)
    return resultado

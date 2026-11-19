"""Aviso al dueño de las referencias retiradas de sus agentes (HU-AOS-59, ADR-0184). Solo lectura.

Mismo detector que `auditar-referencias` (`core.releases.referencias_retiradas`), visto por agente y
en palabras simples (`core.agents.aviso_retiros`). Lo consumen el editor de agentes y el flujo de
publicar. No escribe nada ni corrige: la corrección sigue siendo de una persona del equipo.

Por agente reúne tres cosas:

- el **borrador** (`draft`): es lo que el dueño publica. Se mira siempre, también si es idéntico a la
  última versión publicada (el caso típico: nace de ella). Con una referencia retirada aquí, publicar
  falla; el aviso lo dice antes de intentarlo.
- la **última versión publicada** (`published`);
- lo que **corre hoy** (`in_use`): una release activa de una conexión viva que nombra algo retirado,
  aunque la versión más nueva del agente ya esté limpia.

Debe correr dentro del `tenant_scope` del negocio (lo abre `get_principal` en la API): sin él, RLS
devolvería cero filas y el aviso saldría limpio por error. Por eso falla cerrado.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from core.agents import referencias_retiradas as ref
from core.agents.aviso_retiros import Tipo, avisos_de_contenido, texto_de, tipo_api, tipo_de
from core.releases.referencias_retiradas import (  # lectores del módulo hermano
    _auditar_negocio,
    _leer_agentes,
)
from core.shared.tenant import get_current_tenant_id

Ambito = Literal["draft", "published", "in_use"]


@dataclass(frozen=True)
class Aviso:
    ambito: Ambito
    campo: str
    tipo: Tipo
    nombre: str
    texto: str


@dataclass
class AvisosDeAgente:
    agent_id: uuid.UUID
    key: str
    name: str
    # Número de la última versión publicada (None si nunca se publicó).
    version_publicada: int | None = None
    en_uso: bool = False
    avisos: list[Aviso] = field(default_factory=list)

    @property
    def bloquea_publicar(self) -> bool:
        return any(a.ambito == "draft" for a in self.avisos)


async def avisos_del_negocio(
    tenant_id: uuid.UUID,
    nombres: dict[uuid.UUID, str],
    *,
    modules: Iterable[Any] | None = None,
    agent_id: uuid.UUID | None = None,
) -> list[AvisosDeAgente]:
    """Un `AvisosDeAgente` por cada agente activo del negocio (con avisos o sin ellos; con
    `agent_id`, solo ese). `nombres` es `{agent_id: nombre}` de los agentes del negocio."""
    from core.plugin_manager import plugin_manager

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("avisos de referencias retiradas: fuera del tenant_scope del negocio")
    binario = ref.binario_de(modules if modules is not None else plugin_manager.list_modules())
    agentes, ultimas, borradores = await _leer_agentes(tenant_id)
    negocio = await _auditar_negocio(tenant_id, "", binario)  # lo que corre: releases activas

    por_clave: dict[str, AvisosDeAgente] = {}
    for aid, clave in agentes.items():
        if agent_id is not None and aid != agent_id:
            continue
        ultima = ultimas.get(aid)
        fila = AvisosDeAgente(aid, clave, nombres.get(aid, clave), ultima.numero if ultima else None)
        borrador = borradores.get(aid)
        if borrador is not None:
            for campo, tipo, nombre, texto in avisos_de_contenido(borrador, binario):
                fila.avisos.append(Aviso("draft", campo, tipo, nombre, texto))
        if ultima is not None:
            for campo, tipo, nombre, texto in avisos_de_contenido(ultima.contenido, binario):
                fila.avisos.append(Aviso("published", campo, tipo, nombre, texto))
        por_clave[clave] = fila

    for r in negocio.filas:
        destino = por_clave.get(r.agente)
        if destino is None or (r.ambito == "version" and not r.donde):
            continue  # una versión que no corre en ninguna conexión viva no es «en uso»
        destino.en_uso = True
        if r.ambito == "release" and not any(a.nombre == r.nombre for a in destino.avisos):
            # Lo que corre nombra algo que ni el borrador ni la última versión nombran ya.
            tipo = tipo_de(r.codigo)
            destino.avisos.append(Aviso("in_use", r.ruta, tipo_api(tipo), r.nombre, texto_de(r.ruta, tipo, r.nombre, en_uso=True)))
    return list(por_clave.values())


__all__ = ["Ambito", "Aviso", "AvisosDeAgente", "avisos_del_negocio"]

"""Contrato de los datos de un alta y registro de fuentes (HU-SH-03, ADR-0121 § 3.4).

El core no lee tablas de los packs. Un pack que vende planes de la plataforma (hoy
`pack_subscriptions`, con sus inscripciones) registra una `FuenteDeAltas`: dada la referencia
pública de SU inscripción (`SUB-0001`), devuelve los `DatosDeAlta`. Los datos son de la
inscripción que arma el servidor, no del texto del LLM: el vertical es un id de preset validado
al inscribir.

Este módulo es lo único de `core.provisioning` que un pack puede importar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, TypedDict, runtime_checkable


class DatosDeAlta(TypedDict, total=False):
    """Lo que el consumidor necesita de la inscripción para dar de alta el negocio."""

    plan: str | None  # id del plan de la plataforma (core/billing/plans.py); None = la línea no vende uno
    vertical: str | None  # id de preset (core/presets) elegido en el checkout
    negocio: str | None  # nombre del negocio del comprador
    email: str | None  # correo del owner
    telefono: str | None
    nombre: str | None  # nombre de la persona que compró
    customer_id: str | None  # cliente de la inscripción: el consumidor lo coteja con el del evento
    pagado: bool  # la inscripción cubre su plan (Free nace pagada, D-16)
    origen: str | None  # SPEC-43: 'agente' (chat) o 'panel' (operador); 'panel' pasa por revisión humana


@runtime_checkable
class FuenteDeAltas(Protocol):
    """Fuente de los datos de un alta (implementada por un pack que vende planes)."""

    provider_id: str

    async def datos_de_alta(self, referencia: str) -> DatosDeAlta | None:
        """Datos de la inscripción `referencia` del negocio actual (`tenant_scope`), o None si no
        existe todavía."""
        ...


async def plan_plataforma_de(fuentes: list[FuenteDeAltas], *, plan_id: str | None = None) -> str | None:
    """Plan de la plataforma que vende un plan del catálogo, preguntando a las fuentes del negocio (SPEC-43).

    Usa la operación opcional `plan_de_inscripcion` de la fuente: sirve para un cambio de plan o un cargo
    de período, que no traen una referencia. La primera fuente que responde con un plan de la plataforma que
    existe gana. None si ninguna lo conoce."""
    from core.billing.plans import get_plan

    for fuente in fuentes:
        metodo = getattr(fuente, "plan_de_inscripcion", None)
        if metodo is None or not plan_id:
            continue
        candidato = str(await metodo(str(plan_id)) or "").strip().lower()
        if candidato and get_plan(candidato) is not None:
            return candidato
    return None


@dataclass
class FuentesDeAltasRegistry:
    _fuentes: dict[str, FuenteDeAltas] = field(default_factory=dict)

    def register(self, provider_id: str, fuente: FuenteDeAltas) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._fuentes[provider_id] = fuente

    def unregister(self, provider_id: str) -> None:
        self._fuentes.pop(provider_id, None)

    def provider_ids(self) -> list[str]:
        return sorted(self._fuentes)

    async def del_negocio(self, tenant_id: uuid.UUID) -> list[FuenteDeAltas]:
        """Fuentes de los packs habilitados en el AgentSpec del negocio, por id. Puede haber más de una
        (un segundo proveedor de suscripciones): quien consulta pregunta a cada una hasta que una conozca
        la referencia."""
        from core.agents.spec import enabled_packs, load_active_spec
        from core.shared.tenant import tenant_scope

        async with tenant_scope(tenant_id):
            spec = await load_active_spec(tenant_id)
        packs = enabled_packs(spec)
        return [self._fuentes[pid] for pid in sorted(self._fuentes) if pid in packs]

    async def para_negocio(self, tenant_id: uuid.UUID) -> FuenteDeAltas | None:
        """Fuente de un pack habilitado en el AgentSpec del negocio (la primera por id)."""
        fuentes = await self.del_negocio(tenant_id)
        return fuentes[0] if fuentes else None


fuentes_de_alta = FuentesDeAltasRegistry()

__all__ = ["DatosDeAlta", "FuenteDeAltas", "FuentesDeAltasRegistry", "fuentes_de_alta", "plan_plataforma_de"]

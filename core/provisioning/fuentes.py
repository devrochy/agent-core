"""Contrato de los datos de un alta y registro de fuentes (HU-SH-03, ADR-0121 § 3.4).

El core no lee tablas de los packs. Un pack que vende planes de la plataforma (hoy
`pack_commerce`) registra una `FuenteDeAltas`: dada la referencia de SU pedido, devuelve
los `DatosDeAlta`. Mismo patrón que el catálogo de planes (ADR-0111). Los datos son del
pedido que arma el servidor, no del texto del LLM: el vertical es un id de preset validado
en el checkout.

Este módulo es lo único de `core.provisioning` que un pack puede importar.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, TypedDict, runtime_checkable


class DatosDeAlta(TypedDict, total=False):
    """Lo que el consumidor necesita del pedido para dar de alta el negocio."""

    plan: str | None  # id del plan de la plataforma (core/billing/plans.py); None = la línea no vende uno
    vertical: str | None  # id de preset (core/presets) elegido en el checkout
    negocio: str | None  # nombre del negocio del comprador
    email: str | None  # correo del owner
    telefono: str | None
    nombre: str | None  # nombre de la persona que compró
    customer_id: str | None  # cliente del pedido: el consumidor lo coteja con el del evento
    pagado: bool  # el pedido cubre el plan de la línea (Free nace pagado, D-16)


@runtime_checkable
class FuenteDeAltas(Protocol):
    """Fuente de los datos de un alta (implementada por un pack que vende planes)."""

    provider_id: str

    async def datos_de_alta(
        self, referencia: str, *, variant_id: str | None = None
    ) -> DatosDeAlta | None:
        """Datos del pedido `referencia` del negocio actual (`tenant_scope`), o None si no
        existe todavía. `variant_id` elige la línea de un pedido con varios planes."""
        ...


async def plan_de_la_variante(fuente: FuenteDeAltas | None, variant_id: str | None) -> str | None:
    """Plan de la plataforma que vende una variante del vendedor (HU-SH-04, ADR-0123).

    Operación opcional de la fuente (`plan_de_variante`): sirve para un cambio de plan o un
    cargo de período, que no traen un pedido. None si la fuente no la implementa, si la
    variante no vende un plan de la plataforma o si el plan no existe en el catálogo."""
    from core.billing.plans import get_plan

    if fuente is None or not variant_id:
        return None
    metodo = getattr(fuente, "plan_de_variante", None)
    if metodo is None:
        return None
    plan_id = str(await metodo(str(variant_id)) or "").strip().lower()
    return plan_id if plan_id and get_plan(plan_id) is not None else None


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

    async def para_negocio(self, tenant_id: uuid.UUID) -> FuenteDeAltas | None:
        """Fuente de un pack habilitado en el AgentSpec del negocio (la primera por id)."""
        from core.agents.spec import enabled_packs, load_active_spec
        from core.shared.tenant import tenant_scope

        async with tenant_scope(tenant_id):
            spec = await load_active_spec(tenant_id)
        packs = enabled_packs(spec)
        for pid in sorted(self._fuentes):
            if pid in packs:
                return self._fuentes[pid]
        return None


fuentes_de_alta = FuentesDeAltasRegistry()

__all__ = ["DatosDeAlta", "FuenteDeAltas", "FuentesDeAltasRegistry", "fuentes_de_alta", "plan_de_la_variante"]

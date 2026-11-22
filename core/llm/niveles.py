"""Niveles de IA: el negocio elige un nivel por agente y el core lo traduce al modelo (ADR-0210, HU-BILL-24).

- **Mapa versionado en código** (`MAPA`, `VERSION_DEL_MAPA`): cada nivel fija `primary` y `router`. El `router`
  también va porque `route_model` cambia de modelo en los turnos difíciles: sin él, un turno difícil en Avanzado
  bajaría a flash (§2). El `fallback` no está en el mapa.
- **Al publicar** (`versiones.publicar`) el nivel del agente fija `model.primary` y `model.router` de la versión,
  salvo con llave propia (BYOK): ahí manda el modelo del negocio (§3).
- **En el turno** el plan pone un techo (§4): un modelo que pertenece a un nivel por encima del que incluye el plan
  se recorta al equivalente del techo. Un modelo que el mapa no conoce no es «de un nivel» y pasa intacto.
- El negocio nunca ve el modelo, solo el nivel (§5).

Las funciones de este módulo son puras salvo `techo_del_negocio`, que lee la suscripción.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from core.shared.logger import get_logger

log = get_logger(__name__)

Nivel = Literal["estandar", "avanzado"]
Rol = Literal["primary", "router"]

ESTANDAR: Nivel = "estandar"
AVANZADO: Nivel = "avanzado"
# De menor a mayor: el orden define qué nivel está «por encima» de otro.
NIVELES: tuple[Nivel, ...] = (ESTANDAR, AVANZADO)

# Cambiar un modelo es publicar una versión nueva del mapa: las versiones vivas de los agentes no cambian hasta su
# próxima publicación (ADR-0210 §3).
VERSION_DEL_MAPA = "2026-11-22"


@dataclass(frozen=True)
class ModelosDelNivel:
    primary: str
    router: str

    def de(self, rol: Rol) -> str:
        return self.router if rol == "router" else self.primary


MAPA: Mapping[Nivel, ModelosDelNivel] = MappingProxyType({
    ESTANDAR: ModelosDelNivel(primary="deepseek-flash", router="deepseek-flash"),
    AVANZADO: ModelosDelNivel(primary="deepseek-v4-pro", router="deepseek-v4-pro"),
})


def es_nivel(valor: Any) -> bool:
    return valor in NIVELES


def normalizar(valor: Any) -> Nivel:
    """Un valor que no es un nivel conocido cuenta como Estándar (nunca sube el costo por un dato raro)."""
    return valor if es_nivel(valor) else ESTANDAR  # type: ignore[return-value]


def modelos_de(nivel: Any) -> ModelosDelNivel:
    return MAPA[normalizar(nivel)]


def nivel_del_plan(features: Mapping[str, Any] | None) -> Nivel:
    """El nivel más alto que incluye un plan (`features.nivel_ia` del catálogo, `core/billing/plans.py`)."""
    return normalizar((features or {}).get("nivel_ia"))


def plan_incluye(nivel: Any, features: Mapping[str, Any] | None) -> bool:
    return NIVELES.index(normalizar(nivel)) <= NIVELES.index(nivel_del_plan(features))


def nivel_de_modelo(modelo: str | None) -> Nivel | None:
    """El nivel más bajo cuyo mapa usa ese modelo, o None si el mapa no lo conoce."""
    for nivel in NIVELES:
        if modelo in (MAPA[nivel].primary, MAPA[nivel].router):
            return nivel
    return None


def recortar(modelo: str, techo: Nivel | None, rol: Rol = "primary") -> str:
    """El modelo, o el equivalente del techo si pertenece a un nivel por encima de él. `techo=None` no recorta
    (llave propia o negocio sin plan). Devuelve un texto nuevo: nunca toca el spec (está cacheado por versión)."""
    if techo is None or not modelo:
        return modelo
    nivel = nivel_de_modelo(modelo)
    if nivel is None or NIVELES.index(nivel) <= NIVELES.index(techo):
        return modelo
    return MAPA[techo].de(rol)


def usa_llave_propia(app_settings: Any) -> bool:
    """BYOK: el negocio configuró proveedor y llave. La misma condición que `loop._usa_llave_propia`."""
    return bool(
        app_settings is not None
        and getattr(app_settings, "llm_provider", None)
        and getattr(app_settings, "llm_api_key_encrypted", None)
    )


class NivelNoIncluido(Exception):
    """El plan no incluye el nivel pedido (409 `plan_nivel_ia_not_included`). No se escribió nada."""

    code = "plan_nivel_ia_not_included"

    def __init__(self, nivel: str, plan_id: str | None, plan_name: str) -> None:
        self.nivel = nivel
        self.plan_id = plan_id
        super().__init__(
            f"Tu plan {plan_name} no incluye el nivel de IA Avanzado. Mejora tu plan para usarlo. "
            "Tu agente sigue en Estándar."
        )

    @property
    def details(self) -> dict[str, Any]:
        return {"nivel_ia": self.nivel, "plan": self.plan_id}


@dataclass(frozen=True)
class PlanDelNegocio:
    """Lo que el nivel necesita del plan: su id y el techo. `techo=None` = sin suscripción o plan desconocido."""

    plan_id: str | None
    plan_name: str
    techo: Nivel | None


async def plan_del_negocio(tenant_id: uuid.UUID, *, session: Any = None) -> PlanDelNegocio:
    """El techo del plan activo. Sin suscripción o con un plan que el catálogo no conoce no hay techo
    (grandfathered, el mismo criterio que `enforcement.quota_decision`)."""
    from core.billing.plans import get_plan
    from core.billing.service import get_active_subscription

    sub = await get_active_subscription(tenant_id, session=session)
    if sub is None:
        return PlanDelNegocio(None, "—", None)
    plan = get_plan(sub.plan_id)
    if plan is None:
        return PlanDelNegocio(sub.plan_id, sub.plan_id, None)
    return PlanDelNegocio(plan.id, plan.name, nivel_del_plan(plan.features))


async def techo_del_negocio(tenant_id: uuid.UUID) -> Nivel | None:
    """El techo del turno (ADR-0210 §4). Si la lectura falla se cierra en Estándar: el techo protege el costo."""
    try:
        return (await plan_del_negocio(tenant_id)).techo
    except PermissionError:
        raise
    except Exception:
        log.warning("nivel_ia_techo_no_leido", tenant_id=str(tenant_id), exc_info=True)
        return ESTANDAR


__all__ = [
    "AVANZADO",
    "ESTANDAR",
    "MAPA",
    "NIVELES",
    "VERSION_DEL_MAPA",
    "ModelosDelNivel",
    "Nivel",
    "NivelNoIncluido",
    "PlanDelNegocio",
    "es_nivel",
    "modelos_de",
    "nivel_de_modelo",
    "nivel_del_plan",
    "normalizar",
    "plan_del_negocio",
    "plan_incluye",
    "recortar",
    "techo_del_negocio",
    "usa_llave_propia",
]

"""Recorrido del onboarding obligatorio pero saltable (HU-SH-10, adenda a ADR-0124).

Los **pasos** del checklist siguen calculándose del estado real del negocio (ADR-0124: ningún paso
se marca a mano). Lo que se guarda aquí es el **recorrido** de la persona por el asistente de
primer uso: dónde va, si lo omitió o si lo terminó. Vive en `tenants.settings.onboarding`
(JSONB, sin migración) y se escribe con un UPDATE atómico que no pisa el resto de `settings`.

Estados:

- `pendiente`: nunca lo abrió (clave ausente). El panel lo abre solo, a pantalla completa.
- `en_curso`: empezó y no ha terminado ni omitido.
- `omitido`: lo saltó. El panel deja un aviso visible y permanente hasta que lo retome.
- `completo`: lo validó el servidor (`finalizar`), nunca el panel.
- `no_aplica`: no se calcula de la clave; es del negocio. Negocios creados antes de
  `ONBOARDING_DESDE`, o todos si la variable está vacía (función apagada).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, Literal

from sqlalchemy import text

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

EstadoDelRecorrido = Literal["pendiente", "en_curso", "omitido", "completo", "no_aplica"]
ESTADOS_GUARDADOS: Final = ("en_curso", "omitido", "completo")

TOTAL_PASOS: Final = 6
ACCION_AUDITORIA: Final = "onboarding_recorrido"


class NegocioNoEncontrado(LookupError):
    pass


class TransicionInvalida(ValueError):
    """La acción no aplica al estado actual (p. ej. omitir un recorrido ya completo)."""


class FaltanPasos(Exception):
    """`finalizar` sin lo que lo hace completo. `faltan` lleva las claves, en orden."""

    def __init__(self, faltan: list[str]) -> None:
        super().__init__(", ".join(faltan))
        self.faltan = faltan


@dataclass(frozen=True)
class Recorrido:
    estado: EstadoDelRecorrido
    paso: int
    aplica: bool
    omitido_en: datetime | None = None
    completado_en: datetime | None = None
    agente_id: str | None = None
    conexion_id: str | None = None
    probado_en: datetime | None = None

    @property
    def obliga(self) -> bool:
        """El panel abre el asistente solo si el recorrido es del negocio y nunca se tocó."""
        return self.estado == "pendiente"

    @property
    def avisa(self) -> bool:
        """El aviso persistente se ve mientras el recorrido está empezado o saltado."""
        return self.estado in ("en_curso", "omitido")


def _fecha(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


def aplica_al_negocio(creado_en: datetime, desde: date | None) -> bool:
    """Solo los negocios creados desde `ONBOARDING_DESDE` pasan por el recorrido. Sin fecha, ninguno
    (función apagada): así desplegar el código no fuerza nada a los negocios que ya operan."""
    if desde is None:
        return False
    ref = creado_en if creado_en.tzinfo else creado_en.replace(tzinfo=UTC)
    return ref.date() >= desde


def desde_crudo(crudo: Any, *, creado_en: datetime, desde: date | None) -> Recorrido:
    """El recorrido a partir de `settings.onboarding` (tolera claves ausentes o mal formadas)."""
    datos = crudo if isinstance(crudo, dict) else {}
    estado = datos.get("estado")
    if estado in ESTADOS_GUARDADOS:
        # Si ya se guardó, manda lo guardado aunque cambie la fecha de corte.
        paso = datos.get("paso")
        return Recorrido(
            estado=estado,
            paso=min(max(int(paso), 1), TOTAL_PASOS) if isinstance(paso, int) else 1,
            aplica=True,
            omitido_en=_fecha(datos.get("omitido_en")),
            completado_en=_fecha(datos.get("completado_en")),
            agente_id=datos.get("agente_id") if isinstance(datos.get("agente_id"), str) else None,
            conexion_id=datos.get("conexion_id")
            if isinstance(datos.get("conexion_id"), str)
            else None,
            probado_en=_fecha(datos.get("probado_en")),
        )
    if not aplica_al_negocio(creado_en, desde):
        return Recorrido(estado="no_aplica", paso=1, aplica=False)
    return Recorrido(estado="pendiente", paso=1, aplica=True)


# --------------------------------------------------------------------- transiciones (puras)


def cambios_de_avanzar(actual: Recorrido, paso: int) -> dict[str, Any]:
    if not actual.aplica:
        raise TransicionInvalida("el recorrido no aplica a este negocio")
    if actual.estado == "completo":
        raise TransicionInvalida("el recorrido ya está completo")
    if not 1 <= paso <= TOTAL_PASOS:
        raise TransicionInvalida(f"el paso debe estar entre 1 y {TOTAL_PASOS}")
    # Retomar un recorrido omitido es avanzar: vuelve a `en_curso`.
    return {"estado": "en_curso", "paso": paso}


def cambios_de_omitir(actual: Recorrido, *, por: str, ahora: datetime) -> dict[str, Any]:
    if not actual.aplica:
        raise TransicionInvalida("el recorrido no aplica a este negocio")
    if actual.estado == "completo":
        raise TransicionInvalida("el recorrido ya está completo")
    return {
        "estado": "omitido",
        "paso": actual.paso,
        "omitido_en": ahora.isoformat(),
        "omitido_por": por,
    }


def cambios_de_reanudar(actual: Recorrido) -> dict[str, Any]:
    if actual.estado != "omitido":
        raise TransicionInvalida("solo se reanuda un recorrido omitido")
    return {"estado": "en_curso", "paso": actual.paso}


def claves_que_faltan(
    actual: Recorrido, *, datos_negocio_completo: bool, prueba_hecha: bool
) -> list[str]:
    """Lo que impide dar por completo el recorrido. Lo decide el servidor con el estado real."""
    faltan: list[str] = []
    if not datos_negocio_completo:
        faltan.append("datos_negocio")
    if not actual.agente_id:
        faltan.append("agente")
    if not actual.conexion_id:
        faltan.append("agente_publicado")
    if not (prueba_hecha or actual.probado_en):
        faltan.append("prueba")
    return faltan


def cambios_de_finalizar(
    actual: Recorrido, faltan: list[str], *, ahora: datetime
) -> dict[str, Any]:
    if not actual.aplica:
        raise TransicionInvalida("el recorrido no aplica a este negocio")
    if actual.estado == "completo":
        raise TransicionInvalida("el recorrido ya está completo")
    if faltan:
        raise FaltanPasos(faltan)
    return {"estado": "completo", "paso": TOTAL_PASOS, "completado_en": ahora.isoformat()}


# --------------------------------------------------------------------- base de datos


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("onboarding: fuera del tenant_scope del negocio")


def desde_fecha_de_corte() -> date | None:
    from core.config import get_settings

    return get_settings().onboarding_desde


async def leer(tenant_id: uuid.UUID) -> Recorrido:
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT settings->'onboarding', created_at FROM tenants WHERE id = :t AND deleted_at IS NULL"
                ),
                {"t": tenant_id},
            )
        ).first()
    if fila is None:
        raise NegocioNoEncontrado(str(tenant_id))
    return desde_crudo(fila[0], creado_en=fila[1], desde=desde_fecha_de_corte())


async def fijar(
    tenant_id: uuid.UUID, cambios: dict[str, Any], *, actor: str, accion: str
) -> Recorrido:
    """Mezcla `cambios` dentro de `settings.onboarding` con un UPDATE atómico (no pisa el resto de
    `settings`) y lo audita. Devuelve el recorrido resultante."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{onboarding}', "
                    "coalesce(settings->'onboarding', '{}'::jsonb) || cast(:cambios as jsonb)) "
                    "WHERE id = :t AND deleted_at IS NULL RETURNING settings->'onboarding', created_at"
                ),
                {"t": tenant_id, "cambios": json.dumps(cambios)},
            )
        ).first()
        if fila is None:
            raise NegocioNoEncontrado(str(tenant_id))
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor[:120],
                action=ACCION_AUDITORIA,
                input={"accion": accion, "cambios": cambios},
            )
        )
    log.info(
        "onboarding_recorrido",
        tenant_id=str(tenant_id),
        accion=accion,
        estado=cambios.get("estado"),
    )
    return desde_crudo(fila[0], creado_en=fila[1], desde=desde_fecha_de_corte())


async def marcar_prueba(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Anota la primera prueba con el agente que el onboarding dejó listo (idempotente). Solo cuenta
    si el recorrido está empezado y la conexión es la que `dejar-listo` guardó."""
    actual = await leer(tenant_id)
    if actual.estado not in ("en_curso", "omitido") or actual.probado_en is not None:
        return
    if actual.conexion_id != str(connection_id):
        return
    ahora = datetime.now(UTC)
    await fijar(tenant_id, {"probado_en": ahora.isoformat()}, actor="core:onboarding", accion="probar")


__all__ = [
    "ACCION_AUDITORIA",
    "TOTAL_PASOS",
    "FaltanPasos",
    "NegocioNoEncontrado",
    "Recorrido",
    "TransicionInvalida",
    "aplica_al_negocio",
    "cambios_de_avanzar",
    "cambios_de_finalizar",
    "cambios_de_omitir",
    "cambios_de_reanudar",
    "claves_que_faltan",
    "desde_crudo",
    "fijar",
    "leer",
    "marcar_prueba",
]

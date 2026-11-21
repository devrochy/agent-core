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

La apertura automática y el aviso solo tienen sentido mientras el negocio no tenga un agente propio
**ajeno a la bienvenida**: en cuanto existe uno, el recorrido deja de insistir (`con_agente`). No cuentan
el Coordinador del alta (`kind = 'core'`), que lo crea el sistema para todos, ni el agente que creó la
propia bienvenida (el `agente_id` que guarda `dejar-listo`): la bienvenida de 5 estaciones (SPEC-49) lo
crea al salir de la estación 1 y el aviso tiene que seguir hasta terminarla (decisión del dueño del
2026-11-20). Un agente creado por «Nuevo agente», o antes de existir el recorrido, sí apaga la insistencia.
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

# SPEC-49 (HU-AOS-84, D-77): la bienvenida es la ruta de 5 estaciones: 1 Tu negocio y tipo de agente ·
# 2 Habilidades y formas de pago · 3 Datos del cliente · 4 Datos para probar · 5 Revisar, probar y publicar.
# Antes fueron 4 (HU-AOS-81), 9 (HU-UX-23) y 6 (HU-UX-21, la APK todavía los manda sin `pasos`). Lo guardado
# lleva `pasos` con el total con que se guardó; uno de 4, 9 o 6 se traduce al leerlo.
TOTAL_PASOS: Final = 5
PASOS_DE_4: Final = 4
PASOS_ANTERIORES: Final = 9
PASO_DE_6_A_9: Final = {1: 1, 2: 3, 3: 4, 4: 4, 5: 7, 6: 8}
PASO_DE_9_A_4: Final = {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2, 7: 3, 8: 4, 9: 4}
PASO_DE_4_A_5: Final = {1: 1, 2: 2, 3: 4, 4: 5}
# Totales con que un cliente puede numerar `avanzar` (sin `pasos`, la escala de 6 de la APK).
ESCALAS_DE_AVANZAR: Final = frozenset({PASOS_DE_4, TOTAL_PASOS, PASOS_ANTERIORES})
# Al dejar listo el agente sigue «Habilidades».
PASO_DE_DEJAR_LISTO: Final = 2
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
    # El negocio ya tiene un agente propio ajeno a la bienvenida: ni el Coordinador del sistema ni el
    # `agente_id` que creó el recorrido.
    con_agente: bool = False

    @property
    def obliga(self) -> bool:
        """El panel abre el asistente solo si el recorrido es del negocio, nunca se tocó y no hay
        ningún agente propio ajeno a la bienvenida."""
        return self.estado == "pendiente" and not self.con_agente

    @property
    def avisa(self) -> bool:
        """El aviso persistente se ve mientras el recorrido está empezado o saltado y el negocio no
        tiene un agente propio ajeno a la bienvenida (el que creó la bienvenida no lo apaga)."""
        return self.estado in ("en_curso", "omitido") and not self.con_agente


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


def desde_crudo(
    crudo: Any, *, creado_en: datetime, desde: date | None, con_agente: bool = False
) -> Recorrido:
    """El recorrido a partir de `settings.onboarding` (tolera claves ausentes o mal formadas)."""
    datos = crudo if isinstance(crudo, dict) else {}
    estado = datos.get("estado")
    if estado in ESTADOS_GUARDADOS:
        # Si ya se guardó, manda lo guardado aunque cambie la fecha de corte.
        return Recorrido(
            estado=estado,
            paso=paso_guardado(datos),
            aplica=True,
            omitido_en=_fecha(datos.get("omitido_en")),
            completado_en=_fecha(datos.get("completado_en")),
            agente_id=datos.get("agente_id") if isinstance(datos.get("agente_id"), str) else None,
            conexion_id=datos.get("conexion_id")
            if isinstance(datos.get("conexion_id"), str)
            else None,
            probado_en=_fecha(datos.get("probado_en")),
            con_agente=con_agente,
        )
    if not aplica_al_negocio(creado_en, desde):
        return Recorrido(estado="no_aplica", paso=1, aplica=False, con_agente=con_agente)
    return Recorrido(estado="pendiente", paso=1, aplica=True, con_agente=con_agente)


def paso_de_6_a_9(paso: int) -> int:
    """El paso de 9 que corresponde a uno del asistente de 6. Uno fuera de 1..6 llega ya numerado sobre 9
    (un cliente viejo que reenvía lo que leyó del servidor) y se deja como está."""
    return PASO_DE_6_A_9.get(paso, paso)


def paso_de_4(paso: int, pasos: int | None) -> int:
    """El paso de la creación rápida de 4 (HU-AOS-81) que corresponde a `paso` numerado sobre `pasos` (4, 9 o,
    sin dato, 6). Un `{paso: 5, pasos: 4}` solo lo escribía el `dejar_listo` anterior a HU-AOS-84 con la
    numeración de 9 («Habilidades»): un paso mayor que 4 guardado con 4 se lee sobre 9."""
    if pasos == PASOS_DE_4 and paso <= PASOS_DE_4:
        return max(paso, 1)
    if pasos in (PASOS_DE_4, PASOS_ANTERIORES):
        de_9 = paso
    else:
        de_9 = paso_de_6_a_9(min(max(paso, 1), 6))
    return PASO_DE_9_A_4.get(min(max(de_9, 1), PASOS_ANTERIORES), PASOS_DE_4)


def paso_de_5(paso: int, pasos: int | None) -> int:
    """El paso de la bienvenida de 5 que corresponde a `paso` numerado sobre `pasos` (5, 4, 9 o, sin dato, 6).
    Lo de 9 y de 6 pasa por las tablas de siempre hasta la escala de 4 y de ahí a la de 5. Siempre 1..5."""
    if pasos == TOTAL_PASOS:
        return min(max(paso, 1), TOTAL_PASOS)
    return PASO_DE_4_A_5[paso_de_4(paso, pasos)]


def paso_guardado(datos: dict[str, Any]) -> int:
    """El paso guardado, entre 1 y `TOTAL_PASOS`. Uno guardado con 4, 9 o 6 pasos se lleva a su
    equivalente de 5. Un recorrido completo queda en el último paso."""
    paso = datos.get("paso")
    if not isinstance(paso, int) or isinstance(paso, bool):
        return 1
    if datos.get("estado") == "completo":
        return TOTAL_PASOS
    pasos = datos.get("pasos")
    if not isinstance(pasos, int) or isinstance(pasos, bool):
        pasos = None
    return paso_de_5(paso, pasos)


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

# Un agente propio ajeno a la bienvenida es cualquier fila vigente de `agents` del negocio que no sea el
# Coordinador (`kind='core'`), que se crea con el negocio, ni el agente que guardó la bienvenida en
# `settings.onboarding.agente_id`. Se compara como texto para que un valor guardado mal formado no rompa la
# lectura. Se evalúa en el mismo query (también en el RETURNING de `fijar`, sobre la fila ya escrita): el
# aviso se decide en cada lectura.
_AGENTE_PROPIO_SQL: Final = (
    "EXISTS (SELECT 1 FROM agents a WHERE a.tenant_id = tenants.id "
    "AND a.deleted_at IS NULL AND a.kind <> 'core' "
    "AND a.id::text <> coalesce(tenants.settings #>> '{onboarding,agente_id}', ''))"
)


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
                    "SELECT settings->'onboarding', created_at, "
                    f"{_AGENTE_PROPIO_SQL} FROM tenants WHERE id = :t AND deleted_at IS NULL"
                ),
                {"t": tenant_id},
            )
        ).first()
    if fila is None:
        raise NegocioNoEncontrado(str(tenant_id))
    return desde_crudo(
        fila[0], creado_en=fila[1], desde=desde_fecha_de_corte(), con_agente=bool(fila[2])
    )


async def fijar(
    tenant_id: uuid.UUID, cambios: dict[str, Any], *, actor: str, accion: str
) -> Recorrido:
    """Mezcla `cambios` dentro de `settings.onboarding` con un UPDATE atómico (no pisa el resto de
    `settings`) y lo audita. Devuelve el recorrido resultante."""
    from core.db.models import AuditLog
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    # Todo lo que se escribe queda con el total de 5 pasos. Si el cambio no trae `paso`, se escribe el ya
    # traducido: si no, un recorrido guardado con 4, 9 o 6 quedaría marcado como de 5 con el número viejo.
    if "paso" not in cambios:
        cambios = {**cambios, "paso": (await leer(tenant_id)).paso}
    cambios = {**cambios, "pasos": TOTAL_PASOS}
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{onboarding}', "
                    "coalesce(settings->'onboarding', '{}'::jsonb) || cast(:cambios as jsonb)) "
                    "WHERE id = :t AND deleted_at IS NULL "
                    f"RETURNING settings->'onboarding', created_at, {_AGENTE_PROPIO_SQL}"
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
    return desde_crudo(
        fila[0], creado_en=fila[1], desde=desde_fecha_de_corte(), con_agente=bool(fila[2])
    )


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
    "ESCALAS_DE_AVANZAR",
    "PASOS_ANTERIORES",
    "PASOS_DE_4",
    "PASO_DE_4_A_5",
    "PASO_DE_6_A_9",
    "PASO_DE_9_A_4",
    "PASO_DE_DEJAR_LISTO",
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
    "paso_de_4",
    "paso_de_5",
    "paso_de_6_a_9",
    "paso_guardado",
]

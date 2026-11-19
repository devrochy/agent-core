"""Hitos del onboarding y embudo de activación (HU-SH-06, ADR-0124).

Un hito es la **primera vez** que un negocio cumplió un paso, con los segundos desde su alta
(`tenants.created_at`, la misma fila que el alta automática deja en `provisioning_requests` y en
`audit_log` como `negocio_creado`). Se registra al evaluar los primeros pasos (cada vez que se
abre el Resumen) y en el barrido del embudo (`agent-core tenant embudo`). Es idempotente: el
índice único `(tenant_id, paso)` y `ON CONFLICT DO NOTHING` hacen que dos evaluaciones a la
vez no dupliquen nada. Si el paso después se deshace (se desconectó el WhatsApp), el hito queda:
mide cuándo se llegó, no el estado de hoy.

La hora sale del propio dato cuando lo hay (`fuente = dato`: la conexión, el primer producto,
el primer turno); si no, es la hora en que se detectó (`fuente = deteccion`).
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import OnboardingHito, Tenant, utcnow
from core.db.session import get_session
from core.primeros_pasos import ACTOR, reglas
from core.primeros_pasos.calculo import Evaluacion, calcular
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

Fuente = Literal["dato", "deteccion"]
UN_DIA = 24 * 3600

# Orden del embudo: el recorrido de la HU, de la invitación al primer mensaje real.
ORDEN_DEL_EMBUDO = (
    reglas.INVITACION_ACEPTADA,
    reglas.DATOS_NEGOCIO,
    reglas.WHATSAPP,
    reglas.CATALOGO,
    reglas.PROBAR_ASISTENTE,
    reglas.EQUIPO,
    reglas.ACTIVAR_PRESET,
    reglas.PRIMER_MENSAJE,
)


# Nombres legibles para el reporte del embudo (CLI del master).
_ETIQUETAS = {
    reglas.INVITACION_ACEPTADA: "Invitación aceptada",
    reglas.DATOS_NEGOCIO: "Datos del negocio",
    reglas.WHATSAPP: "WhatsApp conectado y verificado",
    reglas.CATALOGO: "Catálogo o servicios",
    reglas.PROBAR_ASISTENTE: "Probó el asistente",
    reglas.EQUIPO: "Equipo invitado",
    reglas.ACTIVAR_PRESET: "Tipo de negocio activado",
    reglas.PRIMER_MENSAJE: "Primer mensaje de un cliente",
}


def etiqueta(paso: str) -> str:
    if paso.startswith(reglas.PREFIJO_CONFIGURAR):
        return f"Configuró {paso.removeprefix(reglas.PREFIJO_CONFIGURAR)}"
    return _ETIQUETAS.get(paso, paso)


@dataclass(frozen=True)
class Hito:
    paso: str
    completado_en: datetime
    segundos_desde_alta: int
    fuente: Fuente


async def hitos_de(tenant_id: uuid.UUID) -> dict[str, Hito]:
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(OnboardingHito).where(
                        OnboardingHito.tenant_id == tenant_id, OnboardingHito.deleted_at.is_(None)
                    )
                )
            ).scalars().all()
    return {
        f.paso: Hito(
            paso=f.paso,
            completado_en=f.completado_en,
            segundos_desde_alta=f.segundos_desde_alta,
            fuente="dato" if f.fuente == "dato" else "deteccion",
        )
        for f in filas
    }


def candidatos(evaluacion: Evaluacion) -> dict[str, datetime | None]:
    """Lo que hoy está cumplido: los pasos completos y los bordes del embudo que ya ocurrieron."""
    cumplidos: dict[str, datetime | None] = {p.clave: p.completado_en for p in evaluacion.pasos if p.completo}
    cumplidos.update(evaluacion.hitos_del_embudo)
    return cumplidos


async def registrar(
    tenant_id: uuid.UUID,
    alta_en: datetime,
    cumplidos: Mapping[str, datetime | None],
    registrados: Mapping[str, Hito],
) -> list[Hito]:
    """Registra la primera vez de cada paso cumplido que todavía no tenía hito. Devuelve los
    nuevos (los que ganó esta llamada)."""
    ahora = utcnow()
    filas: list[dict[str, object]] = []
    for paso, en in cumplidos.items():
        if paso in registrados:
            continue
        fuente: Fuente = "dato" if en is not None else "deteccion"
        cuando = min(en, ahora) if en is not None else ahora
        filas.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "paso": paso,
                "completado_en": cuando,
                "segundos_desde_alta": reglas.segundos_desde_alta(alta_en, cuando),
                "fuente": fuente,
                "created_at": ahora,
            }
        )
    if not filas:
        return []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            insertados = set(
                (
                    await session.execute(
                        pg_insert(OnboardingHito)
                        .values(filas)
                        .on_conflict_do_nothing(
                            index_elements=["tenant_id", "paso"], index_where=text("deleted_at is null")
                        )
                        .returning(OnboardingHito.paso)
                    )
                ).scalars()
            )
        nuevos = [
            Hito(
                paso=str(f["paso"]),
                completado_en=f["completado_en"],  # type: ignore[arg-type]
                segundos_desde_alta=int(f["segundos_desde_alta"]),  # type: ignore[call-overload]
                fuente=f["fuente"],  # type: ignore[arg-type]
            )
            for f in filas
            if f["paso"] in insertados
        ]
        for hito in nuevos:
            await record_audit(
                actor=ACTOR,
                action="primer_paso_completado",
                output_data={
                    "paso": hito.paso,
                    "segundos_desde_alta": hito.segundos_desde_alta,
                    "fuente": hito.fuente,
                },
            )
            log.info(
                "onboarding_hito",
                tenant_id=str(tenant_id),
                paso=hito.paso,
                segundos_desde_alta=hito.segundos_desde_alta,
                fuente=hito.fuente,
            )
    return nuevos


async def evaluar(tenant_id: uuid.UUID) -> tuple[Evaluacion, dict[str, Hito]] | None:
    """Calcula los primeros pasos del negocio y registra los hitos nuevos. Devuelve la evaluación
    y todos sus hitos (los de antes y los de ahora), o None si el negocio no existe.

    Registrar es secundario: si falla, la evaluación igual se devuelve (queda en el log)."""
    registrados = await hitos_de(tenant_id)
    evaluacion = await calcular(tenant_id, omitir_hitos=registrados.keys())
    if evaluacion is None:
        return None
    hitos = dict(registrados)
    try:
        nuevos = await registrar(tenant_id, evaluacion.alta_en, candidatos(evaluacion), registrados)
    except Exception:
        log.warning("onboarding_hitos_no_registrados", tenant_id=str(tenant_id), exc_info=True)
        nuevos = []
    hitos.update({h.paso: h for h in nuevos})
    return evaluacion, hitos


# ------------------------------------------------------------------ embudo
@dataclass(frozen=True)
class FilaDelEmbudo:
    paso: str
    negocios: int  # cuántos negocios de la ventana llegaron a este paso
    porcentaje: float  # sobre las altas de la ventana
    mediana_segundos: int | None  # tiempo desde el alta (mediana)
    en_24_horas: int  # cuántos llegaron dentro de las primeras 24 horas


@dataclass(frozen=True)
class Embudo:
    desde: datetime
    hasta: datetime
    altas: int
    filas: list[FilaDelEmbudo]


def _orden(paso: str) -> tuple[int, str]:
    if paso in ORDEN_DEL_EMBUDO:
        return ORDEN_DEL_EMBUDO.index(paso), paso
    return len(ORDEN_DEL_EMBUDO), paso  # `configurar_<pack>` al final, por nombre


def armar_embudo(desde: datetime, hasta: datetime, hitos_por_negocio: list[dict[str, Hito]]) -> Embudo:
    """Agrega los hitos de los negocios dados de alta en la ventana (función pura)."""
    altas = len(hitos_por_negocio)
    por_paso: dict[str, list[int]] = {}
    for hitos in hitos_por_negocio:
        for paso, hito in hitos.items():
            por_paso.setdefault(paso, []).append(hito.segundos_desde_alta)
    filas = [
        FilaDelEmbudo(
            paso=paso,
            negocios=len(segundos),
            porcentaje=round(100 * len(segundos) / altas, 1) if altas else 0.0,
            mediana_segundos=int(statistics.median(segundos)) if segundos else None,
            en_24_horas=sum(1 for s in segundos if s <= UN_DIA),
        )
        for paso, segundos in sorted(por_paso.items(), key=lambda par: _orden(par[0]))
    ]
    return Embudo(desde=desde, hasta=hasta, altas=altas, filas=filas)


async def embudo(
    desde: datetime, hasta: datetime, *, solo_automaticas: bool = False, barrer: bool = True
) -> Embudo:
    """Embudo de activación de los negocios dados de alta en `[desde, hasta)`.

    `barrer` evalúa antes cada negocio (registra lo que se cumplió aunque nadie haya abierto el
    Resumen). `solo_automaticas`: solo los del alta automática (ADR-0121)."""
    async with get_session() as session:
        negocios = (
            await session.execute(
                select(Tenant.id, Tenant.settings).where(
                    Tenant.created_at >= desde,
                    Tenant.created_at < hasta,
                    Tenant.deleted_at.is_(None),
                )
            )
        ).all()
    hitos_por_negocio: list[dict[str, Hito]] = []
    for tenant_id, settings in negocios:
        alta = (settings or {}).get("alta")
        if solo_automaticas and not (isinstance(alta, Mapping) and alta.get("origen") == "provisioning"):
            continue
        if barrer:
            try:
                await evaluar(tenant_id)
            except Exception:
                log.warning("onboarding_embudo_negocio_fallido", tenant_id=str(tenant_id), exc_info=True)
        hitos_por_negocio.append(await hitos_de(tenant_id))
    return armar_embudo(desde, hasta, hitos_por_negocio)

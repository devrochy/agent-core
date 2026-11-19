"""Política por negocio de los crons (ADR-0201).

Único punto de lectura y escritura de `tenant_cron_configs` (tabla de NEGOCIO con RLS). Tres
usuarios:

- el helper de cada cron de negocio (`core/tasks/cron_negocio.py`): ¿debe correr para este
  negocio?, ¿a qué hora/ventana?, y registrar el resumen de la última ejecución;
- las rutas de `/admin/automatizaciones` (dueño, colaborador con `write` y master);
- la vista por negocio del master en `Plataforma › Crons` (fase de historial).

Sin fila = valores por defecto: un cron de pack está encendido si el pack lo está (el recorrido
ya filtra por `enabled_packs`); uno de core, siempre. Un valor desconocido o un fallo de lectura
nunca enciende nada por accidente: el cron se salta.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from core.admin.crons_service import proxima_ejecucion
from core.db.models import AuditLog, TenantCronConfig, TenantCronRun, utcnow
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tiempo_negocio import zona_del_negocio
from core.tasks.cron_catalog import CATALOGO, MODULOS, CronMeta, get_meta

log = get_logger(__name__)

# Motivos de omisión por política (se muestran en la pantalla y en el historial).
OMITIDA_DESHABILITADA = "deshabilitado_por_negocio"
OMITIDA_FUERA_DE_HORA = "fuera_de_hora"
OMITIDA_FUERA_DE_VENTANA = "fuera_de_ventana"

# Centinela para distinguir «no cambies este campo» de «ponlo en NULL».
SIN_CAMBIO: Any = object()

# Throttle del resumen de la última ejecución en barridos sin efecto.
RESUMEN_MIN_INTERVALO = timedelta(hours=1)
# Retención del historial detallado (ADR-0201).
RETENCION_DIAS = 90


class CronNegocioNoEncontrado(LookupError):
    """El cron no existe o no es de ámbito negocio."""


class CronNegocioNoConfigurable(ValueError):
    """El cron no se puede configurar por negocio (o el campo no aplica)."""


class CronNegocioInvalido(ValueError):
    """La hora o la ventana no son válidas."""


@dataclass(frozen=True)
class PoliticaCron:
    """Resultado de preguntar si un cron debe correr para un negocio."""

    cron_name: str
    habilitada: bool
    hora_local: int | None
    ventana: dict[str, Any] | None
    motivo: str | None = None

    @property
    def debe_correr(self) -> bool:
        return self.motivo is None


@dataclass
class ConfigCronNegocio:
    """Fila de configuración (o los valores por defecto si no hay fila)."""

    cron_name: str
    habilitada: bool = True
    hora_local: int | None = None
    ventana: dict[str, Any] | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_resumen: str | None = None
    last_acciones: int | None = None
    updated_at: datetime | None = None


@dataclass
class CronNegocioView:
    """Lo que pinta la pantalla de Automatizaciones (catálogo + configuración + última)."""

    name: str
    module: str
    label: str
    description: str
    pack: str | None
    sensitive: bool
    configurable: bool
    admite_hora_local: bool
    admite_ventana: bool
    parametros: tuple[str, ...]
    habilitada: bool
    hora_local: int | None
    hora_local_por_defecto: int | None
    hora_local_personalizada: bool
    ventana: dict[str, Any] | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    last_resumen: str | None
    last_acciones: int | None
    acciones: list[str] = field(default_factory=list)


def _meta_configurable(cron_name: str) -> CronMeta:
    meta = get_meta(cron_name)
    if meta is None or meta.ambito != "negocio":
        raise CronNegocioNoEncontrado(cron_name)
    if not meta.configurable:
        raise CronNegocioNoConfigurable(f"«{meta.label}» no se configura por negocio.")
    return meta


async def cargar(tenant_id: uuid.UUID, cron_name: str) -> ConfigCronNegocio | None:
    """Fila viva del negocio, o None si nunca se configuró (defaults del catálogo)."""
    async with get_session() as session:
        row = (
            await session.execute(
                select(TenantCronConfig).where(
                    TenantCronConfig.tenant_id == tenant_id,
                    TenantCronConfig.cron_name == cron_name,
                    TenantCronConfig.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return ConfigCronNegocio(
        cron_name=row.cron_name,
        habilitada=bool(row.habilitada),
        hora_local=row.hora_local,
        ventana=row.ventana,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        last_resumen=row.last_resumen,
        last_acciones=row.last_acciones,
        updated_at=row.updated_at,
    )


def en_ventana(local: datetime, ventana: dict[str, Any] | None) -> bool:
    """¿La hora de pared `local` cae en la ventana? Formato `{desde, hasta, dias}`.

    `dias` usa 0=lunes … 6=domingo (`datetime.weekday()`). Una ventana que cruza la medianoche
    (`desde > hasta`) cuenta el día en que empieza.
    """
    if not ventana:
        return True
    try:
        desde = time.fromisoformat(str(ventana.get("desde") or "00:00"))
        hasta = time.fromisoformat(str(ventana.get("hasta") or "23:59"))
    except ValueError:
        return True
    dias = ventana.get("dias")
    dias_validos = {int(d) for d in dias} if isinstance(dias, list) else set(range(7))
    if not dias_validos:
        dias_validos = set(range(7))
    ahora = local.time().replace(tzinfo=None)
    if desde <= hasta:
        return local.weekday() in dias_validos and desde <= ahora <= hasta
    # Cruza medianoche: [desde, 24h) del día de inicio o [0, hasta] del día siguiente.
    if ahora >= desde:
        return local.weekday() in dias_validos
    if ahora <= hasta:
        return ((local.weekday() - 1) % 7) in dias_validos
    return False


async def politica(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    ahora: datetime | None = None,
) -> PoliticaCron:
    """¿Debe correr este cron para este negocio ahora? Fail-open solo en la ausencia de política.

    Un cron de plataforma (o desconocido) siempre corre: el wrapper global ya decide. Un cron de
    negocio sin fila corre con los defaults. Si la lectura falla, se salta (fail-closed).
    """
    meta = get_meta(cron_name)
    if meta is None or meta.ambito != "negocio":
        return PoliticaCron(cron_name, True, None, None)

    try:
        config = await cargar(tenant_id, cron_name)
    except Exception as exc:  # base caída: no enciendas una automatización por accidente
        log.error("cron_negocio_politica_fallida", task=cron_name, error=type(exc).__name__)
        return PoliticaCron(cron_name, False, None, None, OMITIDA_DESHABILITADA)

    habilitada = True if config is None else config.habilitada
    # La hora efectiva: la que eligió el negocio o la del catálogo mientras no elija.
    hora_local = (
        config.hora_local
        if config is not None and config.hora_local is not None
        else meta.hora_local_por_defecto
    )
    ventana = config.ventana if config else None

    if not habilitada:
        return PoliticaCron(cron_name, False, hora_local, ventana, OMITIDA_DESHABILITADA)

    momento = ahora or utcnow()
    if meta.hora_local and hora_local is not None:
        local = momento.astimezone(await zona_del_negocio(tenant_id))
        if local.hour != hora_local:
            return PoliticaCron(cron_name, True, hora_local, ventana, OMITIDA_FUERA_DE_HORA)

    if meta.ventana and ventana:
        local = momento.astimezone(await zona_del_negocio(tenant_id))
        if not en_ventana(local, ventana):
            return PoliticaCron(cron_name, True, hora_local, ventana, OMITIDA_FUERA_DE_VENTANA)

    return PoliticaCron(cron_name, True, hora_local, ventana)


def _validar_ventana(ventana: dict[str, Any] | None) -> dict[str, Any] | None:
    if ventana is None:
        return None
    if not isinstance(ventana, dict):
        raise CronNegocioInvalido("La ventana debe ser un objeto con desde, hasta y dias.")
    try:
        desde = time.fromisoformat(str(ventana.get("desde")))
        hasta = time.fromisoformat(str(ventana.get("hasta")))
    except (TypeError, ValueError) as exc:
        raise CronNegocioInvalido("Usa horas HH:MM en desde y hasta.") from exc
    dias = ventana.get("dias")
    if dias is None:
        dias_limpios: list[int] = list(range(7))
    elif isinstance(dias, list):
        try:
            dias_limpios = sorted({int(d) for d in dias})
        except (TypeError, ValueError) as exc:
            raise CronNegocioInvalido("Los días son números de 0 (lunes) a 6 (domingo).") from exc
        if any(d < 0 or d > 6 for d in dias_limpios):
            raise CronNegocioInvalido("Los días son números de 0 (lunes) a 6 (domingo).")
    else:
        raise CronNegocioInvalido("Los días deben ser una lista de 0 a 6.")
    return {"desde": desde.strftime("%H:%M"), "hasta": hasta.strftime("%H:%M"), "dias": dias_limpios}


def _proxima(meta: CronMeta, config: ConfigCronNegocio | None, zona: ZoneInfo, ahora: datetime) -> datetime | None:
    """Próxima ejecución para ESTE negocio (UTC).

    Con hora local: la próxima vez que su reloj marque esa hora. Sin ella: el horario global del
    catálogo (el mismo para todos). Las ventanas no mueven la próxima: la ejecución existe, pero
    puede saltarse fuera de la ventana.
    """
    hora = (
        config.hora_local
        if config is not None and config.hora_local is not None
        else meta.hora_local_por_defecto
    )
    if meta.hora_local and hora is not None:
        local = ahora.astimezone(zona)
        candidato = local.replace(hour=hora, minute=0, second=0, microsecond=0)
        if candidato <= local:
            candidato += timedelta(days=1)
        return candidato.astimezone(UTC)
    if meta.default:
        try:
            return proxima_ejecucion(meta.default, ahora)
        except ValueError:
            return None
    return None


async def _packs_habilitados() -> set[str] | None:
    """Packs del negocio activo (ADR-0019). `None` = no se pudo leer: no se oculta nada."""
    try:
        from core.agents.spec import enabled_packs, load_active_spec

        return set(enabled_packs(await load_active_spec()))
    except Exception:
        return None


async def listar(tenant_id: uuid.UUID, *, ahora: datetime | None = None) -> list[CronNegocioView]:
    """Crons de negocio visibles para este negocio, en el orden de los módulos."""
    async with get_session() as session:
        filas = (
            await session.execute(
                select(TenantCronConfig).where(
                    TenantCronConfig.tenant_id == tenant_id,
                    TenantCronConfig.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    configs = {
        f.cron_name: ConfigCronNegocio(
            cron_name=f.cron_name,
            habilitada=bool(f.habilitada),
            hora_local=f.hora_local,
            ventana=f.ventana,
            last_run_at=f.last_run_at,
            last_status=f.last_status,
            last_resumen=f.last_resumen,
            last_acciones=f.last_acciones,
            updated_at=f.updated_at,
        )
        for f in filas
    }
    packs = await _packs_habilitados()
    zona = await zona_del_negocio(tenant_id)
    momento = ahora or utcnow()
    vistas: list[CronNegocioView] = []
    for meta in sorted(
        (m for m in CATALOGO.values() if m.ambito == "negocio"),
        key=lambda m: (MODULOS.index(m.module) if m.module in MODULOS else len(MODULOS), m.label),
    ):
        if meta.pack is not None and packs is not None and meta.pack not in packs:
            continue
        config = configs.get(meta.name)
        vistas.append(
            CronNegocioView(
                name=meta.name,
                module=meta.module,
                label=meta.label,
                description=meta.description,
                pack=meta.pack,
                sensitive=meta.sensitive,
                configurable=meta.configurable,
                admite_hora_local=meta.hora_local,
                admite_ventana=meta.ventana,
                parametros=meta.parametros,
                habilitada=True if config is None else config.habilitada,
                hora_local=(
                    config.hora_local
                    if config is not None and config.hora_local is not None
                    else meta.hora_local_por_defecto
                ),
                hora_local_por_defecto=meta.hora_local_por_defecto,
                hora_local_personalizada=bool(config is not None and config.hora_local is not None),
                ventana=config.ventana if config else None,
                next_run_at=_proxima(meta, config, zona, momento),
                last_run_at=config.last_run_at if config else None,
                last_status=config.last_status if config else None,
                last_resumen=config.last_resumen if config else None,
                last_acciones=config.last_acciones if config else None,
                acciones=_acciones(meta),
            )
        )
    return vistas


def _acciones(meta: CronMeta) -> list[str]:
    if not meta.configurable:
        return []
    acciones = ["enable", "disable"]
    if meta.hora_local:
        acciones.append("hora")
    if meta.ventana:
        acciones.append("ventana")
    return acciones


async def resumen(tenant_id: uuid.UUID) -> dict[str, int]:
    vistas = await listar(tenant_id)
    return {
        "total": len(vistas),
        "habilitadas": sum(1 for v in vistas if v.habilitada),
        "apagadas": sum(1 for v in vistas if not v.habilitada),
        "con_fallo": sum(1 for v in vistas if v.last_status == "failed"),
    }


async def fijar(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    habilitada: Any = SIN_CAMBIO,
    hora_local: Any = SIN_CAMBIO,
    ventana: Any = SIN_CAMBIO,
    updated_by: uuid.UUID | None = None,
) -> ConfigCronNegocio:
    """Crea o actualiza la política del negocio y la audita. Solo campos que el catálogo admite."""
    meta = _meta_configurable(cron_name)
    if hora_local is not SIN_CAMBIO and not meta.hora_local:
        raise CronNegocioNoConfigurable(f"«{meta.label}» no admite hora local.")
    if ventana is not SIN_CAMBIO and not meta.ventana:
        raise CronNegocioNoConfigurable(f"«{meta.label}» no admite ventana horaria.")
    if hora_local is not SIN_CAMBIO and hora_local is not None:
        if not isinstance(hora_local, int) or not 0 <= hora_local <= 23:
            raise CronNegocioInvalido("La hora local va de 0 a 23.")
    ventana_limpia = _validar_ventana(ventana) if ventana is not SIN_CAMBIO else SIN_CAMBIO

    async with get_session() as session:
        row = (
            await session.execute(
                select(TenantCronConfig).where(
                    TenantCronConfig.tenant_id == tenant_id,
                    TenantCronConfig.cron_name == cron_name,
                )
            )
        ).scalar_one_or_none()
        antes = _como_dict(row)
        if row is None:
            row = TenantCronConfig(tenant_id=tenant_id, cron_name=cron_name)
            session.add(row)
        if habilitada is not SIN_CAMBIO:
            row.habilitada = bool(habilitada)
        if hora_local is not SIN_CAMBIO:
            row.hora_local = hora_local
        if ventana_limpia is not SIN_CAMBIO:
            row.ventana = ventana_limpia
        row.updated_by = updated_by
        row.deleted_at = None
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=f"admin:{updated_by}" if updated_by else "admin",
                action="cron_negocio_config",
                input={"cron": cron_name, "antes": antes, "despues": _como_dict(row)},
            )
        )
    return ConfigCronNegocio(
        cron_name=row.cron_name,
        habilitada=bool(row.habilitada),
        hora_local=row.hora_local,
        ventana=row.ventana,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        last_resumen=row.last_resumen,
        last_acciones=row.last_acciones,
        updated_at=row.updated_at,
    )


def _como_dict(row: TenantCronConfig | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "habilitada": bool(row.habilitada),
        "hora_local": row.hora_local,
        "ventana": row.ventana,
    }


async def registrar_ultima(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    status: str,
    acciones: int = 0,
    resumen: str | None = None,
    forzar: bool = False,
) -> None:
    """Actualiza el resumen de la última ejecución (best-effort; nunca tumba al cron).

    En barridos exitosos sin efecto se escribe como mucho una vez por hora: la tarjeta muestra
    «corrió hace X» sin multiplicar escrituras por tenant.
    """
    meta = get_meta(cron_name)
    if meta is None or meta.ambito != "negocio":
        return
    try:
        async with get_session() as session:
            ahora = utcnow()
            row = (
                await session.execute(
                    select(TenantCronConfig).where(
                        TenantCronConfig.tenant_id == tenant_id,
                        TenantCronConfig.cron_name == cron_name,
                    )
                )
            ).scalar_one_or_none()
            if (
                not forzar
                and status == "success"
                and acciones == 0
                and row is not None
                and row.last_run_at is not None
                and ahora - row.last_run_at < RESUMEN_MIN_INTERVALO
            ):
                return
            if row is None:
                row = TenantCronConfig(tenant_id=tenant_id, cron_name=cron_name)
                session.add(row)
            row.last_run_at = ahora
            row.last_status = status
            row.last_resumen = (resumen or "")[:280] or None
            row.last_acciones = int(acciones)
            row.deleted_at = None
    except Exception as exc:
        log.error("cron_negocio_registro_fallido", task=cron_name, error=type(exc).__name__)


# --------------------------------------------------------------------------- historial detallado
@dataclass
class EjecucionCronNegocio:
    id: uuid.UUID
    status: str
    trigger: str
    acciones: int
    resumen: str | None
    error_class: str | None
    detail: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


async def registrar_ejecucion(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    status: str,
    acciones: int = 0,
    resumen: str | None = None,
    trigger: str = "scheduled",
    error_class: str | None = None,
    detail: str | None = None,
    started_at: datetime | None = None,
    duration_ms: int | None = None,
) -> None:
    """Añade una fila al historial SOLO si tuvo efecto, falló o fue manual.

    Un barrido exitoso sin efecto no deja fila: la tarjeta ya muestra «corrió hace X» con el
    resumen de `tenant_cron_configs`. Best-effort: nunca tumba al cron.
    """
    meta = get_meta(cron_name)
    if meta is None or meta.ambito != "negocio":
        return
    if status == "skipped":
        return
    if status == "success" and acciones <= 0 and trigger != "manual":
        return
    try:
        async with get_session() as session:
            ahora = utcnow()
            session.add(
                TenantCronRun(
                    tenant_id=tenant_id,
                    cron_name=cron_name,
                    status=status,
                    trigger=trigger,
                    acciones=max(0, int(acciones)),
                    resumen=(resumen or "")[:280] or None,
                    error_class=error_class,
                    detail=detail,
                    started_at=started_at or ahora,
                    finished_at=ahora,
                    duration_ms=duration_ms,
                )
            )
    except Exception as exc:
        log.error("cron_negocio_historial_fallido", task=cron_name, error=type(exc).__name__)


async def historial(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    offset: int = 0,
    limit: int = 50,
    status: str | None = None,
) -> tuple[list[EjecucionCronNegocio], int]:
    """Ejecuciones con efecto/fallo/manuales del negocio, de la más nueva a la más vieja."""
    meta = get_meta(cron_name)
    if meta is None or meta.ambito != "negocio":
        raise CronNegocioNoEncontrado(cron_name)
    condiciones = [TenantCronRun.tenant_id == tenant_id, TenantCronRun.cron_name == cron_name]
    if status is not None:
        condiciones.append(TenantCronRun.status == status)
    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(TenantCronRun).where(*condiciones))
        ).scalar_one()
        filas = (
            await session.execute(
                select(TenantCronRun)
                .where(*condiciones)
                .order_by(TenantCronRun.started_at.desc())
                .offset(max(0, offset))
                .limit(max(1, min(200, limit)))
            )
        ).scalars().all()
    return (
        [
            EjecucionCronNegocio(
                id=f.id,
                status=f.status,
                trigger=f.trigger,
                acciones=f.acciones,
                resumen=f.resumen,
                error_class=f.error_class,
                detail=f.detail,
                started_at=f.started_at,
                finished_at=f.finished_at,
                duration_ms=f.duration_ms,
            )
            for f in filas
        ],
        int(total),
    )


async def mantenimiento(dias: int = RETENCION_DIAS) -> int:
    """Purga el historial de más de `dias` con la función SECURITY DEFINER (RLS no deja borrar)."""
    async with get_session() as session:
        borradas = (
            await session.execute(
                text("select admin_purgar_tenant_cron_runs(:d)"), {"d": max(1, int(dias))}
            )
        ).scalar_one()
    return int(borradas or 0)


# --------------------------------------------------------------------------- vista master
@dataclass
class NegocioCronResumen:
    tenant_id: uuid.UUID
    tenant_name: str
    habilitada: bool
    hora_local: int | None
    ventana: dict[str, Any] | None
    last_run_at: datetime | None
    last_status: str | None
    last_resumen: str | None
    last_acciones: int | None
    fallos_24h: int


async def resumen_por_negocio(cron_name: str) -> list[NegocioCronResumen]:
    """Por negocio que tenga fila, su política y su última ejecución (master, ADR-0201).

    La lectura cruza tenants: la hace `admin_tenant_cron_resumen` (SECURITY DEFINER) y solo
    devuelve metadatos operativos. Los nombres salen de `tenants` (tabla de sistema).
    """
    meta = get_meta(cron_name)
    if meta is None or meta.ambito != "negocio":
        raise CronNegocioNoEncontrado(cron_name)
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "select tenant_id, habilitada, hora_local, ventana, last_run_at, last_status, "
                    "last_resumen, last_acciones, fallos_24h "
                    "from admin_tenant_cron_resumen(:c)"
                ),
                {"c": cron_name},
            )
        ).all()
        if not filas:
            return []
        ids = [f[0] for f in filas]
        nombres = dict(
            (await session.execute(text("select id, name from tenants where id = any(:ids)"), {"ids": ids})).all()
        )
    return [
        NegocioCronResumen(
            tenant_id=f[0],
            tenant_name=nombres.get(f[0], ""),
            habilitada=bool(f[1]),
            hora_local=f[2],
            ventana=f[3],
            last_run_at=f[4],
            last_status=f[5],
            last_resumen=f[6],
            last_acciones=f[7],
            fallos_24h=int(f[8] or 0),
        )
        for f in filas
    ]

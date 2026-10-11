"""Procedimiento `pack_cobranza.promesa_de_pago`, su *resolver* y sus reglas (HU-AOS-08).

El JSON vive en `packages/pack-cobranza/procedures/`. "La IA propone, las reglas deciden": la
fecha que escribe el modelo la valida `fecha_de_promesa` con las reglas del pack, y el monto de la
promesa es el saldo real del servidor (`cob_registrar_promesa_saldo` lo recalcula y rechaza si no
coincide con el que confirmó el cliente). Las reglas se leen de la configuración del pack, nunca del
prompt:

- la fecha no es pasada ni pasa de `ReglaNegociacion.max_dias_prorroga` días desde hoy;
- no cae en un día no laborable de la cartera del préstamo (`CarteraConfig.dias_no_laborables`:
  hoy los dos valores incluyen el domingo; los festivos no se validan porque el pack no tiene
  calendario de festivos);
- el préstamo no tiene ya `ReglaNegociacion.max_reprogramaciones` promesas pendientes.

El saldo es el de `cob_consultar_saldo` (`_saldo_y_mora`: las cuotas pendientes y atrasadas).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.mcp.types import ResolverResult, ToolContext

# Valores de `CarteraConfig.dias_no_laborables` en los que el domingo no es laborable.
_SIN_DOMINGOS = frozenset({"solo_domingos", "domingos_y_feriados"})
_DOMINGO = 6


@dataclass(frozen=True)
class ReglasDePromesa:
    hoy: date
    max_dias: int
    sin_domingos: bool
    pendientes: int
    max_promesas: int


def error_de_fecha(fecha: date, reglas: ReglasDePromesa) -> str | None:
    """El motivo por el que la fecha no se acepta, o None. Puro."""
    if fecha < reglas.hoy:
        return f"La fecha de la promesa no puede ser pasada: hoy es {reglas.hoy.isoformat()}."
    limite = reglas.hoy + timedelta(days=max(reglas.max_dias, 0))
    if fecha > limite:
        return (
            f"La cartera acepta promesas hasta el {limite.isoformat()} ({reglas.max_dias} días): "
            "pide una fecha más cercana."
        )
    if reglas.sin_domingos and fecha.weekday() == _DOMINGO:
        return f"El {fecha.isoformat()} es domingo, día no laborable de la cartera: pide otro día."
    if reglas.max_promesas > 0 and reglas.pendientes >= reglas.max_promesas:
        return (
            f"El cliente ya tiene {reglas.pendientes} promesa(s) pendiente(s) y la cartera permite "
            f"{reglas.max_promesas}: ofrece pasarlo con un asesor."
        )
    return None


def mismo_saldo(real: Decimal, confirmado: Any, moneda: str | None) -> bool:
    """¿El saldo que confirmó el cliente es el real? Al peso en COP, al centavo en otras monedas."""
    paso = Decimal("1") if (moneda or "COP").upper() == "COP" else Decimal("0.01")
    if isinstance(confirmado, bool):
        return False
    try:
        valor = Decimal(str(confirmado)).quantize(paso, rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError):
        return False
    return valor == Decimal(real).quantize(paso, rounding=ROUND_HALF_UP)


async def reglas_del_prestamo(session: Any, prestamo: Any) -> ReglasDePromesa:
    from sqlalchemy import func, select

    from core.shared.tenant import get_current_tenant_id
    from pack_cobranza.models import CarteraConfig, Promesa
    from pack_cobranza.skills import _hoy, _reglas

    tenant_id = get_current_tenant_id()
    reglas = await _reglas(session)
    no_laborables = (
        await session.execute(
            select(CarteraConfig.dias_no_laborables).where(
                CarteraConfig.tenant_id == tenant_id, CarteraConfig.cartera_id == prestamo.cartera_id
            )
        )
    ).scalar_one_or_none()
    pendientes = (
        await session.execute(
            select(func.count()).select_from(Promesa).where(
                Promesa.tenant_id == tenant_id,
                Promesa.prestamo_id == prestamo.id,
                Promesa.estado == "pendiente",
                Promesa.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return ReglasDePromesa(
        hoy=_hoy(),
        max_dias=int(reglas.max_dias_prorroga or 0),
        sin_domingos=(no_laborables or "solo_domingos") in _SIN_DOMINGOS,
        pendientes=int(pendientes or 0),
        max_promesas=int(reglas.max_reprogramaciones or 0),
    )


async def fecha_de_promesa(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    from core.db.session import get_session
    from pack_cobranza.skills import _cliente_de_customer, _prestamo_activo

    try:
        fecha = date.fromisoformat(str(valor or "").strip())
    except ValueError:
        return ResolverResult(ok=False, error="Fecha inválida: usa el formato AAAA-MM-DD.")
    async with get_session() as session:
        cliente = await _cliente_de_customer(session, ctx.customer_id)
        prestamo = await _prestamo_activo(session, cliente.id) if cliente is not None else None
        if prestamo is None:
            return ResolverResult(ok=False, error="El cliente no tiene un préstamo vigente: no hay promesa que registrar.")
        error = error_de_fecha(fecha, await reglas_del_prestamo(session, prestamo))
    if error is not None:
        return ResolverResult(ok=False, error=error)
    return ResolverResult(ok=True, value=fecha.isoformat())


def registrar(host: Any, plugin_id: str) -> None:
    """Publica el procedimiento y su resolver (lo llama `on_activate`). Sin banderas encendidas
    nadie los usa: el motor solo actúa con `tenants.aos_procedures` (o en sombra con `aos_runtime = v3`)."""
    from core.kernel.procedures import de_pack

    host.register_resolver(f"{plugin_id}.fecha_de_promesa", validar=fecha_de_promesa)
    host.register_procedures(de_pack.cargar(plugin_id))

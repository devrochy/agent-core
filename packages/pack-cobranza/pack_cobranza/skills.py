"""Skills y tools de pack-cobranza.

El asistente atiende al cliente por WhatsApp: consulta saldo, estado de cuenta,
registra promesas y abonos, y evalúa negociaciones. **La IA propone, las reglas
deciden**: `cob_evaluar_negociacion` es 100% determinista (portado del motor de
`app-cobranza`). También hay tools operativas de campo (gasto, inyección) que el
asistente solo usa si se lo piden explícitamente.

Modelo (ADR-0060): Propietario → **Cartera** → Cliente → Préstamo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.db.session import get_owned, get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_cobranza.models import (
    Abono,
    Cartera,
    Cliente,
    Cuota,
    Prestamo,
    Promesa,
    ReglaNegociacion,
    Suscripcion,
)

log = get_logger(__name__)


def _hoy() -> date:
    return datetime.now().date()


def _money(v: Decimal | float | None, moneda: str = "COP") -> str:
    if v is None:
        return "—"
    return f"{moneda} {Decimal(v):,.0f}".replace(",", ".")


async def _cliente_de_customer(session, customer_id: uuid.UUID) -> Cliente | None:
    return (
        await session.execute(
            select(Cliente).where(
                Cliente.tenant_id == get_current_tenant_id(),
                Cliente.customer_id == customer_id,
                Cliente.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _prestamo_activo(session, cliente_id: uuid.UUID) -> Prestamo | None:
    return (
        await session.execute(
            select(Prestamo)
            .where(
                Prestamo.tenant_id == get_current_tenant_id(),
                Prestamo.cliente_id == cliente_id,
                Prestamo.estatus == "vigente",
                Prestamo.deleted_at.is_(None),
            )
            .order_by(Prestamo.created_at.desc())
        )
    ).scalars().first()


async def _saldo_y_mora(session, prestamo: Prestamo) -> tuple[Decimal, int, Cuota | None]:
    cuotas = (
        await session.execute(
            select(Cuota).where(
                Cuota.tenant_id == get_current_tenant_id(),
                Cuota.prestamo_id == prestamo.id,
                Cuota.estatus.in_(("pendiente", "atrasada")),
                Cuota.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    saldo = sum((c.valor_esperado for c in cuotas), Decimal(0))
    vencidas = [c for c in cuotas if c.fecha_vencimiento < _hoy()]
    mora = max(((_hoy() - c.fecha_vencimiento).days for c in vencidas), default=0)
    proxima = min(cuotas, key=lambda c: c.fecha_vencimiento, default=None)
    return saldo, mora, proxima


async def _reglas(session) -> ReglaNegociacion:
    r = (
        await session.execute(
            select(ReglaNegociacion).where(
                ReglaNegociacion.tenant_id == get_current_tenant_id()
            )
        )
    ).scalar_one_or_none()
    if r is None:
        # Defaults EFECTIVOS: los `default=` de SQLAlchemy solo aplican en el INSERT.
        r = ReglaNegociacion(
            tenant_id=get_current_tenant_id(),
            max_dias_prorroga=15,
            min_abono_pct=Decimal(10),
            max_reprogramaciones=2,
            umbral_saldo_autonomo=Decimal(0),
        )
    return r


def evaluar_negociacion(
    saldo: Decimal,
    mora_dias: int,
    valor_propuesto: Decimal,
    tipo: str,
    reglas: ReglaNegociacion,
) -> dict[str, Any]:
    """Motor determinista: decide si una negociación es aceptable (portado de app-cobranza)."""
    if saldo <= 0:
        return {"aceptada": False, "motivo": "sin_saldo"}
    if saldo > reglas.umbral_saldo_autonomo > 0:
        return {"aceptada": False, "motivo": "supera_umbral_autonomo", "derivar": True}
    min_abono = (saldo * Decimal(reglas.min_abono_pct) / Decimal(100)).quantize(Decimal("1"))
    if tipo == "abono_parcial" and valor_propuesto < min_abono:
        return {"aceptada": False, "motivo": "abono_bajo_minimo", "minimo": float(min_abono)}
    if tipo == "refinanciacion" and mora_dias > reglas.max_dias_prorroga:
        return {"aceptada": False, "motivo": "mora_supera_prorroga", "derivar": True}
    if valor_propuesto > saldo:
        return {"aceptada": False, "motivo": "valor_mayor_al_saldo"}
    return {"aceptada": True, "motivo": "ok", "minimo": float(min_abono)}


class CobranzaSkill(BaseSkill):
    name = "cobranza"
    description = (
        "Cartera del cliente: consultar saldo y próxima cuota, estado de cuenta, "
        "registrar promesas y abonos, evaluar negociaciones y operar gastos/inyecciones."
    )
    keywords: list[str] = []  # noqa: RUF012 -- siempre activa con el pack habilitado

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="cob_consultar_saldo",
                description=(
                    "Devuelve el saldo actual, la próxima cuota y los días de mora del cliente "
                    "de la conversación. Usar cuando el cliente pregunte cuánto debe o cuándo paga."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.consultar_saldo,
            ),
            MCPTool(
                name="cob_estado_cuenta",
                description=(
                    "Detalle del préstamo vigente: valor, saldo, cuotas y vencimientos. "
                    "Usar para dar el estado de cuenta completo, no para un saldo puntual."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.estado_cuenta,
            ),
            MCPTool(
                name="cob_evaluar_negociacion",
                description=(
                    "Evalúa si una propuesta del cliente (abono parcial o refinanciación) es "
                    "aceptable según las reglas de la cartera. Usar SIEMPRE antes de prometer "
                    "un acuerdo; el resultado es determinista y el asistente no puede cambiarlo."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "tipo": {"type": "string", "enum": ["abono_parcial", "refinanciacion"]},
                        "valor": {"type": "number", "description": "Valor propuesto por el cliente"},
                    },
                    "required": ["tipo", "valor"],
                },
                handler=self.evaluar_negociacion_tool,
            ),
            MCPTool(
                name="cob_registrar_promesa_pago",
                description=(
                    "Registra una promesa de pago del cliente (fecha + valor). Solo usar cuando "
                    "el cliente confirme una fecha y un valor concretos."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "fecha": {"type": "string", "description": "Fecha prometida (YYYY-MM-DD)"},
                        "valor": {"type": "number", "description": "Valor prometido"},
                        "tipo": {"type": "string", "enum": ["promesa", "abono_parcial", "refinanciacion"]},
                    },
                    "required": ["fecha", "valor"],
                },
                handler=self.registrar_promesa,
            ),
            MCPTool(
                name="cob_registrar_abono",
                description=(
                    "Registra un abono parcial del cliente a su préstamo. Usar solo cuando el "
                    "cliente confirme que va a abonar y el monto."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "valor": {"type": "number", "description": "Valor del abono"},
                        "metodo_pago": {"type": "string", "description": "efectivo|qr|transferencia|tarjeta|deposito"},
                    },
                    "required": ["valor"],
                },
                handler=self.registrar_abono,
            ),
            MCPTool(
                name="cob_consultar_suscripcion",
                description=(
                    "Consulta la suscripción recurrente del cliente (servicio mensual): estado, "
                    "valor del período, próximo cargo y últimos cargos. Usar cuando pregunte por su "
                    "suscripción, su mensualidad o su próximo cobro."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.consultar_suscripcion,
            ),
            MCPTool(
                name="cob_crear_suscripcion",
                description=(
                    "Crea una suscripción recurrente para el cliente (servicio mensual). Usar SOLO "
                    "cuando el cliente confirmó explícitamente el servicio, el valor y la "
                    "periodicidad. No inventes el valor: debe salir del catálogo o de lo acordado."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "concepto": {"type": "string", "description": "Nombre del servicio, ej. 'Plan Basic'"},
                        "valor": {"type": "number", "description": "Valor por período (mensual)"},
                        "dia_facturacion": {"type": "integer", "description": "Día del mes del cobro (1-28, opcional)"},
                    },
                    "required": ["concepto", "valor"],
                },
                handler=self.crear_suscripcion,
            ),
            MCPTool(
                name="cob_pausar_suscripcion",
                description=(
                    "Pausa la suscripción del cliente (suspende los cargos). Usar solo cuando el "
                    "cliente lo pida y confirme."
                ),
                parameters={
                    "type": "object",
                    "properties": {"motivo": {"type": "string", "description": "Motivo (opcional)"}},
                    "required": [],
                },
                handler=self.pausar_suscripcion,
            ),
            MCPTool(
                name="cob_cancelar_suscripcion",
                description=(
                    "Cancela la suscripción del cliente (fin del servicio). Usar solo cuando el "
                    "cliente lo pida explícitamente y confirme."
                ),
                parameters={
                    "type": "object",
                    "properties": {"motivo": {"type": "string", "description": "Motivo (opcional)"}},
                    "required": [],
                },
                handler=self.cancelar_suscripcion,
            ),
            MCPTool(
                name="cob_renovar_suscripcion",
                description=(
                    "Reactiva una suscripción pausada (y reprograma el próximo cargo). Usar cuando "
                    "el cliente pida reactivarla."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.renovar_suscripcion,
            ),
            MCPTool(
                name="cob_link_suscripcion",
                description=(
                    "Genera el link/QR de pago del cargo del período pendiente de la suscripción. "
                    "Usar cuando el cliente quiera pagar su mensualidad."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self.link_suscripcion,
            ),
        ]
        # NOTA (auditoría M3): las operaciones de administración/de campo (registrar
        # gasto, inyección, visita y generar liquidación) NO se exponen al asistente
        # orientado al CLIENTE — un deudor por WhatsApp no debe poder registrarlas.
        # Viven en el panel (`admin.py`); la versión para gestor irá por la API
        # pública del pack en v2 (ADR-0057).

    # ---------------------------------------------------------------- handlers
    async def consultar_saldo(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            if cliente is None:
                return {"encontrado": False, "mensaje": "El cliente no está en la cartera."}
            prestamo = await _prestamo_activo(session, cliente.id)
            if prestamo is None:
                return {"encontrado": True, "saldo": 0, "mensaje": "El cliente no tiene préstamos vigentes."}
            saldo, mora, proxima = await _saldo_y_mora(session, prestamo)
            cartera = await get_owned(session, Cartera, prestamo.cartera_id)
            moneda = cartera.moneda if cartera else "COP"
            return {
                "encontrado": True,
                "cliente": f"{cliente.nombre} {cliente.apellido or ''}".strip(),
                "saldo": float(saldo),
                "mora_dias": mora,
                "proxima_cuota": {
                    "fecha": proxima.fecha_vencimiento.isoformat() if proxima else None,
                    "valor": float(proxima.valor_esperado) if proxima else None,
                },
                "_safe_append": (
                    f"Saldo actual: {_money(saldo, moneda)}. "
                    + (f"Mora: {mora} días. " if mora > 0 else "")
                    + (f"Próxima cuota: {_money(proxima.valor_esperado, moneda)} el {proxima.fecha_vencimiento}." if proxima else "")
                ),
            }

    async def estado_cuenta(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            if cliente is None:
                return {"encontrado": False}
            prestamo = await _prestamo_activo(session, cliente.id)
            if prestamo is None:
                return {"encontrado": True, "prestamos": []}
            cuotas = (
                await session.execute(
                    select(Cuota)
                    .where(Cuota.tenant_id == get_current_tenant_id(), Cuota.prestamo_id == prestamo.id)
                    .order_by(Cuota.numero_cuota)
                )
            ).scalars().all()
            return {
                "encontrado": True,
                "prestamo": {
                    "valor": float(prestamo.valor),
                    "saldo": float(prestamo.saldo),
                    "num_cuotas": prestamo.num_cuotas,
                    "fecha_otorgado": prestamo.fecha_otorgado.isoformat(),
                },
                "cuotas": [
                    {"numero": c.numero_cuota, "valor": float(c.valor_esperado), "vence": c.fecha_vencimiento.isoformat(), "estatus": c.estatus}
                    for c in cuotas
                ],
            }

    async def evaluar_negociacion_tool(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        tipo = str(arguments.get("tipo") or "").strip()
        try:
            valor = Decimal(str(arguments.get("valor") or 0))
        except Exception:
            return {"error": "valor inválido"}
        if tipo not in ("abono_parcial", "refinanciacion"):
            return {"error": "tipo inválido"}
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            if cliente is None:
                return {"encontrado": False}
            prestamo = await _prestamo_activo(session, cliente.id)
            if prestamo is None:
                return {"encontrado": True, "aceptada": False, "motivo": "sin_prestamo"}
            saldo, mora, _ = await _saldo_y_mora(session, prestamo)
            reglas = await _reglas(session)
            resultado = evaluar_negociacion(saldo, mora, valor, tipo, reglas)
            resultado["encontrado"] = True
            return resultado

    async def registrar_promesa(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        fecha_raw = str(arguments.get("fecha") or "").strip()
        try:
            fecha = date.fromisoformat(fecha_raw)
        except Exception:
            return {"error": "fecha inválida (usar YYYY-MM-DD)"}
        try:
            valor = Decimal(str(arguments.get("valor") or 0))
        except Exception:
            return {"error": "valor inválido"}
        tipo = str(arguments.get("tipo") or "promesa")
        if valor <= 0:
            return {"error": "el valor debe ser mayor a cero"}
        if fecha < _hoy():
            return {"error": "la fecha prometida no puede ser pasada"}
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            if cliente is None:
                return {"encontrado": False}
            prestamo = await _prestamo_activo(session, cliente.id)
            if prestamo is None:
                return {"encontrado": True, "error": "sin_préstamo_vigente"}
            existente = (
                await session.execute(
                    select(Promesa).where(
                        Promesa.tenant_id == get_current_tenant_id(),
                        Promesa.idempotency_key == context.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existente is not None:
                return {"registrada": True, "ya_existe": True, "promesa_id": str(existente.id)}
            promesa = Promesa(
                tenant_id=get_current_tenant_id(),
                prestamo_id=prestamo.id,
                cliente_id=cliente.id,
                fecha_prometida=fecha,
                valor_prometido=valor,
                tipo=tipo,
                creado_por="ia",
                idempotency_key=context.idempotency_key,
            )
            session.add(promesa)
            await session.flush()
            return {"registrada": True, "promesa_id": str(promesa.id), "fecha": fecha.isoformat(), "valor": float(valor)}

    async def registrar_abono(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        try:
            valor = Decimal(str(arguments.get("valor") or 0))
        except Exception:
            return {"error": "valor inválido"}
        if valor <= 0:
            return {"error": "el valor debe ser mayor a cero"}
        metodo = str(arguments.get("metodo_pago") or "efectivo")
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            if cliente is None:
                return {"encontrado": False}
            prestamo = await _prestamo_activo(session, cliente.id)
            if prestamo is None:
                return {"encontrado": True, "error": "sin_préstamo_vigente"}
            existente = (
                await session.execute(
                    select(Abono).where(
                        Abono.tenant_id == get_current_tenant_id(),
                        Abono.idempotency_key == context.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existente is not None:
                return {"registrado": True, "ya_existe": True, "abono_id": str(existente.id)}
            abono = Abono(
                tenant_id=get_current_tenant_id(),
                prestamo_id=prestamo.id,
                cliente_id=cliente.id,
                valor=valor,
                metodo_pago=metodo,
                registrado_por="ia",
                idempotency_key=context.idempotency_key,
            )
            session.add(abono)
            await session.flush()
            return {"registrado": True, "abono_id": str(abono.id), "valor": float(valor)}

    # Las operaciones de administración (gasto, inyección, visita, liquidación)
    # viven en el panel (`admin.py`), no en el asistente del cliente (auditoría M3).

    # ------------------------------------------------------------ suscripción
    async def _cliente_conversacion(self, session, context: ToolContext) -> Cliente | None:
        customer_id = getattr(context, "customer_id", None)
        if customer_id is None:
            return None
        return await _cliente_de_customer(session, customer_id)

    async def _suscripcion_del_cliente(
        self, session, cliente_id: uuid.UUID
    ) -> Suscripcion | None:
        return (
            await session.execute(
                select(Suscripcion)
                .where(
                    Suscripcion.tenant_id == get_current_tenant_id(),
                    Suscripcion.cliente_id == cliente_id,
                    Suscripcion.deleted_at.is_(None),
                )
                .order_by(Suscripcion.created_at.desc())
            )
        ).scalars().first()

    async def consultar_suscripcion(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        async with get_session() as session:
            cliente = await self._cliente_conversacion(session, context)
            if cliente is None:
                return {"encontrado": False, "mensaje": "No encontré tu registro de cliente."}
            sub = await self._suscripcion_del_cliente(session, cliente.id)
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción registrada."}
            cargos = (
                await session.execute(
                    select(Cuota)
                    .where(
                        Cuota.tenant_id == get_current_tenant_id(),
                        Cuota.suscripcion_id == sub.id,
                        Cuota.deleted_at.is_(None),
                    )
                    .order_by(Cuota.fecha_vencimiento.desc())
                    .limit(6)
                )
            ).scalars().all()
            return {
                "encontrado": True,
                "suscripcion_id": str(sub.id),
                "concepto": sub.concepto,
                "estado": sub.estado,
                "valorPeriodo": float(sub.valor_periodo or 0),
                "moneda": sub.moneda,
                "periodicidad": sub.periodicidad,
                "proximoCargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
                "cargos": [
                    {
                        "numero": c.numero_cuota,
                        "valor": float(c.valor_esperado),
                        "vence": c.fecha_vencimiento.isoformat(),
                        "estatus": c.estatus,
                    }
                    for c in cargos
                ],
            }

    async def crear_suscripcion(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        from pack_cobranza.jobs import fecha_cargo

        concepto = str(arguments.get("concepto") or "").strip()
        valor = Decimal(str(arguments.get("valor") or 0))
        dia = min(max(int(arguments.get("dia_facturacion") or 1), 1), 28)
        if not concepto or valor <= 0:
            return {
                "error": "faltan_datos",
                "mensaje": "Necesito el concepto del servicio y el valor del período (> 0).",
            }
        async with get_session() as session:
            cliente = await self._cliente_conversacion(session, context)
            if cliente is None:
                return {"error": "cliente_no_encontrado"}
            existente = await self._suscripcion_del_cliente(session, cliente.id)
            if existente is not None and existente.estado in ("activa", "pausada"):
                return {
                    "ya_existe": True,
                    "suscripcion_id": str(existente.id),
                    "estado": existente.estado,
                }
            sub = Suscripcion(
                tenant_id=get_current_tenant_id(),
                cliente_id=cliente.id,
                cartera_id=cliente.cartera_id,
                concepto=concepto,
                valor_periodo=valor,
                dia_facturacion=dia,
                estado="activa",
                proximo_cargo=fecha_cargo(dia, _hoy(), estricto=False),
                suscrito_en=datetime.now(UTC),
            )
            session.add(sub)
            await session.flush()
            return {
                "creada": True,
                "suscripcion_id": str(sub.id),
                "concepto": concepto,
                "valorPeriodo": float(valor),
                "proximoCargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
            }

    async def _cambiar_estado_suscripcion(
        self, estado: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        motivo = str(arguments.get("motivo") or "").strip()
        async with get_session() as session:
            cliente = await self._cliente_conversacion(session, context)
            if cliente is None:
                return {"error": "cliente_no_encontrado"}
            sub = await self._suscripcion_del_cliente(session, cliente.id)
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción registrada."}
            sub.estado = estado
            if motivo:
                sub.notas = motivo[:300]
            if estado == "cancelada":
                sub.cancelado_en = datetime.now(UTC)
                sub.proximo_cargo = None
            await session.flush()
            return {"estado": estado, "suscripcion_id": str(sub.id)}

    async def pausar_suscripcion(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        return await self._cambiar_estado_suscripcion("pausada", arguments, context)

    async def cancelar_suscripcion(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        return await self._cambiar_estado_suscripcion("cancelada", arguments, context)

    async def renovar_suscripcion(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        from pack_cobranza.jobs import fecha_cargo

        async with get_session() as session:
            cliente = await self._cliente_conversacion(session, context)
            if cliente is None:
                return {"error": "cliente_no_encontrado"}
            sub = await self._suscripcion_del_cliente(session, cliente.id)
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción registrada."}
            sub.estado = "activa"
            sub.cancelado_en = None
            if sub.proximo_cargo is None or sub.proximo_cargo < _hoy():
                sub.proximo_cargo = fecha_cargo(int(sub.dia_facturacion or 1), _hoy(), estricto=False)
            await session.flush()
            return {
                "estado": "activa",
                "suscripcion_id": str(sub.id),
                "proximoCargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
            }

    async def link_suscripcion(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        from pack_cobranza.pagos import config_del_tenant, crear_link_para_cuota

        async with get_session() as session:
            cliente = await self._cliente_conversacion(session, context)
            if cliente is None:
                return {"error": "cliente_no_encontrado"}
            sub = await self._suscripcion_del_cliente(session, cliente.id)
            if sub is None:
                return {"encontrado": False, "mensaje": "No tenés una suscripción registrada."}
            cuota = (
                await session.execute(
                    select(Cuota)
                    .where(
                        Cuota.tenant_id == get_current_tenant_id(),
                        Cuota.suscripcion_id == sub.id,
                        Cuota.estatus.in_(("pendiente", "atrasada")),
                        Cuota.deleted_at.is_(None),
                    )
                    .order_by(Cuota.fecha_vencimiento.asc())
                )
            ).scalars().first()
            if cuota is None:
                return {
                    "encontrado": True,
                    "sin_cargo": True,
                    "mensaje": "Todavía no hay un cargo pendiente para pagar.",
                }
            cfg = await config_del_tenant()
        if cfg is None:
            return {
                "encontrado": True,
                "sin_pasarela": True,
                "monto": float(cuota.valor_esperado),
                "mensaje": "El pago en línea no está configurado; coordinemos el pago por otro medio.",
            }
        payload = await crear_link_para_cuota(cuota, cfg, cliente_id=cliente.id)
        return {"encontrado": True, "concepto": sub.concepto, "cuota_id": str(cuota.id), **payload}

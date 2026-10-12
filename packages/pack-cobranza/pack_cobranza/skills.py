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
from core.kernel.oferta import sin_motor_activo, solo_con_motor_activo
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


async def _cuotas_abiertas(session, prestamo: Prestamo) -> list[Cuota]:
    return list(
        (
            await session.execute(
                select(Cuota).where(
                    Cuota.tenant_id == get_current_tenant_id(),
                    Cuota.prestamo_id == prestamo.id,
                    Cuota.estatus.in_(("pendiente", "atrasada")),
                    Cuota.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )


async def _saldo_y_vencido(session, prestamo: Prestamo) -> tuple[Decimal, Decimal]:
    """(saldo total, lo vencido): lo vencido son las cuotas abiertas que vencieron antes de hoy
    (la misma cuenta que la mora de `_saldo_y_mora`). Alcances de la promesa (HU-AOS-08)."""
    cuotas = await _cuotas_abiertas(session, prestamo)
    saldo = sum((c.valor_esperado for c in cuotas), Decimal(0))
    vencido = sum((c.valor_esperado for c in cuotas if c.fecha_vencimiento < _hoy()), Decimal(0))
    return saldo, vencido


async def _saldo_y_mora(session, prestamo: Prestamo) -> tuple[Decimal, int, Cuota | None]:
    cuotas = await _cuotas_abiertas(session, prestamo)
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
            alcance_promesa="ambos",
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
                # Con el motor activo la promesa va por el procedimiento (HU-AOS-08).
                connection_predicate=sin_motor_activo(),
            ),
            MCPTool(
                # HU-AOS-08 (alcance, decisión del dueño 2026-10-11): solo la ofrece el motor de
                # procedimientos (`promesa_de_pago`). `eleccion` NO se llama como el dato `alcance`
                # a propósito: es una elección que el servidor valida contra lo que ofrece, y la
                # acción fija `alcance` y `monto` en el mismo lote.
                name="cob_opciones_promesa",
                description=(
                    "Opciones de promesa de pago del cliente con los montos del sistema: por lo "
                    "vencido o por el saldo total, según lo que permita la cartera. Llámala sin "
                    "argumentos para ver las opciones y muéstraselas al cliente; cuando elija, "
                    "llámala con su elección. Si hay una sola opción, queda elegida."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "eleccion": {
                            "type": "string",
                            "enum": ["vencido", "saldo_total"],
                            "description": "La opción que eligió el cliente",
                        },
                    },
                    "required": [],
                },
                handler=self.opciones_promesa,
                connection_predicate=solo_con_motor_activo(),
            ),
            MCPTool(
                # HU-AOS-08: solo la ofrece el motor de procedimientos (`promesa_de_pago`): ningún
                # `flow.json` la nombra, así que el loop de siempre no la ve.
                name="cob_registrar_promesa_saldo",
                description=(
                    "Registra la promesa del cliente de pagar en una fecha por la opción que eligió "
                    "(lo vencido o el saldo total). Pasá la fecha (YYYY-MM-DD), el alcance y el monto "
                    "EXACTO que devolvió cob_opciones_promesa: el valor de la promesa es el del "
                    "sistema y si no coincide no se registra. Pide la confirmación del cliente antes "
                    "de ejecutarse."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "fecha": {"type": "string", "description": "Fecha prometida (YYYY-MM-DD)"},
                        "alcance": {"type": "string", "enum": ["vencido", "saldo_total"]},
                        "monto": {"type": "number", "description": "monto de cob_opciones_promesa"},
                    },
                    "required": ["fecha", "alcance", "monto"],
                },
                handler=self.registrar_promesa_saldo,
                connection_predicate=solo_con_motor_activo(),
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

    async def opciones_promesa(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Opciones de la promesa (HU-AOS-08, alcance): los montos y los alcances los decide el
        servidor con la configuración del negocio. Con una elección válida (o una sola opción)
        devuelve `alcance` y `monto`, que el procedimiento guarda juntos."""
        from pack_cobranza.procedimientos import ALCANCES, ROTULO_ALCANCE, opciones_del_prestamo

        eleccion = arguments.get("eleccion")
        if eleccion is not None and eleccion not in ALCANCES:
            return {"error": "Elección inválida: usa vencido o saldo_total."}
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            prestamo = await _prestamo_activo(session, cliente.id) if cliente is not None else None
            if prestamo is None:
                return {"error": "El cliente no tiene un préstamo vigente: no hay promesa que registrar."}
            saldo, vencido = await _saldo_y_vencido(session, prestamo)
            opciones = await opciones_del_prestamo(session, prestamo)
            cartera = await get_owned(session, Cartera, prestamo.cartera_id)
            moneda = cartera.moneda if cartera else "COP"
        if not opciones:
            if saldo <= 0:
                return {"error": "El cliente no tiene saldo pendiente: no hay promesa que registrar."}
            return {
                "error": "El cliente no tiene cuotas vencidas y la cartera solo acepta promesas por lo "
                "vencido: no hay promesa que registrar."
            }
        resultado: dict[str, Any] = {
            "saldo_total": float(saldo),
            "vencido": float(min(vencido, saldo)),
            "opciones": [{"alcance": o.alcance, "monto": float(o.monto)} for o in opciones],
        }
        elegida = next((o for o in opciones if o.alcance == eleccion), None)
        if elegida is None and eleccion is None and len(opciones) == 1:
            elegida = opciones[0]
        if eleccion is not None and elegida is None:
            return {
                "error": "Esa opción no está disponible para este cliente: ofrécele las opciones.",
                "opciones": resultado["opciones"],
            }
        if elegida is not None:
            resultado["alcance"] = elegida.alcance
            resultado["monto"] = float(elegida.monto)
        if eleccion is None:
            # Las opciones con los montos del servidor, tal cual, al final de la respuesta.
            resultado["_safe_append"] = "Opciones de promesa: " + "; ".join(
                f"{ROTULO_ALCANCE[o.alcance]}: {_money(o.monto, moneda)}" for o in opciones
            ) + "."
        return resultado

    async def registrar_promesa_saldo(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Promesa por lo vencido o por el saldo total (HU-AOS-08): la fecha pasa las reglas del
        pack, el alcance tiene que ser uno de los que el servidor ofrece y el monto que confirmó el
        cliente se compara con el del servidor en la misma transacción. El valor del modelo nunca
        se usa como monto. Todo camino sin efecto devuelve `error`."""
        from pack_cobranza.procedimientos import (
            ALCANCES,
            ROTULO_ALCANCE,
            error_de_fecha,
            mismo_saldo,
            opciones_del_prestamo,
            reglas_del_prestamo,
        )

        try:
            fecha = date.fromisoformat(str(arguments.get("fecha") or "").strip())
        except ValueError:
            return {"error": "fecha inválida (usar YYYY-MM-DD)"}
        alcance = arguments.get("alcance")
        if alcance not in ALCANCES:
            return {"error": "Alcance inválido: usa vencido o saldo_total."}
        async with get_session() as session:
            cliente = await _cliente_de_customer(session, context.customer_id)
            prestamo = await _prestamo_activo(session, cliente.id) if cliente is not None else None
            if cliente is None or prestamo is None:
                return {"error": "El cliente no tiene un préstamo vigente: no hay promesa que registrar."}
            existente = (
                await session.execute(
                    select(Promesa).where(
                        Promesa.tenant_id == get_current_tenant_id(),
                        Promesa.idempotency_key == context.idempotency_key,
                        # Solo la promesa de ESTE cliente y préstamo (auditor-multitenant).
                        Promesa.cliente_id == cliente.id,
                        Promesa.prestamo_id == prestamo.id,
                    )
                )
            ).scalar_one_or_none()
            if existente is not None:
                return {
                    "registrada": True, "ya_existe": True, "promesa_id": str(existente.id),
                    "fecha": existente.fecha_prometida.isoformat(),
                    "alcance": existente.alcance, "monto": float(existente.valor_prometido),
                }
            # Serializa las promesas del préstamo (el tope de pendientes es count-then-insert).
            await session.refresh(prestamo, with_for_update=True)
            opciones = await opciones_del_prestamo(session, prestamo)
            opcion = next((o for o in opciones if o.alcance == alcance), None)
            if opcion is None:
                return {
                    "error": "Ese alcance no está disponible para este cliente: vuelve a consultar las opciones.",
                    "opciones": [{"alcance": o.alcance, "monto": float(o.monto)} for o in opciones],
                }
            cartera = await get_owned(session, Cartera, prestamo.cartera_id)
            moneda = cartera.moneda if cartera else "COP"
            if not mismo_saldo(opcion.monto, arguments.get("monto"), moneda):
                return {
                    "error": "El monto cambió desde que se consultó: vuelve a consultar las opciones y confírmalo de nuevo.",
                    "monto": float(opcion.monto),
                }
            motivo = error_de_fecha(fecha, await reglas_del_prestamo(session, prestamo))
            if motivo is not None:
                return {"error": motivo}
            promesa = Promesa(
                tenant_id=get_current_tenant_id(),
                prestamo_id=prestamo.id,
                cliente_id=cliente.id,
                fecha_prometida=fecha,
                valor_prometido=opcion.monto,
                alcance=opcion.alcance,
                tipo="promesa",
                creado_por="ia",
                idempotency_key=context.idempotency_key,
            )
            session.add(promesa)
            await session.flush()
            return {
                "registrada": True,
                "promesa_id": str(promesa.id),
                "fecha": fecha.isoformat(),
                "alcance": opcion.alcance,
                "monto": float(opcion.monto),
                "_safe_append": (
                    f"Promesa registrada: {_money(opcion.monto, moneda)} ({ROTULO_ALCANCE[opcion.alcance]}) "
                    f"el {fecha.isoformat()}."
                ),
            }

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

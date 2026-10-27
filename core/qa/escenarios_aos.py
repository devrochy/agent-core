"""Escenarios del Agent OS (HU-QA-13, adenda de ADR-0135 §7 y §10).

Los turnos corren dentro del ledger del kernel (`decisions.en_turno`) y se anotan con sus
funciones: el kernel escribe `core_decisions` y `turn_outcomes` y corre la fase 2 del traspaso al
cerrar. Aquí no se escriben esas dos tablas. Los pasos van con `record_audit` (toma el turno
vigente) y los mensajes y el consumo con sus modelos; el consumo es `escenario-sin-llm` y no gasta
el crédito del plan (`credits = 0`). Sin LLM: el texto de cada turno es fijo.

Todo corre en el `tenant_scope` del negocio de prueba y bajo el candado de `escenarios.crear`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.kernel import decisions
from core.qa.escenarios import (
    ACTOR,
    MODELO,
    Contexto,
    FalloEscenario,
    Resultado,
    conexion_web,
    crear_cliente,
)
from core.shared.audit import record_audit

COMERCIO = "pack_commerce"
CONTRATAR_PLAN = ("pack_commerce.contratar_plan", "1.3.0")
TOMAR_PEDIDO = ("pack_commerce.tomar_pedido", "1.1.0")


@dataclass(frozen=True)
class Agente:
    instancia: uuid.UUID
    agente: uuid.UUID
    nombre: str


@dataclass(frozen=True)
class Chat:
    """La conexión del chat web con su release activa y los agentes Ventas y Agenda."""

    connection_id: uuid.UUID
    activa: uuid.UUID
    canary: uuid.UUID | None
    ventas: Agente
    agenda: Agente


async def _chat(tenant_id: uuid.UUID) -> Chat:
    cid, activa, canary = await conexion_web(tenant_id)
    if activa is None:
        raise FalloEscenario("El chat web de tu negocio de prueba no tiene una versión activa: pulsa «Preparar mis datos».")
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT a.key, i.id, a.id, a.name FROM agent_instances i JOIN agents a "
                    "ON a.tenant_id = i.tenant_id AND a.id = i.agent_id WHERE i.tenant_id = :t AND i.connection_id = :c "
                    "AND i.deleted_at IS NULL AND a.deleted_at IS NULL AND a.key IN ('ventas', 'agenda')"
                ),
                {"t": tenant_id, "c": cid},
            )
        ).all()
    agentes = {f[0]: Agente(f[1], f[2], f[3]) for f in filas}
    if {"ventas", "agenda"} - set(agentes):
        raise FalloEscenario("Faltan los agentes Ventas o Agenda en el chat web: pulsa «Preparar mis datos».")
    return Chat(cid, activa, canary, agentes["ventas"], agentes["agenda"])


async def _conversacion(
    ctx: Contexto,
    chat: Chat,
    nombre: str,
    *,
    dueno: Agente,
    release_id: uuid.UUID | None = None,
    lane: str = "stable",
) -> SimpleNamespace:
    """Cliente ficticio y su conversación del chat web, atendida por `dueno` con la release del
    carril. Devuelve el objeto que espera `decisions.decidir` (id, negocio y conexión)."""
    _cliente, conv = await crear_cliente(ctx.tenant_id, nombre, sufijo=ctx.sufijo, connection_id=chat.connection_id)
    assert conv is not None
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE conversations SET release_id = :r, release_lane = :l, active_agent_instance_id = :i, "
                "owner_since = now() WHERE tenant_id = :t AND id = :c"
            ),
            {"t": ctx.tenant_id, "c": conv, "r": release_id or chat.activa, "l": lane, "i": dueno.instancia},
        )
    return SimpleNamespace(id=conv, tenant_id=ctx.tenant_id, connection_id=chat.connection_id)


async def _mensaje(conv: Any, direccion: str, texto: str, *, quien: str) -> uuid.UUID:
    from core.db.models import Message

    async with get_session() as session:
        fila = Message(
            tenant_id=conv.tenant_id,
            conversation_id=conv.id,
            direction=direccion,
            sender_type=quien,
            content=texto,
            status="sent",
            idempotency_key=f"web:qa-escenario-{uuid.uuid4()}" if direccion == "inbound" else None,
        )
        session.add(fila)
        await session.flush()
        return fila.id


async def _uso(conv: Any, chat: Chat, agente: Agente, release_id: uuid.UUID, tokens: tuple[int, int], costo: str) -> None:
    """Consumo del turno como lo deja el runtime, pero a cargo de la plataforma (`credits = 0`)."""
    from core.billing.tarifario import TARIFARIO_VIGENTE, trm_vigente
    from core.db.models import UsageLedger

    async with get_session() as session:
        session.add(
            UsageLedger(
                tenant_id=conv.tenant_id,
                conversation_id=conv.id,
                model=MODELO,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                latency_ms=900,
                cost_usd=Decimal(costo),
                kind="turn",
                connection_id=chat.connection_id,
                agent_instance_id=agente.instancia,
                release_id=release_id,
                credits=Decimal("0.0"),
                key_mode="platform",
                tarifario_version=TARIFARIO_VIGENTE.version,
                trm_cop=trm_vigente(),
            )
        )
    decisions.anotar_uso(tokens[0], tokens[1], float(costo))


def _ruta(procedimiento: str, agente: str, *, regla: str, confianza: float) -> SimpleNamespace:
    """La ruta del router como la anota el kernel (`anotar_ruta`), sin LLM."""
    return SimpleNamespace(
        candidates=[{"procedure_id": procedimiento, "owner": agente, "score": confianza}],
        source="rule",
        rule_id=regla,
        confidence=confianza,
        pila_sombra=None,
        sombra=lambda: None,
    )


async def _turno(
    conv: Any,
    chat: Chat,
    agente: Agente,
    *,
    entrada: str,
    respuesta: str | None,
    release_id: uuid.UUID | None = None,
    lane: str = "stable",
    ruta: SimpleNamespace | None = None,
    escalado: bool = False,
    tokens: tuple[int, int] = (1200, 90),
    costo: str = "0.0011",
    verificador: list[dict[str, Any]] | None = None,
    reintentos: int = 0,
    dentro: Any = None,
) -> uuid.UUID:
    """Un turno del ledger del kernel: mensaje del cliente, decisión, lo que haga `dentro` (pasos,
    tareas, A2A), respuesta, consumo y veredicto. Devuelve el `turn_id`."""
    rid = release_id or chat.activa

    @decisions.en_turno
    async def correr() -> uuid.UUID:
        turno = decisions.turno_actual()
        if turno is None:
            raise FalloEscenario("No se pudo abrir el turno en el ledger del kernel.")
        decisions.anotar_entrada(await _mensaje(conv, "inbound", entrada, quien="customer"))
        decisions.anotar_release(rid, lane, active_instance_id=agente.instancia)
        if ruta is not None:
            decisions.anotar_ruta(ruta, activa=True)
        if escalado:
            await decisions.decidir(conv, "escalated")
            decisions.marcar_escalado()
        else:
            await decisions.enrutar(conv)
        if turno._token_vigente is None:  # la cabecera no se escribió (best-effort del kernel)
            raise FalloEscenario("El kernel no registró el turno; vuelve a intentarlo.")
        if dentro is not None:
            await dentro(turno)
        if respuesta:
            await _mensaje(conv, "outbound", respuesta, quien="agent")
        await _uso(conv, chat, agente, rid, tokens, costo)
        if verificador is not None:
            decisions.anotar_veredicto(verificador, reintentos)
            motivos = [v["kind"] for v in verificador if v.get("verdict") == "fail"]
            await record_audit(
                actor="core:verificador",
                action="verificacion",
                conversation_id=conv.id,
                input_data={"resultado": "reescrita" if motivos else "ok", "intentos": reintentos + 1, "motivos": motivos},
            )
        decisions.anotar_resultado(SimpleNamespace(text=respuesta or "", escalated=escalado))
        return turno.id

    return await correr()


async def _comando(conv: Any, nombre: str, claves: list[str], *, ok: bool = True) -> None:
    await record_audit(
        actor="core:comando",
        action=nombre,
        conversation_id=conv.id,
        input_data={"keys": claves},
        output_data={"ok": ok, "errors": 0 if ok else 1, "codigos": [] if ok else ["dato_invalido"]},
    )


def _grafo(chat: Chat) -> Any:
    """Enlace Ventas → Agenda para consultar y traspasar. Es el grafo que el negocio de prueba
    tendría al conectar los dos agentes en Colaboración."""
    from core.kernel import a2a

    return a2a.GrafoCongelado.desde_manifiesto(
        {
            "links": [
                {
                    "from_agent_id": str(chat.ventas.agente),
                    "to_agent_id": str(chat.agenda.agente),
                    "modes": ["query", "handoff"],
                    "budget_usd": "0.01",
                    "budget_tokens": 4000,
                }
            ]
        },
        {chat.ventas.instancia: chat.ventas.agente, chat.agenda.instancia: chat.agenda.agente},
    )


# ------------------------------------------------------------------ conversación con turnos
async def conversacion_con_turnos(ctx: Contexto) -> Resultado:
    from core.kernel import a2a, handoff, state

    chat = await _chat(ctx.tenant_id)
    conv = await _conversacion(ctx, chat, f"Ana Pérez · {ctx.sufijo}", dueno=chat.ventas)
    turnos: list[uuid.UUID] = []

    # 1. Normal: pregunta de catálogo, sin acciones.
    turnos.append(
        await _turno(
            conv, chat, chat.ventas,
            entrada="Hola, ¿qué cafés tienen en grano?",
            respuesta="¡Hola! Tenemos Café de origen 500 g en grano a $38.000 y también molido. ¿Te cuento más?",
            ruta=_ruta("pack_commerce.tomar_pedido", "ventas", regla="preset_ventas_palabras", confianza=0.86),
            verificador=[{"kind": "monto_sin_evidencia", "verdict": "pass", "count": 1}],
        )
    )

    # 2. Acción con datos personales ficticios, una consulta a Agenda y un rechazo del verificador.
    async def accion(turno: Any) -> None:
        async with get_session() as s:
            tarea = await state.iniciar(
                s, tenant_id=ctx.tenant_id, conversation_id=conv.id, procedure_id=TOMAR_PEDIDO[0],
                procedure_version=TOMAR_PEDIDO[1], stage="armar", actor="kernel",
                owner_instance_id=chat.ventas.instancia, turn_id=turno.id,
            )
            for slot, valor, clase in (("address", "Calle 23 # 21-45, Manizales", "pii"), ("payment_reference", "QA-REF-48213907", "payment")):
                await state.registrar(
                    s, tenant_id=ctx.tenant_id, conversation_id=conv.id, task_id=tarea.id, tipo="slot_set",
                    data={"slot": slot, "value": valor, "source": "llm", "data_class": clase}, actor="llm",
                    turn_id=turno.id,
                )
        await record_audit(
            actor="tool:crear_cotizacion",
            action="execute",
            conversation_id=conv.id,
            input_data={"email": "ana.perez@ejemplo.test", "telefono": "3005550147", "sku": "QA-CAFE-500", "cantidad": 2, "total": 76000},
            output_data={"quote_id": "COT-QA-0412", "total": 76000, "status": "draft"},
        )
        await _comando(conv, "actualizar_tarea", ["address", "payment_reference"])
        estado = a2a.EstadoDelTurno(ctx.tenant_id, chat.connection_id, conv.id, turno.id, release_id=chat.activa)
        decision = await a2a.registrar(
            estado,
            a2a.Operacion(chat.ventas.instancia, chat.agenda.instancia, "query", "consultar_disponibilidad", {"dia": "sabado"}),
            _grafo(chat),
            a2a.Limites(),
        )
        if not decision.permitido:
            raise FalloEscenario(f"La consulta entre agentes no se registró ({decision.razon}).")
        await a2a.liquidar(
            estado, decision.call_id, ok=True,
            gasto=a2a.Gasto(MODELO, 140, 25, Decimal("0.0004"), 380), resultado={"cupos": 3},
        )
        decisions.anotar_a2a(estado)

    turnos.append(
        await _turno(
            conv, chat, chat.ventas,
            entrada="Quiero 2 de 500 g. Mi correo es ana.perez@ejemplo.test y mi celular 300 555 0147.",
            respuesta="Listo, te preparé la cotización COT-QA-0412 por $76.000 con envío a Manizales.",
            ruta=_ruta("pack_commerce.tomar_pedido", "ventas", regla="preset_ventas_palabras", confianza=0.93),
            verificador=[
                {"kind": "monto_sin_evidencia", "verdict": "fail", "count": 1},
                {"kind": "links_sin_respaldo", "verdict": "pass", "count": 1},
            ],
            reintentos=1,
            tokens=(2400, 160),
            costo="0.0023",
            dentro=accion,
        )
    )

    # 3. Traspaso de Ventas a Agenda (fase 1 aquí; la fase 2 la corre el kernel al cerrar el turno).
    async def traspaso(turno: Any) -> None:
        estado = a2a.EstadoDelTurno(ctx.tenant_id, chat.connection_id, conv.id, turno.id, release_id=chat.activa)
        decision = await a2a.registrar(
            estado, a2a.Operacion(chat.ventas.instancia, chat.agenda.instancia, "handoff", "handoff"), _grafo(chat), a2a.Limites()
        )
        if not decision.permitido:
            raise FalloEscenario(f"El traspaso entre agentes no se registró ({decision.razon}).")
        a2a.liberar(estado, decision.call_id)
        async with get_session() as s:
            abiertas = await state.tareas_abiertas(s, tenant_id=ctx.tenant_id, conversation_id=conv.id)
        decisions.anotar_a2a(estado)
        decisions.anotar_handoff(
            handoff.Pendiente(
                estado, decision.call_id, chat.ventas.instancia, chat.agenda.instancia, conv.id,
                frozenset(t.id for t in abiertas),
            )
        )
        await _comando(conv, "pasar_a_agente", ["agente"])

    turnos.append(
        await _turno(
            conv, chat, chat.ventas,
            entrada="Antes de pagar, ¿puedo agendar una asesoría para aprender a prepararlo?",
            respuesta="¡Claro! Te paso con Agenda para buscar un horario.",
            ruta=_ruta("pack_calendar.agendar_cita", "agenda", regla="preset_agenda_palabras", confianza=0.81),
            dentro=traspaso,
        )
    )
    async with get_session() as s:
        dueno = (
            await s.execute(
                text("SELECT active_agent_instance_id FROM conversations WHERE tenant_id = :t AND id = :c"),
                {"t": ctx.tenant_id, "c": conv.id},
            )
        ).scalar_one()
    if dueno != chat.agenda.instancia:
        raise FalloEscenario("El traspaso a Agenda no se aplicó.")
    return Resultado(
        detalle=[
            f"Conversación de «Ana Pérez · {ctx.sufijo}» en el chat web con 3 turnos",
            "Turno 1: respuesta normal de Ventas",
            "Turno 2: acción con correo y celular ficticios, una consulta a Agenda y un rechazo del verificador",
            f"Turno 3: traspaso de {chat.ventas.nombre} a {chat.agenda.nombre}",
        ],
        conteos={"turnos": len(turnos), "conversaciones": 1},
        ruta=f"/conversaciones/{conv.id}",
    )


# ------------------------------------------------------------------ escalación en curso
async def escalacion_en_curso(ctx: Contexto) -> Resultado:
    from core.kernel import state
    from core.orchestrator.escalation import escalate

    chat = await _chat(ctx.tenant_id)
    conv = await _conversacion(ctx, chat, f"Mateo Salazar · {ctx.sufijo}", dueno=chat.ventas)
    await _turno(
        conv, chat, chat.ventas,
        entrada="Quiero contratar el plan de asesoría, pero prefiero hablar con una persona.",
        respuesta="Te comunico con alguien del equipo. Mientras tanto, guardo lo que ya me diste.",
        ruta=_ruta(CONTRATAR_PLAN[0], "ventas", regla="preset_ventas_palabras", confianza=0.9),
    )
    await escalate(conv.id, "tool:solicitud_cliente", "Quiere contratar el plan de asesoría y pide hablar con una persona.")
    async with get_session() as s:
        escalada = (
            await s.execute(
                text("SELECT status FROM conversations WHERE tenant_id = :t AND id = :c"), {"t": ctx.tenant_id, "c": conv.id}
            )
        ).scalar_one()
    if escalada != "escalated":
        raise FalloEscenario("La conversación no quedó escalada.")
    # Las tareas van después de la escalación: la tarjeta muestra una tarea cerrada como `escalated`
    # solo si se cerró después de escalar.
    async with get_session() as s:
        pedido = await state.iniciar(
            s, tenant_id=ctx.tenant_id, conversation_id=conv.id, procedure_id=TOMAR_PEDIDO[0],
            procedure_version=TOMAR_PEDIDO[1], stage="armar", actor="kernel", owner_instance_id=chat.ventas.instancia,
        )
        for tipo, data, actor in (
            ("action_requested", {"action": "crear_cotizacion"}, "llm"),
            ("action_failed", {"action": "crear_cotizacion", "reason": "sin_stock"}, "tool"),
            ("escalated", {"reason": "action_error"}, "kernel"),
        ):
            await state.registrar(
                s, tenant_id=ctx.tenant_id, conversation_id=conv.id, task_id=pedido.id, tipo=tipo, data=data, actor=actor
            )
    async with get_session() as s:
        plan = await state.iniciar(
            s, tenant_id=ctx.tenant_id, conversation_id=conv.id, procedure_id=CONTRATAR_PLAN[0],
            procedure_version=CONTRATAR_PLAN[1], stage="elegir_plan", actor="kernel", owner_instance_id=chat.ventas.instancia,
        )
        for slot, valor, clase, actor, origen in (
            ("full_name", "Mateo Salazar", "pii", "llm", "llm"),
            ("email", "mateo.salazar@ejemplo.test", "pii", "llm", "llm"),
            ("payment_reference", "QA-PAGO-77310459", "payment", "tool", "action:cotizar_plan"),
        ):
            await state.registrar(
                s, tenant_id=ctx.tenant_id, conversation_id=conv.id, task_id=plan.id, tipo="slot_set",
                data={"slot": slot, "value": valor, "source": origen, "data_class": clase}, actor=actor,
            )
    return Resultado(
        detalle=[
            f"Conversación de «Mateo Salazar · {ctx.sufijo}» escalada y pendiente",
            "«Contratar un plan» en «elegir plan» con nombre, correo y una referencia de pago; falta el plan",
            "«Tomar pedido» cerrado al pasar a una persona, con una acción fallida",
        ],
        conteos={"conversaciones": 1, "escalaciones": 1, "tareas": 2},
        ruta=f"/conversaciones/{conv.id}",
    )


# ------------------------------------------------------------------ calidad con muestra
TURNOS_POR_AGENTE = 25  # por encima de `fiabilidad.MIN_TURNOS` (20) por agente
CONVERSACIONES_POR_AGENTE = 5
RECORRIDOS = 12  # terminados, por encima de `fiabilidad.MIN_TAREAS` (10)


async def calidad_con_muestra(ctx: Contexto) -> Resultado:
    from core.kernel import state

    chat = await _chat(ctx.tenant_id)
    turnos = escalados = rechazos = 0
    recorridos: dict[str, int] = {"completed": 0, "escalated": 0, "failed": 0, "active": 0}
    # Patrón fijo: el turno i de cada agente escala si i % 9 == 8, el verificador rechaza si i % 6 == 5.
    for agente, proc, etapa in ((chat.ventas, TOMAR_PEDIDO, "armar"), (chat.agenda, ("pack_calendar.agendar_cita", "1.0.0"), "elegir_horario")):
        convs = [
            await _conversacion(ctx, chat, f"Cliente de calidad {agente.nombre} {n + 1} · {ctx.sufijo}", dueno=agente)
            for n in range(CONVERSACIONES_POR_AGENTE)
        ]
        for i in range(TURNOS_POR_AGENTE):
            conv = convs[i % len(convs)]
            escala = i % 9 == 8
            rechaza = i % 6 == 5
            # Los recorridos son de Ventas (`tomar_pedido`): uno por turno en los primeros.
            nuevo_recorrido = agente is chat.ventas and i < RECORRIDOS + 2

            async def dentro(
                turno: Any, conv: Any = conv, i: int = i, nuevo: bool = nuevo_recorrido,
                agente: Agente = agente, proc: tuple[str, str] = proc, etapa: str = etapa,
            ) -> None:
                if nuevo:
                    await _recorrido(ctx, conv, agente, proc, etapa, turno.id, i, recorridos, state)

            await _turno(
                conv, chat, agente,
                entrada=f"Mensaje de prueba {i + 1} para {agente.nombre}.",
                respuesta=None if escala else f"Respuesta de prueba {i + 1} de {agente.nombre}.",
                ruta=_ruta(proc[0], "ventas" if agente is chat.ventas else "agenda", regla="preset_palabras", confianza=0.8),
                escalado=escala,
                tokens=(900 + 40 * (i % 5), 70 + 5 * (i % 4)),
                costo=f"0.00{10 + (i % 7)}",
                verificador=[{"kind": "monto_sin_evidencia" if i % 2 else "links_sin_respaldo", "verdict": "fail", "count": 1}]
                if rechaza
                else [{"kind": "monto_sin_evidencia", "verdict": "pass", "count": 1}],
                reintentos=1 if rechaza else 0,
                dentro=dentro,
            )
            turnos += 1
            escalados += int(escala)
            rechazos += int(rechaza)
    await _veredicto_de_consistencia(ctx, chat)
    terminados = recorridos["completed"] + recorridos["escalated"] + recorridos["failed"]
    return Resultado(
        detalle=[
            f"{turnos} turnos de {chat.ventas.nombre} y {chat.agenda.nombre} en {2 * CONVERSACIONES_POR_AGENTE} conversaciones",
            f"{escalados} pasaron a una persona y el verificador rechazó {rechazos} respuestas",
            f"{terminados} recorridos terminados ({recorridos['completed']} completos) y {recorridos['active']} en curso",
            "Un veredicto de consistencia de la versión activa (sin LLM)",
        ],
        conteos={"turnos": turnos, "escalados": escalados, "rechazos": rechazos, "recorridos": terminados},
        ruta="/agentes/calidad",
    )


async def _recorrido(
    ctx: Contexto, conv: Any, agente: Agente, proc: tuple[str, str], etapa: str, turno: uuid.UUID, i: int,
    cuenta: dict[str, int], state: Any,
) -> None:
    """Un recorrido empezado en el turno: completo, escalado, fallido o en curso, en un patrón fijo.
    Una conversación con un recorrido en curso no abre otro (una sola tarea en foco)."""
    final = ("completed", "completed", "escalated", "completed", "failed", "completed", "completed", "active")[i % 8]
    async with get_session() as s:
        abiertas = await state.tareas_abiertas(s, tenant_id=ctx.tenant_id, conversation_id=conv.id)
        if abiertas:  # una sola tarea en foco por conversación: la anterior queda en curso
            return
        tarea = await state.iniciar(
            s, tenant_id=ctx.tenant_id, conversation_id=conv.id, procedure_id=proc[0], procedure_version=proc[1],
            stage=etapa, actor="kernel", owner_instance_id=agente.instancia, turn_id=turno,
        )
        if final == "completed":
            await state.registrar(s, tenant_id=ctx.tenant_id, conversation_id=conv.id, task_id=tarea.id, tipo="completed", data={}, actor="kernel", turn_id=turno)
        elif final in ("escalated", "failed"):
            motivo = "pidio_humano" if final == "escalated" else "action_error"
            await state.registrar(s, tenant_id=ctx.tenant_id, conversation_id=conv.id, task_id=tarea.id, tipo=final, data={"reason": motivo}, actor="kernel", turn_id=turno)
    cuenta[final] += 1


async def _veredicto_de_consistencia(ctx: Contexto, chat: Chat) -> None:
    """Un veredicto sobre la release ACTIVA (lo que lee `consistencia_vigente`), sin LLM: base nula,
    `origen = escenario` y un sha centinela que no coincide con ninguna medición real."""
    from core.releases import veredicto as v
    from core.releases.cargar import cargar_release

    async with get_session() as s:
        release = await cargar_release(s, ctx.tenant_id, chat.activa)
    procedimientos = sorted(release.procedimientos()) if release is not None else []
    if not procedimientos:
        return
    mediciones = [
        v.Medicion(
            pid,
            tuple(v.Escenario(f"{n:02d}", 4, 1.0, 1.0 if (n + k) % 5 else 0.75, True) for n in range(4)),
            f"escenario-qa:{ctx.sufijo}",
        )
        for k, pid in enumerate(procedimientos)
    ]
    await v.registrar(
        ctx.tenant_id, chat.connection_id, chat.activa,
        base_release_id=None, tocados={}, mediciones=mediciones, veredicto=v.calcular(mediciones),
        costo_usd=0.0, modelo=MODELO, origen="escenario", actor=ACTOR,
    )


# ------------------------------------------------------------------ versiones y canary
async def _candidata(ctx: Contexto, chat: Chat) -> tuple[uuid.UUID, int, str]:
    """Cambia el nombre del asistente (la misma asignación que `PUT /settings/identity`) a uno único
    y construye. 409 si el dedupe cae en la activa o en una versión detenida."""
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.plugin_manager import plugin_manager
    from core.releases.build import construir
    from core.releases.promocion import bloqueada, ultimo_ciclo

    nombre = f"Asistente QA {ctx.sufijo}"
    async with get_session() as s:
        app = (
            await s.execute(
                select(AppSettings).where(AppSettings.tenant_id == ctx.tenant_id, AppSettings.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if app is None:
            raise FalloEscenario("Tu negocio de prueba no tiene identidad configurada.")
        app.assistant_name = nombre
    build = await construir(
        ctx.tenant_id, chat.connection_id, actor=ACTOR, modules=list(plugin_manager.list_modules()),
        notas=f"Escenario de prueba: nombre del asistente «{nombre}».",
    )
    if build.release_id == chat.activa:
        raise FalloEscenario("La versión nueva salió igual a la activa; vuelve a intentarlo.")
    async with get_session() as s:
        if bloqueada(*await ultimo_ciclo(s, ctx.tenant_id, build.release_id)):
            raise FalloEscenario("La versión nueva coincide con una versión detenida; vuelve a intentarlo.")
    return build.release_id, build.number, nombre


async def version_candidata(ctx: Contexto) -> Resultado:
    chat = await _chat(ctx.tenant_id)
    if chat.canary is not None:
        raise FalloEscenario("Hay una prueba con una parte en curso en el chat web: detenla o publícala primero.")
    _rid, numero, nombre = await _candidata(ctx, chat)
    return Resultado(
        detalle=[f"Versión {numero} candidata en el chat web: el asistente se llama «{nombre}»", "No se publicó"],
        conteos={"versiones": 1},
        ruta=f"/conexiones/{chat.connection_id}/versiones",
    )


MUESTRA_ESTABLE = 12
MUESTRA_CANARY = 4
PORCENTAJE = 20


async def canary_en_curso(ctx: Contexto) -> Resultado:
    from core.releases import canary

    chat = await _chat(ctx.tenant_id)
    if chat.canary is not None:
        raise FalloEscenario("Ya hay una prueba con una parte en curso en el chat web: detenla primero.")
    rid, numero, nombre = await _candidata(ctx, chat)
    await canary.iniciar(ctx.tenant_id, chat.connection_id, rid, percent=PORCENTAJE, actor=ACTOR)
    # Muestra parcial: turnos `replied` (ni fallidos ni escalados: sin señal dura) en los dos
    # carriles, por debajo de las 200 conversaciones (el job la deja en `sin_muestra`).
    for lane, release, n in (("stable", chat.activa, MUESTRA_ESTABLE), ("canary", rid, MUESTRA_CANARY)):
        for k in range(n):
            agente = chat.ventas if k % 2 == 0 else chat.agenda
            conv = await _conversacion(
                ctx, chat, f"Cliente de la prueba {lane} {k + 1} · {ctx.sufijo}", dueno=agente, release_id=release, lane=lane
            )
            await _turno(
                conv, chat, agente,
                entrada="¿Cómo te llamas?",
                respuesta=f"Soy el asistente de la tienda (versión {lane}).",
                release_id=release, lane=lane,
                tokens=(800 + 30 * k, 60), costo=f"0.00{9 + (k % 3)}",
            )
    return Resultado(
        detalle=[
            f"Versión {numero} («{nombre}») en prueba con el {PORCENTAJE} % de las conversaciones nuevas",
            f"Muestra parcial: {MUESTRA_ESTABLE} conversaciones en la versión activa y {MUESTRA_CANARY} en la de prueba",
        ],
        conteos={"versiones": 1, "conversaciones": MUESTRA_ESTABLE + MUESTRA_CANARY},
        ruta=f"/conexiones/{chat.connection_id}/versiones",
    )

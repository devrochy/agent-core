"""SPEC-43 · el motor de procedimientos sobre `pack_subscriptions.contratar_plan`, contra el loop real, sin LLM.

Cuando `contratar_plan` salió de commerce (HU-COM-17) estos casos del kernel perdieron su vehículo: los de las
pruebas reales del 2026-10-12/13 (datos que el modelo inventa, confirmaciones que vencen, correo mal escrito,
comandos y negaciones que no escalan en falso). Se reproducen aquí con el recorrido nuevo, que usa los mismos
mecanismos del kernel. Todo con transferencia: el stack de la suite no tiene pasarela de pago.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from test_procedimientos_agenda_subs_cobranza import (
    _banderas,
    _conversacion,
    _Guion,
    _negocio,
    _scope,
    _tarea,
    _turno,
    packs_registrados,  # noqa: F401  (fixture del módulo hermano)
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PROC = "pack_subscriptions.contratar_plan"
DOCUMENTO, TELEFONO = "10203040", "+573001112233"


async def _con_plan(tid: uuid.UUID, codigo: str = "PREMIUM", precio: str = "600000") -> None:
    from pack_subscriptions.planes import DatosDePlan, crear

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await crear(DatosDePlan(codigo=codigo, nombre=codigo.title(), precio=Decimal(precio), periodicidad="mensual"))


async def _listo() -> tuple[uuid.UUID, Any]:
    """Negocio con el motor y el plan Premium; devuelve (negocio, conversación)."""
    _asegurar_resumen()
    tid = await _negocio(["pack_subscriptions"])
    await _banderas(tid, True)
    await _con_plan(tid)
    return tid, await _conversacion(tid)


def _asegurar_resumen() -> None:
    """El resumen de `sub_confirmar_inscripcion` lo registra el pack al activarse y solo si las skills del core ya
    estaban; otro test del mismo proceso puede haber activado el pack antes (o quitado su registro), y entonces el
    kernel cae al texto fijo con los argumentos crudos. Se vuelve a publicar solo el resumen para que este módulo no
    dependa del orden de la corrida."""
    from pack_subscriptions.procedimientos import resumen_de_inscripcion

    from core.mcp.host import mcp_host
    from core.tools.core_skill import register_core_skills

    if mcp_host.get_resumen("sub_confirmar_inscripcion") is not None:
        return
    if mcp_host.get_tool("sub_confirmar_inscripcion") is None:
        register_core_skills(mcp_host)
    mcp_host.begin_activation("pack_subscriptions")
    try:
        mcp_host.register_resumen("sub_confirmar_inscripcion", resumen_de_inscripcion)
    finally:
        mcp_host.end_activation()


async def _hasta_datos(conv: Any, monkeypatch: Any) -> Any:  # type: ignore[no-untyped-def]
    """Elige el plan y lo cotiza: la tarea queda en `datos`."""
    guion = _Guion([
        ("iniciar_procedimiento", {"procedure_id": PROC}),
        ("actualizar_tarea", {"slots": {"plan_code": "premium"}}),
        ("sub_cotizar_inscripcion", {"plan_code": "PREMIUM"}),
        "Plan Premium: $600.000 al mes. Necesito unos datos…",
    ])
    resultado = await _turno(conv, guion, monkeypatch, "Quiero contratar el plan Premium")
    tarea, _ = await _tarea(conv, PROC)
    assert tarea.stage == "datos", tarea.stage
    return resultado


def _pedidos(tarea: Any) -> set[str]:
    return {k[5:] for k, v in tarea.slots.items() if k.startswith("pide_") and (v or {}).get("value") is True}


def _valor(tarea: Any, dato: str) -> Any:
    return (tarea.slots.get(dato) or {}).get("value")


def _datos(pedidos: set[str], vertical: str, correo: str = "ana@ejemplo.co") -> dict[str, str]:
    todos = {
        "negocio": "Taller Ana", "vertical": vertical, "nombre": "Ana Gómez", "documento": DOCUMENTO,
        "email": correo, "telefono": TELEFONO,
    }
    return {k: v for k, v in todos.items() if k in pedidos}


async def _hasta_confirmar(conv: Any, monkeypatch: Any) -> Any:  # type: ignore[no-untyped-def]
    """Todos los datos y el método de pago: la tarea pasa a `confirmar` con el resumen del kernel."""
    from core.presets import list_presets

    await _hasta_datos(conv, monkeypatch)
    tarea, _ = await _tarea(conv, PROC)
    datos = _datos(_pedidos(tarea), list_presets()[0].id)
    guion = _Guion([
        ("actualizar_tarea", {"slots": {**datos, "payment_method": "transferencia"}}),
        ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "monto_a_pagar": 600000}),
        "¿Confirmás todo?",
    ])
    resultado = await _turno(conv, guion, monkeypatch, "Mis datos: " + ", ".join(datos.values()) + ", transferencia")
    tarea, _ = await _tarea(conv, PROC)
    assert tarea.stage == "confirmar", (tarea.stage, guion.vistos[-1][-600:])
    # El harness guarda «(respuesta)» como saliente: el eco del resumen necesita el texto real que vio el cliente.
    async with _scope(conv.tenant_id) as s:
        await s.execute(
            text("update messages set content = :x where id = (select id from messages where tenant_id = :t and "
                 "conversation_id = :c and direction = 'outbound' order by created_at desc limit 1)"),
            {"x": resultado.text, "t": conv.tenant_id, "c": conv.id},
        )
    return resultado


async def test_el_resumen_es_el_del_kernel_y_un_si_a_secas_no_confirma(packs_registrados, monkeypatch) -> None:  # noqa: F811
    tid, conv = await _listo()
    resultado = await _hasta_confirmar(conv, monkeypatch)
    assert resultado.confirmacion is not None and resultado.confirmacion.modo == "interactive_button"
    assert "600.000" in resultado.text and "¿Confirmás todo?" not in resultado.text  # el texto del modelo no sale

    # Un «Si» a secas NO confirma la acción con monto (HU-AOS-09 sigue estricto).
    guion = _Guion([("confirmar_tarea", {}), "Para confirmar toca «Confirmar» o escribe «confirmo»."])
    await _turno(conv, guion, monkeypatch, "Si")
    assert "Falta la confirmación explícita" in guion.vistos[-1]
    tarea, _ = await _tarea(conv, PROC)
    assert tarea.stage == "confirmar"
    async with _scope(tid) as s:
        n = (await s.execute(text("select count(*) from pack_subscriptions_subscriptions where tenant_id = :t"), {"t": tid})).scalar_one()
    assert n == 0  # nada se inscribe sin el «confirmo» explícito


async def test_accion_antes_de_confirmar_tarea_sigue_funcionando(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """En el turno siguiente a la petición, el modelo llama primero la acción, después `confirmar_tarea` y otra
    vez la acción: la petición del turno anterior sigue valiendo (no se vuelve a pedir)."""
    _tid, conv = await _listo()
    await _hasta_confirmar(conv, monkeypatch)
    guion = _Guion([
        ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "monto_a_pagar": 600000}),
        ("confirmar_tarea", {}),
        ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "monto_a_pagar": 600000}),
        "Listo.",
    ])
    await _turno(conv, guion, monkeypatch, "Sí, confirmo")
    tarea, _ = await _tarea(conv, PROC)
    assert tarea.stage == "pagar", guion.vistos[-1][-800:]


async def test_el_nombre_que_el_modelo_inventa_o_dijo_otro_cliente_no_vale(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """Un dato solo se acepta si el cliente de ESTA conversación lo escribió (incidente del 2026-10-12: el modelo
    fijó `nombre` con el del perfil del canal; la regla filtra por negocio y conversación)."""
    tid = await _negocio(["pack_subscriptions"])
    await _banderas(tid, True)
    await _con_plan(tid)
    otra = await _conversacion(tid)
    await _turno(otra, _Guion(["Hola Valentina."]), monkeypatch, "Me llamo Valentina Ospina")
    conv = await _conversacion(tid)
    await _hasta_datos(conv, monkeypatch)
    guion = _Guion([
        ("actualizar_tarea", {"slots": {"nombre": "Valentina Ospina"}}),
        ("actualizar_tarea", {"slots": {"nombre": "Rob"}}),
        "¿Cuál es tu nombre completo?",
    ])
    await _turno(conv, guion, monkeypatch, "Mi documento es " + DOCUMENTO)
    assert "nombre no aparece en lo que escribió el cliente" in guion.vistos[-1]
    tarea, _ = await _tarea(conv, PROC)
    assert _valor(tarea, "nombre") is None


async def test_correo_con_dominio_mal_escrito_se_pregunta(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """`ana@gmai.com`: el primer intento se rechaza con la sugerencia (todo o nada); si el cliente insiste en el
    turno siguiente se acepta, y la corrección a `gmail.com` también vale."""
    tid, conv = await _listo()
    await _hasta_datos(conv, monkeypatch)
    malo = f"ana{uuid.uuid4().hex[:6]}@gmai.com"

    guion = _Guion([
        ("actualizar_tarea", {"slots": {"nombre": "Ana Gómez", "email": malo}}),
        "¿Quisiste decir @gmail.com?",
    ])
    await _turno(conv, guion, monkeypatch, f"Ana Gómez, {malo}")
    visto = guion.vistos[-1]
    assert '"ok": false' in visto and "@gmail.com" in visto, visto[-500:]
    tarea, _ = await _tarea(conv, PROC)
    assert _valor(tarea, "email") is None and _valor(tarea, "nombre") is None
    # La auditoría del comando fallido guarda el código del error.
    async with _scope(tid) as s:
        filas = (await s.execute(
            text("select output from audit_log where tenant_id = :t and conversation_id = :c "
                 "and actor = 'core:comando' and action = 'actualizar_tarea' order by created_at"),
            {"t": tid, "c": conv.id},
        )).all()
    assert "correo_dudoso" in [c for f in filas for c in (f.output.get("codigos") or [])]

    guion = _Guion([("actualizar_tarea", {"slots": {"nombre": "Ana Gómez", "email": malo}}), "Listo."])
    await _turno(conv, guion, monkeypatch, "No, está bien así")
    assert '"ok": true' in guion.vistos[-1]
    tarea, _ = await _tarea(conv, PROC)
    assert _valor(tarea, "email") == malo

    corregido = malo.replace("gmai.com", "gmail.com")
    guion = _Guion([("actualizar_tarea", {"slots": {"email": corregido}}), "Listo."])
    await _turno(conv, guion, monkeypatch, "Mejor sí, era gmail")
    assert '"ok": true' in guion.vistos[-1]
    assert _valor((await _tarea(conv, PROC))[0], "email") == corregido


async def test_comandos_y_negaciones_no_escalan_en_falso(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """Un turno de comandos, consultas y negaciones del kernel: los comandos no gastan vueltas y, si se agotan
    con la tarea en foco, el kernel pide lo que falta en vez de decir «te comunico con una persona»."""
    tid, conv = await _listo()
    pasos: list[Any] = [
        ("iniciar_procedimiento", {"procedure_id": PROC}),
        ("sub_listar_planes", {}),
        ("actualizar_tarea", {"slots": {"vertical": "astronautas"}}),  # error del resolver
        ("actualizar_tarea", {"slots": {"plan_code": "premium"}}),
        ("sub_cotizar_inscripcion", {"plan_code": "PREMIUM"}),
        ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM"}),  # negada: fuera de etapa (`datos`)
    ] + [("sub_listar_planes", {})] * 12
    guion = _Guion(pasos)
    resultado = await _turno(conv, guion, monkeypatch, "Si")
    assert not resultado.escalated, resultado.text
    tarea, estado = await _tarea(conv, PROC)
    assert tarea.stage == "datos" and estado == "active"
    async with _scope(tid) as s:
        escaladas = (await s.execute(text("select id from escalations where tenant_id = :t"), {"t": tid})).all()
    assert escaladas == []
    assert "Para seguir necesito estos datos" in resultado.text, resultado.text


async def _inscripciones(tid: uuid.UUID) -> list[Any]:
    async with _scope(tid) as s:
        return list((await s.execute(
            text("select id, estado from pack_subscriptions_subscriptions where tenant_id = :t"), {"t": tid}
        )).all())


async def test_confirmar_nombrando_el_plan_del_resumen_inscribe(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """Medición pass^4 del 2026-10-24: «Sí, confirmo. Todo bien con el mensual.» no confirmaba. Las palabras del
    resumen que el cliente vio acompañan a la marca de confirmación."""
    tid, conv = await _listo()
    await _hasta_confirmar(conv, monkeypatch)
    guion = _Guion([
        ("confirmar_tarea", {}),
        ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "monto_a_pagar": 600000}),
        ("sub_inscribir", {"plan_code": "PREMIUM", "monto_a_pagar": 600000, "customer_data": {}}),
        "Listo, quedaste inscrito.",
    ])
    resultado = await _turno(conv, guion, monkeypatch, "Sí, confirmo. Todo bien con el mensual.")
    assert "Falta la confirmación" not in "".join(guion.vistos)
    assert len(await _inscripciones(tid)) == 1
    assert resultado.confirmacion is None


async def test_nombrar_otro_plan_que_el_del_resumen_no_confirma(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """«Confirmo, todo bien con el anual» ante un resumen del mensual no confirma: el cliente nombra lo que NO se le
    mostró. No se inscribe nada y el kernel sigue pidiendo la confirmación."""
    tid, conv = await _listo()
    await _hasta_confirmar(conv, monkeypatch)
    guion = _Guion([("confirmar_tarea", {}), "Falta tu confirmación."])
    await _turno(conv, guion, monkeypatch, "Sí, confirmo. Todo bien con el anual.")
    assert "Falta la confirmación" in "".join(guion.vistos), guion.vistos[-1][-400:]
    assert await _inscripciones(tid) == []
    assert (await _tarea(conv, PROC))[0].stage == "confirmar"


async def test_en_el_negocio_vendedor_el_resumen_lleva_los_documentos_y_el_confirmo_inscribe(
    packs_registrados, monkeypatch  # noqa: F811
) -> None:
    """HU-SH-07 por el loop real: en el negocio vendedor (documentos legales publicados) el resumen del kernel lleva
    los enlaces y el «Sí, confirmo» del cliente acepta los términos en el mismo turno de la inscripción, sin
    `terminos_pendientes`. (Antes lo cubría `test_journeys_terminos_alta` sobre `crear_pedido` de planes.)"""
    from test_conversacion_ordenada_motor import journeys

    tid, conv = await _listo()
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({tid}))
    await journeys.publicar_documentos_legales(tid, [
        {"tipo": t, "version": "motor", "url": f"https://solvershub.test/legal/{t}"}
        for t in ("terminos", "privacidad", "dpa")
    ])
    resultado = await _hasta_confirmar(conv, monkeypatch)
    assert all(f"https://solvershub.test/legal/{t}" in resultado.text for t in ("terminos", "privacidad", "dpa")), resultado.text
    assert await _inscripciones(tid) == []

    guion = _Guion([
        ("confirmar_tarea", {}),
        ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "monto_a_pagar": 600000}),
        ("sub_inscribir", {"plan_code": "PREMIUM", "monto_a_pagar": 600000, "customer_data": {}}),
        "Listo, quedaste inscrito.",
    ])
    await _turno(conv, guion, monkeypatch, "Sí, confirmo")
    assert "terminos_pendientes" not in "".join(guion.vistos), guion.vistos[-1][-600:]
    assert len(await _inscripciones(tid)) == 1
    from core.provisioning.antiabuso import pedido_de_alta
    from core.shared.tenant import tenant_scope

    async with _scope(tid) as s:
        [(numero,)] = (await s.execute(
            text("select numero_publico from pack_subscriptions_subscriptions where tenant_id = :t"), {"t": tid}
        )).all()
        [(confirmo,)] = (await s.execute(
            text("select id from messages where tenant_id = :t and conversation_id = :c and direction = 'inbound' "
                 "and content = 'Sí, confirmo'"),
            {"t": tid, "c": conv.id},
        )).all()
    async with tenant_scope(tid):
        registro = await pedido_de_alta(tid, numero)
    assert registro is not None
    assert {d["tipo"]: d["version"] for d in registro.aceptacion["documentos"]} == {
        "terminos": "motor", "privacidad": "motor", "dpa": "motor",
    }
    assert registro.aceptacion["mensaje_id"] == str(confirmo)  # el «Sí, confirmo» del cliente


async def test_dos_negocios_no_se_cruzan(packs_registrados, monkeypatch) -> None:  # noqa: F811
    """El «Sí, confirmo» de un cliente de A no confirma nada en B."""
    _a, conv_a = await _listo()
    _b, conv_b = await _listo()
    await _hasta_confirmar(conv_a, monkeypatch)
    await _hasta_datos(conv_b, monkeypatch)
    guion = _Guion([("confirmar_tarea", {}), "Falta tu confirmación."])
    await _turno(conv_b, guion, monkeypatch, "Sí, confirmo")
    tarea_b, _ = await _tarea(conv_b, PROC)
    assert tarea_b.stage == "datos"
    tarea_a, _ = await _tarea(conv_a, PROC)
    assert tarea_a.stage == "confirmar" and tarea_a.pending_confirmation["kind"] == "action"
    assert json.dumps(tarea_b.slots).find(str(conv_a.customer_id)) == -1


async def test_que_etapas_pide_a_confirmar_el_kernel(packs_registrados) -> None:  # noqa: F811
    """El punto de reposo de la acción solo aplica donde TODOS los argumentos salen de datos de la tarea: contratar
    un plan y la promesa de pago de cobranza, no el cambio de plan (lleva `monto_a_pagar` de la cotización) ni la
    agenda."""
    from pathlib import Path

    from agentcore_contracts import Procedure
    from pack_cobranza.skills import CobranzaSkill

    from core.kernel.procedures import motor
    from core.mcp.host import mcp_host

    de_cobranza = {t.name: t for t in CobranzaSkill().get_tools()}  # el pack no está activo aquí

    def propiedades(accion: str) -> list[str]:
        tool = mcp_host.get_tool(accion) or de_cobranza.get(accion)
        return list(((tool.parameters if tool else {}) or {}).get("properties") or {})

    raiz = Path(__file__).resolve().parents[2]
    procs = [Procedure.model_validate_json(f.read_text()) for f in sorted(raiz.glob("packages/pack-*/procedures/*.json"))]
    ligables = {proc.id: [a for st in proc.stages if (a := motor.accion_de_salida_ligable(proc, st, propiedades))]
                for proc in procs}
    assert "pack_commerce.contratar_plan" not in ligables
    assert ligables["pack_subscriptions.contratar_plan"] == ["sub_confirmar_inscripcion"]
    assert ligables["pack_cobranza.promesa_de_pago"] == ["cob_registrar_promesa_saldo"]
    assert ligables["pack_subscriptions.cambiar_plan"] == []
    assert ligables["pack_calendar.agendar_cita"] == []

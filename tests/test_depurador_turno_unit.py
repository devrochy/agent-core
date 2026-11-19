"""HU-AOS-28 · depurador del turno (ADR-0169 §2-§3): redacción pura según permisos."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from core.observability.turno import (
    EventoDeTarea,
    Paso,
    Turno,
    redactar,
    resultado_sin_valores,
    tipo_de_paso,
)

AHORA = datetime(2026, 10, 26, 12, tzinfo=UTC)


def _turno(**extra: object) -> Turno:
    base = dict(
        turn_id=uuid.uuid4(), conversation_id=None, connection_id=None, at=AHORA, latencia_ms=10, release_id=None,
        release_number=None, carril="legacy", mensajes_entrantes=[], resultado="routed", bloqueado_por=None,
        origen_ruta=None, regla=None, candidatos=[], confianza=None, agente_activo=None, agente_final=None, pila=None,
        estado_final="replied", verificador=[], reintentos_verificador=0, pasos=[], estado=[], a2a=[], envio=[], costo=[],
    )
    base.update(extra)
    return Turno(**base)  # type: ignore[arg-type]


def test_tipos_de_paso() -> None:
    assert [tipo_de_paso(a) for a in ("core:comando", "core:motor", "tool:crear_pedido", "core:escalation", "core:otra")] == [
        "command", "engine", "action", "escalation", "other",
    ]


def test_resultado_sin_valores() -> None:
    assert resultado_sin_valores({"ok": False, "errors": 1, "codigos": ["dato_invalido"]}) == (False, ["dato_invalido"])
    assert resultado_sin_valores({"error": {"code": "sin_stock", "message": "Ana no tiene"}}) == (False, ["sin_stock"])
    assert resultado_sin_valores({"error": "texto libre con datos"}) == (False, [])
    assert resultado_sin_valores({"order_id": "PED-1"}) == (True, [])
    assert resultado_sin_valores(None) == (None, [])


def test_sin_observabilidad_no_hay_valores_y_con_ella_van_enmascarados() -> None:
    paso = Paso(AHORA, "accion", "tool:crear_pedido", "crear_pedido", "execute", {"email": "ana@x.co"}, {"ok": True})
    oculto = redactar(_turno(pasos=[paso]), ver_detalle=False, ver_costos=False).pasos[0]
    assert (oculto.entrada, oculto.salida, oculto.detalle_oculto) == (None, None, True)
    visto = redactar(_turno(pasos=[paso]), ver_detalle=True, ver_costos=False).pasos[0]
    assert visto.entrada == {"email": "a***@x.co"} and visto.detalle_oculto is False


def test_pago_siempre_oculto_en_los_eventos_de_tarea() -> None:
    evento = EventoDeTarea(AHORA, uuid.uuid4(), "pedido", 3, "slot_set", "llm", {"name": "tarjeta", "value": "4111111111111111", "data_class": "payment"})
    pii = EventoDeTarea(AHORA, uuid.uuid4(), "pedido", 4, "slot_set", "llm", {"name": "nombre", "value": "Ana Pérez", "data_class": "pii"})
    t = redactar(_turno(estado=[evento, pii]), ver_detalle=True, ver_costos=True)
    assert t.estado[0].data["value"] == "•••1111"
    assert t.estado[1].data["value"] == "Ana Pérez"  # quien ve la conversación ya ve sus datos


def test_codigos_con_texto_libre_se_descartan() -> None:
    assert resultado_sin_valores({"error": {"code": "ana@x.co 3001234567"}}) == (False, [])
    assert resultado_sin_valores({"ok": False, "codigos": ["sin_stock", "Ana Pérez no paga"]}) == (False, ["sin_stock"])


def test_clase_desconocida_se_oculta_y_los_candidatos_solo_se_cuentan() -> None:
    sin_clase = EventoDeTarea(AHORA, uuid.uuid4(), "pedido", 5, "slot_set", "kernel", {"name": "x", "value": "secreto"})
    accion = EventoDeTarea(AHORA, uuid.uuid4(), "pedido", 6, "action_succeeded", "tool", {"action": "buscar", "candidates": ["a", "b"]})
    t = redactar(_turno(estado=[sin_clase, accion]), ver_detalle=True, ver_costos=True)
    assert t.estado[0].data["value"] == "•••" and t.estado[1].data["candidates"] == 2

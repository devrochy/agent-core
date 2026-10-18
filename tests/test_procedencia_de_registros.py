"""HU-AOS-47 · procedencia de registros: qué cuenta como «creado» (ADR-0140). Sin BD."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.agents import procedencia
from core.agents.procedencia import referencia_creada
from core.mcp.types import RegistroCreado


def _tools() -> dict[str, Any]:
    from pack_calendar.skills import CalendarSkill
    from pack_cobranza.skills import CobranzaSkill
    from pack_commerce.skills import LeadSkill, OrderSkill

    return {t.name: t for s in (LeadSkill(), OrderSkill(), CalendarSkill(), CobranzaSkill()) for t in s.get_tools()}


@pytest.mark.parametrize(
    ("tool", "resultado", "esperado"),
    [
        ("crear_lead", {"lead_id": "L1", "status": "created"}, ("L1", None)),
        ("crear_lead", {"lead_id": "L1", "status": "ya_existe"}, None),  # ya era lead (landing)
        ("crear_lead", {"lead_id": "L1", "status": "duplicate"}, None),  # reintento
        ("crear_pedido", {"order_id": "O1", "status": "confirmed", "public_order_number": "P-7"}, ("O1", "P-7")),
        ("crear_pedido", {"order_id": "O1", "status": "duplicate", "public_order_number": "P-7"}, None),
        ("crear_pedido", {"status": "faltan_datos_cliente"}, None),
        ("create_calendar_event", {"event_id": "E1", "status": "created"}, ("E1", None)),
        ("create_calendar_event", {"error": "ocupado"}, None),
        ("cob_registrar_promesa_pago", {"registrada": True, "promesa_id": "P1"}, ("P1", None)),
        ("cob_registrar_promesa_pago", {"registrada": True, "ya_existe": True, "promesa_id": "P1"}, None),
        ("cob_registrar_promesa_saldo", {"registrada": True, "promesa_id": "P2"}, ("P2", None)),
    ],
)
def test_cada_pack_decide_que_es_nuevo(tool: str, resultado: dict[str, Any], esperado: Any) -> None:
    declaracion = _tools()[tool].crea_registro
    assert declaracion is not None
    assert referencia_creada(declaracion, resultado) == esperado


def test_solo_las_tools_que_crean_declaran_procedencia() -> None:
    declaradas = {n: t.crea_registro.kind for n, t in _tools().items() if t.crea_registro is not None}
    assert declaradas == {
        "crear_lead": "lead",
        "crear_pedido": "pedido",
        "create_calendar_event": "cita",
        "cob_registrar_promesa_pago": "promesa",
        "cob_registrar_promesa_saldo": "promesa",
    }


def test_regla_generica_y_limites() -> None:
    d = RegistroCreado(kind="pedido", id_key="id", es_nuevo=lambda r: True, label_key="num")
    assert referencia_creada(d, {"id": "x", "ok": False}) is None
    assert referencia_creada(d, {"id": True}) is None
    assert referencia_creada(d, {"id": "x" * 201}) is None
    assert referencia_creada(d, "no es dict") is None
    assert referencia_creada(d, {"id": uuid.UUID(int=1), "num": "N" * 300}) == (str(uuid.UUID(int=1)), "N" * 120)
    # El nombre del cliente no puede colarse: solo la clave pública declarada.
    assert referencia_creada(d, {"id": "x", "num": {"nombre": "Ana"}}) == ("x", None)

    def _falla(_r: dict[str, Any]) -> bool:
        raise RuntimeError

    assert referencia_creada(RegistroCreado(kind="k", id_key="id", es_nuevo=_falla), {"id": "x"}) is None


async def test_sin_release_no_escribe(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[Any] = []
    monkeypatch.setattr(procedencia, "get_session", lambda: llamadas.append(1))
    tool = _tools()["crear_lead"]
    await procedencia.registrar(tool, {"lead_id": "L1", "status": "created"}, uuid.uuid4())
    await procedencia.registrar(None, {"lead_id": "L1", "status": "created"}, uuid.uuid4())
    assert llamadas == []

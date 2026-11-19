"""HU-AOS-28 · `taken` y `released` (ADR-0169 §5): anotaciones de la toma humana en la tarea."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core.kernel.state import Evento, TransicionInvalida, aplicar

T0 = datetime(2026, 10, 26, 12, tzinfo=UTC)
V = "1.3.0"


def _inicio():
    return aplicar(None, Evento(1, "started", {"procedure_id": "pack_commerce.contratar_plan", "procedure_version": V,
                                               "stage": "elegir_plan", "schema_version": V}, "kernel", T0))


def _toma(seq: int, tipo: str = "taken", actor: str = "human", **data: object) -> Evento:
    return Evento(seq, tipo, {"schema_version": V, **data}, actor, T0 + timedelta(minutes=seq))


def test_la_toma_no_cambia_la_tarea_salvo_la_secuencia() -> None:
    p = _inicio()
    tomada = aplicar(p, _toma(2, admin_id=str(uuid.uuid4()), escalation_id=str(uuid.uuid4())))
    assert (tomada.status, tomada.stage, tomada.slots, tomada.last_seq) == (p.status, p.stage, p.slots, 2)
    assert aplicar(tomada, _toma(3, "released", admin_id=str(uuid.uuid4()))).status == "active"


def test_solo_una_persona_con_su_id_y_nunca_sobre_una_tarea_cerrada() -> None:
    p = _inicio()
    for evento in (_toma(2, actor="kernel", admin_id=str(uuid.uuid4())), _toma(2), _toma(2, admin_id="no-es-uuid")):
        with pytest.raises(TransicionInvalida):
            aplicar(p, evento)
    cerrada = aplicar(p, Evento(2, "escalated", {"schema_version": V, "reason": "action_error"}, "kernel", T0))
    with pytest.raises(TransicionInvalida):
        aplicar(cerrada, _toma(3, admin_id=str(uuid.uuid4())))

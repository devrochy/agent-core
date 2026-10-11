"""Reductor de tareas event-sourced (HU-AOS-04, ADR-0114 §1): puro, sin base."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.kernel.state import (
    EPISODE_EXPIRED,
    Evento,
    Proyeccion,
    TransicionInvalida,
    aplicar,
    reducir,
    valores,
)

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class _Historia:
    """Arma eventos con seq y schema_version correlativos, como `registrar`."""

    def __init__(self, version: str = "1.0.0") -> None:
        self.version = version
        self.eventos: list[Evento] = []

    def __call__(self, tipo: str, actor: str = "kernel", **data: Any) -> Evento:
        if tipo == "revalidated":
            data.setdefault("schema_version", data["to_version"])
            self.version = data["to_version"]
        else:
            data.setdefault("schema_version", self.version)
        evento = Evento(
            seq=len(self.eventos) + 1,
            type=tipo,
            data=data,
            actor=actor,
            created_at=T0 + timedelta(minutes=len(self.eventos)),
        )
        self.eventos.append(evento)
        return evento


def _inicio(h: _Historia, **extra: Any) -> Evento:
    return h(
        "started",
        procedure_id="pack_commerce.contratar_plan",
        procedure_version=h.version,
        stage="datos",
        **extra,
    )


def _slot(h: _Historia, nombre: str, valor: Any, **extra: Any) -> Evento:
    extra.setdefault("source", "llm")
    extra.setdefault("data_class", "public")
    actor = "tool" if str(extra["source"]).startswith("action:") else "llm"
    return h("slot_set", actor=actor, slot=nombre, value=valor, **extra)


def _pliega(eventos: list[Evento]) -> Proyeccion:
    p = None
    for e in eventos:
        p = aplicar(p, e)
    assert p is not None
    return p


def test_started_crea_la_tarea_activa_en_foco() -> None:
    h = _Historia()
    p = aplicar(None, _inicio(h))
    assert (p.status, p.stage, p.stack_position, p.last_seq) == ("active", "datos", 0, 1)
    assert p.procedure_version == "1.0.0"
    assert p.created_at == p.updated_at == T0


def test_started_en_pausa_queda_suspended() -> None:
    p = aplicar(None, _inicio(_Historia(), stack_position=1))
    assert (p.status, p.stack_position) == ("suspended", 1)


def test_datos_con_origen_y_confirmacion() -> None:
    h = _Historia()
    _inicio(h)
    _slot(h, "plan", "premium", source="action:informar_plan")
    _slot(h, "correo", "a@b.co", data_class="pii")
    h("confirmation_requested", target={"kind": "slot", "name": "correo"})
    p = reducir(h.eventos)
    assert p.pending_confirmation == {"kind": "slot", "name": "correo", "seq": 4}
    h("confirmed", target={"kind": "slot", "name": "correo"})
    p = reducir(h.eventos)
    assert p.pending_confirmation is None
    assert p.slots["correo"]["confirmed"] is True
    assert p.slots["plan"]["source"] == "action:informar_plan"
    assert valores(p.slots) == {"plan": "premium", "correo": "a@b.co"}


def test_confirmar_sin_pedido_es_invalido() -> None:
    h = _Historia()
    _inicio(h)
    _slot(h, "correo", "a@b.co")
    h("confirmed", target={"kind": "slot", "name": "correo"})
    with pytest.raises(TransicionInvalida, match="confirmación pendiente"):
        reducir(h.eventos)


@pytest.mark.parametrize(
    "limpiador",
    [
        lambda h: h("stage_entered", from_stage="datos", to_stage="pago"),
        lambda h: h("slot_cleared", slot="correo"),
        lambda h: _slot(h, "correo", "otro@b.co"),
        lambda h: h("cancelled", actor="human", reason="pedido_del_cliente"),
    ],
)
def test_la_confirmacion_pendiente_se_limpia(limpiador: Any) -> None:
    h = _Historia()
    _inicio(h)
    _slot(h, "correo", "a@b.co")
    h("confirmation_requested", target={"kind": "slot", "name": "correo"})
    limpiador(h)
    assert reducir(h.eventos).pending_confirmation is None


def test_pila_pausar_y_retomar() -> None:
    h = _Historia()
    _inicio(h)
    h("suspended", stack_position=1)
    assert (reducir(h.eventos).status, reducir(h.eventos).stack_position) == ("suspended", 1)
    h("resumed")
    assert (reducir(h.eventos).status, reducir(h.eventos).stack_position) == ("active", 0)


def test_episode_expired_cancela_con_motivo() -> None:
    h = _Historia()
    _inicio(h)
    _slot(h, "plan", "premium")
    h(EPISODE_EXPIRED, reason=EPISODE_EXPIRED, slots=["plan"])
    p = reducir(h.eventos)
    assert (p.status, p.status_reason) == ("cancelled", EPISODE_EXPIRED)
    assert p.completed_at == h.eventos[-1].created_at
    assert p.slots["plan"]["value"] == "premium"  # la tarea conserva sus datos (registro del negocio)


def test_nada_se_aplica_sobre_una_tarea_cerrada() -> None:
    h = _Historia()
    _inicio(h)
    h("completed")
    _slot(h, "plan", "premium")
    with pytest.raises(TransicionInvalida, match="completed"):
        reducir(h.eventos)


def test_reconstruccion_entre_versiones_del_procedimiento() -> None:
    """Un `revalidated` cambia la versión; los eventos siguientes llevan la nueva."""
    h = _Historia("1.0.0")
    _inicio(h)
    _slot(h, "plan", "premium")
    _slot(h, "codigo_viejo", "X1")
    h("revalidated", from_version="1.0.0", to_version="1.1.0", to_stage="confirmar", dropped_slots=["codigo_viejo"])
    _slot(h, "correo", "a@b.co", data_class="pii")
    h("action_succeeded", actor="tool", action="crear_pedido")
    p = reducir(h.eventos)
    assert p.procedure_version == "1.1.0"
    assert p.stage == "confirmar"
    assert set(p.slots) == {"plan", "correo"}
    assert p.last_seq == 6
    # Mismo resultado aplicando uno a uno (lo que hace `registrar`) que reconstruyendo.
    assert _pliega(h.eventos) == p


def test_schema_version_de_otra_version_se_rechaza() -> None:
    h = _Historia("1.0.0")
    _inicio(h)
    h("slot_set", actor="llm", slot="plan", value="premium", source="llm", data_class="public",
      schema_version="2.0.0")
    with pytest.raises(TransicionInvalida, match="schema_version"):
        reducir(h.eventos)


@pytest.mark.parametrize(
    ("data", "motivo"),
    [
        ({"slot": "plan", "value": "x", "source": "llm"}, "data_class"),
        ({"slot": "Plan!", "value": "x", "source": "llm", "data_class": "public"}, "slot"),
        ({"slot": "plan", "value": None, "source": "llm", "data_class": "public"}, "sin valor"),
        ({"slot": "plan", "value": "x", "source": "cualquiera", "data_class": "public"}, "source"),
        ({"slot": "plan", "value": "x" * 5000, "source": "llm", "data_class": "public"}, "grande"),
    ],
)
def test_slot_set_invalido(data: dict[str, Any], motivo: str) -> None:
    h = _Historia()
    _inicio(h)
    h("slot_set", actor="llm", **data)
    with pytest.raises(TransicionInvalida, match=motivo):
        reducir(h.eventos)


def test_seq_fuera_de_orden_y_tipo_desconocido() -> None:
    h = _Historia()
    inicio = _inicio(h)
    p = aplicar(None, inicio)
    salto = Evento(seq=3, type="completed", data={"schema_version": "1.0.0"}, actor="kernel", created_at=T0)
    with pytest.raises(TransicionInvalida, match="seq 2"):
        aplicar(p, salto)
    raro = Evento(seq=2, type="borrado", data={"schema_version": "1.0.0"}, actor="kernel", created_at=T0)
    with pytest.raises(TransicionInvalida, match="desconocido"):
        aplicar(p, raro)


def test_owner_changed_y_cancelled_exige_motivo() -> None:
    h = _Historia()
    _inicio(h)
    duenio = uuid.uuid4()
    h("owner_changed", to_instance_id=str(duenio))
    assert reducir(h.eventos).owner_instance_id == duenio
    h("cancelled", actor="human")
    with pytest.raises(TransicionInvalida, match="reason"):
        reducir(h.eventos)


@pytest.mark.parametrize(
    ("tipo", "actor", "data"),
    [
        ("episode_expired", "llm", {"reason": "episode_expired"}),
        ("revalidated", "llm", {"from_version": "1.0.0", "to_version": "2.0.0", "to_stage": "x"}),
        ("owner_changed", "llm", {"to_instance_id": None}),
        ("confirmed", "llm", {"target": {"kind": "slot", "name": "plan"}}),
        ("stage_entered", "tool", {"from_stage": "datos", "to_stage": "pago"}),
    ],
)
def test_matriz_de_actores(tipo: str, actor: str, data: dict[str, Any]) -> None:
    h = _Historia()
    _inicio(h)
    h(tipo, actor=actor, **data)
    with pytest.raises(TransicionInvalida, match="no puede emitir"):
        reducir(h.eventos)


@pytest.mark.parametrize(
    ("actor", "extra", "motivo"),
    [
        ("llm", {"source": "human"}, "origen"),
        ("llm", {"source": "action:crear_pedido"}, "origen"),
        ("tool", {"source": "llm"}, "origen"),
        ("llm", {"confirmed": True}, "confirmado"),
        ("kernel", {"confirmed": "false"}, "confirmado"),
    ],
)
def test_el_llm_no_falsifica_origen_ni_confirmacion(actor: str, extra: dict[str, Any], motivo: str) -> None:
    h = _Historia()
    _inicio(h)
    data = {"slot": "plan", "value": "x", "source": "llm", "data_class": "public", **extra}
    h("slot_set", actor=actor, **data)
    with pytest.raises(TransicionInvalida, match=motivo):
        reducir(h.eventos)


def test_data_demasiado_grande_y_pila_acotada() -> None:
    h = _Historia()
    _inicio(h)
    h("cancelled", actor="human", reason="x", nota="y" * 9000)
    with pytest.raises(TransicionInvalida, match="data demasiado grande"):
        reducir(h.eventos)
    h2 = _Historia()
    _inicio(h2)
    h2("suspended", stack_position=40000)
    with pytest.raises(TransicionInvalida, match="stack_position"):
        reducir(h2.eventos)

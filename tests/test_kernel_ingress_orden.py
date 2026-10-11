"""Orden de las políticas de entrada del kernel sin base de datos (HU-AOS-10, parte 2).

Las políticas se mudaron del consumer a `core.kernel.ingress` sin cambiar el orden: conexión
pausada → kill-switch o conversación escalada → palabras de escalamiento → horario. Aquí se
prueba solo el orden y qué efecto corre en cada rama; el camino completo, con `core_decisions`
y `turn_outcomes`, está en `tests/integration/test_kernel_ingress.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from core.kernel import ingress

CERRADO = {"tz": "UTC", "ranges": [{"day": d, "from": "00:00", "to": "00:00"} for d in range(7)]}


@pytest.fixture
def efectos(monkeypatch) -> list[tuple[str, Any]]:
    """Registra bloqueos, escalamientos, envíos y avisos en vez de ejecutarlos."""
    vistos: list[tuple[str, Any]] = []

    async def _bloquear(_conv, motivo, *, status=None):
        vistos.append(("bloquear", (motivo, status)))

    async def _escalar(_cid, reason, summary=""):
        vistos.append(("escalar", reason))

    async def _enviar(_cid, texto, **_k):
        vistos.append(("enviar", texto))
        return True

    async def _sin_cuenta(_conv):
        return None

    async def _spec():
        return SimpleNamespace()

    async def _respuesta(_spec, _clave):
        return "Te paso con una persona."

    monkeypatch.setattr(ingress, "bloquear", _bloquear)
    monkeypatch.setattr(ingress, "load_response", _respuesta)
    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escalar)
    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _enviar)
    monkeypatch.setattr("core.gateway.meta_connection_state.cuenta_meta_de_conversacion", _sin_cuenta)
    monkeypatch.setattr("core.agents.spec.load_active_spec", _spec)
    return vistos


def _conv(**kw: Any) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "status": "open", "bot_paused": False}
    return SimpleNamespace(**{**base, **kw})


def _ajustes(**kw: Any) -> SimpleNamespace:
    base = {
        "bot_paused": False,
        "escalation_config": {},
        "assistant_name": "Lupe",
        "business_description": "panadería",
    }
    return SimpleNamespace(**{**base, **kw})


def _cliente(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**{"bot_paused": False, **kw})


async def test_sin_politica_que_aplique_sigue_al_despacho(efectos) -> None:
    assert await ingress.politicas_de_entrada(_conv(), _cliente(), _ajustes(), "hola") is False
    assert await ingress.politicas_de_entrada(_conv(), _cliente(), None, "hola") is False
    assert efectos == []


@pytest.mark.parametrize(
    ("conv_kw", "cliente_kw", "ajustes_kw", "motivo"),
    [
        ({}, {}, {"bot_paused": True}, "kill_switch"),
        ({}, {"bot_paused": True}, {}, "kill_switch"),
        ({"bot_paused": True}, {}, {}, "kill_switch"),
        ({"status": "escalated", "bot_paused": True}, {}, {}, "escalated_conversation"),
    ],
)
async def test_kill_switch_en_tres_niveles_y_escalada(
    efectos, conv_kw, cliente_kw, ajustes_kw, motivo
) -> None:
    config = {"keywords": ["humano"], "business_hours": CERRADO, "out_of_hours_message": "Cerrado"}
    ajustes = _ajustes(escalation_config=config, **ajustes_kw)
    conv, cliente = _conv(**conv_kw), _cliente(**cliente_kw)
    assert await ingress.politicas_de_entrada(conv, cliente, ajustes, "quiero un humano") is True
    assert efectos == [("bloquear", (motivo, "silent"))], "va antes que palabras y horario"


async def test_la_palabra_va_antes_que_el_horario(efectos) -> None:
    ajustes = _ajustes(escalation_config={"keywords": ["humano"], "business_hours": CERRADO})
    assert await ingress.politicas_de_entrada(_conv(), _cliente(), ajustes, "Un HUMANO") is True
    assert efectos == [
        ("bloquear", ("escalation_keyword", "escalated")),
        ("escalar", "keyword"),
        ("enviar", "Te paso con una persona."),
    ]


async def test_fuera_de_horario_reemplaza_los_marcadores(efectos) -> None:
    ajustes = _ajustes(
        escalation_config={
            "business_hours": CERRADO,
            "out_of_hours_message": " {assistant_name} de {business_description}: cerrado ",
        }
    )
    assert await ingress.politicas_de_entrada(_conv(), _cliente(), ajustes, "hola") is True
    assert efectos == [
        ("bloquear", ("out_of_hours", "replied")),
        ("enviar", "Lupe de panadería: cerrado"),
    ]


async def test_la_conexion_pausada_va_primero_y_avisa_solo_si_se_pide(monkeypatch, efectos) -> None:
    cuenta = uuid.uuid4()
    avisos: list[str] = []

    async def _cuenta(_conv):
        return cuenta

    async def _motivo(_cuenta):
        return "Pausada"

    async def _avisar(_tid, _cuenta, motivo):
        avisos.append(motivo)

    monkeypatch.setattr("core.gateway.meta_connection_state.cuenta_meta_de_conversacion", _cuenta)
    monkeypatch.setattr("core.gateway.meta_connection_state.motivo_pausa_de", _motivo)
    monkeypatch.setattr("core.gateway.meta_connection_state.avisar_mensaje_en_conexion_pausada", _avisar)

    conv = _conv(bot_paused=True)
    assert await ingress.politicas_de_entrada(conv, _cliente(), _ajustes(), "hola") is True
    assert efectos == [("bloquear", ("connection_paused", "silent"))] and avisos == ["Pausada"]

    assert await ingress.conexion_pausada(conv, avisar_duenos=False) is True
    assert avisos == ["Pausada"], "al disparar el debounce no se avisa otra vez"

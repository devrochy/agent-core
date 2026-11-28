"""Bloque del turno detrás del historial (adenda 2026-11-27 de ADR-0117), sin base de datos.

- OpenAI-compatible (DeepSeek): los bloques fijos al principio y el del turno como `system` justo
  antes del mensaje del cliente; el historial anterior queda en el prefijo cacheado.
- Anthropic: el bloque del turno sigue en `system` (la API no acepta `system` entre los mensajes y
  como texto del usuario quedaría pegado al del cliente).
- Sin `BloqueDelTurno`, la serialización es la de siempre (simulador, golden, IA estructurada).
- El ancla entra en la clave de las grabaciones solo cuando hay bloque del turno.
"""

from __future__ import annotations

from core.llm import cassette
from core.llm.anthropic_provider import AnthropicProvider
from core.llm.openai_compatible import OpenAICompatibleProvider
from core.llm.types import BloqueDelTurno, LLMMessage, LLMToolCall, separar_bloque_del_turno


def _deepseek() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(api_key="x", model="deepseek-flash", base_url="https://api.deepseek.com",
                                    provider_name="deepseek")


def _historial() -> list[LLMMessage]:
    return [
        LLMMessage(role="user", content="hola"),
        LLMMessage(role="assistant", content="¡Hola! ¿En qué te ayudo?"),
        LLMMessage(role="user", content="quiero una cita"),
    ]


def test_bloque_del_turno_sigue_siendo_un_str() -> None:
    bloque = BloqueDelTurno("## Fecha y hora actual\nHoy es…", 2)
    assert isinstance(bloque, str) and bloque.startswith("## Fecha")
    assert bloque.ancla == 2
    assert separar_bloque_del_turno(["fijo", bloque]) == (["fijo"], bloque)
    assert separar_bloque_del_turno(["fijo", "dato"]) == (["fijo", "dato"], None)


# ---------------------------------------------------------------- OpenAI-compatible
def test_deepseek_pone_el_bloque_del_turno_antes_del_mensaje_del_cliente() -> None:
    mensajes = _historial()
    out = _deepseek()._serialize_messages(["fijo 1", "fijo 2", BloqueDelTurno("dinámico", 2)], mensajes)
    assert [(m["role"], m["content"]) for m in out] == [
        ("system", "fijo 1\n\nfijo 2"),
        ("user", "hola"),
        ("assistant", "¡Hola! ¿En qué te ayudo?"),
        ("system", "dinámico"),
        ("user", "quiero una cita"),
    ]


def test_deepseek_el_ancla_no_se_mueve_con_las_vueltas_de_tools() -> None:
    mensajes = [
        *_historial(),
        LLMMessage(role="assistant", tool_calls=[LLMToolCall(id="c1", name="ver_agenda", arguments={})]),
        LLMMessage(role="tool", tool_call_id="c1", content="{}"),
    ]
    primera = _deepseek()._serialize_messages(["fijo", BloqueDelTurno("dinámico", 2)], mensajes[:3])
    segunda = _deepseek()._serialize_messages(["fijo", BloqueDelTurno("dinámico", 2)], mensajes)
    # La vuelta con tools extiende el request anterior: todo él es prefijo cacheable.
    assert segunda[: len(primera)] == primera
    assert [m["role"] for m in segunda[len(primera):]] == ["assistant", "tool"]


def test_deepseek_entre_turnos_comparte_el_historial_aunque_cambie_la_hora() -> None:
    turno_1 = _deepseek()._serialize_messages(["fijo", BloqueDelTurno("Hoy es lunes — 10:01", 2)], _historial())
    siguiente = [*_historial(), LLMMessage(role="assistant", content="¿Qué día?"), LLMMessage(role="user", content="el martes")]
    turno_2 = _deepseek()._serialize_messages(["fijo", BloqueDelTurno("Hoy es lunes — 10:02", 4)], siguiente)
    # Antes el bloque cambiante iba delante del historial y el prefijo común era solo lo fijo.
    assert turno_2[:3] == turno_1[:3]


def test_deepseek_ancla_al_final_y_fuera_de_rango() -> None:
    mensajes = _historial()
    al_final = _deepseek()._serialize_messages(["fijo", BloqueDelTurno("dinámico", 3)], mensajes)
    assert al_final[-1] == {"role": "system", "content": "dinámico"}
    fuera = _deepseek()._serialize_messages(["fijo", BloqueDelTurno("dinámico", 99)], mensajes)
    assert fuera == al_final


def test_deepseek_sin_bloque_del_turno_serializa_como_siempre() -> None:
    out = _deepseek()._serialize_messages(["instrucciones", "datos"], _historial())
    assert out[0] == {"role": "system", "content": "instrucciones\n\ndatos"}
    assert [m["role"] for m in out[1:]] == ["user", "assistant", "user"]


# ---------------------------------------------------------------- Anthropic
def test_anthropic_deja_el_bloque_del_turno_en_system() -> None:
    bloques = ["uno", "dos", BloqueDelTurno("dinámico", 2)]
    sistema = AnthropicProvider._serialize_system(bloques)
    assert [b["text"] for b in sistema] == ["uno", "dos", "dinámico"]
    assert [b["text"] for b in sistema if "cache_control" in b] == ["dos"]
    out = AnthropicProvider._serialize_messages(_historial())
    assert out == [{"role": m.role, "content": m.content} for m in _historial()]


# ---------------------------------------------------------------- grabaciones
def _pedido(sistema: list[str]) -> cassette.Pedido:
    return cassette.normalizar(proveedor="deepseek", modelo="deepseek-flash", thinking=False, system_blocks=sistema,
                               messages=_historial(), tools=None, max_tokens=100, temperature=None)


def test_la_clave_incluye_el_ancla_solo_con_bloque_del_turno() -> None:
    sin = _pedido(["fijo", "dinámico"])
    assert "ancla" not in sin.completo()
    con = _pedido(["fijo", BloqueDelTurno("dinámico", 2)])
    assert con.completo()["ancla"] == 2
    assert con.sistema == sin.sistema
    assert con.clave != sin.clave
    assert _pedido(["fijo", BloqueDelTurno("dinámico", 1)]).clave != con.clave

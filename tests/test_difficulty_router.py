"""`route_model` (`core/llm/difficulty_router.py`): no tenía NINGÚN test
hasta ahora. Cubre el comportamiento existente (rutas default/reasoner,
tools fuerza default, cada señal por separado) y los 3 umbrales nuevos
parametrizables por tenant (ROADMAP, ítem 1 del mapa de parametrización).
"""

from __future__ import annotations

from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
    route_model,
)

DEFAULT_MODEL = "deepseek-chat"
REASONING_MODEL = "deepseek-reasoner"


def _route(**overrides):
    kwargs = {
        "message": "hola",
        "turnos_sin_resolver": 0,
        "default_model": DEFAULT_MODEL,
        "reasoning_model": REASONING_MODEL,
        "routing_enabled": True,
        "requires_tools": False,
    }
    kwargs.update(overrides)
    return route_model(**kwargs)


# --------------------------------------------------------------------
# Comportamiento existente (sin tocar, primera vez que se testea)
# --------------------------------------------------------------------


def test_routing_deshabilitado_siempre_usa_default():
    decision = _route(routing_enabled=False, message="por qué compara estrategia paso a paso" * 50)
    assert decision.model == DEFAULT_MODEL
    assert decision.allow_tools is True


def test_requires_tools_fuerza_default_aunque_sea_dificil():
    decision = _route(
        requires_tools=True, message="explica en detalle pros y contras" * 50, turnos_sin_resolver=5
    )
    assert decision.model == DEFAULT_MODEL
    assert decision.allow_tools is True


def test_mensaje_simple_usa_default():
    decision = _route(message="hola, quiero un pollo asado")
    assert decision.model == DEFAULT_MODEL
    assert decision.reason == "default"


def test_senal_compleja_sola_alcanza_el_umbral_default():
    """Una sola señal de "compara"/"por qué" da 2 puntos -- con el umbral
    default (2) SÍ alcanza; queda como caso de referencia del puntaje
    exacto que usa el resto de tests de umbrales."""
    decision = _route(message="¿por qué esto cuesta tanto?")
    assert decision.model == REASONING_MODEL
    assert decision.reason == "complex_signals"
    assert decision.allow_tools is False


def test_mensaje_largo_solo_no_alcanza_el_umbral_default():
    """Mensaje largo por sí solo da 1 punto -- no llega a 2, sigue en default."""
    decision = _route(message="a" * (LONG_MESSAGE_THRESHOLD + 1))
    assert decision.model == DEFAULT_MODEL


def test_turnos_sin_resolver_alcanza_el_umbral_default():
    decision = _route(turnos_sin_resolver=TURNOS_SIN_RESOLVER_THRESHOLD)
    assert decision.model == REASONING_MODEL


def test_turnos_sin_resolver_por_debajo_del_umbral_no_escala():
    decision = _route(turnos_sin_resolver=TURNOS_SIN_RESOLVER_THRESHOLD - 1)
    assert decision.model == DEFAULT_MODEL


# --------------------------------------------------------------------
# Umbrales parametrizables por tenant (nuevo)
# --------------------------------------------------------------------


def test_long_message_threshold_mas_bajo_hace_que_un_mensaje_normal_escale():
    message = "a" * 100
    # con el default (400), 100 caracteres no suma el punto de "largo"
    assert _route(message=message).model == DEFAULT_MODEL
    # con un umbral custom más bajo, el mismo mensaje ya cuenta como largo
    # -- pero un solo punto tampoco alcanza el umbral de score por sí solo,
    # así que se combina con turnos_sin_resolver para llegar a 2.
    decision = _route(
        message=message, long_message_threshold=50, turnos_sin_resolver=TURNOS_SIN_RESOLVER_THRESHOLD
    )
    assert decision.model == REASONING_MODEL


def test_turnos_sin_resolver_threshold_custom_mas_bajo():
    decision = _route(turnos_sin_resolver=1, turnos_sin_resolver_threshold=1)
    assert decision.model == REASONING_MODEL


def test_turnos_sin_resolver_threshold_custom_mas_alto_no_escala():
    """Con el default (2), 2 turnos sin resolver alcanza -- con un umbral
    custom más alto, el mismo conteo YA NO alcanza."""
    assert _route(turnos_sin_resolver=2).model == REASONING_MODEL
    decision = _route(turnos_sin_resolver=2, turnos_sin_resolver_threshold=5)
    assert decision.model == DEFAULT_MODEL


def test_score_threshold_mas_alto_exige_mas_senales():
    """Con el default (2), una sola señal compleja (2 puntos) alcanza --
    con un score_threshold más alto, esa misma señal sola ya no alcanza."""
    assert _route(message="¿por qué?").model == REASONING_MODEL
    decision = _route(message="¿por qué?", score_threshold=5)
    assert decision.model == DEFAULT_MODEL


def test_score_threshold_mas_bajo_escala_mas_facil():
    """Con score_threshold=1, el solo punto de "mensaje largo" (sin
    ninguna otra señal) ya alcanza para escalar."""
    message = "a" * (LONG_MESSAGE_THRESHOLD + 1)
    assert _route(message=message).model == DEFAULT_MODEL  # con el default, 1 punto no alcanza
    decision = _route(message=message, score_threshold=1)
    assert decision.model == REASONING_MODEL


def test_defaults_de_los_parametros_coinciden_con_las_constantes():
    """Sin pasar los 3 parámetros nuevos, el comportamiento es idéntico al
    de antes de parametrizar -- regresión explícita."""
    decision_explicito = _route(
        turnos_sin_resolver=TURNOS_SIN_RESOLVER_THRESHOLD,
        long_message_threshold=LONG_MESSAGE_THRESHOLD,
        turnos_sin_resolver_threshold=TURNOS_SIN_RESOLVER_THRESHOLD,
        score_threshold=DIFFICULTY_SCORE_THRESHOLD,
    )
    decision_implicito = _route(turnos_sin_resolver=TURNOS_SIN_RESOLVER_THRESHOLD)
    assert decision_explicito == decision_implicito

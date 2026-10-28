"""HU-AOS-35 · caché por versión y prefijo estable (adenda de ADR-0117), sin base de datos.

- Anthropic: una sola marca de caché, en el último bloque fijo.
- La lectura de lo cacheado de OpenAI y de DeepSeek, y el total de entrada del ledger con Anthropic.
- La grabación conserva el indicador `input_includes_cache` sin cambiar las grabaciones de siempre.
- El aviso de lo no disponible y su playbook del kernel.
- `bloques_fijos` + `bloque_dinamico` es exactamente `build_system_blocks`.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from typing import Any

import pytest

from core.agents.compiler import _textos_no_disponible, aviso_no_disponible
from core.agents.flow import FlowSection, ResolvedFlow
from core.llm.anthropic_provider import AnthropicProvider
from core.llm.openai_compatible import _tokens_cacheados
from core.llm.types import LLMResponse, LLMUsageInfo


# ---------------------------------------------------------------- Anthropic
def test_anthropic_marca_solo_el_ultimo_bloque_fijo() -> None:
    bloques = AnthropicProvider._serialize_system(["uno", "dos", "", "tres", "cuatro", "dinámico"])
    assert [b["text"] for b in bloques] == ["uno", "dos", "tres", "cuatro", "dinámico"]
    marcados = [b["text"] for b in bloques if "cache_control" in b]
    assert marcados == ["cuatro"]


def test_anthropic_con_un_solo_bloque_no_marca() -> None:
    assert AnthropicProvider._serialize_system(["solo"]) == [{"type": "text", "text": "solo"}]
    assert AnthropicProvider._serialize_system([]) == []


# ---------------------------------------------------------------- lectura de lo cacheado
@pytest.mark.parametrize(
    ("usage", "esperado"),
    [
        ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 60}, 60),  # DeepSeek
        ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}}, 40),  # OpenAI
        ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 0, "prompt_tokens_details": {"cached_tokens": 9}}, 0),
        ({"prompt_tokens": 100}, 0),
        ({"prompt_tokens": 100, "prompt_tokens_details": None}, 0),
    ],
)
def test_tokens_cacheados_de_openai_y_deepseek(usage: dict[str, Any], esperado: int) -> None:
    assert _tokens_cacheados(usage) == esperado


def _respuesta(**uso: Any) -> LLMResponse:
    return LLMResponse(content="ok", stop_reason="end_turn", usage=LLMUsageInfo(**uso), model="m")


def test_el_ledger_lleva_el_total_de_entrada_y_separa_lectura_de_escritura() -> None:
    from core.orchestrator.loop import _TurnUsage

    proveedor = types.SimpleNamespace(estimate_cost_usd=lambda i, o: 0.0, pricing_per_mtok=(0.0, 0.0))
    uso = _TurnUsage()
    # Anthropic: `input_tokens` sin lo cacheado.
    uso.add(proveedor, _respuesta(input_tokens=10, output_tokens=5, cache_read_tokens=80, cache_write_tokens=30,
                                  input_includes_cache=False))
    # DeepSeek / OpenAI: `input_tokens` ya incluye lo cacheado.
    uso.add(proveedor, _respuesta(input_tokens=100, output_tokens=5, cache_read_tokens=70))
    assert uso.input_tokens == 10 + 80 + 30 + 100
    assert uso.cached_tokens == 80 + 70
    assert uso.cache_write_tokens == 30
    assert uso.output_tokens == 10


def test_el_costo_sigue_saliendo_del_input_del_proveedor() -> None:
    from core.orchestrator.loop import _response_cost

    llamadas: list[tuple[int, int]] = []

    def _estimar(i: int, o: int) -> float:
        llamadas.append((i, o))
        return 0.0

    proveedor = types.SimpleNamespace(estimate_cost_usd=_estimar, pricing_per_mtok=(1.0, 1.0))
    _response_cost(proveedor, _respuesta(input_tokens=10, output_tokens=5, cache_read_tokens=80,
                                         input_includes_cache=False))
    assert llamadas == [(10, 5)]


def test_la_grabacion_conserva_el_indicador_sin_cambiar_las_de_siempre() -> None:
    from core.llm.cassette import _Normalizador, _respuesta_a_dict, _respuesta_de_dict

    norm = _Normalizador()
    de_siempre = _respuesta_a_dict(_respuesta(input_tokens=10, output_tokens=5), norm)
    assert "input_includes_cache" not in de_siempre["usage"]
    assert _respuesta_de_dict(de_siempre, norm).usage.input_includes_cache is True

    anthropic = _respuesta_a_dict(_respuesta(input_tokens=10, cache_read_tokens=3, input_includes_cache=False), norm)
    assert anthropic["usage"]["input_includes_cache"] is False
    assert _respuesta_de_dict(anthropic, norm).usage.input_includes_cache is False


# ---------------------------------------------------------------- aviso de lo no disponible
def _flujo() -> ResolvedFlow:
    return ResolvedFlow(
        sections=[
            FlowSection(id="saludo", label="Saludo", purpose="", pack="core", tools=["escalar"]),
            FlowSection(id="citas", label="Citas", purpose="", pack="pack_calendar",
                        tools=["consultar_disponibilidad", "agendar_cita"]),
            FlowSection(id="pedido", label="Pedido", purpose="", pack="pack_commerce", tools=["crear_pedido"]),
        ],
        source="pack",
    )


def test_sin_nada_que_falte_no_hay_aviso() -> None:
    disponibles = {"escalar", "consultar_disponibilidad", "agendar_cita", "crear_pedido", "otra"}
    assert aviso_no_disponible(_flujo(), disponibles, "es") == ""
    assert aviso_no_disponible(None, set(), "es") == ""


def test_el_aviso_lista_las_etapas_y_herramientas_que_faltan() -> None:
    aviso = aviso_no_disponible(_flujo(), {"escalar", "crear_pedido"}, "es-CO")
    textos = _textos_no_disponible()
    assert aviso.startswith(textos["encabezado"] + "\n")
    assert "Citas (consultar_disponibilidad, agendar_cita)" in aviso
    assert "Pedido" not in aviso and "Saludo" not in aviso
    en = aviso_no_disponible(_flujo(), {"escalar"}, "en")
    assert "not available" in en and "Citas (consultar_disponibilidad, agendar_cita); Pedido (crear_pedido)" in en


def test_el_playbook_del_aviso_tiene_todos_los_idiomas() -> None:
    from core.kernel.fuera_de_horario import IDIOMAS

    textos = _textos_no_disponible()
    for idioma in IDIOMAS:
        assert "{detalle}" in textos[idioma]


def test_el_aviso_va_en_el_bloque_dinamico_antes_de_la_fecha() -> None:
    from core.llm.prompt_builder import CURRENT_DATETIME_HEADER, PromptContext, bloque_dinamico

    ctx = PromptContext(assistant_name="A", business_description="", no_disponible="## No disponible\nalgo",
                        now=datetime(2026, 10, 27, 12, tzinfo=UTC))
    bloque = bloque_dinamico(ctx)
    assert bloque.index("## No disponible") < bloque.index(CURRENT_DATETIME_HEADER)


# ---------------------------------------------------------------- un solo ensamblado
async def test_bloques_fijos_mas_dinamico_es_build_system_blocks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.llm import prompt_builder

    async def _slug() -> str:
        return ""

    monkeypatch.setattr("core.shared.tenant.current_tenant_slug", _slug)
    spec = _spec()
    ctx = prompt_builder.PromptContext(
        assistant_name="Ana", business_description="Tienda", brand_voice="Cercana", flow_summary="1. Saludo",
        now=datetime(2026, 10, 27, 12, tzinfo=UTC),
    )
    completos = await prompt_builder.build_system_blocks(spec, ctx)
    fijos = await prompt_builder.bloques_fijos(spec, ctx, tenant_slug="")
    assert completos == [*fijos, prompt_builder.bloque_dinamico(ctx)]
    assert len(fijos) >= 3 and fijos[-1].startswith("## Flujo de atención")


def _spec() -> Any:
    from agentcore_contracts import AgentSpec

    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={"role": "asistente", "tone": "cercano", "warmth": 3, "emojis": "sparse", "maxLength": 500,
                 "forbiddenPhrases": []},
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=[
            {"name": "identidad", "description": "Quién es el asistente"},
            {"name": "reglas-operacion", "description": "Reglas de operación"},
        ],
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=[],
    )

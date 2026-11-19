"""Política de modelo por riesgo y piezas del proveedor (HU-AOS-09).

- Solo `risk: high` cambia el modelo; por defecto el de siempre (regla del dueño: costo mínimo).
- `reasoning_content` de DeepSeek viaja solo si existe: sin la política, el request y la clave del
  cassette son idénticos a los de siempre (el replay del gate no se regraba).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agentcore_contracts import KernelSpec

from core.kernel.politica_modelo import UsoDePolitica, decidir, politica_por_defecto
from core.llm import cassette
from core.llm.openai_compatible import OpenAICompatibleProvider
from core.llm.types import LLMMessage, LLMResponse, LLMToolCall


def test_etapa_normal_no_cambia_nada() -> None:
    assert decidir(False, politica_por_defecto(), "deepseek-flash") is None


def test_riesgo_alto_usa_thinking_con_el_modelo_del_agente() -> None:
    eleccion = decidir(True, politica_por_defecto(), "deepseek-flash")
    assert eleccion is not None and eleccion.model == "deepseek-flash" and eleccion.thinking


def test_riesgo_alto_con_modelo_declarado_y_sin_thinking() -> None:
    politica = KernelSpec.model_validate({"modelPolicy": {"highRiskStage": {"thinking": False, "model": "deepseek-v4-pro"}}}).modelPolicy
    eleccion = decidir(True, politica, "deepseek-flash")
    assert eleccion is not None and eleccion.model == "deepseek-v4-pro" and not eleccion.thinking
    sin_cambio = KernelSpec.model_validate({"modelPolicy": {"highRiskStage": {"thinking": False}}}).modelPolicy
    assert decidir(True, sin_cambio, "deepseek-flash") is None


def test_uso_de_politica_solo_aparece_si_hizo_algo() -> None:
    uso = UsoDePolitica()
    assert uso.vacio() and uso.como_dict() == {}
    uso.riesgo_alto, uso.modelo, uso.thinking, uso.respaldo_verificador = 2, "deepseek-flash", True, 1
    assert uso.como_dict() == {"riesgo_alto": 2, "modelo": "deepseek-flash", "thinking": True, "respaldo_verificador": 1}


# ---------------------------------------------------------------- reasoning_content


def _proveedor(thinking: bool | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(api_key="x", model="deepseek-flash", base_url="https://api.deepseek.com",
                                    provider_name="deepseek", thinking=thinking)


def _mensajes(razonamiento: str = "") -> list[LLMMessage]:
    return [
        LLMMessage(role="user", content="hola"),
        LLMMessage(role="assistant", content="", tool_calls=[LLMToolCall(id="c1", name="crear_pedido", arguments={"a": 1})],
                   reasoning_content=razonamiento),
        LLMMessage(role="tool", tool_call_id="c1", content="{}"),
    ]


def test_sin_razonamiento_el_request_es_el_de_siempre() -> None:
    serial = _proveedor()._serialize_messages(["s"], _mensajes())
    assert all("reasoning_content" not in m for m in serial)


def test_con_razonamiento_se_devuelve_en_la_vuelta_siguiente() -> None:
    serial = _proveedor(True)._serialize_messages(["s"], _mensajes("pienso"))
    assert serial[2]["reasoning_content"] == "pienso"
    assert "reasoning_content" not in serial[1] and "reasoning_content" not in serial[3]


@pytest.mark.asyncio
async def test_el_proveedor_lee_el_razonamiento(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _proveedor(True)

    async def falso(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "ok", "reasoning_content": "r"}, "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(prov, "_post_with_retries", falso)
    respuesta = await prov.generate(["s"], [LLMMessage(role="user", content="hola")])
    assert respuesta.reasoning_content == "r"


def test_la_clave_del_cassette_no_cambia_sin_razonamiento() -> None:
    def clave(mensajes: list[LLMMessage]) -> str:
        return cassette.normalizar(proveedor="deepseek", modelo="deepseek-flash", thinking=False, system_blocks=["s"],
                                   messages=mensajes, tools=None, max_tokens=10, temperature=0.3).clave

    viejo = [LLMMessage(role="user", content="hola"), LLMMessage(role="assistant", content="hola, ¿qué necesitas?")]
    assert clave(viejo) == clave([LLMMessage(role=m.role, content=m.content) for m in viejo])
    assert "reasoning_content" not in json.dumps(cassette.normalizar(
        proveedor="deepseek", modelo="deepseek-flash", thinking=False, system_blocks=["s"], messages=viejo,
        tools=None, max_tokens=10, temperature=0.3).completo())
    con = [*viejo[:1], LLMMessage(role="assistant", content="x", reasoning_content="r")]
    assert clave(con) != clave([*viejo[:1], LLMMessage(role="assistant", content="x")])


def test_respuesta_grabada_sin_razonamiento_queda_igual() -> None:
    norm = cassette._Normalizador()
    dato = cassette._respuesta_a_dict(LLMResponse(content="hola", stop_reason="end_turn", model="m"), norm)
    assert "reasoning_content" not in dato
    dato = cassette._respuesta_a_dict(LLMResponse(content="hola", stop_reason="end_turn", reasoning_content="r"), norm)
    assert cassette._respuesta_de_dict(dato, norm).reasoning_content == "r"


# ---------------------------------------------------------------- proveedor de la política


class _Guion:
    """Proveedor de prueba: responde o falla, y cuenta llamadas."""

    def __init__(self, nombre: str, *, falla: bool = False, proveedor: str = "deepseek", precio: float = 1.0) -> None:
        self.model, self.provider_name, self.falla, self.llamadas = nombre, proveedor, falla, 0
        self.pricing_per_mtok = (precio, precio)

    async def generate(self, system_blocks: Any, messages: Any, tools: Any = None, max_tokens: int = 0,
                       temperature: Any = None) -> LLMResponse:
        from core.shared.exceptions import LLMProviderError

        self.llamadas += 1
        if self.falla:
            raise LLMProviderError("400")
        return LLMResponse(content=self.model, stop_reason="end_turn", model=self.model)

    def estimate_cost_usd(self, i: int, o: int) -> float:
        return (i + o) * self.pricing_per_mtok[0]


@pytest.mark.asyncio
async def test_riesgo_alto_usa_su_proveedor_y_degrada_una_vez() -> None:
    from core.kernel.politica_modelo import Eleccion, PoliticaDelTurno

    base, alto = _Guion("base"), _Guion("alto", falla=True)
    pol = PoliticaDelTurno(alto=alto, eleccion=Eleccion("deepseek-flash", True))  # type: ignore[arg-type]
    prov = pol.envolver(base)  # type: ignore[arg-type]
    assert (await prov.generate([], [])).content == "base"
    assert (await prov.generate([], [])).content == "base"
    assert alto.llamadas == 1 and pol.degradada and pol.uso.como_dict()["degradada"] == 1
    assert "riesgo_alto" not in pol.uso.como_dict()

    alto_ok = _Guion("alto", precio=5.0)
    pol = PoliticaDelTurno(alto=alto_ok, eleccion=Eleccion("deepseek-flash", True))  # type: ignore[arg-type]
    prov = pol.envolver(base)  # type: ignore[arg-type]
    assert (await prov.generate([], [])).content == "alto"
    assert prov.estimate_cost_usd(1, 1) == 10.0  # el costo lo estima quien respondió
    assert pol.uso.como_dict() == {"riesgo_alto": 1, "modelo": "deepseek-flash", "thinking": True}


@pytest.mark.asyncio
async def test_reintento_del_verificador_usa_el_respaldo_una_vez() -> None:
    from core.kernel.politica_modelo import PoliticaDelTurno

    base, respaldo = _Guion("base"), _Guion("respaldo")
    pol = PoliticaDelTurno(respaldo=respaldo, hay_respaldo_declarado=True)  # type: ignore[arg-type]
    prov = pol.envolver(base)  # type: ignore[arg-type]
    pol.reintento_del_verificador()
    assert (await prov.generate([], [])).content == "respaldo"
    assert (await prov.generate([], [])).content == "base"
    assert pol.uso.respaldo_verificador == 1

    pol = PoliticaDelTurno(respaldo=_Guion("r", falla=True), hay_respaldo_declarado=True)  # type: ignore[arg-type]
    prov = pol.envolver(base)  # type: ignore[arg-type]
    pol.reintento_del_verificador()
    assert (await prov.generate([], [])).content == "base" and pol.uso.respaldo_fallido == 1

    pol = PoliticaDelTurno(hay_respaldo_declarado=True)
    prov = pol.envolver(base)  # type: ignore[arg-type]
    pol.reintento_del_verificador()
    assert (await prov.generate([], [])).content == "base" and pol.uso.respaldo_no_disponible == 1


@pytest.mark.parametrize(
    ("modelo", "base", "esperado"),
    [("gpt-4o-mini", "deepseek-flash", False), ("deepseek-v4-pro", "deepseek-flash", True),
     ("mi-modelo", "deepseek-flash", True), ("claude-sonnet", "gpt-4o", False)],
)
def test_misma_familia(modelo: str, base: str, esperado: bool) -> None:
    from core.kernel.politica_modelo import misma_familia

    assert misma_familia(modelo, base) is esperado


def test_modelo_de_otra_familia_se_ignora() -> None:
    politica = KernelSpec.model_validate({"modelPolicy": {"highRiskStage": {"thinking": True, "model": "gpt-4o"}}}).modelPolicy
    eleccion = decidir(True, politica, "deepseek-flash")
    assert eleccion is not None and eleccion.model == "deepseek-flash"


@pytest.mark.asyncio
async def test_un_400_con_la_politica_no_se_reintenta(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from core.shared.exceptions import LLMProviderError

    llamadas = 0

    class _Cliente:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Cliente:
            return self
        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, *a: Any, **k: Any) -> httpx.Response:
            nonlocal llamadas
            llamadas += 1
            return httpx.Response(400, text="falta reasoning_content", request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(httpx, "AsyncClient", _Cliente)
    prov = _proveedor(True)
    prov.reintentar_4xx = False
    with pytest.raises(LLMProviderError):
        await prov.generate(["s"], [LLMMessage(role="user", content="hola")])
    assert llamadas == 1


def test_razonamiento_no_va_a_otro_proveedor_ni_sin_thinking() -> None:
    otro = OpenAICompatibleProvider(api_key="x", model="gpt-4o-mini", base_url="https://api.openai.com", provider_name="openai")
    assert all("reasoning_content" not in m for m in otro._serialize_messages([], _mensajes("r")))
    assert all("reasoning_content" not in m for m in _proveedor(False)._serialize_messages([], _mensajes("r")))

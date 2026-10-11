"""Política de modelo por riesgo (HU-AOS-09, design §7.1, ADR-0113 §3).

Regla del dueño: producción con el costo más bajo posible. Por defecto el turno usa el modelo del
agente (el barato, sin *thinking*); solo una etapa que declara `risk: high` cambia el modelo, con lo
que dice `KernelSpec.modelPolicy.highRiskStage` (`thinking`, y `model` si se declara uno distinto).
Después de un fallo del verificador, el reintento usa el respaldo del agente (`model.fallback`).

Todo esto rige solo con el motor activo (`tenants.aos_procedures`): con la bandera apagada el loop
no llama a nada de este módulo y el turno es el de siempre.

Puro: sin base, red ni reloj. El loop resuelve los proveedores y registra el uso.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agentcore_contracts import KernelSpec
from agentcore_contracts.kernelspec import ModelPolicy

from core.llm.base import BaseLLMProvider
from core.llm.types import LLMMessage, LLMResponse, LLMToolSchema
from core.shared.exceptions import LLMProviderError
from core.shared.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Eleccion:
    """Modelo y modo de una llamada bajo la política de riesgo alto."""

    model: str
    thinking: bool


def politica_por_defecto() -> ModelPolicy:
    """La `modelPolicy` del KernelSpec del turno.

    Hasta que HU-AOS-14 entregue el KernelSpec congelado en la release de la conexión, el lector
    tolerante lo deriva de la `persona` v2 con los valores por defecto (ADR-0113 §5), así que la
    política es la del contrato: `highRiskStage = {thinking: true, model: null}`."""
    return KernelSpec().modelPolicy


def decidir(riesgo_alto: bool, politica: ModelPolicy, modelo_agente: str) -> Eleccion | None:
    """None = la llamada sigue con el modelo de siempre. Solo `risk: high` cambia algo."""
    if not riesgo_alto:
        return None
    alto = politica.highRiskStage
    # Un modelo de otra familia que la del turno no se puede pedir con su proveedor: se ignora.
    modelo = alto.model if alto.model and misma_familia(alto.model, modelo_agente) else modelo_agente
    if modelo == modelo_agente and not alto.thinking:
        return None  # la etapa no pide nada distinto
    return Eleccion(model=modelo, thinking=alto.thinking)


@dataclass
class UsoDePolitica:
    """Lo que la política hizo en el turno, para `usage_ledger.result` (solo si hizo algo)."""

    riesgo_alto: int = 0          # llamadas con la elección de riesgo alto
    modelo: str = ""
    thinking: bool = False
    degradada: int = 0            # llamadas de riesgo alto que fallaron y siguieron con el modelo base
    reasoning_tokens: int = 0
    respaldo_verificador: int = 0  # reintentos del verificador con `model.fallback`
    respaldo_fallido: int = 0      # reintentos cuyo respaldo falló y siguieron con el modelo base
    respaldo_no_disponible: int = 0  # reintentos sin respaldo utilizable (otra familia de modelo)

    def vacio(self) -> bool:
        return not self.como_dict()

    def como_dict(self) -> dict[str, Any]:
        salida: dict[str, Any] = {}
        if self.riesgo_alto:
            salida.update(riesgo_alto=self.riesgo_alto, modelo=self.modelo, thinking=self.thinking)
            if self.reasoning_tokens:
                salida["reasoning_tokens"] = self.reasoning_tokens
        for campo in ("degradada", "respaldo_verificador", "respaldo_fallido", "respaldo_no_disponible"):
            if valor := getattr(self, campo):
                salida[campo] = valor
        return salida


_FAMILIAS = (("deepseek", "deepseek"), ("gpt-", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
             ("claude", "anthropic"))


def familia(modelo: str) -> str | None:
    """`deepseek-flash` → deepseek, `gpt-4o-mini` → openai. None = no se reconoce."""
    bajo = (modelo or "").lower()
    return next((f for prefijo, f in _FAMILIAS if bajo.startswith(prefijo)), None)


def misma_familia(modelo: str, modelo_base: str) -> bool:
    """¿El modelo se puede pedir con el proveedor y la clave del turno?

    El proveedor del turno (BYOK del negocio o el de la plataforma) sirve a una familia: pedirle un
    modelo de otra familia falla siempre (`gpt-4o-mini` contra `api.deepseek.com`), así que la
    política no lo intenta (ADR-0132). Un modelo que no se reconoce cuenta como de la misma familia
    (p. ej. un `openai_compatible` propio): el proveedor del negocio sabe servirlo."""
    f, base = familia(modelo), familia(modelo_base)
    return f is None or base is None or f == base


class PoliticaDelTurno:
    """Lo que la política decidió para el turno (lo arma el loop solo con el motor activo).

    - Riesgo alto: el modo se decide UNA vez, con la etapa en foco al empezar el turno (la acción
      irreversible está detrás de la puerta de confirmación, así que se ejecuta en un turno que ya
      empezó en esa etapa). Si una llamada falla, el turno queda degradado al proveedor base hasta
      el final: nunca se mezcla una vuelta con thinking sobre mensajes que no lo traen.
    - Reintento del verificador: la vuelta siguiente usa el respaldo del agente; si falla, el base.
    """

    def __init__(
        self,
        *,
        alto: BaseLLMProvider | None = None,
        eleccion: Eleccion | None = None,
        respaldo: BaseLLMProvider | None = None,
        hay_respaldo_declarado: bool = False,
    ) -> None:
        self.alto = alto
        self.respaldo = respaldo
        self.hay_respaldo_declarado = hay_respaldo_declarado
        self.degradada = False
        self.reintento = False
        self.uso = UsoDePolitica()
        if alto is not None and eleccion is not None:
            self.uso.modelo = eleccion.model
            # Solo DeepSeek aplica el thinking (los demás ignoran el campo): se registra el efectivo.
            self.uso.thinking = eleccion.thinking and alto.provider_name == "deepseek"

    @property
    def riesgo_alto(self) -> bool:
        return self.alto is not None and not self.degradada

    def reintento_del_verificador(self) -> None:
        """La próxima llamada es el reintento tras un rechazo del verificador."""
        self.reintento = True

    def envolver(self, base: BaseLLMProvider) -> ProveedorDeLaPolitica:
        return ProveedorDeLaPolitica(base, self)


class ProveedorDeLaPolitica(BaseLLMProvider):
    """El proveedor del turno con la política aplicada. El costo de cada respuesta lo estima el
    proveedor que la sirvió (`estimate_cost_usd`)."""

    def __init__(self, base: BaseLLMProvider, politica: PoliticaDelTurno) -> None:
        self.base = base
        self.politica = politica
        self._ultimo: BaseLLMProvider = base
        self.provider_name = base.provider_name
        self.model = base.model

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return self._ultimo.estimate_cost_usd(input_tokens, output_tokens)

    async def generate(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        p, uso = self.politica, self.politica.uso
        kwargs: dict[str, Any] = {
            "system_blocks": system_blocks, "messages": messages, "tools": tools,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if p.reintento:
            p.reintento = False
            if p.respaldo is not None:
                try:
                    respuesta = await p.respaldo.generate(**kwargs)
                except LLMProviderError as exc:
                    uso.respaldo_fallido += 1
                    log.warning("politica_modelo_respaldo_fallo", modelo=p.respaldo.model, error=type(exc).__name__)
                else:
                    self._ultimo = p.respaldo
                    uso.respaldo_verificador += 1
                    return respuesta
            elif p.hay_respaldo_declarado:
                uso.respaldo_no_disponible += 1
        if p.alto is not None and not p.degradada:
            try:
                respuesta = await p.alto.generate(**kwargs)
            except LLMProviderError as exc:
                p.degradada = True
                uso.degradada += 1
                log.warning("politica_modelo_degradada", modelo=p.alto.model, error=type(exc).__name__)
            else:
                self._ultimo = p.alto
                uso.riesgo_alto += 1
                uso.reasoning_tokens += int(getattr(respuesta.usage, "reasoning_tokens", 0) or 0)
                return respuesta
        self._ultimo = self.base
        return await self.base.generate(**kwargs)

    def stream(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        return self.base.stream(system_blocks, messages, tools, max_tokens, temperature)


__all__ = ["Eleccion", "PoliticaDelTurno", "ProveedorDeLaPolitica", "UsoDePolitica", "decidir", "familia", "misma_familia", "politica_por_defecto"]

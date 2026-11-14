"""IA estructurada fuera del turno: texto o imagen → JSON validado + fila en el ledger (ADR-0194).

Un solo camino para las llamadas del core que piden a la IA un objeto con forma conocida
(datos de prueba, mapeo de columnas de un CSV, reconocer un producto en una foto):

1. Instrucciones en un playbook del **kernel** (`core/playbooks/kernel/<playbook>.md`), sin
   dominio de ningún pack (invariante 3). El dominio llega como datos en `entrada`.
2. Llamada al proveedor: texto con un proveedor LLM (inyectable, para tests y para quien
   resuelva la llave por su cuenta) o imagen con `core.media.vision.describir_imagen`.
3. Mide la llamada en `usage_ledger` con el `kind` y el `result` que pasa quien llama, y los
   créditos de `creditos_por_evento` según el concepto y el `key_mode` (con llave propia, solo
   el fee). Se mide aunque la salida no valide: el proveedor ya cobró. Medir nunca rompe la
   llamada.
4. La salida es dato no confiable (invariante 7): se toma el primer objeto JSON y se valida
   con el esquema pydantic.

Errores: `ErrorProveedor` (sin respuesta: caído, sin saldo, timeout, sin llave; no hay fila en
el ledger) y `ErrorFormato` (respondió pero no trae un objeto que valide; la fila ya quedó).
Quien llama decide el respaldo.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from core.shared.logger import get_logger

log = get_logger(__name__)

KeyMode = Literal["platform", "byok"]

_JSON = re.compile(r"\{.*\}", re.S)


class ErrorIA(Exception):
    """La IA no dio un objeto utilizable. `motivo` es estable; `causa`, el tipo o código original."""

    motivo: str = "ia"

    def __init__(self, causa: str) -> None:
        super().__init__(causa)
        self.causa = causa


class ErrorProveedor(ErrorIA):
    """No hubo respuesta: proveedor caído, sin saldo, sin llave o fuera de tiempo."""

    motivo = "proveedor"


class ErrorFormato(ErrorIA):
    """Hubo respuesta (y ya está en el ledger), pero no trae un objeto que valide."""

    motivo = "formato"

    def __init__(self, causa: str, consumo: Consumo) -> None:
        super().__init__(causa)
        self.consumo = consumo


@dataclass(frozen=True)
class Imagen:
    contenido: bytes
    mime_type: str


@dataclass(frozen=True)
class Consumo:
    """Lo medido de una llamada. `credits` es `None` si la fila del ledger no se pudo escribir."""

    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: Decimal
    key_mode: KeyMode
    credits: Decimal | None


@dataclass(frozen=True)
class Estructurado[M: BaseModel]:
    datos: M
    consumo: Consumo


def extraer_objeto[M: BaseModel](contenido: str | None, esquema: type[M]) -> M:
    """El primer objeto JSON de la respuesta, validado. Lanza ValueError o ValidationError."""
    hallado = _JSON.search(contenido or "")
    if hallado is None:
        raise ValueError("la respuesta no trae un objeto JSON")
    return esquema.model_validate(json.loads(hallado.group(0)))


async def _app_settings(tenant_id: uuid.UUID) -> Any:
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
        ).scalar_one_or_none()


def resolver_llm(app_settings: Any) -> tuple[Any, KeyMode]:
    """Proveedor de texto del negocio y su `key_mode`.

    Con proveedor y llave propios en el panel, BYOK: la misma condición con la que el turno usa
    la llave del negocio y el crédito cobra solo el fee (ADR-0144). Si no, la llave de plataforma.
    """
    from core.llm import resolve_provider
    from core.shared.crypto import get_secret_box

    propia = bool(
        app_settings is not None
        and app_settings.llm_provider
        and app_settings.llm_api_key_encrypted
    )
    if not propia:
        return resolve_provider(thinking=False), "platform"
    return (
        resolve_provider(
            provider_key=app_settings.llm_provider,
            api_key=get_secret_box().decrypt(app_settings.llm_api_key_encrypted),
            model=app_settings.llm_model or None,
            thinking=False,
        ),
        "byok",
    )


async def _medir(
    tenant_id: uuid.UUID,
    *,
    kind: str,
    concepto: str,
    key_mode: KeyMode,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    cost_usd: Decimal,
    result: Mapping[str, Any],
) -> Consumo:
    from core.billing.tarifario import TARIFARIO_VIGENTE, creditos_por_evento, trm_vigente
    from core.db.models import UsageLedger
    from core.db.session import get_session

    credits: Decimal | None = None
    try:
        trm = trm_vigente()
        calculados = creditos_por_evento(cost_usd, concepto, key_mode, trm)  # type: ignore[arg-type]
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=tenant_id,
                    conversation_id=None,
                    turn_id=None,
                    kind=kind,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                    result=dict(result),
                    credits=calculados,
                    key_mode=key_mode,
                    tarifario_version=TARIFARIO_VIGENTE.version,
                    trm_cop=trm,
                )
            )
        credits = calculados
    except Exception as exc:
        log.warning("usage_ledger_ia_estructurada_write_failed", kind=kind, error=type(exc).__name__)
    return Consumo(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        key_mode=key_mode,
        credits=credits,
    )


def _validar[M: BaseModel](texto: str | None, esquema: type[M], consumo: Consumo) -> Estructurado[M]:
    try:
        return Estructurado(datos=extraer_objeto(texto, esquema), consumo=consumo)
    except (ValueError, ValidationError) as exc:
        raise ErrorFormato(type(exc).__name__, consumo) from exc


async def generar_estructurado[M: BaseModel](
    tenant_id: uuid.UUID,
    *,
    playbook: str,
    esquema: type[M],
    entrada: str,
    kind: str,
    result: Mapping[str, Any],
    proveedor: Callable[[], Any] | None = None,
    key_mode: KeyMode | None = None,
    max_tokens: int,
    temperatura: float,
    timeout_s: float,
) -> Estructurado[M]:
    """Texto → objeto validado. Mide con el concepto `llm_turn`.

    `proveedor`: fábrica del proveedor (se llama dentro del manejo de errores, así que una
    resolución fallida es `ErrorProveedor`). Si se pasa, `key_mode` dice cómo cobrar (por
    defecto `platform`). Sin fábrica, se resuelve el del negocio con `resolver_llm`.
    """
    from core.agents.playbooks import load_core_playbook
    from core.llm.types import LLMMessage

    try:
        sistema = load_core_playbook(playbook)
        if proveedor is not None:
            llm, modo = proveedor(), key_mode or "platform"
        else:
            llm, modo = resolver_llm(await _app_settings(tenant_id))
        respuesta = await asyncio.wait_for(
            llm.generate(
                system_blocks=[sistema],
                messages=[LLMMessage(role="user", content=entrada)],
                max_tokens=max_tokens,
                temperature=temperatura,
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # proveedor caído, sin saldo, timeout: decide quien llama
        raise ErrorProveedor(type(exc).__name__) from exc

    try:
        uso = respuesta.usage
        estimar = getattr(llm, "estimate_cost_usd", None)
        # Un proveedor sin tarifas (el fake de DEMO_MODE) cuenta 0.
        costo = Decimal(str(estimar(uso.input_tokens, uso.output_tokens))) if estimar else Decimal("0")
    except Exception:
        uso, costo = None, Decimal("0")
    consumo = await _medir(
        tenant_id,
        kind=kind,
        concepto="llm_turn",
        key_mode=modo,
        model=respuesta.model or getattr(llm, "model", ""),
        input_tokens=getattr(uso, "input_tokens", 0),
        output_tokens=getattr(uso, "output_tokens", 0),
        latency_ms=respuesta.latency_ms,
        cost_usd=costo,
        result=result,
    )
    return _validar(respuesta.content, esquema, consumo)


async def reconocer_imagen[M: BaseModel](
    tenant_id: uuid.UUID,
    *,
    playbook: str,
    esquema: type[M],
    imagen: Imagen,
    entrada: str = "",
    kind: str = "vision",
    result: Mapping[str, Any],
    max_tokens: int,
    config: Any | None = None,
) -> Estructurado[M]:
    """Imagen → objeto validado, con el proveedor de visión del negocio. Mide con el concepto
    `vision` y el `key_mode` de su configuración (BYOK si el negocio puso su llave).

    `config`: un `VisionConfig` ya resuelto; sin él, se resuelve con `resolve_vision_config`.
    No mira `vision_enabled` ni `vision_mode`: esos gobiernan la ingesta de WhatsApp, no una
    acción que el dueño pide a mano. El tiempo límite es el de visión (`VISION_TIMEOUT_S`).
    """
    from core.agents.playbooks import load_core_playbook
    from core.media.config import MediaConfigError, resolve_vision_config
    from core.media.errors import MediaProviderError
    from core.media.vision import describir_imagen

    prompt = load_core_playbook(playbook)
    if entrada:
        prompt = f"{prompt}\n\n{entrada}"
    try:
        if config is None:
            config = resolve_vision_config(await _app_settings(tenant_id))
        inicio = time.monotonic()
        respuesta = await describir_imagen(
            imagen.contenido,
            imagen.mime_type,
            provider=config.provider,
            model=config.model,
            api_key=config.api_key,
            prompt=prompt,
            max_tokens=max_tokens,
        )
    except (MediaConfigError, MediaProviderError) as exc:
        raise ErrorProveedor(getattr(exc, "code", type(exc).__name__)) from exc
    consumo = await _medir(
        tenant_id,
        kind=kind,
        concepto="vision",
        key_mode=config.key_mode,
        model=respuesta.model,
        input_tokens=respuesta.input_tokens,
        output_tokens=respuesta.output_tokens,
        latency_ms=int((time.monotonic() - inicio) * 1000),
        cost_usd=Decimal(str(respuesta.cost_usd)),
        result=result,
    )
    return _validar(respuesta.text, esquema, consumo)


__all__ = [
    "Consumo",
    "ErrorFormato",
    "ErrorIA",
    "ErrorProveedor",
    "Estructurado",
    "Imagen",
    "extraer_objeto",
    "generar_estructurado",
    "reconocer_imagen",
    "resolver_llm",
]

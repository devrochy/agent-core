"""Helper de IA estructurada (ADR-0194): JSON validado, errores con motivo y fila en el ledger.

Proveedor falso (texto) y `describir_imagen` falso (imagen): no llama a ningún LLM. El ledger
se captura con una sesión falsa; la escritura real la cubren las pruebas de integración de
datos de prueba (`test_qa_datos_generados.py`), que no cambian con el refactor.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from core.ia import estructurado
from core.ia.estructurado import (
    ErrorFormato,
    ErrorProveedor,
    Imagen,
    extraer_objeto,
    generar_estructurado,
    reconocer_imagen,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a194")
PLAYBOOK = "datos_de_prueba"  # cualquiera del kernel: el helper no interpreta su contenido


class Producto(BaseModel):
    nombre: str
    precio: int


class ProveedorFalso:
    model = "falso-1"

    def __init__(self, contenido: str = "", error: Exception | None = None) -> None:
        self.contenido = contenido
        self.error = error
        self.kwargs: dict[str, Any] = {}

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMUsageInfo

        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=self.contenido,
            stop_reason="end_turn",
            usage=LLMUsageInfo(input_tokens=1000, output_tokens=500),
            model="",
            latency_ms=7,
        )

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.002


@pytest.fixture
def ledger(monkeypatch) -> list[Any]:
    filas: list[Any] = []

    class Sesion:
        def add(self, fila: Any) -> None:
            filas.append(fila)

    @asynccontextmanager
    async def sesion_falsa(*_a: Any, **_k: Any):
        yield Sesion()

    monkeypatch.setattr("core.db.session.get_session", sesion_falsa)
    return filas


async def _texto(proveedor: ProveedorFalso, **extra: Any) -> Any:
    return await generar_estructurado(
        TENANT,
        playbook=PLAYBOOK,
        esquema=Producto,
        entrada='{"esquema": "producto"}',
        kind="generacion",
        result={"proposito": "prueba"},
        proveedor=lambda: proveedor,
        max_tokens=100,
        temperatura=0.1,
        timeout_s=5,
        **extra,
    )


def test_extrae_el_objeto_aunque_venga_con_prosa() -> None:
    datos = extraer_objeto('Aquí va:\n{"nombre": "Café", "precio": 9000}\nListo.', Producto)
    assert datos == Producto(nombre="Café", precio=9000)
    with pytest.raises(ValueError):
        extraer_objeto("sin json", Producto)


async def test_texto_valido_valida_y_mide_con_plataforma(ledger) -> None:
    proveedor = ProveedorFalso('Claro: {"nombre": "Café", "precio": 9000}')
    salida = await _texto(proveedor)

    assert salida.datos == Producto(nombre="Café", precio=9000)
    # La entrada viaja tal cual y las instrucciones salen del playbook del kernel.
    assert proveedor.kwargs["messages"][0].content == '{"esquema": "producto"}'
    assert proveedor.kwargs["system_blocks"][0].strip()
    assert proveedor.kwargs["max_tokens"] == 100
    (fila,) = ledger
    assert fila.tenant_id == TENANT
    assert fila.kind == "generacion"
    assert fila.result == {"proposito": "prueba"}
    assert fila.key_mode == "platform"
    assert fila.model == "falso-1"  # la respuesta no trae modelo: se usa el del proveedor
    assert (fila.input_tokens, fila.output_tokens) == (1000, 500)
    assert fila.cost_usd == Decimal("0.002")
    assert fila.credits > Decimal("0.2")  # variable + fee de llm_turn
    assert salida.consumo.credits == fila.credits


async def test_salida_invalida_es_error_de_formato_y_ya_esta_medida(ledger) -> None:
    with pytest.raises(ErrorFormato) as error:
        await _texto(ProveedorFalso('{"nombre": "Café"}'))
    assert error.value.motivo == "formato"
    assert error.value.causa == "ValidationError"
    assert len(ledger) == 1  # el proveedor ya cobró: se mide igual
    assert error.value.consumo.credits == ledger[0].credits


async def test_proveedor_caido_es_error_de_proveedor_sin_medir(ledger) -> None:
    with pytest.raises(ErrorProveedor) as error:
        await _texto(ProveedorFalso(error=RuntimeError("402 sin saldo")))
    assert error.value.motivo == "proveedor"
    assert error.value.causa == "RuntimeError"
    assert ledger == []


async def test_fabrica_que_falla_es_error_de_proveedor(ledger) -> None:
    def fabrica() -> Any:
        raise ValueError("sin llave")

    with pytest.raises(ErrorProveedor):
        await generar_estructurado(
            TENANT,
            playbook=PLAYBOOK,
            esquema=Producto,
            entrada="{}",
            kind="generacion",
            result={},
            proveedor=fabrica,
            max_tokens=10,
            temperatura=0,
            timeout_s=5,
        )
    assert ledger == []


async def test_con_llave_propia_solo_cobra_el_fee(ledger) -> None:
    await _texto(ProveedorFalso('{"nombre": "Café", "precio": 1}'), key_mode="byok")
    (fila,) = ledger
    assert fila.key_mode == "byok"
    assert fila.credits == Decimal("0.2")


async def test_ledger_caido_no_rompe_la_llamada(monkeypatch) -> None:
    @asynccontextmanager
    async def rota(*_a: Any, **_k: Any):
        raise RuntimeError("sin base")
        yield

    monkeypatch.setattr("core.db.session.get_session", rota)
    salida = await _texto(ProveedorFalso('{"nombre": "Café", "precio": 1}'))
    assert salida.datos.nombre == "Café"
    assert salida.consumo.credits is None


async def test_sin_fabrica_resuelve_el_proveedor_del_negocio(ledger, monkeypatch) -> None:
    proveedor = ProveedorFalso('{"nombre": "Té", "precio": 2}')

    async def ajustes(_tenant: uuid.UUID) -> Any:
        return None

    monkeypatch.setattr(estructurado, "_app_settings", ajustes)
    monkeypatch.setattr(estructurado, "resolver_llm", lambda _ajustes: (proveedor, "byok"))
    salida = await generar_estructurado(
        TENANT,
        playbook=PLAYBOOK,
        esquema=Producto,
        entrada="{}",
        kind="generacion",
        result={"proposito": "mapeo_catalogo"},
        max_tokens=10,
        temperatura=0,
        timeout_s=5,
    )
    assert salida.datos.nombre == "Té"
    assert ledger[0].key_mode == "byok"


# ------------------------------------------------------------------ imagen
def _config(key_mode: str = "platform") -> Any:
    from core.media.config import VisionConfig

    return VisionConfig(
        enabled=True, mode="describir", provider="openai", model="gpt-vision", api_key="k", key_mode=key_mode
    )


def _vision(monkeypatch, texto: str | None = None, error: Exception | None = None) -> dict[str, Any]:
    from core.media.vision import VisionResult

    llamada: dict[str, Any] = {}

    async def describir(contenido: bytes, mime_type: str, **kwargs: Any) -> VisionResult:
        llamada.update(kwargs, contenido=contenido, mime_type=mime_type)
        if error is not None:
            raise error
        return VisionResult(
            text=texto, input_tokens=800, output_tokens=60, cost_usd=0.001, provider="openai", model="gpt-vision"
        )

    monkeypatch.setattr("core.media.vision.describir_imagen", describir)
    return llamada


async def _imagen(config: Any) -> Any:
    return await reconocer_imagen(
        TENANT,
        playbook=PLAYBOOK,
        esquema=Producto,
        imagen=Imagen(contenido=b"\x89PNG", mime_type="image/png"),
        entrada='{"esquema": "producto"}',
        result={"proposito": "foto_catalogo", "image_key": "t/abc.png"},
        max_tokens=300,
        config=config,
    )


async def test_imagen_valida_mide_vision_con_el_key_mode_de_su_config(ledger, monkeypatch) -> None:
    llamada = _vision(monkeypatch, '{"nombre": "Bolso", "precio": 50000}')
    salida = await _imagen(_config())

    assert salida.datos == Producto(nombre="Bolso", precio=50000)
    assert llamada["contenido"] == b"\x89PNG"
    assert llamada["max_tokens"] == 300
    assert llamada["prompt"].endswith('{"esquema": "producto"}')
    (fila,) = ledger
    assert fila.kind == "vision"
    assert fila.result["image_key"] == "t/abc.png"
    assert fila.key_mode == "platform"
    assert fila.credits > Decimal("1.0")  # variable + fee de visión


async def test_imagen_con_llave_propia_solo_cobra_el_fee(ledger, monkeypatch) -> None:
    _vision(monkeypatch, '{"nombre": "Bolso", "precio": 1}')
    await _imagen(_config("byok"))
    assert ledger[0].credits == Decimal("1.0")


async def test_imagen_sin_texto_es_error_de_formato_medido(ledger, monkeypatch) -> None:
    _vision(monkeypatch, None)
    with pytest.raises(ErrorFormato):
        await _imagen(_config())
    assert len(ledger) == 1


async def test_imagen_con_proveedor_caido_no_mide(ledger, monkeypatch) -> None:
    from core.media.errors import MediaProviderError

    _vision(monkeypatch, error=MediaProviderError("caído", transient=True, code="vision_timeout"))
    with pytest.raises(ErrorProveedor) as error:
        await _imagen(_config())
    assert error.value.causa == "vision_timeout"
    assert ledger == []

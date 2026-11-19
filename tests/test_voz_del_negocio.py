"""Adenda 2026-11-14 de ADR-0191: la voz de Configuración se siembra en el Coordinador.

La voz del turno sale del `KernelSpec` congelado en la release. Este módulo prueba que
`sembrar_voz_del_negocio` publica una versión nueva del Coordinador cuando la persona del negocio
cambió, y que no escribe nada cuando ya coinciden. Sin base de datos: las piezas que tocan BD se
monkeypatchean.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from agentcore_contracts import KernelSpec

from core.agents import base, versiones

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
AGENTE = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _persona(*, warmth: int = 5, tone: str = "cercano", emojis: str = "sparse", role: str = "asistente") -> Any:
    return SimpleNamespace(persona=SimpleNamespace(tone=tone, warmth=warmth, emojis=emojis, role=role))


def _kernel(**voice: Any) -> KernelSpec:
    datos = {"tone": "cercano", "warmth": 3, "emojis": "sparse", "role": "asistente", **voice}
    return KernelSpec.model_validate({"voice": datos})


class _Resultado:
    def scalar_one_or_none(self) -> uuid.UUID:
        return AGENTE


class _Sesion:
    async def execute(self, _stmt: Any) -> _Resultado:
        return _Resultado()

    async def __aenter__(self) -> _Sesion:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _parchear(monkeypatch: pytest.MonkeyPatch, *, kernel: KernelSpec, persona: Any, publicar: Any) -> None:
    async def leer(_tid: uuid.UUID) -> tuple[str, Any]:
        return ("agent-default", persona)

    async def listar(_tid: uuid.UUID, _aid: uuid.UUID) -> list[Any]:
        return [SimpleNamespace(content=kernel.model_dump(mode="json", by_alias=True))]

    from core.agents import spec as spec_mod
    from core.kernel import procedures
    from core.plugin_manager import plugin_manager

    monkeypatch.setattr(base, "get_current_tenant_id", lambda: TENANT)
    monkeypatch.setattr(base, "get_session", lambda: _Sesion())
    monkeypatch.setattr(spec_mod, "leer_spec_activo", leer)
    monkeypatch.setattr(versiones, "listar_versiones", listar)
    monkeypatch.setattr(versiones, "publicar", publicar)
    monkeypatch.setattr(plugin_manager, "list_modules", lambda: [])
    monkeypatch.setattr(procedures, "catalogo_de_acciones", lambda _modulos: {})


@pytest.mark.asyncio
async def test_rechaza_otro_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "get_current_tenant_id", lambda: uuid.uuid4())

    with pytest.raises(PermissionError):
        await base.sembrar_voz_del_negocio(TENANT)


@pytest.mark.asyncio
async def test_publica_una_version_del_coordinador_cuando_la_voz_cambia(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict[str, Any] = {}

    async def publicar(_tid: uuid.UUID, _aid: uuid.UUID, **kwargs: Any) -> tuple[Any, bool]:
        capturado.update(kwargs)
        return (SimpleNamespace(version=2), True)

    _parchear(monkeypatch, kernel=_kernel(warmth=3), persona=_persona(warmth=5), publicar=publicar)

    creada = await base.sembrar_voz_del_negocio(TENANT)

    assert creada is True
    assert capturado["label"] == "voz"
    assert capturado["contenido"]["voice"]["warmth"] == 5
    assert capturado["contenido"]["voice"]["tone"] == "cercano"
    assert capturado["contenido"]["voice"]["emojis"] == "sparse"


@pytest.mark.asyncio
async def test_no_escribe_si_la_voz_ya_coincide(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[dict[str, Any]] = []

    async def publicar(_tid: uuid.UUID, _aid: uuid.UUID, **kwargs: Any) -> tuple[Any, bool]:
        llamadas.append(kwargs)
        return (SimpleNamespace(version=1), False)

    _parchear(monkeypatch, kernel=_kernel(warmth=5), persona=_persona(warmth=5), publicar=publicar)

    creada = await base.sembrar_voz_del_negocio(TENANT)

    assert creada is False
    assert llamadas == []


@pytest.mark.asyncio
async def test_conserva_el_resto_del_kernelspec(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict[str, Any] = {}
    kernel = KernelSpec.model_validate(
        {
            "voice": {"tone": "cercano", "warmth": 3, "emojis": "sparse"},
            "routing": {"defaultAgent": "ventas"},
        }
    )

    async def publicar(_tid: uuid.UUID, _aid: uuid.UUID, **kwargs: Any) -> tuple[Any, bool]:
        capturado.update(kwargs)
        return (SimpleNamespace(version=2), True)

    _parchear(monkeypatch, kernel=kernel, persona=_persona(warmth=5), publicar=publicar)

    assert await base.sembrar_voz_del_negocio(TENANT) is True
    assert capturado["contenido"]["routing"]["defaultAgent"] == "ventas"

"""De dónde lee Personalizar › Datos del cliente las operaciones del agente (`_contenido`).

El borrador manda (es lo que el dueño edita). Cae a la última versión publicada solo si no hay
borrador o si está intacto (`lock_version` 0) y sin tools ni procedimientos: es el inicial de
`crear_agente`, no lo que el agente ejecuta."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.agents import versiones
from core.api.routers import datos_cliente

VERSION = {"tools": [{"tool": "crear_pedido"}], "procedures": [{"id": "pack_commerce.tomar_pedido"}]}
INICIAL = {"model": {"primary": "x"}, "tools": [], "procedures": []}


def _principal() -> SimpleNamespace:
    return SimpleNamespace(tenant_id=uuid.uuid4())


def _con(monkeypatch: pytest.MonkeyPatch, borrador: object | Exception, versiones_lista: list) -> None:
    async def obtener_borrador(_t, _a):
        if isinstance(borrador, Exception):
            raise borrador
        return borrador

    async def listar_versiones(_t, _a):
        return versiones_lista

    monkeypatch.setattr(versiones, "obtener_borrador", obtener_borrador)
    monkeypatch.setattr(versiones, "listar_versiones", listar_versiones)


@pytest.mark.parametrize(
    ("borrador", "lista", "esperado"),
    [
        # Borrador inicial intacto encima de una versión completa: la versión.
        (SimpleNamespace(content=INICIAL, lock_version=0), [SimpleNamespace(content=VERSION)], VERSION),
        # Sin borrador: la versión.
        (versiones.AgenteNoEncontrado("x"), [SimpleNamespace(content=VERSION)], VERSION),
        # Un borrador que el dueño guardó manda, aunque lo haya dejado sin tools.
        (SimpleNamespace(content=INICIAL, lock_version=3), [SimpleNamespace(content=VERSION)], INICIAL),
        # Un borrador intacto con contenido (el preset ya lo dejó igual a la v1) manda.
        (SimpleNamespace(content=VERSION, lock_version=0), [], VERSION),
        # Borrador intacto y vacío sin versiones: el borrador.
        (SimpleNamespace(content=INICIAL, lock_version=0), [], INICIAL),
        # Nada: vacío.
        (versiones.AgenteNoEncontrado("x"), [], {}),
    ],
)
async def test_contenido_cae_a_la_version_solo_con_borrador_intacto_y_vacio(
    monkeypatch: pytest.MonkeyPatch, borrador: object, lista: list, esperado: dict
) -> None:
    _con(monkeypatch, borrador, lista)
    assert await datos_cliente._contenido(_principal(), uuid.uuid4()) == esperado  # type: ignore[arg-type]

"""HU-CRM-28 · el agente de Ventas sabe durante la conversación qué métodos de entrega tiene.

- Con un método apagado, la referencia del turno lo dice (`si`/`no`): el agente no promete
  «recoger en tienda» antes de que `crear_pedido` lo rechace.
- Con los dos (el valor por defecto) no hay línea: el prompt y las grabaciones quedan igual.
- La línea sale tanto en la conversación real como en el sandbox del playground.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.datos_cliente import ajustes
from core.orchestrator.referencias import renderizar
from core.shared import release_vigente


def _politica(monkeypatch, data_policy):  # type: ignore[no-untyped-def]
    async def _cargar(_agent_id):  # type: ignore[no-untyped-def]
        return data_policy

    monkeypatch.setattr(ajustes, "cargar_data_policy", _cargar)


@pytest.mark.asyncio
async def test_solo_domicilio_lo_dice_la_referencia(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import skills

    monkeypatch.setattr(
        release_vigente, "agente_del_turno", lambda: SimpleNamespace(agent_id=uuid.uuid4())
    )
    _politica(monkeypatch, {"metodos_entrega": ["domicilio"]})
    [ref] = await skills._referencia_de_entrega()
    assert renderizar(ref) == "- metodos_entrega: domicilio=si, recoger_en_tienda=no"

    _politica(monkeypatch, {"metodos_entrega": ["recoger_en_tienda"]})
    [ref] = await skills._referencia_de_entrega()
    assert renderizar(ref) == "- metodos_entrega: domicilio=no, recoger_en_tienda=si"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data_policy",
    [{}, {"metodos_entrega": ["domicilio", "recoger_en_tienda"]}, {"metodos_entrega": []}],
)
async def test_con_los_dos_no_hay_linea(monkeypatch, data_policy) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import skills

    monkeypatch.setattr(
        release_vigente, "agente_del_turno", lambda: SimpleNamespace(agent_id=uuid.uuid4())
    )
    _politica(monkeypatch, data_policy)
    assert await skills._referencia_de_entrega() == []


@pytest.mark.asyncio
async def test_sin_agente_en_foco_no_hay_linea(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import skills

    monkeypatch.setattr(release_vigente, "agente_del_turno", lambda: None)
    _politica(monkeypatch, {"metodos_entrega": ["domicilio"]})
    assert await skills._referencia_de_entrega() == []


@pytest.mark.asyncio
async def test_el_sandbox_tambien_la_lleva(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_commerce import skills

    from core.mcp.sandbox import SandboxPrueba

    class _Vacio:
        async def listar(self, _tabla):  # type: ignore[no-untyped-def]
            return []

    monkeypatch.setattr(SandboxPrueba, "de_contexto", classmethod(lambda cls, _cid: _Vacio()))
    monkeypatch.setattr(
        release_vigente, "agente_del_turno", lambda: SimpleNamespace(agent_id=uuid.uuid4())
    )

    async def _sin_formas(_agent):  # type: ignore[no-untyped-def]
        return None

    from pack_commerce import formas_de_pago

    monkeypatch.setattr(formas_de_pago, "metodos_configurados", _sin_formas)
    _politica(monkeypatch, {"metodos_entrega": ["domicilio"]})
    refs = await skills._referencias_del_sandbox(uuid.uuid4())
    assert [renderizar(r) for r in refs] == [
        "- metodos_entrega: domicilio=si, recoger_en_tienda=no"
    ]

"""Las tools `sub_*` se ofrecen aunque las skills del core se registren ANTES de activar
los packs (arranque en frío).

`core/main.py` registra las skills del core al importar el módulo y activa los packs
después, en el lifespan; `evals/journeys.py` sobre una base recién migrada tampoco tiene
el proveedor registrado al registrar el core. Antes, el predicado de oferta congelaba los
proveedores de ese momento (ninguno) y las `sub_*` no se ofrecían nunca en ese proceso.
"""

from __future__ import annotations

import pytest

import core.agents.spec as spec_mod
from core.orchestrator.loop import _connections_ok
from core.subscriptions import subscription_registry

NOMBRES = {"sub_consultar", "sub_link_pago", "sub_pausar", "sub_cancelar", "sub_cambiar_plan"}
# Las del procedimiento `cambiar_plan` (HU-AOS-08): mismo predicado, pero no `always_available`.
NOMBRES |= {"sub_cotizar_cambio_plan", "sub_aplicar_cambio_plan"}


class _Spec:
    def __init__(self, packs: set[str]) -> None:
        self.packs = packs


@pytest.fixture()
def registro_vacio():
    """El registro de proveedores como en un proceso recién arrancado (sin packs activos)."""
    previos = {
        pid: subscription_registry.get(pid)
        for pid in subscription_registry.registered_provider_ids()
    }
    for pid in previos:
        subscription_registry.unregister(pid)
    yield
    for pid in subscription_registry.registered_provider_ids():
        subscription_registry.unregister(pid)
    for pid, cap in previos.items():
        if cap is not None:
            subscription_registry.register(pid, cap)


@pytest.fixture()
def negocio_con_suscripciones(monkeypatch):
    async def _spec() -> _Spec:
        return _Spec({"pack_subscriptions"})

    monkeypatch.setattr(spec_mod, "load_active_spec", _spec)


async def _ofrecidas(host) -> set[str]:  # type: ignore[no-untyped-def]
    tools = host.get_skill_tools(["subscriptions"])
    return {t.name for t in tools if await _connections_ok(t, set())}


@pytest.mark.asyncio
async def test_core_registrado_antes_de_activar_el_pack(
    registro_vacio, negocio_con_suscripciones
) -> None:
    from core.mcp.host import MCPHost
    from core.tools.core_skill import register_core_skills

    host = MCPHost()
    register_core_skills(host)  # 1. skills del core, sin proveedores todavía
    assert await _ofrecidas(host) == set()

    subscription_registry.register("pack_subscriptions", object())  # type: ignore[arg-type]  # 2. on_activate del pack
    assert await _ofrecidas(host) == NOMBRES

    subscription_registry.unregister("pack_subscriptions")  # on_deactivate: deja de ofrecerse
    assert await _ofrecidas(host) == set()


@pytest.mark.asyncio
async def test_orden_del_servidor_api(registro_vacio, negocio_con_suscripciones) -> None:
    """El orden real de `core/main.py`: el import registra el core; el lifespan activa los packs."""
    import core.main  # noqa: F401 -- registra las skills del core en `mcp_host`
    from core.mcp.host import mcp_host

    subscription_registry.register("pack_subscriptions", object())  # type: ignore[arg-type]
    assert await _ofrecidas(mcp_host) == NOMBRES


@pytest.mark.asyncio
async def test_sin_el_pack_en_el_spec_no_se_ofrecen(registro_vacio, monkeypatch) -> None:
    from core.mcp.host import MCPHost
    from core.tools.core_skill import register_core_skills

    async def _spec() -> _Spec:
        return _Spec({"pack_commerce"})

    monkeypatch.setattr(spec_mod, "load_active_spec", _spec)
    host = MCPHost()
    register_core_skills(host)
    subscription_registry.register("pack_subscriptions", object())  # type: ignore[arg-type]
    assert await _ofrecidas(host) == set()


@pytest.mark.asyncio
async def test_sin_negocio_en_contexto_no_se_ofrecen(registro_vacio) -> None:
    """Fail-closed con el `load_active_spec` real: sin tenant en contexto, no se ofrecen."""
    from core.mcp.host import MCPHost
    from core.tools.core_skill import register_core_skills

    host = MCPHost()
    register_core_skills(host)
    subscription_registry.register("pack_subscriptions", object())  # type: ignore[arg-type]
    assert await _ofrecidas(host) == set()

"""HU-SUB-01 / ADR-0106: selección del proveedor de suscripciones.

`capability_for_tenant` resuelve entre los proveedores habilitados; dos sin
elección explícita lanzan `SubscriptionProviderAmbiguity` (config visible en
el panel)."""
from __future__ import annotations

import uuid

import pytest

from core.plugin_manager import plugin_manager
from core.subscriptions import (
    SubscriptionError,
    SubscriptionProviderAmbiguity,
    SubscriptionRegistry,
)


class _Cap:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def activate(self, **_k):
        raise NotImplementedError


@pytest.fixture()
def registro(monkeypatch):
    """`activos` son los packs del negocio: `packs_del_negocio` (HU-SUB-09, ADR-0206 §5)."""
    import core.agents.instalaciones_agente as inst_mod

    reg = SubscriptionRegistry()
    activos: set[str] = set()

    async def _packs(_tenant_id=None, *, con_core=True):
        return set(activos)

    monkeypatch.setattr(inst_mod, "packs_del_negocio", _packs)
    return reg, activos


@pytest.mark.asyncio
async def test_habilitado_es_por_negocio_no_por_proceso(registro, monkeypatch) -> None:
    """HU-SUB-09: un pack activo en el proceso pero no en el AgentSpec no cuenta."""
    reg, _ = registro
    monkeypatch.setattr(plugin_manager, "is_active", lambda _pid: True)
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    reg.register("pack_cobranza", _Cap("pack_cobranza"))
    assert await reg.enabled_for_tenant(uuid.uuid4()) == []
    assert await reg.capability_for_tenant(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_sin_proveedores_habilitados_es_none(registro) -> None:
    reg, _ = registro
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    assert await reg.capability_for_tenant(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_un_solo_proveedor_habilitado(registro) -> None:
    reg, activos = registro
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    activos.add("pack_subscriptions")
    cap = await reg.capability_for_tenant(uuid.uuid4())
    assert cap is not None
    assert cap.provider_id == "pack_subscriptions"


@pytest.mark.asyncio
async def test_dos_proveedores_sin_eleccion_es_ambiguedad(registro) -> None:
    reg, activos = registro
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    reg.register("pack_cobranza", _Cap("pack_cobranza"))
    activos.update({"pack_subscriptions", "pack_cobranza"})
    with pytest.raises(SubscriptionProviderAmbiguity) as exc:
        await reg.capability_for_tenant(uuid.uuid4())
    assert exc.value.code == "proveedor_ambiguo"
    assert set(exc.value.provider_ids) == {"pack_cobranza", "pack_subscriptions"}


@pytest.mark.asyncio
async def test_proveedor_explicito_desempata(registro) -> None:
    reg, activos = registro
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    reg.register("pack_cobranza", _Cap("pack_cobranza"))
    activos.update({"pack_subscriptions", "pack_cobranza"})
    cap = await reg.capability_for_tenant(uuid.uuid4(), provider="pack_cobranza")
    assert cap is not None and cap.provider_id == "pack_cobranza"


@pytest.mark.asyncio
async def test_proveedor_explicito_no_registrado_error(registro) -> None:
    reg, _ = registro
    with pytest.raises(SubscriptionError) as exc:
        await reg.capability_for_tenant(uuid.uuid4(), provider="inexistente")
    assert exc.value.code == "proveedor_no_registrado"


@pytest.mark.asyncio
async def test_proveedor_explicito_deshabilitado_error(registro) -> None:
    reg, _ = registro
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    with pytest.raises(SubscriptionError) as exc:
        await reg.capability_for_tenant(uuid.uuid4(), provider="pack_subscriptions")
    assert exc.value.code == "proveedor_deshabilitado"

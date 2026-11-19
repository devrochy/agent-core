"""HU-SUB-09 · elección del proveedor de suscripciones por configuración (sin BD)."""

from __future__ import annotations

import pathlib
import uuid

import pytest

import core.subscriptions as subs


class _Cap:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id


@pytest.fixture()
def negocio(monkeypatch):
    """`estado["habilitados"]` y `estado["configurado"]` simulan AgentSpec y settings."""
    reg = subs.SubscriptionRegistry()
    estado: dict = {"habilitados": [], "configurado": None}

    async def _habilitados(_tid):
        return sorted(p for p in reg._caps if p in estado["habilitados"])

    async def _configurado(_tid):
        return estado["configurado"]

    monkeypatch.setattr(reg, "enabled_for_tenant", _habilitados)
    monkeypatch.setattr(subs, "subscription_registry", reg)
    monkeypatch.setattr(subs, "proveedor_configurado", _configurado)
    reg.register("pack_subscriptions", _Cap("pack_subscriptions"))
    reg.register("pack_cobranza", _Cap("pack_cobranza"))
    return estado


@pytest.mark.asyncio
async def test_eleccion_por_configuracion(negocio) -> None:
    negocio["habilitados"] = ["pack_subscriptions", "pack_cobranza"]
    negocio["configurado"] = "pack_cobranza"
    cap = await subs.capability_del_negocio(uuid.uuid4())
    assert cap is not None and cap.provider_id == "pack_cobranza"
    estado = await subs.estado_proveedor(uuid.uuid4())
    assert estado["estado"] == "ok"
    assert estado["efectivo"] == "pack_cobranza"


@pytest.mark.asyncio
async def test_dos_sin_eleccion_es_ambiguo(negocio) -> None:
    negocio["habilitados"] = ["pack_subscriptions", "pack_cobranza"]
    with pytest.raises(subs.SubscriptionProviderAmbiguity):
        await subs.capability_del_negocio(uuid.uuid4())
    estado = await subs.estado_proveedor(uuid.uuid4())
    assert estado["estado"] == "ambiguo"
    assert "Elige uno" in estado["mensaje"]


@pytest.mark.asyncio
async def test_uno_solo_no_necesita_eleccion(negocio) -> None:
    negocio["habilitados"] = ["pack_subscriptions"]
    estado = await subs.estado_proveedor(uuid.uuid4())
    assert (estado["estado"], estado["efectivo"]) == ("ok", "pack_subscriptions")


@pytest.mark.asyncio
async def test_eleccion_deshabilitada_es_invalida(negocio) -> None:
    negocio["habilitados"] = ["pack_subscriptions"]
    negocio["configurado"] = "pack_cobranza"
    estado = await subs.estado_proveedor(uuid.uuid4())
    assert estado["estado"] == "invalido"


@pytest.mark.asyncio
async def test_sin_proveedor_explica_que_activar(negocio) -> None:
    estado = await subs.estado_proveedor(uuid.uuid4())
    assert estado["estado"] == "sin_proveedor"
    assert "Suscripciones" in estado["mensaje"] and "Cobranza" in estado["mensaje"]


def test_commerce_no_nombra_al_proveedor() -> None:
    """Gherkin «Sin nombre fijo»: commerce no escribe ningún pack de suscripciones."""
    raiz = pathlib.Path(__file__).resolve().parents[1] / "packages/pack-commerce/pack_commerce"
    culpables = [
        str(f.relative_to(raiz))
        for f in raiz.rglob("*.py")
        if "pack_subscriptions" in f.read_text() or "pack_cobranza" in f.read_text()
    ]
    assert culpables == []

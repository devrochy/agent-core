"""Una Página de Facebook o una cuenta de Instagram pertenecen a un solo negocio (ADR-0152).

Unitarios sin BD ni red: el candado y la pregunta de reclamo (`claim_messaging_assets`) y el
ruteo del webhook de `page` e `instagram` (conflicto cuando lo reclama más de un negocio). Lo
que depende de las funciones SQL reales está en
`tests/integration/test_meta_paginas_cuentas_unicas.py`.
"""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_asset_claims as claims_mod
import core.gateway.webhook_route_tenant as route_mod
from core.gateway.meta_asset_claims import (
    CLAIMED_MESSAGING_MESSAGE,
    MetaAssetError,
    claim_messaging_assets,
)
from core.gateway.webhook_flatten import FlattenedEvent


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _Session:
    def __init__(self, claimed: bool):
        self.claimed = claimed
        self.sql: list[tuple[str, dict]] = []

    async def execute(self, stmt, params):
        self.sql.append((str(stmt), params))
        return _Result(self.claimed if "meta_messaging_asset_claimed_by_other_tenant" in str(stmt) else None)


@pytest.mark.asyncio
async def test_claim_toma_un_candado_por_activo_en_orden_y_luego_pregunta():
    session = _Session(claimed=False)
    tid = uuid.uuid4()

    await claim_messaging_assets(session, tid, page_id=" page-1 ", ig_user_id="ig-1")

    candados = [p["k"] for sql, p in session.sql if "pg_advisory_xact_lock" in sql]
    assert candados == ["meta_asset:page:page-1", "meta_asset:ig:ig-1"]
    sql, params = session.sql[-1]
    assert "meta_messaging_asset_claimed_by_other_tenant" in sql
    assert params == {"p": "page-1", "i": "ig-1", "t": tid}


@pytest.mark.asyncio
async def test_claim_de_un_activo_ajeno_falla_sin_decir_de_quien_es():
    session = _Session(claimed=True)

    with pytest.raises(MetaAssetError) as exc:
        await claim_messaging_assets(session, uuid.uuid4(), page_id="page-1")

    assert exc.value.code == "asset_claimed"
    assert exc.value.message == CLAIMED_MESSAGING_MESSAGE
    assert [p.get("k") for _, p in session.sql if "k" in p] == ["meta_asset:page:page-1"]


@pytest.mark.asyncio
async def test_claim_sin_activos_no_toma_candados():
    session = _Session(claimed=False)

    await claim_messaging_assets(session, uuid.uuid4(), page_id="  ", ig_user_id=None)

    assert not [1 for sql, _ in session.sql if "pg_advisory_xact_lock" in sql]


# ------------------------------------------------------------------ ruteo
def _evento(object_type: str, entry_id: str) -> FlattenedEvent:
    return FlattenedEvent(
        object_type=object_type, field="messages", external_id="m-1", entry_id=entry_id, sub_id=None, value={}
    )


def _stub(monkeypatch, *, resuelve: uuid.UUID | None, reclamado: bool, llamadas: list) -> None:
    async def por_pagina(_page_id):
        return resuelve

    async def por_cuenta(_ig_user_id):
        return resuelve

    async def reclamado_por_otro(tenant_id, *, page_id=None, ig_user_id=None):
        llamadas.append((tenant_id, page_id, ig_user_id))
        return reclamado

    monkeypatch.setattr(route_mod, "_resolve_tenant_for_page", por_pagina)
    monkeypatch.setattr(route_mod, "_resolve_tenant_for_ig_user", por_cuenta)
    monkeypatch.setattr(claims_mod, "messaging_asset_claimed_by_other_tenant", reclamado_por_otro)


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type", ["page", "instagram"])
async def test_un_activo_con_un_dueno_rutea_a_su_negocio(monkeypatch, object_type):
    tenant = uuid.uuid4()
    llamadas: list = []
    _stub(monkeypatch, resuelve=tenant, reclamado=False, llamadas=llamadas)

    route = await route_mod.route_event(_evento(object_type, "activo-1"))

    assert (route.tenant_id, route.conflict) == (tenant, False)
    assert llamadas == []  # con dueño no se pregunta nada más


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_type", "esperado"),
    [("page", (None, "activo-1", None)), ("instagram", (None, None, "activo-1"))],
)
async def test_un_activo_con_dos_duenos_es_un_conflicto(monkeypatch, object_type, esperado):
    llamadas: list = []
    _stub(monkeypatch, resuelve=None, reclamado=True, llamadas=llamadas)

    route = await route_mod.route_event(_evento(object_type, "activo-1"))

    assert (route.tenant_id, route.conflict) == (None, True)
    assert llamadas == [esperado]


@pytest.mark.asyncio
@pytest.mark.parametrize("object_type", ["page", "instagram"])
async def test_un_activo_de_nadie_no_es_un_conflicto(monkeypatch, object_type):
    _stub(monkeypatch, resuelve=None, reclamado=False, llamadas=[])

    route = await route_mod.route_event(_evento(object_type, "activo-1"))

    assert (route.tenant_id, route.conflict) == (None, False)

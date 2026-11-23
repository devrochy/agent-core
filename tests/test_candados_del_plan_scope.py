"""Los candados por plan con la sesión del caller exigen el `tenant_scope` del negocio (revisión de SPEC-51).

Con otro negocio en contexto, la RLS devolvería "sin suscripción" en silencio y el candado se abriría. Sin sesión,
`get_active_subscription` abre su propio scope y no hace falta exigirlo (crons, altas)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.shared.tenant import tenant_scope


def _candados():  # type: ignore[no-untyped-def]
    from core.billing.campanas_del_plan import RECORDATORIOS, plan_que_frena
    from core.billing.canales_del_plan import canal_permitido
    from core.billing.features_del_plan import marca_propia_del_negocio
    from core.billing.habilidades_del_plan import exigir_min_plan

    return {
        "features": lambda tid, s: marca_propia_del_negocio(tid, session=s),
        "canales": lambda tid, s: canal_permitido(tid, "whatsapp", session=s),
        "campanas": lambda tid, s: plan_que_frena(tid, RECORDATORIOS, session=s),
        "habilidades": lambda tid, s: exigir_min_plan(tid, ["pack_x"], session=s),
    }


@pytest.fixture
def sin_suscripcion(monkeypatch):  # type: ignore[no-untyped-def]
    llamados: list[object] = []

    async def get_active_subscription(tenant_id, *, session=None):  # type: ignore[no-untyped-def]
        llamados.append(session)
        return None

    monkeypatch.setattr("core.billing.service.get_active_subscription", get_active_subscription)
    monkeypatch.setattr("core.billing.habilidades_del_plan._min_planes", lambda packs: {"pack_x": "premium"})
    return llamados


@pytest.mark.parametrize("candado", ["features", "canales", "campanas", "habilidades"])
async def test_con_la_sesion_de_otro_negocio_falla(candado: str, sin_suscripcion) -> None:  # type: ignore[no-untyped-def]
    a, b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(RuntimeError, match="tenant_scope"):
        async with tenant_scope(b):
            await _candados()[candado](a, SimpleNamespace())
    with pytest.raises(RuntimeError, match="tenant_scope"):
        await _candados()[candado](a, SimpleNamespace())  # sin scope
    assert sin_suscripcion == []


@pytest.mark.parametrize("candado", ["features", "canales", "campanas", "habilidades"])
async def test_en_su_scope_o_sin_sesion_sigue_igual(candado: str, sin_suscripcion) -> None:  # type: ignore[no-untyped-def]
    a = uuid.uuid4()
    sesion = SimpleNamespace()
    async with tenant_scope(a):
        await _candados()[candado](a, sesion)
    await _candados()[candado](a, None)  # sin sesión: abre su propio scope
    assert sin_suscripcion == [sesion, None]

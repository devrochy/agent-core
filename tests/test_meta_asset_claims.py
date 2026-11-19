"""Un número o una WABA de Meta pertenecen a un solo negocio (ADR-0095).

Unitarios sin BD ni red: la verificación contra Graph (`verify_waba_and_phone`), el
candado y la pregunta de reclamo (`claim_assets`) y el orden de ruteo del webhook
(número primero). Lo que depende de las funciones SQL reales está en
`tests/integration/test_meta_reclamo_waba_numero.py`.
"""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_asset_claims as claims_mod
import core.gateway.webhook_route_tenant as route_mod
from core.gateway.meta_asset_claims import (
    CLAIMED_MESSAGE,
    MetaAssetError,
    claim_assets,
    claimed_elsewhere,
    verify_waba_and_phone,
)
from core.gateway.meta_errors import GraphError
from core.gateway.webhook_flatten import FlattenedEvent

_VERIFY = {"token": "tok", "app_secret": "sec", "version": "v21.0", "connection_id": None}


# ------------------------------------------------------------------ verificación en Graph
@pytest.mark.asyncio
async def test_el_numero_de_la_waba_pasa_y_devuelve_lo_que_dice_meta(monkeypatch):
    async def fake_list(waba_id, token, app_secret, version, connection_id):
        assert (waba_id, token, app_secret) == ("waba-1", "tok", "sec")
        return [{"id": "otro"}, {"id": "phone-1", "display_phone_number": "+57 300"}]

    monkeypatch.setattr(claims_mod, "list_phone_numbers", fake_list)

    numero = await verify_waba_and_phone(waba_id="waba-1", phone_number_id="phone-1", **_VERIFY)

    assert numero["display_phone_number"] == "+57 300"


@pytest.mark.asyncio
async def test_un_numero_que_no_es_de_la_waba_se_rechaza(monkeypatch):
    async def fake_list(*_a):
        return [{"id": "phone-de-la-waba"}]

    monkeypatch.setattr(claims_mod, "list_phone_numbers", fake_list)

    with pytest.raises(MetaAssetError) as exc:
        await verify_waba_and_phone(waba_id="waba-1", phone_number_id="phone-ajeno", **_VERIFY)

    assert exc.value.code == "phone_not_in_waba"


@pytest.mark.asyncio
async def test_una_waba_que_el_token_no_ve_se_rechaza(monkeypatch):
    async def fake_list(*_a):
        raise GraphError("(#100) Unsupported get request", category="PERMANENT", http_status=400)

    monkeypatch.setattr(claims_mod, "list_phone_numbers", fake_list)

    with pytest.raises(MetaAssetError) as exc:
        await verify_waba_and_phone(waba_id="waba-ajena", phone_number_id="phone-1", **_VERIFY)

    assert exc.value.code == "waba_not_accessible"


@pytest.mark.asyncio
async def test_sin_numero_basta_con_que_el_token_vea_la_waba(monkeypatch):
    llamadas = []

    async def fake_fetch(path, **kwargs):
        llamadas.append((path, kwargs["query"]))
        return {"id": "waba-1"}

    async def no_list(*_a):
        raise AssertionError("sin número no se listan los números")

    monkeypatch.setattr(claims_mod, "graph_fetch", fake_fetch)
    monkeypatch.setattr(claims_mod, "list_phone_numbers", no_list)

    assert await verify_waba_and_phone(waba_id="waba-1", phone_number_id=None, **_VERIFY) == {}
    assert llamadas == [("/waba-1", {"fields": "id"})]


# ------------------------------------------------------------------ reclamo
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
        return _Result(self.claimed if "meta_asset_claimed_by_other_tenant" in str(stmt) else None)


@pytest.mark.asyncio
async def test_claim_toma_un_candado_por_activo_en_orden_y_luego_pregunta():
    session = _Session(claimed=False)
    tid = uuid.uuid4()

    await claim_assets(session, tid, waba_id=" waba-1 ", phone_number_id="phone-1")

    candados = [p["k"] for sql, p in session.sql if "pg_advisory_xact_lock" in sql]
    assert candados == ["meta_asset:waba:waba-1", "meta_asset:phone:phone-1"]
    sql, params = session.sql[-1]
    assert "meta_asset_claimed_by_other_tenant" in sql
    assert params == {"w": "waba-1", "p": "phone-1", "t": tid}


@pytest.mark.asyncio
async def test_claim_de_un_activo_ajeno_falla_sin_decir_de_quien_es():
    session = _Session(claimed=True)

    with pytest.raises(MetaAssetError) as exc:
        await claim_assets(session, uuid.uuid4(), waba_id="waba-1", phone_number_id=None)

    assert exc.value.code == "asset_claimed"
    assert exc.value.message == CLAIMED_MESSAGE
    assert [p.get("k") for _, p in session.sql if "k" in p] == ["meta_asset:waba:waba-1"]


@pytest.mark.asyncio
async def test_claimed_elsewhere_toma_el_candado_y_responde_sin_lanzar():
    """Sincronizar números que Graph lista en una WABA propia: si otro negocio tiene el
    número, el llamador no lo guarda (sigue con los demás) en vez de cortar la sincronización."""
    session = _Session(claimed=True)

    assert await claimed_elsewhere(
        session, uuid.uuid4(), phone_number_id="phone-1", source="sync_phone_numbers"
    ) is True
    assert [p.get("k") for _, p in session.sql if "k" in p] == ["meta_asset:phone:phone-1"]


# ------------------------------------------------------------------ orden de ruteo
def _evento(entry_id: str, sub_id: str | None) -> FlattenedEvent:
    return FlattenedEvent(
        object_type="whatsapp_business_account", field="messages", external_id="wamid.1",
        entry_id=entry_id, sub_id=sub_id, value={},
    )


@pytest.fixture
def ruteo(monkeypatch):
    """Dueños por número y por WABA; `*-doble` los reclaman dos negocios (el resolvedor da
    NULL, pero tienen dueño). Los demás IDs no tienen dueño."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    numeros = {"phone-de-a": tenant_a, "phone-de-b": tenant_b}
    wabas = {"waba-de-a": tenant_a, "waba-de-b": tenant_b}

    async def por_numero(phone_number_id):
        return numeros.get(phone_number_id)

    async def por_waba(waba_id):
        return wabas.get(waba_id)

    async def tiene_dueno(*, waba_id=None, phone_number_id=None):
        activo = waba_id or phone_number_id
        return activo in numeros or activo in wabas or activo.endswith("-doble")

    monkeypatch.setattr(route_mod, "resolve_tenant_for_phone", por_numero)
    monkeypatch.setattr(route_mod, "_resolve_tenant_for_waba", por_waba)
    monkeypatch.setattr(route_mod, "_has_owner", tiene_dueno)
    return tenant_a, tenant_b


async def _ruta(entry_id: str, sub_id: str | None) -> tuple[uuid.UUID | None, bool]:
    route = await route_mod.route_event(_evento(entry_id, sub_id))
    return route.tenant_id, route.conflict


@pytest.mark.asyncio
async def test_el_numero_decide_si_la_waba_no_es_de_otro(ruteo):
    tenant_a, _tenant_b = ruteo

    assert await _ruta("waba-sin-dueno", "phone-de-a") == (tenant_a, False)
    assert await _ruta("waba-de-a", "phone-de-a") == (tenant_a, False)
    assert await route_mod.resolve_tenant_for_event(_evento("waba-de-a", "phone-de-a")) == tenant_a


@pytest.mark.asyncio
async def test_numero_y_waba_de_negocios_distintos_es_conflicto(ruteo):
    """El caso del hallazgo y su espejo: si el número es de A y la WABA de B, una de las
    dos filas es ajena. Ni la WABA (el hallazgo original) ni el número (una fila heredada
    sin verificar) deciden solos."""
    assert await _ruta("waba-de-b", "phone-de-a") == (None, True)
    assert await _ruta("waba-de-a", "phone-de-b") == (None, True)
    assert await route_mod.resolve_tenant_for_event(_evento("waba-de-b", "phone-de-a")) is None


@pytest.mark.asyncio
async def test_un_numero_con_dos_duenos_no_deja_decidir_a_la_waba(ruteo):
    """Si un número ambiguo cayera a la WABA, quien la tiene podría provocar la ambigüedad
    (registrando el número) para quedarse con los mensajes."""
    assert await _ruta("waba-de-b", "phone-doble") == (None, True)


@pytest.mark.asyncio
async def test_sin_numero_o_numero_sin_dueno_decide_la_waba(ruteo):
    _tenant_a, tenant_b = ruteo

    assert await _ruta("waba-de-b", None) == (tenant_b, False)
    assert await _ruta("waba-de-b", "phone-sin-dueno") == (tenant_b, False)
    assert await _ruta("waba-sin-dueno", "phone-sin-dueno") == (None, False)
    assert await _ruta("waba-doble", None) == (None, True)
    assert await _ruta("waba-doble", "phone-sin-dueno") == (None, True)

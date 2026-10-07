"""Números del portafolio de Meta de una conexión (`meta_numbers_service.business_numbers`).

El límite de números de Meta cuenta TODAS las WABAs propias del portafolio; la vista los lista
para decidir cuál liberar. Con Graph, la BD y las credenciales falsos: lo que se prueba es el
recorrido (solo WABAs propias, nunca las de clientes), los avisos por partes, que con Embedded
Signup no se recorre el portafolio y que no se escribe nada en `phone_numbers`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

import core.admin.meta_numbers_service as service
import core.gateway.meta_phone_numbers as phone_numbers_mod
from core.gateway.meta_errors import classify_and_build_error

TENANT = uuid.uuid4()
CONNECTION = uuid.uuid4()
WABA = "waba-conexion"
BIZ = "biz-1"


def _graph_error(msg: str):
    body = {"error": {"message": "Permission", "code": 200, "error_user_msg": msg}}
    return classify_and_build_error(http_status=403, body=body, headers=httpx.Headers())


def _numero(pnid: str, display: str, status: str = "CONNECTED") -> dict:
    return {
        "id": pnid,
        "display_phone_number": display,
        "verified_name": "Negocio",
        "status": status,
        "code_verification_status": "VERIFIED",
        "platform_type": "CLOUD_API",
        "quality_rating": "GREEN",
        "name_status": "APPROVED",
    }


@pytest.fixture
def mundo(monkeypatch):
    """Portafolio con la WABA de la conexión y otra propia; registra cada llamada a Graph."""
    llamadas: list[str] = []
    estado = SimpleNamespace(
        origin="byok",
        owner={"id": BIZ, "name": "Mi portafolio"},
        owned_error=None,
        waba_errors={},
        numeros={
            WABA: [_numero("ph-1", "+57 300 000 0001")],
            "waba-otra": [_numero("ph-2", "+57 300 000 0002", "PENDING")],
        },
        en_conexion={"ph-1"},
        escrituras=[],
        de_otro_negocio=set(),
    )

    async def fake_resolve(cid, tenant_id):
        assert (cid, tenant_id) == (CONNECTION, TENANT)
        connection = SimpleNamespace(id=CONNECTION, tenant_id=TENANT, origin=estado.origin)
        creds = SimpleNamespace(access_token="tok", app_secret="sec", graph_api_version="v21.0")
        return connection, SimpleNamespace(waba_id=WABA), creds

    async def fake_graph_fetch(path, *, query=None, method="GET", **_kwargs):
        llamadas.append(path)
        assert method == "GET", "la vista es de solo lectura"
        if path == f"/{WABA}":
            return {"owner_business_info": estado.owner}
        if path == f"/{BIZ}/owned_whatsapp_business_accounts":
            if estado.owned_error is not None:
                raise estado.owned_error
            if (query or {}).get("after") == "c2":
                return {"data": [{"id": "waba-otra", "name": "Otra cuenta"}], "paging": {"cursors": {}}}
            return {
                "data": [{"id": WABA, "name": "Cuenta de la conexión"}],
                "paging": {"cursors": {"after": "c2"}, "next": "https://graph/next"},
            }
        if path.endswith("/phone_numbers"):
            waba_id = path.split("/")[1]
            if waba_id in estado.waba_errors:
                raise estado.waba_errors[waba_id]
            return {"data": estado.numeros.get(waba_id, []), "paging": {"cursors": {}}}
        raise AssertionError(f"path no esperado: {path}")

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def __iter__(self):
            return iter(self._values)

    class _Session:
        async def execute(self, stmt):
            where = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True})).replace("-", "")
            assert f"phone_numbers.connection_id = '{CONNECTION.hex}'" in where
            assert f"phone_numbers.tenant_id = '{TENANT.hex}'" in where
            return SimpleNamespace(scalars=lambda: _Scalars(sorted(estado.en_conexion)))

        def add(self, obj):
            estado.escrituras.append(obj)

    @asynccontextmanager
    async def fake_session():
        yield _Session()

    async def fake_claimed(tenant_id, *, waba_id=None, phone_number_id=None):
        assert tenant_id == TENANT
        return phone_number_id in estado.de_otro_negocio

    monkeypatch.setattr("core.gateway.meta_asset_claims.asset_claimed_by_other_tenant", fake_claimed)
    monkeypatch.setattr(service, "resolve_waba_and_credentials", fake_resolve)
    monkeypatch.setattr(service, "get_session", fake_session)
    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", fake_graph_fetch)
    return SimpleNamespace(llamadas=llamadas, estado=estado)


@pytest.mark.asyncio
async def test_lista_los_numeros_de_todas_las_wabas_propias_del_portafolio(mundo):
    data, error = await service.business_numbers(TENANT, str(CONNECTION))

    assert error is None
    assert (data["business_id"], data["business_name"]) == (BIZ, "Mi portafolio")
    assert data["warnings"] == []
    por_id = {n["phone_number_id"]: n for n in data["numbers"]}
    assert set(por_id) == {"ph-1", "ph-2"}
    assert (por_id["ph-1"]["waba_name"], por_id["ph-1"]["is_connection_waba"], por_id["ph-1"]["in_this_connection"]) == (
        "Cuenta de la conexión", True, True
    )
    assert (por_id["ph-2"]["waba_id"], por_id["ph-2"]["is_connection_waba"], por_id["ph-2"]["in_this_connection"]) == (
        "waba-otra", False, False
    )
    assert por_id["ph-2"]["status"] == "PENDING"
    # Siguió la paginación de las WABAs propias y nunca pidió las de clientes.
    assert mundo.llamadas.count(f"/{BIZ}/owned_whatsapp_business_accounts") == 2
    assert not any("client_whatsapp_business_accounts" in p for p in mundo.llamadas)


@pytest.mark.asyncio
async def test_no_escribe_nada_en_la_base(mundo):
    await service.business_numbers(TENANT, str(CONNECTION))
    assert mundo.estado.escrituras == []


@pytest.mark.asyncio
async def test_sin_permiso_sobre_el_portafolio_muestra_la_waba_de_la_conexion_con_aviso(mundo):
    mundo.estado.owned_error = _graph_error("Falta business_management")

    data, error = await service.business_numbers(TENANT, str(CONNECTION))

    assert error is None
    assert [n["phone_number_id"] for n in data["numbers"]] == ["ph-1"]
    (aviso,) = data["warnings"]
    assert "business_management" in aviso and "Falta business_management" in aviso


@pytest.mark.asyncio
async def test_una_waba_que_falla_deja_aviso_y_las_demas_siguen(mundo):
    mundo.estado.waba_errors = {"waba-otra": _graph_error("Sin acceso a esta cuenta")}

    data, _ = await service.business_numbers(TENANT, str(CONNECTION))

    assert [n["phone_number_id"] for n in data["numbers"]] == ["ph-1"]
    assert data["warnings"] == ["Cuenta Otra cuenta: Sin acceso a esta cuenta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["embedded_signup", "manual"])
async def test_con_token_de_la_plataforma_no_recorre_el_portafolio(mundo, origin):
    # Embedded Signup y manual usan un system user de la PLATAFORMA (ADR-0094): su portafolio
    # puede tener WABAs de otros negocios. Solo la WABA de la conexión.
    mundo.estado.origin = origin

    data, _ = await service.business_numbers(TENANT, str(CONNECTION))

    assert [n["phone_number_id"] for n in data["numbers"]] == ["ph-1"]
    assert mundo.llamadas == [f"/{WABA}/phone_numbers"]
    assert data["business_id"] is None
    assert len(data["warnings"]) == 1


@pytest.mark.asyncio
async def test_los_numeros_de_otro_negocio_de_la_plataforma_cuentan_pero_no_se_muestran(mundo):
    # Un token de agencia (BYOK) ve WABAs de varios clientes.
    mundo.estado.de_otro_negocio = {"ph-2"}

    data, _ = await service.business_numbers(TENANT, str(CONNECTION))

    assert [n["phone_number_id"] for n in data["numbers"]] == ["ph-1"]
    assert data["other_tenants_count"] == 1
    assert "+57 300 000 0002" not in str(data)
    assert any("otro negocio" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_conexion_invalida(mundo):
    assert await service.business_numbers(TENANT, "no-es-uuid") == (None, "Conexión inválida")
    assert mundo.llamadas == []


@pytest.mark.asyncio
async def test_el_tope_de_wabas_corta_la_paginacion(monkeypatch):
    llamadas: list[str] = []

    async def fake_graph_fetch(path, *, query=None, **_kwargs):
        llamadas.append(path)
        n = len(llamadas)
        return {
            "data": [{"id": f"w{n}-{i}"} for i in range(30)],
            "paging": {"cursors": {"after": f"c{n}"}, "next": "https://graph/next"},
        }

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", fake_graph_fetch)
    wabas = await phone_numbers_mod.list_owned_wabas(BIZ, "tok", "sec", "v21.0", CONNECTION)

    assert len(wabas) == phone_numbers_mod._MAX_OWNED_WABAS
    assert len(llamadas) == 2

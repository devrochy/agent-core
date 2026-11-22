"""HU-BILL-23 · recargas de créditos por Wompi, sin base (ADR-0209 §4 y §5).

- precio de los paquetes con el descuento de cada plan (D-79), en pesos enteros;
- quién puede comprar: Free, sin suscripción o sin suscripción activa no;
- el link de Wompi de una recarga dice «Recarga de N créditos» y el mínimo del sandbox da un error claro;
- la ruta `POST /admin/plan/recargas`: 400 con un paquete desconocido y 409 `recarga_no_disponible` /
  `recarga_bajo_minimo`, con el mismo permiso que `POST /subscribe`.

El flujo contra Postgres (pago, webhook firmado, abono, idempotencia, anulación y aislamiento) está en
`tests/integration/test_recargas_wompi.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.billing.checkout import RecargaBajoElMinimo, RecargaNoDisponible, recarga_disponible
from core.billing.plans import (
    PAQUETES_DE_RECARGA,
    get_paquete_de_recarga,
    get_plan,
    precio_de_recarga,
)
from core.billing.providers.base import BillingRequestRejected, MontoBajoElMinimo
from core.billing.providers.wompi import WompiBillingProvider
from core.db.models import TenantSubscription


def _plan(plan_id: str):  # type: ignore[no-untyped-def]
    plan = get_plan(plan_id)
    assert plan is not None
    return plan


# ------------------------------------------------------------------ catálogo y precio
def test_paquetes_del_catalogo_d79() -> None:
    assert [(p.id, p.creditos, p.precio_cop) for p in PAQUETES_DE_RECARGA] == [
        ("recarga_5000", 5_000, 55_000),
        ("recarga_20000", 20_000, 199_000),
    ]


@pytest.mark.parametrize(
    ("plan_id", "precio_5000", "precio_20000"),
    [
        ("basic", 55_000, 199_000),  # 0 %
        ("premium", 46_750, 169_150),  # 15 %
        ("empresarial", 41_250, 149_250),  # 25 %
    ],
)
def test_precio_con_el_descuento_del_plan(plan_id: str, precio_5000: int, precio_20000: int) -> None:
    plan = _plan(plan_id)
    p5 = get_paquete_de_recarga("recarga_5000")
    p20 = get_paquete_de_recarga("recarga_20000")
    assert p5 is not None and p20 is not None
    assert precio_de_recarga(p5, plan) == precio_5000
    assert precio_de_recarga(p20, plan) == precio_20000
    assert isinstance(precio_de_recarga(p5, plan), int)


def test_free_no_compra_recargas() -> None:
    p5 = get_paquete_de_recarga("recarga_5000")
    assert p5 is not None
    assert precio_de_recarga(p5, _plan("free")) is None


def test_el_redondeo_es_a_pesos_enteros_con_mitades_hacia_arriba() -> None:
    from dataclasses import replace

    from core.billing.plans import PaqueteDeRecarga

    premium = _plan("premium")  # 15 %
    # 10.001 × 0,85 = 8.500,85 → 8.501; 10.003 × 0,85 = 8.502,55 → 8.503; 10 × 0,85 = 8,5 → 9.
    assert precio_de_recarga(PaqueteDeRecarga("x", 1, 10_001), premium) == 8_501
    assert precio_de_recarga(PaqueteDeRecarga("x", 1, 10_003), premium) == 8_503
    assert precio_de_recarga(PaqueteDeRecarga("x", 1, 10), premium) == 9
    raro = replace(premium, features={**premium.features, "descuento_recarga": 150})
    assert precio_de_recarga(PaqueteDeRecarga("x", 1, 10), raro) is None


# ------------------------------------------------------------------ quién compra
def _sub(plan_id: str, status: str = "active") -> TenantSubscription:
    return TenantSubscription(tenant_id=uuid.uuid4(), plan_id=plan_id, status=status)


def test_sin_suscripcion_o_en_free_no_puede_recargar() -> None:
    assert recarga_disponible(None) is None
    assert recarga_disponible(_sub("free")) is None
    assert recarga_disponible(_sub("free", "trialing")) is None


@pytest.mark.parametrize("status", ["past_due", "expired", "canceled", "trialing"])
def test_sin_suscripcion_activa_no_puede_recargar(status: str) -> None:
    assert recarga_disponible(_sub("premium", status)) is None


@pytest.mark.parametrize("plan_id", ["basic", "premium", "empresarial"])
def test_un_plan_pago_activo_puede_recargar(plan_id: str) -> None:
    plan = recarga_disponible(_sub(plan_id))
    assert plan is not None and plan.id == plan_id


# ------------------------------------------------------------------ link de Wompi
def _provider(*, sandbox: bool) -> WompiBillingProvider:
    return WompiBillingProvider(public_key="pub", private_key="prv", events_secret="sec", sandbox=sandbox)


def _cliente_falso(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    capturado: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"data": {"id": "link-rec"}}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            capturado["json"] = json
            return _Resp()

    monkeypatch.setattr("core.billing.providers.wompi.httpx.AsyncClient", _Client)
    return capturado


@pytest.mark.asyncio
async def test_el_link_de_una_recarga_dice_recarga_y_no_suscripcion(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado = _cliente_falso(monkeypatch)
    await _provider(sandbox=False).create_payment_link(
        reference="r", amount_cop=46_750, plan_name="Premium", concepto="recarga",
        descripcion="Recarga de 5.000 créditos — SolversHub",
    )
    body = capturado["json"]
    assert body["description"] == "Recarga de 5.000 créditos — SolversHub"
    assert "Suscripción" not in f"{body['name']} {body['description']}"
    assert "SolversHub" in body["name"]
    assert body["amount_in_cents"] == 4_675_000
    assert body["single_use"] is True


@pytest.mark.asyncio
async def test_el_link_del_plan_no_cambia(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado = _cliente_falso(monkeypatch)
    await _provider(sandbox=False).create_payment_link(reference="r", amount_cop=299_000, plan_name="Premium")
    assert capturado["json"]["description"] == "Suscripción a SolversHub — plan Premium"
    assert capturado["json"]["name"] == "Premium · SolversHub"


@pytest.mark.asyncio
async def test_en_sandbox_una_recarga_bajo_el_minimo_se_rechaza_antes_de_la_red(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado = _cliente_falso(monkeypatch)
    with pytest.raises(MontoBajoElMinimo) as exc:
        await _provider(sandbox=True).create_payment_link(reference="r", amount_cop=46_750, concepto="recarga")
    assert exc.value.minimo_cop == 150_000
    assert "recarga" in str(exc.value).lower()
    assert isinstance(exc.value, BillingRequestRejected)
    assert capturado == {}


def test_el_error_del_sandbox_dice_precio_y_minimo() -> None:
    error = RecargaBajoElMinimo(46_750, 150_000)
    assert error.code == "recarga_bajo_minimo"
    assert "$46.750" in str(error) and "$150.000" in str(error)


# ------------------------------------------------------------------ ruta
def _cliente(monkeypatch: pytest.MonkeyPatch, *, rol: str = "owner", acceso: str | None = "write") -> TestClient:
    from fastapi.exceptions import RequestValidationError

    from core.api import deps
    from core.api.errors import ApiError, api_error_handler, validation_error_handler
    from core.api.routers import plan as router_mod
    from core.db.models import Admin

    async def principal():  # type: ignore[no-untyped-def]
        admin = Admin(username="x", email="x@x.test", password_hash="x", role=rol, active=True)
        admin.id = uuid.uuid4()
        yield deps.Principal(admin=admin, tenant_id=uuid.uuid4(), tenants=[], permissions={})

    async def acceso_efectivo(_admin, _section, _tenant_id):  # type: ignore[no-untyped-def]
        return acceso

    monkeypatch.setattr(deps, "effective_access", acceso_efectivo)
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[deps.get_principal] = principal
    return TestClient(app)


def _start_recarga(monkeypatch: pytest.MonkeyPatch, resultado: Any) -> list[str]:
    import core.billing.checkout as checkout

    pedidos: list[str] = []

    async def falso(paquete_id: str) -> str | None:
        pedidos.append(paquete_id)
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    monkeypatch.setattr(checkout, "start_recarga", falso)
    return pedidos


def test_la_ruta_devuelve_el_link(monkeypatch: pytest.MonkeyPatch) -> None:
    pedidos = _start_recarga(monkeypatch, "https://checkout.wompi.co/l/abc")
    r = _cliente(monkeypatch).post("/api/v1/admin/plan/recargas", json={"paquete": "recarga_5000"})
    assert r.status_code == 200, r.text
    assert r.json() == {"checkout_url": "https://checkout.wompi.co/l/abc"}
    assert pedidos == ["recarga_5000"]


def test_free_o_sin_suscripcion_da_409(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_recarga(monkeypatch, RecargaNoDisponible())
    r = _cliente(monkeypatch).post("/api/v1/admin/plan/recargas", json={"paquete": "recarga_5000"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "recarga_no_disponible"


def test_bajo_el_minimo_del_sandbox_da_409_claro(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_recarga(monkeypatch, RecargaBajoElMinimo(46_750, 150_000))
    r = _cliente(monkeypatch).post("/api/v1/admin/plan/recargas", json={"paquete": "recarga_5000"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "recarga_bajo_minimo"


def test_un_rechazo_del_proveedor_no_filtra_el_detalle(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_recarga(monkeypatch, BillingRequestRejected("Wompi rechazó el link de pago: 422 {secreto}"))
    r = _cliente(monkeypatch).post("/api/v1/admin/plan/recargas", json={"paquete": "recarga_5000"})
    assert r.status_code == 502
    assert "secreto" not in r.text


def test_paquete_desconocido_da_400_sin_llamar_al_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    pedidos = _start_recarga(monkeypatch, "https://x")
    r = _cliente(monkeypatch).post("/api/v1/admin/plan/recargas", json={"paquete": "recarga_1"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_package"
    assert pedidos == []


@pytest.mark.parametrize("acceso", ["read", None])
def test_sin_permiso_de_escritura_en_plan_da_403(monkeypatch: pytest.MonkeyPatch, acceso: str | None) -> None:
    pedidos = _start_recarga(monkeypatch, "https://x")
    r = _cliente(monkeypatch, acceso=acceso).post("/api/v1/admin/plan/recargas", json={"paquete": "recarga_5000"})
    assert r.status_code == 403
    assert pedidos == []

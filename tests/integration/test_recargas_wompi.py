"""HU-BILL-23 · recargas de créditos por Wompi contra el stack real (ADR-0209 §4 y §5).

- `POST /admin/plan/recargas` crea el pago (`concepto = 'recarga'`, sin período, plan vigente, monto con
  descuento) y devuelve el link; Free o sin suscripción activa da 409 `recarga_no_disponible`; bajo el
  mínimo del sandbox, 409 `recarga_bajo_minimo` sin dejar fila;
- el webhook firmado de Wompi sandbox (monto ≥ 150.000) suma los créditos al saldo del ciclo, el negocio
  bloqueado vuelve a atender, no toca período, plan ni estado, es idempotente y no se reenvía al negocio
  que vende los planes;
- la anulación de pendientes al cambiar de plan no toca un link de recarga;
- un pago del plan sigue igual (`concepto = 'plan'`, sin abono);
- aislamiento: un negocio no ve ni aplica las recargas de otro.

Sin LLM ni red: el proveedor es el `WompiBillingProvider` real (mínimo por ambiente y firma del webhook) con
la llamada HTTP de `POST /payment_links` simulada.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_SECRETO_DE_EVENTOS = "secreto-de-eventos-recargas"
UN_MES = timedelta(days=30)


# ------------------------------------------------------------------ dobles
class _Pasarela:
    """`WompiBillingProvider` real (mínimo del ambiente, nombre y descripción del link, firma del webhook)
    con `POST /payment_links` simulado: guarda cada cuerpo enviado y devuelve un id de link nuevo."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, sandbox: bool) -> None:
        from core.billing.providers.wompi import WompiBillingProvider

        self.proveedor = WompiBillingProvider(
            public_key="pub", private_key="prv", events_secret=_SECRETO_DE_EVENTOS, sandbox=sandbox
        )
        self.cuerpos: list[dict[str, Any]] = []
        self.links: list[str] = []
        pasarela = self

        class _Resp:
            status_code = 200

            def __init__(self, link: str) -> None:
                self._link = link

            def json(self) -> dict[str, Any]:
                return {"data": {"id": self._link}}

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
                link = f"link-{uuid.uuid4().hex[:10]}"
                pasarela.cuerpos.append(json)
                pasarela.links.append(link)
                return _Resp(link)

        monkeypatch.setattr("core.billing.providers.wompi.httpx.AsyncClient", _Client)


def _instalar(monkeypatch: pytest.MonkeyPatch, *, sandbox: bool) -> _Pasarela:
    import core.billing.checkout as checkout
    import core.billing.providers as proveedores

    pasarela = _Pasarela(monkeypatch, sandbox=sandbox)
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: pasarela.proveedor)
    monkeypatch.setattr(proveedores, "get_billing_provider", lambda: pasarela.proveedor)
    return pasarela


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> _Pasarela:
    return _instalar(monkeypatch, sandbox=True)


@pytest.fixture
def produccion(monkeypatch: pytest.MonkeyPatch) -> _Pasarela:
    return _instalar(monkeypatch, sandbox=False)


@pytest.fixture
def alertas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    enviadas: list[dict[str, str]] = []

    async def alerta(title: str, detail: str, severity: str = "warning") -> None:
        enviadas.append({"title": title, "detail": detail, "severity": severity})

    monkeypatch.setattr("core.shared.alerts.send_alert", alerta)
    return enviadas


@pytest.fixture
def reenvios(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Cuenta de Wompi compartida (R-05) con un negocio vendedor cualquiera; captura lo que el webhook de la
    plataforma le reenviaría."""
    from core.config import get_settings

    llamados: list[dict[str, Any]] = []

    async def reenviar(payload: dict[str, Any]) -> str:
        llamados.append(payload)
        return "procesado"

    monkeypatch.setattr("core.billing.enrutador.reenviar_al_negocio", reenviar)
    monkeypatch.setenv("BILLING_WOMPI_EVENTOS_TENANT", str(uuid.uuid4()))
    get_settings.cache_clear()
    yield llamados
    monkeypatch.delenv("BILLING_WOMPI_EVENTOS_TENANT", raising=False)
    get_settings.cache_clear()


# ------------------------------------------------------------------ escenario
@asynccontextmanager
async def _scope(tid: uuid.UUID) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


async def _negocio(plan: str, *, status: str = "active") -> uuid.UUID:
    """Negocio con su suscripción en `plan`, empezada hace 10 días y pagada por un mes."""
    from _habilidades import onboard_tenant

    from core.db.models import TenantSubscription

    slug = uniq("rec")
    tid, _ = await onboard_tenant(name=f"Recargas {slug}", slug=slug, sembrar_ejemplos=False, plan="free")
    if plan != "free" or status != "active":
        inicio = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)
        async with _scope(tid) as s:
            await s.execute(
                TenantSubscription.__table__.update()
                .where(TenantSubscription.tenant_id == tid)
                .values(
                    plan_id=plan,
                    status=status,
                    period_start=inicio,
                    period_end=inicio + UN_MES,
                    dia_ancla=inicio.day,
                    trial_ends_at=None,
                )
            )
            await s.commit()
    return tid


async def _suscripcion(tid: uuid.UUID) -> Any:
    from core.db.models import TenantSubscription

    async with _scope(tid) as s:
        return (await s.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == tid))).scalar_one()


async def _pagos(tid: uuid.UUID) -> list[Any]:
    from core.db.models import SubscriptionPayment

    async with _scope(tid) as s:
        return list(
            (
                await s.execute(
                    select(SubscriptionPayment)
                    .where(SubscriptionPayment.tenant_id == tid)
                    .order_by(SubscriptionPayment.created_at)
                )
            ).scalars().all()
        )


async def _abonos(tid: uuid.UUID) -> list[Any]:
    from core.db.models import CreditGrant

    async with _scope(tid) as s:
        return list((await s.execute(select(CreditGrant).where(CreditGrant.tenant_id == tid))).scalars().all())


async def _consumo(tid: uuid.UUID, creditos: str) -> None:
    from core.db.models import UsageLedger

    async with _scope(tid) as s:
        s.add(
            UsageLedger(
                tenant_id=tid,
                kind="turn",
                model="claude-haiku-4-5",
                input_tokens=100,
                output_tokens=10,
                cost_usd=Decimal("0.001"),
                credits=Decimal(creditos),
                key_mode="platform",
                tarifario_version="v1",
                trm_cop=Decimal("4000"),
            )
        )
        await s.commit()


async def _comprar(app_client: Any, tid: uuid.UUID, paquete: str) -> Any:
    headers = await api_headers(app_client, tid)
    return await app_client.post("/api/v1/admin/plan/recargas", json={"paquete": paquete}, headers=headers)


def _evento_wompi(link_id: str, *, centavos: int, status: str = "APPROVED") -> dict[str, Any]:
    """Evento `transaction.updated` firmado como Wompi sandbox (checksum de `signature.properties`)."""
    transaccion = {"id": f"tx-{uuid.uuid4().hex[:8]}", "status": status, "amount_in_cents": centavos, "payment_link_id": link_id}
    propiedades = ["transaction.id", "transaction.status", "transaction.amount_in_cents"]
    timestamp = 1_763_800_000
    valores = "".join(str(transaccion[p.split(".")[1]]) for p in propiedades)
    checksum = hashlib.sha256(f"{valores}{timestamp}{_SECRETO_DE_EVENTOS}".encode()).hexdigest()
    return {
        "event": "transaction.updated",
        "data": {"transaction": transaccion},
        "environment": "test",
        "signature": {"properties": propiedades, "checksum": checksum},
        "timestamp": timestamp,
    }


async def _saldo(tid: uuid.UUID) -> Any:
    from core.billing.creditos import decidir_turno

    async with _scope(tid) as s:
        return await decidir_turno(s, tid, uuid.uuid4())


# ------------------------------------------------------------------ comprar
async def test_premium_pide_5000_y_el_link_es_por_46750(app_client, produccion) -> None:
    tid = await _negocio("premium")
    r = await _comprar(app_client, tid, "recarga_5000")
    assert r.status_code == 200, r.text
    [cuerpo] = produccion.cuerpos
    assert cuerpo["amount_in_cents"] == 4_675_000
    assert cuerpo["description"] == "Recarga de 5.000 créditos — SolversHub"
    [pago] = await _pagos(tid)
    assert (pago.concepto, pago.creditos, pago.periodo, pago.plan_id) == ("recarga", Decimal("5000.0"), None, "premium")
    assert (float(pago.amount_cop), pago.status) == (46_750.0, "pendiente")
    assert r.json()["checkout_url"] == pago.checkout_url

    # El mismo pedido otra vez devuelve el mismo link (doble clic): un solo pendiente.
    r2 = await _comprar(app_client, tid, "recarga_5000")
    assert r2.json()["checkout_url"] == pago.checkout_url
    assert len(await _pagos(tid)) == 1


async def test_en_sandbox_bajo_el_minimo_da_409_claro_sin_fila(app_client, sandbox) -> None:
    tid = await _negocio("premium")
    r = await _comprar(app_client, tid, "recarga_5000")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "recarga_bajo_minimo"
    assert "150.000" in r.json()["error"]["message"]
    assert sandbox.cuerpos == []
    assert await _pagos(tid) == []


@pytest.mark.parametrize(("plan", "status"), [("free", "active"), ("free", "trialing"), ("basic", "past_due"), ("premium", "canceled")])
async def test_free_o_sin_suscripcion_activa_da_409(app_client, produccion, plan: str, status: str) -> None:
    tid = await _negocio(plan, status=status)
    r = await _comprar(app_client, tid, "recarga_5000")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "recarga_no_disponible"
    assert produccion.cuerpos == []
    headers = await api_headers(app_client, tid)
    assert (await app_client.get("/api/v1/admin/plan", headers=headers)).json()["recargas"] == []


async def test_get_plan_lista_los_paquetes_y_el_historial_distingue_la_recarga(app_client, produccion, monkeypatch) -> None:
    tid = await _negocio("empresarial")
    # Un negocio que paga el plan por el asistente también recarga por el panel.
    import core.billing.canales as canales

    async def vinculo(_negocio: uuid.UUID) -> object:
        return object()

    monkeypatch.setattr(canales, "vinculo_asistente", vinculo)
    assert (await _comprar(app_client, tid, "recarga_20000")).status_code == 200
    headers = await api_headers(app_client, tid)
    datos = (await app_client.get("/api/v1/admin/plan", headers=headers)).json()
    assert datos["plan_por_el_asistente"] is True
    assert datos["recargas"] == [
        {"id": "recarga_5000", "creditos": 5000, "precio_lista_cop": 55000, "descuento_pct": 25, "precio_cop": 41250},
        {"id": "recarga_20000", "creditos": 20000, "precio_lista_cop": 199000, "descuento_pct": 25, "precio_cop": 149250},
    ]
    [pago] = datos["payments"]
    assert (pago["concepto"], pago["creditos"], pago["periodo"], pago["amount_cop"]) == ("recarga", 20000.0, None, 149250.0)


# ------------------------------------------------------------------ webhook
@pytest.mark.aislamiento
async def test_el_webhook_firmado_de_sandbox_suma_la_recarga_al_saldo_del_ciclo(
    app_client, sandbox, reenvios, alertas, monkeypatch
) -> None:
    """Basic, paquete de 20.000 (199.000 ≥ mínimo del sandbox): el negocio sin saldo vuelve a atender, el
    abono entra en el ciclo actual, no se reenvía al negocio vendedor y el evento repetido no duplica."""
    from core.billing.periodos import ventana_mensual
    from core.config import get_settings

    monkeypatch.setenv("BILLING_BLOQUEAR_POR_CREDITOS", "true")
    get_settings.cache_clear()
    tid = await _negocio("basic")
    antes = await _suscripcion(tid)
    await _consumo(tid, "12500")  # Basic: 12.000 al mes
    sin_saldo = await _saldo(tid)
    assert sin_saldo.salida == "agotado"

    r = await _comprar(app_client, tid, "recarga_20000")
    assert r.status_code == 200, r.text
    [link] = sandbox.links
    evento = _evento_wompi(link, centavos=19_900_000)
    r = await app_client.post("/webhooks/billing/wompi", json=evento)
    assert (r.status_code, r.json()["status"], r.json()["updated"]) == (200, "ok", True)
    assert reenvios == []

    [pago] = await _pagos(tid)
    assert (pago.status, pago.concepto) == ("aprobado", "recarga")
    assert pago.paid_at is not None
    [abono] = await _abonos(tid)
    ciclo, _ = ventana_mensual(antes.period_start, antes.dia_ancla, datetime.now(UTC))
    assert (abono.tipo, abono.creditos, abono.origen, abono.vence_en, abono.ciclo) == (
        "recarga", Decimal("20000.0"), str(pago.id), None, ciclo
    )
    con_saldo = await _saldo(tid)
    assert con_saldo.salida == "normal"
    assert con_saldo.saldo.limite == 12_000 + 20_000

    despues = await _suscripcion(tid)
    assert (despues.plan_id, despues.status, despues.period_start, despues.period_end) == (
        antes.plan_id, antes.status, antes.period_start, antes.period_end
    )

    # Wompi reintenta el mismo evento: no hace nada.
    r = await app_client.post("/webhooks/billing/wompi", json=evento)
    assert (r.status_code, r.json()["updated"]) == (200, False)
    assert len(await _abonos(tid)) == 1
    assert reenvios == []
    assert alertas == []
    get_settings.cache_clear()


@pytest.mark.aislamiento
async def test_una_recarga_por_otro_monto_no_se_acredita(app_client, sandbox, alertas) -> None:
    tid = await _negocio("basic")
    assert (await _comprar(app_client, tid, "recarga_20000")).status_code == 200
    r = await app_client.post("/webhooks/billing/wompi", json=_evento_wompi(sandbox.links[-1], centavos=19_900_100))
    assert r.status_code == 200
    [pago] = await _pagos(tid)
    assert pago.status == "monto_distinto"
    assert await _abonos(tid) == []
    assert [a for a in alertas if a["severity"] == "critical"]


@pytest.mark.aislamiento
async def test_un_negocio_no_ve_ni_aplica_las_recargas_de_otro(app_client, produccion) -> None:
    from core.billing.checkout import apply_payment, tenant_del_pago
    from core.db.models import CreditGrant, SubscriptionPayment

    a = await _negocio("premium")
    b = await _negocio("premium")
    assert (await _comprar(app_client, a, "recarga_5000")).status_code == 200
    link = produccion.links[-1]
    assert await tenant_del_pago(link) == a

    # Con el negocio B en el contexto, el pago de A no existe y no se aplica.
    assert await apply_payment(provider_reference=link, status="aprobado", amount_in_cents=4_675_000, tenant_id=b) is False
    async with _scope(b) as s:
        assert (await s.execute(select(SubscriptionPayment).where(SubscriptionPayment.provider_reference == link))).first() is None
    headers_b = await api_headers(app_client, b)
    assert (await app_client.get("/api/v1/admin/plan", headers=headers_b)).json()["payments"] == []

    assert await apply_payment(provider_reference=link, status="aprobado", amount_in_cents=4_675_000) is True
    assert len(await _abonos(a)) == 1
    async with _scope(b) as s:
        assert (await s.execute(select(CreditGrant))).first() is None
    assert await _abonos(b) == []


# ------------------------------------------------------------------ convivencia con el plan
@pytest.mark.aislamiento
async def test_cambiar_de_plan_no_anula_el_link_de_recarga(app_client, produccion, alertas) -> None:
    from core.billing.checkout import apply_payment, start_subscription
    from core.shared.tenant import tenant_scope

    tid = await _negocio("basic")
    assert (await _comprar(app_client, tid, "recarga_5000")).status_code == 200
    link_recarga = produccion.links[-1]
    async with tenant_scope(tid):
        assert await start_subscription("premium")
    link_plan = produccion.links[-1]
    assert await apply_payment(provider_reference=link_plan, status="aprobado", amount_in_cents=29_900_000) is True
    assert (await _suscripcion(tid)).plan_id == "premium"

    pagos = {p.provider_reference: p for p in await _pagos(tid)}
    assert pagos[link_recarga].status == "pendiente"
    assert (pagos[link_plan].concepto, pagos[link_plan].creditos, pagos[link_plan].status) == ("plan", None, "aprobado")
    # El pago del plan no deja abonos.
    assert await _abonos(tid) == []

    # El link de recarga (comprado en Basic, sin descuento) sigue valiendo con el plan nuevo.
    assert await apply_payment(provider_reference=link_recarga, status="aprobado", amount_in_cents=5_500_000) is True
    [abono] = await _abonos(tid)
    assert (abono.tipo, abono.creditos) == ("recarga", Decimal("5000.0"))
    assert (await _suscripcion(tid)).plan_id == "premium"
    assert alertas == []


async def test_una_recarga_rechazada_no_acredita(app_client, produccion) -> None:
    tid = await _negocio("premium")
    assert (await _comprar(app_client, tid, "recarga_5000")).status_code == 200
    from core.billing.checkout import apply_payment

    assert await apply_payment(provider_reference=produccion.links[-1], status="rechazado", amount_in_cents=4_675_000) is True
    [pago] = await _pagos(tid)
    assert pago.status == "rechazado"
    assert await _abonos(tid) == []


@pytest.mark.parametrize(
    ("concepto", "creditos", "periodo"),
    [("recarga", None, None), ("recarga", "0", None), ("recarga", "NaN", None), ("plan", "10", None), ("recarga", "10", "plat-x-202611"), ("otro", None, None)],
)
async def test_los_check_de_la_tabla_rechazan_una_recarga_mal_formada(concepto: str, creditos: str | None, periodo: str | None) -> None:
    from sqlalchemy.exc import IntegrityError

    from core.db.models import SubscriptionPayment

    tid = await _negocio("premium")
    sub = await _suscripcion(tid)
    with pytest.raises(IntegrityError):
        async with _scope(tid) as s:
            s.add(
                SubscriptionPayment(
                    tenant_id=tid, subscription_id=sub.id, plan_id="premium", amount_cop=1, provider="wompi",
                    provider_reference=uniq("ck"), concepto=concepto, creditos=Decimal(creditos) if creditos else None,
                    periodo=periodo,
                )
            )
            await s.commit()


@pytest.mark.aislamiento
async def test_una_recarga_revertida_despues_de_acreditarse_alerta_y_no_toca_el_abono(app_client, produccion, alertas) -> None:
    from core.billing.checkout import apply_payment

    tid = await _negocio("premium")
    assert (await _comprar(app_client, tid, "recarga_5000")).status_code == 200
    link = produccion.links[-1]
    assert await apply_payment(provider_reference=link, status="aprobado", amount_in_cents=4_675_000) is True
    # Wompi anula la transacción después (VOIDED → rechazado): el abono queda y se alerta.
    assert await apply_payment(provider_reference=link, status="rechazado", amount_in_cents=4_675_000) is False
    [pago] = await _pagos(tid)
    assert pago.status == "aprobado"
    assert len(await _abonos(tid)) == 1
    [alerta] = alertas
    assert (alerta["severity"], alerta["title"]) == ("critical", "Recarga revertida después de acreditarse")

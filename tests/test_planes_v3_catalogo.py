"""HU-BILL-18 · catálogo v3 y retiro de los topes de volumen (D-79, SPEC-51, ADR-0208). Sin base de datos.

- el catálogo v3: precios, créditos, límites y features de cada plan;
- ninguna métrica retirada queda en el registro;
- el turno se corta solo por el crédito del mes (`turn_quota_metric` → `"creditos"`), y no con la bandera apagada;
- el alta de un cliente sigue bloqueándose al tope estructural de `clientes`;
- un audio o una imagen entrante ya no se descarta por cuota.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

RETIRADAS = ("conversaciones", "conversaciones_audio", "conversaciones_ocr", "mensajes", "costo_llm_usd", "plugins")


# ------------------------------------------------------------------ catálogo
CATALOGO_V3 = {
    # plan_id: (precio, créditos, clientes, agentes, colaboradores, documentos, conocimiento_mb)
    "free": (0, 1_500, 100, 1, 2, 3, 10),
    "basic": (129_000, 12_000, 1_000, 2, 5, 20, 100),
    "premium": (299_000, 40_000, 5_000, 5, 15, 100, 500),
    "empresarial": (799_000, 120_000, None, None, 50, 500, 2_000),
}

FEATURES_V3 = {
    "free": {
        "canales": ["whatsapp", "web"],
        "nivel_ia": "estandar",
        "byok": False,
        "acumulable_meses": 0,
        "descuento_recarga": None,
        "marca_propia": False,
        "historial_dias": 30,
        "campanas": "ninguna",
        "soporte": "comunidad",
    },
    "basic": {
        "canales": ["whatsapp", "web", "telegram"],
        "nivel_ia": "estandar",
        "byok": False,
        "acumulable_meses": 0,
        "descuento_recarga": 0,
        "marca_propia": False,
        "historial_dias": 90,
        "campanas": "recordatorios",
        "soporte": "email",
    },
    "premium": {
        "canales": ["whatsapp", "web", "telegram", "instagram", "messenger"],
        "nivel_ia": "avanzado",
        "byok": False,
        "acumulable_meses": 1,
        "descuento_recarga": 15,
        "marca_propia": True,
        "historial_dias": 365,
        "campanas": "completas",
        "soporte": "prioritario",
    },
    "empresarial": {
        "canales": ["whatsapp", "web", "telegram", "instagram", "messenger"],
        "nivel_ia": "avanzado",
        "byok": True,
        "acumulable_meses": 2,
        "descuento_recarga": 25,
        "marca_propia": True,
        "historial_dias": None,
        "campanas": "completas",
        "soporte": "dedicado",
    },
}


def test_mismos_plan_id_que_v2() -> None:
    from core.billing.plans import PLANS

    assert [p.id for p in PLANS] == ["free", "basic", "premium", "empresarial"]


@pytest.mark.parametrize("plan_id", list(CATALOGO_V3))
def test_precios_creditos_y_limites_v3(plan_id: str) -> None:
    from core.billing.plans import PLANS_BY_ID

    precio, creditos, clientes, agentes, colaboradores, documentos, mb = CATALOGO_V3[plan_id]
    plan = PLANS_BY_ID[plan_id]
    assert plan.price_cop == precio
    assert plan.monthly_credits == creditos
    assert plan.limits == {
        "clientes": clientes,
        "agentes": agentes,
        "colaboradores": colaboradores,
        "documentos": documentos,
        "conocimiento_mb": mb,
    }


@pytest.mark.parametrize("plan_id", list(FEATURES_V3))
def test_features_v3(plan_id: str) -> None:
    from core.billing.plans import PLANS_BY_ID

    assert PLANS_BY_ID[plan_id].features == FEATURES_V3[plan_id]


def test_free_tiene_prueba_de_30_dias_y_los_de_pago_no() -> None:
    from core.billing.plans import PLANS_BY_ID

    assert PLANS_BY_ID["free"].trial_days == 30
    assert PLANS_BY_ID["free"].is_free
    assert all(PLANS_BY_ID[p].trial_days == 0 and not PLANS_BY_ID[p].is_free for p in ("basic", "premium", "empresarial"))


def test_validate_limits_acepta_el_catalogo_y_rechaza_los_topes_retirados() -> None:
    from core.billing.plans import PLANS, _validate_limits

    for plan in PLANS:
        _validate_limits(plan.limits)
    for clave in RETIRADAS:
        with pytest.raises(ValueError, match="Métricas desconocidas"):
            _validate_limits({clave: 1})


def test_el_credito_v3_se_espeja_en_features() -> None:
    from core.billing.plans import PLANS, _features_espejadas

    for plan in PLANS:
        espejo = _features_espejadas(plan)
        assert espejo["credito_mensual"] == plan.monthly_credits
        assert {k: v for k, v in espejo.items() if k != "credito_mensual"} == plan.features


# ------------------------------------------------------------------ métricas retiradas
def test_ninguna_metrica_retirada_queda_en_el_registro() -> None:
    from core.billing.metrics import METRIC_KEYS, get_metric

    assert METRIC_KEYS == {"clientes", "agentes", "colaboradores", "creditos"}
    for clave in RETIRADAS:
        assert clave not in METRIC_KEYS
        assert get_metric(clave) is None


def test_el_turno_ya_no_recorre_topes_de_volumen() -> None:
    from core.billing import enforcement

    assert not hasattr(enforcement, "TURN_METRICS")
    assert not hasattr(enforcement, "can_activate_plugin")


async def test_una_metrica_retirada_ya_no_bloquea_aunque_una_fila_vieja_la_traiga(monkeypatch) -> None:
    """Un negocio que ya superó el viejo tope de mensajes o de USD de LLM: `quota_decision` deja pasar."""
    from core.billing import enforcement

    async def _sub(*_a, **_k):  # type: ignore[no-untyped-def]
        return SimpleNamespace(plan_id="free", period_start=datetime.now(UTC))

    monkeypatch.setattr(enforcement, "get_active_subscription", _sub)
    for clave in RETIRADAS:
        decision = await enforcement.quota_decision(uuid.uuid4(), clave)
        assert decision.allowed and decision.limit is None


# ------------------------------------------------------------------ el turno consulta el crédito
def _overview(plan_id: str | None = "basic", *, period_start: datetime | None = None):  # type: ignore[no-untyped-def]
    from core.billing.enforcement import SubscriptionOverview

    inicio = period_start if period_start is not None else datetime.now(UTC) - timedelta(days=3)
    return SubscriptionOverview(
        plan_id=plan_id, plan_name=plan_id or "—", status="active", operational=True, trial_ends_at=None,
        period_start=inicio, period_end=inicio + timedelta(days=30), days_left=27, limits={}, dia_ancla=None,
    )


@pytest.fixture()
def credito(monkeypatch):  # type: ignore[no-untyped-def]
    """Suscripción y consumo falsos; `get_session` devuelve una sesión que falla si alguien toma el lock."""
    from core.billing import creditos, enforcement

    estado = {"overview": _overview(), "usado": Decimal(0), "sql": []}

    async def _sub(_tid):  # type: ignore[no-untyped-def]
        return estado["overview"]

    async def _medir(_session, _tid, *, desde=None, hasta=None, **_k):  # type: ignore[no-untyped-def]
        assert desde is not None and hasta is not None and desde < hasta
        return creditos.ConsumoDeCreditos(total=estado["usado"], por_concepto={})

    class _Sesion:
        async def execute(self, stmt, *_a, **_k):  # type: ignore[no-untyped-def]
            estado["sql"].append(str(stmt))

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield _Sesion()

    monkeypatch.setattr(enforcement, "subscription_overview", _sub)
    monkeypatch.setattr(creditos, "medir_creditos", _medir)
    monkeypatch.setattr("core.db.session.get_session", _get_session)
    return estado


@pytest.mark.parametrize(("usado", "esperado"), [(11_999.9, None), (12_000, "creditos"), (15_000, "creditos")])
async def test_turn_quota_metric_corta_con_el_credito_del_mes_agotado(credito, usado, esperado) -> None:
    from core.billing.enforcement import turn_quota_metric

    credito["usado"] = Decimal(str(usado))
    assert await turn_quota_metric(uuid.uuid4()) == esperado
    # Sin lock consultivo en el turno (ADR-0208 §2).
    assert not any("advisory" in sql for sql in credito["sql"])


@pytest.mark.parametrize(
    "overview",
    [None, "sin_period_start", "plan_desconocido"],
)
async def test_sin_suscripcion_o_sin_credito_en_el_plan_no_corta(credito, overview) -> None:
    from core.billing.enforcement import turn_quota_metric

    credito["usado"] = Decimal(10**9)
    if overview is None:
        credito["overview"] = _overview(None)
    elif overview == "plan_desconocido":
        credito["overview"] = _overview("no-existe")
    else:
        sin_inicio = _overview()
        credito["overview"] = sin_inicio.__class__(**{**sin_inicio.__dict__, "period_start": None})
    assert await turn_quota_metric(uuid.uuid4()) is None


async def test_con_la_bandera_apagada_no_corta(credito, monkeypatch) -> None:
    from core.billing.enforcement import turn_quota_metric
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_bloquear_por_creditos", False)
    credito["usado"] = Decimal(50_000)
    assert await turn_quota_metric(uuid.uuid4()) is None


async def test_credito_agotado_exige_el_tenant_scope(credito) -> None:
    from core.billing.creditos import credito_agotado

    with pytest.raises(RuntimeError, match="tenant_scope"):
        await credito_agotado(SimpleNamespace(), uuid.uuid4())


async def test_alcanza_y_credito_agotado_comparten_el_saldo(credito) -> None:
    """`alcanza` mide lo mismo (y sí toma el lock): con el crédito agotado, una acción de 1 crédito no alcanza."""
    from core.billing.creditos import alcanza, credito_agotado
    from core.billing.enforcement import QuotaExceeded
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    credito["usado"] = Decimal(12_000)

    class _Sesion:
        async def execute(self, stmt, *_a, **_k):  # type: ignore[no-untyped-def]
            credito["sql"].append(str(stmt))

    async with tenant_scope(tid):
        assert await credito_agotado(_Sesion(), tid) is True
        assert not any("advisory" in sql for sql in credito["sql"])
        with pytest.raises(QuotaExceeded) as exc:
            await alcanza(_Sesion(), tid, 1)
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("creditos", 12_000, 12_000)
    assert any("advisory" in sql for sql in credito["sql"])


# ------------------------------------------------------------------ lo que se queda: clientes
async def test_el_alta_de_un_cliente_sigue_bloqueada_al_tope_de_clientes(monkeypatch) -> None:
    from core.billing import enforcement

    async def _sub(*_a, **_k):  # type: ignore[no-untyped-def]
        return SimpleNamespace(plan_id="free", period_start=datetime.now(UTC))

    usados = {"n": 99}

    async def _contar(_tid, key, **_k):  # type: ignore[no-untyped-def]
        assert key == "clientes"
        return usados["n"]

    monkeypatch.setattr(enforcement, "get_active_subscription", _sub)
    monkeypatch.setattr(enforcement, "count_metric", _contar)
    await enforcement.enforce_metric(uuid.uuid4(), "clientes")  # 99/100: pasa
    usados["n"] = 100
    with pytest.raises(enforcement.QuotaExceeded) as exc:
        await enforcement.enforce_metric(uuid.uuid4(), "clientes")
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("clientes", 100, 100)


# ------------------------------------------------------------------ la media entra siempre (webhook)
@pytest.mark.parametrize("mime", ["audio/ogg", "image/jpeg"])
async def test_el_webhook_encola_audio_e_imagen_sin_consultar_cuota(monkeypatch, mime: str) -> None:
    from core.gateway import webhooks

    encolados: list[dict] = []

    class _Redis:
        async def xadd(self, _stream, fields):  # type: ignore[no-untyped-def]
            encolados.append(fields)
            return "1-0"

    async def _no_se_consulta(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("la media no consulta cuotas")

    async def _claim(*_a, **_k) -> bool:  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(webhooks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(webhooks, "claim_message_once", _claim)
    monkeypatch.setattr("core.billing.enforcement.check_metric", _no_se_consulta)
    monkeypatch.setattr("core.billing.enforcement.quota_decision", _no_se_consulta)
    mensaje = SimpleNamespace(
        provider="meta", channel="whatsapp", external_message_id=f"wamid-{mime}", customer_external_id="whatsapp:+57300",
        customer_name="", content="", connection_external_id="pn-1", media=[{"type": mime, "id": "m1"}],
        media_context="", interactive_id=None,
    )

    assert await webhooks.publish_inbound_message(uuid.uuid4(), mensaje) == "enqueued"
    assert len(encolados) == 1 and mime in encolados[0]["media"]

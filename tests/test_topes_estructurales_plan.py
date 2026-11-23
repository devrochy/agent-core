"""HU-BILL-19 · conexiones, correos, canales y habilidades por plan (D-79, ADR-0208 §8-10). Sin base de datos.

- `conexiones` cuenta los canales conversacionales y `correos` el correo; el Centro de pruebas y el calendario no;
- WhatsApp y el chat web en todos los planes; Telegram desde Basic; Instagram y Messenger desde Premium;
- cada alta exige el canal y la métrica que le toca (`correos` para email, `conexiones` para el resto);
- los 409: `plan_limit_conexiones`, `plan_limit_correos` (`{metric, used, limit}`), `plan_channel_not_included` y
  `plan_plugin_not_included`;
- Basic no habilita Cobranza (`min_plan = premium`) y no se escribe nada;
- un negocio sin suscripción no se bloquea.

El tope concurrente, el aislamiento entre negocios y las rutas están en
`tests/integration/test_topes_estructurales_conexiones.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.shared.tenant import tenant_scope

TID = uuid.UUID("19191919-0000-0000-0000-000000000019")


def _sub(plan_id: str | None):
    async def _get(_tenant_id, *, session=None):
        return None if plan_id is None else SimpleNamespace(plan_id=plan_id)

    return _get


# ------------------------------------------------------------------ métricas
def test_conexiones_y_correos_son_topes_estructurales() -> None:
    from core.billing.enforcement import GROWTH_METRICS
    from core.billing.metrics import get_metric

    for clave in ("conexiones", "correos"):
        assert get_metric(clave).scope == "lifetime"
        assert clave in GROWTH_METRICS


@pytest.mark.parametrize(
    ("channel", "metrica"),
    [
        ("whatsapp", "conexiones"),
        ("instagram", "conexiones"),
        ("messenger", "conexiones"),
        ("telegram", "conexiones"),
        ("web", "conexiones"),
        ("email", "correos"),
        ("test", None),  # Centro de pruebas
        ("", None),  # Google Calendar
        (None, None),
    ],
)
def test_metrica_de_cada_canal(channel, metrica) -> None:
    from core.billing.metrics import metrica_de_canal

    assert metrica_de_canal(channel) == metrica


@pytest.mark.parametrize(
    ("plan_id", "conexiones", "correos"),
    [("free", 1, 0), ("basic", 2, 1), ("premium", 6, 3), ("empresarial", 20, 10)],
)
def test_limites_del_catalogo(plan_id, conexiones, correos) -> None:
    from core.billing.plans import plan_limits

    assert plan_limits(plan_id)["conexiones"] == conexiones
    assert plan_limits(plan_id)["correos"] == correos


# ------------------------------------------------------------------ canales
@pytest.mark.parametrize("plan_id", ["free", "basic", "premium", "empresarial"])
def test_whatsapp_y_chat_web_en_todos_los_planes(plan_id) -> None:
    from core.billing.canales_del_plan import plan_incluye_canal

    assert plan_incluye_canal(plan_id, "whatsapp")
    assert plan_incluye_canal(plan_id, "web")


@pytest.mark.parametrize(
    ("plan_id", "canal", "incluido"),
    [
        ("free", "telegram", False),
        ("basic", "telegram", True),
        ("basic", "instagram", False),
        ("basic", "messenger", False),
        ("premium", "instagram", True),
        ("premium", "messenger", True),
        ("empresarial", "messenger", True),
    ],
)
def test_canales_por_plan(plan_id, canal, incluido) -> None:
    from core.billing.canales_del_plan import plan_incluye_canal

    assert plan_incluye_canal(plan_id, canal) is incluido


@pytest.mark.parametrize("canal", ["email", "test", ""])
def test_correo_prueba_y_calendario_quedan_exentos(canal) -> None:
    from core.billing.canales_del_plan import canal_exento, plan_incluye_canal

    assert canal_exento(canal)
    assert plan_incluye_canal("free", canal)


def test_el_import_de_siempre_sigue_funcionando() -> None:
    from core.billing.canales_del_plan import plan_incluye_canal
    from core.gateway import meta_messenger_alta

    assert meta_messenger_alta.plan_incluye_canal is plan_incluye_canal


@pytest.mark.asyncio
@pytest.mark.parametrize(("plan_id", "canal"), [("basic", "instagram"), ("basic", "messenger"), ("free", "telegram")])
async def test_canal_fuera_del_plan_es_plan_channel_not_included(monkeypatch, plan_id, canal) -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing import canales_del_plan

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub(plan_id))
    with pytest.raises(canales_del_plan.CanalNoIncluido) as exc:
        await canales_del_plan.exigir_canal(TID, canal)

    error = plan_limit_error(exc.value)
    assert (error.status_code, error.code) == (409, "plan_channel_not_included")
    assert error.details == {"channel": canal, "plan": plan_id}
    assert "Mejora tu plan" in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_id", [None, "plan-que-no-existe"])
async def test_sin_suscripcion_o_sin_plan_no_se_bloquea(monkeypatch, plan_id) -> None:
    from core.billing import canales_del_plan

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub(plan_id))
    await canales_del_plan.exigir_canal(TID, "messenger")
    assert await canales_del_plan.canal_permitido(TID, "instagram")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canal", "metrica"),
    [("whatsapp", "conexiones"), ("web", "conexiones"), ("telegram", "conexiones"), ("email", "correos"), ("test", None)],
)
async def test_cada_alta_exige_su_metrica(monkeypatch, canal, metrica) -> None:
    from core.billing import canales_del_plan

    exigidas: list[str] = []

    async def _enforce(tenant_id, key, *, session):
        exigidas.append(key)

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("empresarial"))
    monkeypatch.setattr(canales_del_plan, "enforce_growth", _enforce)
    async with tenant_scope(TID):
        await canales_del_plan.exigir_alta_de_conexion(object(), TID, canal)
    assert exigidas == ([metrica] if metrica else [])


@pytest.mark.asyncio
async def test_el_canal_se_mide_antes_que_el_tope(monkeypatch) -> None:
    """Free + Telegram: 409 del canal sin tomar el lock del tope."""
    from core.billing import canales_del_plan

    async def _enforce(*_a, **_k):
        raise AssertionError("no debía medir el tope")

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("free"))
    monkeypatch.setattr(canales_del_plan, "enforce_growth", _enforce)
    with pytest.raises(canales_del_plan.CanalNoIncluido):
        async with tenant_scope(TID):
            await canales_del_plan.exigir_alta_de_conexion(object(), TID, "telegram")


@pytest.mark.asyncio
async def test_el_alta_con_la_sesion_de_otro_negocio_falla(monkeypatch) -> None:
    """Revisión de seguridad de SPEC-51: con otro scope, el conteo del tope daría 0 y dejaría crecer."""
    from core.billing import canales_del_plan

    with pytest.raises(RuntimeError, match="tenant_scope"):
        async with tenant_scope(uuid.uuid4()):
            await canales_del_plan.exigir_alta_de_conexion(object(), TID, "email")


@pytest.mark.asyncio
async def test_verificar_antes_del_oauth_no_toma_lock(monkeypatch) -> None:
    from core.billing import canales_del_plan
    from core.billing.enforcement import QuotaDecision, QuotaExceeded

    async def _decision(tenant_id, key, *, session=None):
        return QuotaDecision(False, key, 6, 6, "cuota_agotada")

    async def _enforce(*_a, **_k):
        raise AssertionError("el chequeo previo no toma el lock")

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("premium"))
    monkeypatch.setattr(canales_del_plan, "quota_decision", _decision)
    monkeypatch.setattr(canales_del_plan, "enforce_growth", _enforce)
    with pytest.raises(QuotaExceeded) as exc:
        await canales_del_plan.verificar_alta_de_conexion(TID, "messenger")
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("conexiones", 6, 6)


# ------------------------------------------------------------------ 409 de los topes
@pytest.mark.parametrize(
    ("metrica", "used", "limit", "texto"),
    [("conexiones", 1, 1, "conexión"), ("correos", 1, 1, "correo")],
)
def test_409_de_conexiones_y_correos(metrica, used, limit, texto) -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing.enforcement import QuotaExceeded

    error = plan_limit_error(QuotaExceeded(metrica, used, limit))
    assert (error.status_code, error.code) == (409, f"plan_limit_{metrica}")
    assert error.details == {"metric": metrica, "used": used, "limit": limit}
    assert texto in error.message and "No se conectó nada" in error.message


def test_la_doc_del_409_nombra_los_codigos() -> None:
    from core.api.plan_limits import plan_conexion_responses, plan_limit_responses_de

    texto = plan_conexion_responses()[409]["description"]
    for codigo in ("plan_limit_conexiones", "plan_limit_correos", "plan_channel_not_included"):
        assert codigo in texto
    assert "plan_plugin_not_included" in plan_limit_responses_de(habilidad=True)[409]["description"]


# ------------------------------------------------------------------ habilidades
@pytest.mark.parametrize(
    ("actual", "minimo", "alcanza"),
    [
        ("free", "free", True),
        ("free", "basic", False),
        ("basic", "basic", True),
        ("basic", "premium", False),
        ("premium", "premium", True),
        ("empresarial", "premium", True),
        ("premium", "empresarial", False),
        (None, "basic", False),
        ("basic", None, True),
        ("basic", "plan-raro", True),
    ],
)
def test_plan_alcanza_por_el_orden_del_catalogo(actual, minimo, alcanza) -> None:
    from core.billing.plans import plan_alcanza

    assert plan_alcanza(actual, minimo) is alcanza


def _modulos(monkeypatch, min_planes: dict[str, str]) -> None:
    from core.plugin_manager import plugin_manager

    monkeypatch.setattr(
        plugin_manager, "get", lambda pack: SimpleNamespace(min_plan=min_planes[pack]) if pack in min_planes else None
    )


@pytest.mark.asyncio
async def test_basic_no_habilita_cobranza(monkeypatch) -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing.habilidades_del_plan import HabilidadNoIncluida, exigir_min_plan

    _modulos(monkeypatch, {"pack_cobranza": "premium", "pack_commerce": "basic"})
    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("basic"))
    with pytest.raises(HabilidadNoIncluida) as exc:
        await exigir_min_plan(TID, ["pack_commerce", "pack_cobranza"])

    error = plan_limit_error(exc.value)
    assert (error.status_code, error.code) == (409, "plan_plugin_not_included")
    assert error.details == {"skills": ["pack_cobranza"], "min_plan": "premium", "plan": "basic"}
    assert "Premium" in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_id", ["premium", "empresarial", None])
async def test_premium_o_sin_suscripcion_habilita_cobranza(monkeypatch, plan_id) -> None:
    from core.billing.habilidades_del_plan import exigir_min_plan

    _modulos(monkeypatch, {"pack_cobranza": "premium"})
    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub(plan_id))
    await exigir_min_plan(TID, ["pack_cobranza"])


@pytest.mark.asyncio
async def test_asegurar_habilidades_no_escribe_nada_si_el_plan_no_alcanza(monkeypatch) -> None:
    """Basic + Cobranza: el 409 sale ANTES de activar, configurar o sembrar el flujo del pack."""
    from core.agents import habilitacion
    from core.billing.habilidades_del_plan import HabilidadNoIncluida
    from core.shared.tenant import tenant_scope

    escrito: list[str] = []

    async def _escribe(pack, *_a, **_k):
        escrito.append(pack)

    async def _instalados(*_a, **_k):
        return set()

    _modulos(monkeypatch, {"pack_cobranza": "premium"})
    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("basic"))
    monkeypatch.setattr(habilitacion, "ensure_pack_activated", _escribe)
    monkeypatch.setattr(habilitacion, "_config_del_negocio", _escribe)
    monkeypatch.setattr(habilitacion, "seed_flow_for_current_tenant", _escribe)
    monkeypatch.setattr(habilitacion, "record_audit", _escribe)
    monkeypatch.setattr("core.agents.instalaciones_agente.packs_instalados", _instalados)

    async with tenant_scope(TID):
        with pytest.raises(HabilidadNoIncluida):
            await habilitacion.asegurar_habilidades(TID, ["pack_cobranza"], actor=None)
    assert escrito == []

"""HU-BILL-25 · marca propia, historial y exportación por plan (D-79, SPEC-51). Sin base de datos.

- `marca_propia`: Premium y Empresarial quitan «SolversHub»; Free, Basic y sin suscripción no.
- `historial_dias`: 30 / 90 / 365 / sin límite; el corte se recalcula y al subir de plan se corre.
- `exportar`: Premium y Empresarial sí; Basic responde 409 `plan_feature_not_included` con
  `details = {feature, plan}`.
- La config pública del widget expone `marca_propia`.
- El turno, la memoria y los crons no dependen del historial del panel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

AHORA = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _con_plan(monkeypatch, plan_id: str | None) -> dict[str, str | None]:
    """Falsea la suscripción del negocio; devuelve un dict para cambiar de plan en caliente."""
    import core.billing.service as service

    estado: dict[str, str | None] = {"plan": plan_id}

    async def falsa(tenant_id, *, session=None):
        return None if estado["plan"] is None else SimpleNamespace(plan_id=estado["plan"])

    monkeypatch.setattr(service, "get_active_subscription", falsa)
    return estado


# ------------------------------------------------------------------ marca propia
@pytest.mark.parametrize(
    ("plan_id", "esperado"),
    [("free", False), ("basic", False), ("premium", True), ("empresarial", True), (None, False), ("desconocido", False)],
)
def test_marca_propia_por_plan(plan_id: str | None, esperado: bool) -> None:
    from core.billing.features_del_plan import marca_propia

    assert marca_propia(plan_id) is esperado


async def test_marca_propia_del_negocio_sigue_al_plan(monkeypatch) -> None:
    from core.billing.features_del_plan import marca_propia_del_negocio

    estado = _con_plan(monkeypatch, "basic")
    tid = uuid.uuid4()
    assert await marca_propia_del_negocio(tid) is False
    estado["plan"] = "premium"
    assert await marca_propia_del_negocio(tid) is True
    estado["plan"] = None  # sin suscripción: se sigue mostrando la marca
    assert await marca_propia_del_negocio(tid) is False


async def test_config_publica_del_widget_expone_marca_propia(monkeypatch) -> None:
    import json

    import core.api.routers.web_chat as web_chat

    tid = uuid.uuid4()

    async def resolver(key):
        return tid, {"enabled": True, "allowed_origins": ["https://a.test"], "turnstile": False}

    monkeypatch.setattr(web_chat, "_resolve_by_key", resolver)
    estado = _con_plan(monkeypatch, "premium")
    r = await web_chat.web_chat_config("clave-publica")
    assert json.loads(r.body) == {
        "enabled": True,
        "allowed_origins": ["https://a.test"],
        "turnstile": False,
        "turnstile_site_key": "",
        "marca_propia": True,
    }
    estado["plan"] = "basic"
    assert json.loads((await web_chat.web_chat_config("clave-publica")).body)["marca_propia"] is False


async def test_config_publica_muestra_la_marca_si_falla_el_billing(monkeypatch) -> None:
    import json

    import core.api.routers.web_chat as web_chat
    import core.billing.service as service

    async def resolver(key):
        return uuid.uuid4(), {"enabled": True, "allowed_origins": [], "turnstile": False}

    async def rota(tenant_id, *, session=None):
        raise RuntimeError("bd caída")

    monkeypatch.setattr(web_chat, "_resolve_by_key", resolver)
    monkeypatch.setattr(service, "get_active_subscription", rota)
    r = await web_chat.web_chat_config("clave-publica")
    assert r.status_code == 200 and json.loads(r.body)["marca_propia"] is False


# ------------------------------------------------------------------ historial
@pytest.mark.parametrize(
    ("plan_id", "dias"),
    [("free", 30), ("basic", 90), ("premium", 365), ("empresarial", None), (None, None), ("desconocido", None)],
)
def test_historial_dias_por_plan(plan_id: str | None, dias: int | None) -> None:
    from core.billing.features_del_plan import historial_dias

    assert historial_dias(plan_id) == dias


def test_desde_para() -> None:
    from core.billing.features_del_plan import desde_para

    assert desde_para(None, ahora=AHORA) is None
    assert desde_para(90, ahora=AHORA) == AHORA - timedelta(days=90)


async def test_desde_del_historial_al_subir_de_plan_se_ve_mas(monkeypatch) -> None:
    from core.billing.features_del_plan import desde_del_historial

    estado = _con_plan(monkeypatch, "basic")
    tid = uuid.uuid4()
    assert await desde_del_historial(tid, ahora=AHORA) == AHORA - timedelta(days=90)
    estado["plan"] = "premium"
    assert await desde_del_historial(tid, ahora=AHORA) == AHORA - timedelta(days=365)
    estado["plan"] = "empresarial"
    assert await desde_del_historial(tid, ahora=AHORA) is None
    estado["plan"] = None  # sin suscripción: sin filtro
    assert await desde_del_historial(tid, ahora=AHORA) is None


def test_conversacion_fuera_del_historial_es_409_con_detalles() -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing.features_del_plan import conversacion_fuera_del_historial

    err = plan_limit_error(conversacion_fuera_del_historial("basic", 90))
    assert err.status_code == 409
    assert err.code == "plan_feature_not_included"
    assert err.details == {"feature": "historial", "plan": "basic", "historial_dias": 90}
    assert "90 días" in err.message and "Basic" in err.message


def test_recortar_periodo_por_agente_al_historial() -> None:
    from core.agents import data_por_agente as svc
    from core.api.routers.agent_data import _recortar_al_historial

    filtros = svc.Filtros(periodo=svc.Periodo(desde=AHORA - timedelta(days=200), hasta=AHORA))
    assert _recortar_al_historial(filtros, None) is filtros
    corte = AHORA - timedelta(days=90)
    assert _recortar_al_historial(filtros, corte).periodo == svc.Periodo(desde=corte, hasta=AHORA)
    # Un período que termina antes del corte queda vacío.
    viejo = svc.Filtros(periodo=svc.Periodo(desde=AHORA - timedelta(days=200), hasta=AHORA - timedelta(days=100)))
    recortado = _recortar_al_historial(viejo, corte).periodo
    assert recortado.desde == recortado.hasta == AHORA - timedelta(days=100)


# ------------------------------------------------------------------ exportar
@pytest.mark.parametrize(
    ("plan_id", "exporta"),
    [("free", False), ("basic", False), ("premium", True), ("empresarial", True), (None, True)],
)
def test_plan_exporta(plan_id: str | None, exporta: bool) -> None:
    from core.billing.features_del_plan import plan_exporta

    assert plan_exporta(plan_id) is exporta


async def test_exigir_exportar_basic_409_premium_pasa(monkeypatch) -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing.features_del_plan import FeatureNoIncluida, exigir_exportar

    estado = _con_plan(monkeypatch, "basic")
    tid = uuid.uuid4()
    with pytest.raises(FeatureNoIncluida) as exc:
        await exigir_exportar(tid)
    err = plan_limit_error(exc.value)
    assert (err.status_code, err.code) == (409, "plan_feature_not_included")
    assert err.details == {"feature": "exportar", "plan": "basic"}

    estado["plan"] = "premium"
    await exigir_exportar(tid)
    estado["plan"] = None  # sin suscripción: no se limita (mismo criterio que los canales)
    await exigir_exportar(tid)


def test_celda_neutraliza_formulas() -> None:
    from core.shared.csv_seguro import celda

    assert celda("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert celda("@x") == "'@x"
    assert celda("\n=1+1") == "'\n=1+1"
    assert celda("\r=1") == "'\r=1"
    assert celda("hola") == "hola"
    assert celda(None) == ""


def test_export_csv_va_antes_del_detalle() -> None:
    """`/{conversation_id}` capturaría `export.csv` si se declarara antes."""
    from core.api.routers.conversations import router

    rutas = [r.path for r in router.routes]
    assert rutas.index("/admin/conversations/export.csv") < rutas.index("/admin/conversations/{conversation_id}")


# ------------------------------------------------------------------ lo que NO cambia
def test_turno_memoria_y_crons_no_usan_el_historial_del_panel() -> None:
    """El historial solo oculta en el panel: el turno, la memoria y los crons no lo importan."""
    raiz = Path(__file__).resolve().parents[1] / "core"
    for carpeta in ("orchestrator", "memory", "kernel", "tasks", "gateway"):
        for archivo in (raiz / carpeta).rglob("*.py"):
            texto = archivo.read_text(encoding="utf-8")
            assert "core.api.historial" not in texto, archivo
            assert "desde_del_historial" not in texto, archivo

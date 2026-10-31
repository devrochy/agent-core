"""P0 de correctitud del ciclo de pruebas 2026-10-13 (sin BD):

- números negativos rechazados por los modelos de entrada y las configs de packs;
- errores de validación en español, por campo, con el sobre de la API (422);
- tope del plan de habilidades: 409 `plan_limit_plugins` con el estado real;
- botón «Sincronizar» de plantillas sin candado horario y con límite de 1/min;
- eliminar una plantilla de ejemplo de Meta: mensaje claro, sin 500;
- el error de límite de números es de Meta y se explica como tal;
- alta BYOK reentrante: `reused`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.shared.validacion import campos_de, mensaje_es

# ------------------------------------------------------------------ 1. negativos


def _errores(model, **valores) -> dict[str, str]:
    with pytest.raises(ValidationError) as exc:
        model(**valores)
    return campos_de(exc.value.errors())


def test_config_cobranza_rechaza_dias_negativos() -> None:
    from pack_cobranza.config import CobranzaConfig

    fields = _errores(CobranzaConfig, dias_tolerancia_cobro=-4)
    assert fields == {"dias_tolerancia_cobro": "Debe ser mayor o igual que 0."}
    assert CobranzaConfig(dias_tolerancia_cobro=0).dias_tolerancia_cobro == 0


def test_reglas_de_cobranza_rechazan_negativos() -> None:
    from pack_cobranza.admin_api import RulesUpdate

    fields = _errores(RulesUpdate, max_dias_prorroga=-1, max_reprogramaciones=-2, min_abono_pct=120)
    assert set(fields) == {"max_dias_prorroga", "max_reprogramaciones", "min_abono_pct"}
    assert fields["min_abono_pct"] == "Debe ser menor o igual que 100."


def test_calendar_duracion_y_anticipacion() -> None:
    from pack_calendar.admin_api import TypeSave
    from pack_calendar.config import CalendarConfig

    assert "event_duration_minutes" in _errores(CalendarConfig, event_duration_minutes=0)
    assert "buffer_minutes" in _errores(CalendarConfig, buffer_minutes=-5)
    assert "recordatorios_horas.1" in _errores(CalendarConfig, recordatorios_horas=[24, -1])
    assert "duration_minutes" in _errores(TypeSave, name="Consulta", duration_minutes=-30)


def test_commerce_minimos_y_stock() -> None:
    from pack_commerce.admin_api import CategorySave, ProductSave
    from pack_commerce.config import CRMConfig

    assert "pago_en_linea_monto_minimo" in _errores(CRMConfig, pago_en_linea_monto_minimo=-1)
    assert "customer_fiel_min_pedidos" in _errores(CRMConfig, customer_fiel_min_pedidos=0)
    assert set(_errores(ProductSave, name="x", price=-1, stock=-3)) == {"price", "stock"}
    assert "sort_order" in _errores(CategorySave, name="x", sort_order=-1)


def test_payments_y_subscriptions() -> None:
    from pack_payments.config import PaymentsConfig
    from pack_subscriptions.admin_api import CrearSuscripcionBody
    from pack_subscriptions.config import SubscriptionConfig

    assert "link_expira_horas" in _errores(
        PaymentsConfig, public_key="p", private_key="s", events_secret="e", link_expira_horas=0
    )
    # Los recordatorios admiten días negativos a propósito (antes del vencimiento).
    assert SubscriptionConfig(recordatorios_dias=[-3, 0, 3]).recordatorios_dias == [-3, 0, 3]
    # El plan sale del catálogo (SPEC-43): el cuerpo ya no trae precio ni prueba propios.
    assert set(_errores(CrearSuscripcionBody, customer_id=uuid.uuid4())) == {"plan_codigo"}


def test_app_settings_turnos_top_k_y_umbral() -> None:
    from core.api.routers.platform import TenantPlanUpdate
    from core.api.routers.settings import BrainUpdate, IdentityUpdate, MemoryUpdate

    assert set(_errores(MemoryUpdate, short_term_turns=0, rag_top_k=-1, rag_min_score=1.5)) == {
        "short_term_turns", "rag_top_k", "rag_min_score",
    }
    assert MemoryUpdate(short_term_turns=30, rag_top_k=0, rag_min_score=0.6).rag_top_k == 0
    assert set(_errores(BrainUpdate, model_primary="m", model_temperature=-0.1, policy_max_tool_calls=0, policy_budget=-1)) == {
        "model_temperature", "policy_max_tool_calls", "policy_budget",
    }
    fields = _errores(
        IdentityUpdate, assistant_name="Bot", guardrail_max_retries=-1, persona_warmth=9, auto_escalate_after_turns=-1
    )
    assert set(fields) == {"guardrail_max_retries", "persona_warmth", "auto_escalate_after_turns"}
    assert "extend_days" in _errores(TenantPlanUpdate, plan_id="free", extend_days=-1)


def test_mensajes_en_espanol() -> None:
    assert mensaje_es({"type": "greater_than_equal", "ctx": {"ge": 0.0}}) == "Debe ser mayor o igual que 0."
    assert mensaje_es({"type": "int_parsing", "msg": "Input should be a valid integer"}) == "Debe ser un número entero."
    assert mensaje_es({"type": "missing"}) == "Este campo es obligatorio."
    assert mensaje_es({"type": "value_error", "msg": "Value error, zona horaria desconocida: X"}) == (
        "zona horaria desconocida: X"
    )
    assert campos_de([{"loc": ("body", "values", "dias"), "type": "missing"}]) == {"values.dias": "Este campo es obligatorio."}


@pytest.mark.asyncio
async def test_handler_422_usa_el_sobre_de_la_api_solo_bajo_api() -> None:
    import json

    from fastapi.exceptions import RequestValidationError

    from core.api.errors import validation_error_handler

    exc = RequestValidationError(
        [{"type": "greater_than_equal", "loc": ("body", "max_dias_prorroga"), "msg": "x", "input": -4, "ctx": {"ge": 0}}]
    )
    api = SimpleNamespace(url=SimpleNamespace(path="/api/v1/pack/cobranza/rules"))
    res = await validation_error_handler(api, exc)  # type: ignore[arg-type]
    body = json.loads(res.body)
    assert res.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["fields"] == {"max_dias_prorroga": "Debe ser mayor o igual que 0."}
    assert "detail" not in body

    ssr = SimpleNamespace(url=SimpleNamespace(path="/admin/plugins"), headers={}, method="POST")
    res_ssr = await validation_error_handler(ssr, exc)  # type: ignore[arg-type]
    assert res_ssr.status_code == 422
    assert "error" not in json.loads(res_ssr.body)


def test_plugin_config_error_trae_mensajes_en_espanol() -> None:
    """`save_config` traduce los `fields` (sin BD: se prueba la conversión que usa)."""
    from pack_cobranza.config import CobranzaConfig

    try:
        CobranzaConfig(dias_tolerancia_cobro=-4, umbral_mora_dias="muchos")  # type: ignore[arg-type]
    except ValidationError as exc:
        fields = campos_de(exc.errors())
    assert fields == {
        "dias_tolerancia_cobro": "Debe ser mayor o igual que 0.",
        "umbral_mora_dias": "Debe ser un número entero.",
    }


# ------------------------------------------------------------------ 2. tope del plan


def test_tope_del_plan_responde_409_con_estado_real() -> None:
    from core.api.routers.plugins import _plan_limit_error
    from core.billing.enforcement import QuotaExceeded

    err = _plan_limit_error(QuotaExceeded("plugins", 1, 1))
    assert err.status_code == 409
    assert err.code == "plan_limit_plugins"
    assert err.details == {"metric": "plugins", "used": 1, "limit": 1, "enabled": False}
    assert "1 habilidad" in err.message
    assert "sigue deshabilitada" in err.message


# ------------------------------------------------------------------ 3. plantillas


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key):
        self.keys.pop(key, None)
        return 1


def _conexion(tenant_id: uuid.UUID):
    connection = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    waba = SimpleNamespace(id=uuid.uuid4(), waba_id="waba-1")
    creds = SimpleNamespace(access_token="tok", app_secret="sec", graph_api_version="v21.0")
    return connection, waba, creds


@pytest.mark.asyncio
async def test_sincronizar_a_mano_ignora_el_candado_horario_y_limita_a_una_por_minuto(monkeypatch) -> None:
    import core.admin.meta_templates_service as svc
    import core.gateway.meta_template_sync as sync_mod

    tenant_id = uuid.uuid4()
    connection, waba, creds = _conexion(tenant_id)
    redis = _FakeRedis()
    sincronizaciones: list[str] = []

    async def _resolve(cid, tid):
        return connection, waba, creds

    async def _sync(waba_id, *args):
        sincronizaciones.append(waba_id)
        return []

    monkeypatch.setattr(svc, "resolve_waba_and_credentials", _resolve)
    monkeypatch.setattr(sync_mod, "sync_templates", _sync)
    monkeypatch.setattr(sync_mod, "get_redis", lambda: redis)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)

    # El cron (o el listado) ya tomó el candado de esta hora: el botón igual sincroniza.
    assert await sync_mod.acquire_sync_lock(tenant_id, "waba-1") is True
    assert await svc.sync_templates_now(tenant_id, str(connection.id)) is None
    assert sincronizaciones == ["waba-1"]

    # Dentro del minuto: límite propio, no «ya se sincronizó en la última hora».
    error = await svc.sync_templates_now(tenant_id, str(connection.id))
    assert error is not None and error.code == "rate_limited"
    assert sincronizaciones == ["waba-1"]

    # Otro negocio con el mismo `waba_id` no comparte el límite.
    otro = uuid.uuid4()
    assert await svc.sync_templates_now(otro, str(connection.id)) is None
    assert any(k.startswith(f"tpl_sync_manual:{otro}:") for k in redis.keys)


@pytest.mark.asyncio
async def test_eliminar_plantilla_de_ejemplo_de_meta_explica_y_no_marca_eliminada(monkeypatch) -> None:
    import core.admin.meta_templates_service as svc
    import core.gateway.meta_templates as templates_mod
    from core.gateway.meta_errors import GraphError

    tenant_id = uuid.uuid4()
    connection, waba, creds = _conexion(tenant_id)
    row = SimpleNamespace(id=uuid.uuid4(), name="hello_world", status="APPROVED", waba_account_id=waba.id)

    async def _resolve(cid, tid):
        return connection, waba, creds

    async def _owned(session, model, template_id, tid):
        return row

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _delete(*args, **kwargs):
        raise GraphError("Error de Meta: Invalid parameter", category="PERMANENT", code=100, http_status=400)

    monkeypatch.setattr(svc, "resolve_waba_and_credentials", _resolve)
    monkeypatch.setattr(svc, "get_owned", _owned)
    monkeypatch.setattr(svc, "get_session", lambda: _Session())
    monkeypatch.setattr(templates_mod, "delete_template_all_languages", _delete)

    error = await svc.delete_template(tenant_id, str(connection.id), str(row.id))
    assert error is not None and error.code == "not_deletable"
    assert "Meta no permite eliminar la plantilla de ejemplo «hello_world»" in error.message
    assert row.status == "APPROVED"

    # Una excepción que no es de Graph tampoco termina en 500.
    async def _boom(*args, **kwargs):
        raise RuntimeError("socket cerrado")

    monkeypatch.setattr(templates_mod, "delete_template_all_languages", _boom)
    error = await svc.delete_template(tenant_id, str(connection.id), str(row.id))
    assert error is not None and error.code == "delete_failed"
    assert "socket" not in error.message

    # Una plantilla del mismo negocio pero de otra WABA: 404, sin llamar a Meta.
    row.waba_account_id = uuid.uuid4()
    error = await svc.delete_template(tenant_id, str(connection.id), str(row.id))
    assert error is not None and error.code == "not_found"


def test_nombres_de_plantillas_de_ejemplo() -> None:
    from core.admin.meta_templates_service import es_plantilla_de_ejemplo, mensaje_no_eliminable

    assert es_plantilla_de_ejemplo("hello_world")
    assert es_plantilla_de_ejemplo("sample_shipping_confirmation")
    assert es_plantilla_de_ejemplo("jaspers_market_order_confirmation_v1")
    assert not es_plantilla_de_ejemplo("pedido_cancelado")
    assert "de ejemplo" not in mensaje_no_eliminable("pedido_cancelado")


# ------------------------------------------------------------------ 4. Meta


def test_error_de_limite_de_numeros_es_de_meta() -> None:
    from core.admin.meta_numbers_service import (
        es_error_de_limite_de_numeros,
        mensaje_limite_de_numeros,
    )
    from core.gateway.meta_errors import GraphError

    exc = GraphError(
        "Error de Meta: Invalid parameter",
        category="PERMANENT",
        code=100,
        error_user_title="Límite de números alcanzado",
        error_user_msg="Esta cuenta excede el límite de números de teléfono permitidos.",
    )
    assert es_error_de_limite_de_numeros(exc)
    assert not es_error_de_limite_de_numeros(GraphError("Error de Meta: Invalid parameter", category="PERMANENT", code=100))
    assert not es_error_de_limite_de_numeros(RuntimeError("limit"))
    texto = mensaje_limite_de_numeros(exc, 1)
    assert "no es un límite del plan de SolversHub" in texto
    assert "1 número(s) activo(s)" in texto
    assert "Límite de números alcanzado" in texto


def test_accion_primary_en_meta_es_una_regla_explicada() -> None:
    from core.api.routers.meta_ops import PRIMARY_NOT_APPLICABLE, MetaConnectionAction

    assert MetaConnectionAction(action="primary").action == "primary"
    assert "conexiones manuales" in PRIMARY_NOT_APPLICABLE


def test_alta_byok_expone_reused() -> None:
    from core.api.routers.meta_wizard import WizardByokOut

    out = WizardByokOut(connectionId="c", environment="sandbox", status="connected", phoneNumberId="1")
    assert out.reused is False
    assert WizardByokOut.model_fields["reused"].description is None  # documentado en el comentario del modelo


# ------------------------------------------------------------------ 5. revisión de seguridad


def test_topes_superiores_y_sin_inf_nan() -> None:
    """Montos y conteos con tope (lo que cabe en la columna) y sin `inf`/`nan`."""
    from pack_cobranza.admin_api import RulesUpdate
    from pack_commerce.admin_api import CategorySave, ProductSave
    from pack_commerce.config import CRMConfig

    assert set(_errores(ProductSave, name="x", price=1e11, stock=10**10)) == {"price", "stock"}
    assert "price" in _errores(ProductSave, name="x", price=float("inf"))
    assert "sort_order" in _errores(CategorySave, name="x", sort_order=10**9)
    assert set(
        _errores(
            CRMConfig,
            customer_frecuente_min_pedidos=10**6,
            customer_inactivo_dias_sin_comprar=10**6,
            monto_alto_threshold=float("nan"),
            pago_en_linea_monto_minimo=1e13,
        )
    ) == {
        "customer_frecuente_min_pedidos",
        "customer_inactivo_dias_sin_comprar",
        "monto_alto_threshold",
        "pago_en_linea_monto_minimo",
    }
    assert "umbral_saldo_autonomo" in _errores(RulesUpdate, umbral_saldo_autonomo=float("inf"))
    assert ProductSave(name="x", price=9_999_999_999, stock=1_000_000_000).stock == 1_000_000_000


def test_checkout_fields_desconocidos_no_reflejan_el_valor() -> None:
    fields = _errores(_crm(), checkout_fields={"domicilio": ["valor-secreto-xyz"]})
    assert "valor-secreto-xyz" not in str(fields)
    assert "full_name" in fields["checkout_fields"]


def _crm():
    from pack_commerce.config import CRMConfig

    return CRMConfig


@pytest.mark.asyncio
async def test_save_config_invalida_no_lleva_el_valor_en_el_mensaje() -> None:
    from pack_payments.config import PaymentsConfig

    from core.plugin_manager import PluginConfigError, PluginManager

    manager = PluginManager()
    manager._modules["payments"] = SimpleNamespace(config_schema=PaymentsConfig)  # type: ignore[assignment]
    with pytest.raises(PluginConfigError) as exc:
        await manager.save_config(
            "payments",
            {"public_key": "p", "private_key": "priv-secreto-123", "events_secret": "e", "link_expira_horas": -9},
        )
    assert "priv-secreto-123" not in str(exc.value)
    assert "-9" not in str(exc.value)
    assert "link_expira_horas" in str(exc.value)
    assert exc.value.fields == {"link_expira_horas": "Debe ser mayor o igual que 1."}


def _preparar_sync(monkeypatch, redis, sync):
    import core.admin.meta_templates_service as svc
    import core.gateway.meta_template_sync as sync_mod

    tenant_id = uuid.uuid4()
    connection, waba, creds = _conexion(tenant_id)

    async def _resolve(cid, tid):
        return connection, waba, creds

    monkeypatch.setattr(svc, "resolve_waba_and_credentials", _resolve)
    monkeypatch.setattr(sync_mod, "sync_templates", sync)
    monkeypatch.setattr(sync_mod, "get_redis", lambda: redis)
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)
    return svc, tenant_id, connection


@pytest.mark.asyncio
async def test_sync_manual_fallido_no_consume_el_minuto(monkeypatch) -> None:
    from core.gateway.meta_errors import GraphError

    redis = _FakeRedis()
    llamadas: list[int] = []

    async def _sync(*args):
        llamadas.append(1)
        if len(llamadas) == 1:
            raise GraphError("Error de Meta: caído", category="TRANSIENT", code=2)
        return []

    svc, tenant_id, connection = _preparar_sync(monkeypatch, redis, _sync)
    error = await svc.sync_templates_now(tenant_id, str(connection.id))
    assert error is not None and error.code == "sync_failed"
    assert not any(k.startswith("tpl_sync_manual:") for k in redis.keys)
    # Reintento inmediato: no responde «espera un minuto».
    assert await svc.sync_templates_now(tenant_id, str(connection.id)) is None
    assert len(llamadas) == 2


class _RedisCaido:
    async def set(self, *args, **kwargs):
        raise ConnectionError("redis caído")

    async def delete(self, *args, **kwargs):
        raise ConnectionError("redis caído")


@pytest.mark.asyncio
async def test_sync_manual_con_redis_caido_no_da_500(monkeypatch) -> None:
    llamadas: list[int] = []

    async def _sync(*args):
        llamadas.append(1)
        return []

    svc, tenant_id, connection = _preparar_sync(monkeypatch, _RedisCaido(), _sync)
    assert await svc.sync_templates_now(tenant_id, str(connection.id)) is None
    assert llamadas == [1]

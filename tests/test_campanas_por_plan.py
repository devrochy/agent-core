"""HU-BILL-26 · campañas, recordatorios y seguimiento de leads por plan (D-79, `features.campanas`).

Sin BD: el orden de los niveles, el 409 `plan_feature_not_included`, sin suscripción no se bloquea, la vista previa
que no arma un borrador en Basic, la ruta que traduce el 409, el recordatorio frenado que el core deja `failed` y el
seguimiento de leads que en Basic no envía y en Premium sí. Contra la BD real:
`tests/integration/test_campanas_por_plan_db.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.billing import campanas_del_plan as campanas
from core.shared.tenant import tenant_scope

TID = uuid.UUID("26000000-0000-0000-0000-000000000026")


def _sub(plan_id: str | None):
    async def _get(*_a, **_k):
        return None if plan_id is None else SimpleNamespace(plan_id=plan_id)

    return _get


# ---------------------------------------------------------------- puras


@pytest.mark.parametrize(
    ("plan_id", "nivel"),
    [("free", "ninguna"), ("basic", "recordatorios"), ("premium", "completas"), ("empresarial", "completas")],
)
def test_nivel_de_cada_plan(plan_id: str, nivel: str) -> None:
    assert campanas.nivel_de_campanas(plan_id) == nivel


@pytest.mark.parametrize(
    ("plan_id", "recordatorios", "completas"),
    [
        ("free", False, False),
        ("basic", True, False),
        ("premium", True, True),
        ("empresarial", True, True),
        ("no_existe", False, False),  # pura: el plan desconocido no incluye nada (la tolerancia es de quien llama)
        (None, False, False),
    ],
)
def test_orden_ninguna_recordatorios_completas(plan_id, recordatorios: bool, completas: bool) -> None:
    assert campanas.plan_incluye_campanas(plan_id, "recordatorios") is recordatorios
    assert campanas.plan_incluye_campanas(plan_id, "completas") is completas


@pytest.mark.parametrize("tipo", ["ninguna", "campañas", ""])
def test_tipo_desconocido_es_un_error_de_programacion(tipo: str) -> None:
    with pytest.raises(ValueError):
        campanas.plan_incluye_campanas("premium", tipo)


# ---------------------------------------------------------------- por negocio


async def test_basic_sin_campanas_completas_es_plan_feature_not_included(monkeypatch) -> None:
    from core.api.plan_limits import plan_limit_error

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("basic"))
    with pytest.raises(campanas.CampanaNoIncluida) as exc:
        await campanas.exigir_campana(TID, "completas")
    assert exc.value.code == "plan_feature_not_included"
    assert exc.value.details == {"feature": "campanas", "plan": "basic"}
    assert "Basic" in exc.value.message and "Premium" in exc.value.message
    error = plan_limit_error(exc.value)
    assert error.status_code == 409
    assert error.code == "plan_feature_not_included"
    # En Basic los recordatorios sí están.
    await campanas.exigir_campana(TID, "recordatorios")
    assert await campanas.campana_permitida(TID, "recordatorios")
    assert not await campanas.campana_permitida(TID, "completas")
    assert await campanas.plan_que_frena(TID, "completas") == "basic"
    assert await campanas.plan_que_frena(TID, "recordatorios") is None


async def test_free_no_incluye_recordatorios(monkeypatch) -> None:
    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("free"))
    assert await campanas.plan_que_frena(TID, "recordatorios") == "free"
    with pytest.raises(campanas.CampanaNoIncluida) as exc:
        await campanas.exigir_campana(TID, "recordatorios")
    assert exc.value.details == {"feature": "campanas", "plan": "free"}


@pytest.mark.parametrize("plan_id", [None, "plan_retirado"])
async def test_sin_suscripcion_o_sin_plan_conocido_no_se_bloquea(monkeypatch, plan_id) -> None:
    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub(plan_id))
    for tipo in ("recordatorios", "completas"):
        await campanas.exigir_campana(TID, tipo)
        assert await campanas.campana_permitida(TID, tipo)
        assert await campanas.plan_que_frena(TID, tipo) is None


# ---------------------------------------------------------------- envío a segmento


async def test_basic_la_vista_previa_no_arma_borrador(monkeypatch) -> None:
    """Gherkin «Campañas» (lado de la vista previa): en Basic no se lee la conexión ni se guarda nada."""
    from core.admin import envios_segmento

    async def _no_debe_llamarse(*_a, **_k):
        raise AssertionError("no se debe tocar la conexión ni guardar el borrador")

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub("basic"))
    monkeypatch.setattr(envios_segmento, "conexion_de_envio", _no_debe_llamarse)
    monkeypatch.setattr(envios_segmento, "get_session", _no_debe_llamarse)
    with pytest.raises(campanas.CampanaNoIncluida):
        await envios_segmento.vista_previa(
            tenant_id=TID, admin_id=uuid.uuid4(), admin_label="", consulta=None, filtro={},  # type: ignore[arg-type]
            connection_id=uuid.uuid4(), template_id=uuid.uuid4(), variables={},
        )


async def test_la_ruta_de_confirmar_responde_409_plan_feature_not_included(monkeypatch) -> None:
    from core.api.errors import ApiError
    from core.api.routers import leads_acciones

    async def _confirmar(**_k):
        raise campanas.CampanaNoIncluida("completas", "basic", "Basic")

    async def _envio(*_a, **_k):
        raise AssertionError("no se arma la respuesta del envío")

    monkeypatch.setattr(leads_acciones.envios, "confirmar", _confirmar)
    monkeypatch.setattr(leads_acciones, "_envio", _envio)
    monkeypatch.setattr(leads_acciones, "require_owner_or_master", lambda _p: None)
    principal = SimpleNamespace(tenant_id=TID, admin=SimpleNamespace(id=uuid.uuid4()))
    with pytest.raises(ApiError) as exc:
        await leads_acciones.confirmar_envio(uuid.uuid4(), principal, "clave-de-prueba-26")  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert exc.value.code == "plan_feature_not_included"
    assert exc.value.details == {"feature": "campanas", "plan": "basic"}


def test_el_409_queda_documentado_en_las_dos_rutas() -> None:
    from core.api.routers import leads_acciones

    rutas = {r.path: r for r in leads_acciones.router.routes}
    for path in ("/admin/leads/acciones/plantilla/vista-previa", "/admin/leads/acciones/plantilla/{envio_id}/confirmar"):
        descripcion = rutas[path].responses[409]["description"]  # type: ignore[attr-defined]
        assert "plan_feature_not_included" in descripcion
        assert "{feature, plan}" in descripcion


# ---------------------------------------------------------------- recordatorio de cita frenado


@pytest.mark.parametrize(
    ("payload_frenado", "esperado"),
    [("plan_feature_not_included", "plan_feature_not_included"), (None, None), ("lo_que_sea", None)],
)
async def test_el_handler_pasa_el_freno_del_plan_al_aviso(monkeypatch, payload_frenado, esperado) -> None:
    """Gherkin «Recordatorios» (lado del core): el evento marcado por el cron llega a `send_event_notice` con el
    motivo; solo se aceptan los motivos conocidos (el payload no decide un `error_reason` cualquiera)."""
    from core.events import core_handlers
    from core.gateway import avisos_bandera, event_notices

    llamadas: list[dict] = []
    conversacion = SimpleNamespace(id=uuid.uuid4())

    async def _conversacion(_cid):
        return conversacion

    async def _apagada():
        return False

    async def _send(conversation_id, topic, context, **kw):
        llamadas.append({"conversation_id": conversation_id, "topic": topic, **kw})
        return event_notices.NoticeResult(kw.get("frenado") or "texto")

    class _Sesion:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a):
            return None

    monkeypatch.setattr(core_handlers, "_whatsapp_conversation_for", _conversacion)
    monkeypatch.setattr(core_handlers, "get_session", lambda: _Sesion())
    monkeypatch.setattr(avisos_bandera, "habilitada_actual", _apagada)
    monkeypatch.setattr(event_notices, "send_event_notice", _send)
    payload = {"appointment_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()), "horas": 24, "titulo": "Demo"}
    if payload_frenado:
        payload["frenado"] = payload_frenado
    async with tenant_scope(TID):
        await core_handlers.on_appointment_reminder(**payload)
    assert len(llamadas) == 1
    assert llamadas[0]["notice_key"] == f"appt_reminder:{payload['appointment_id']}:24"
    assert llamadas[0].get("frenado") == esperado


async def test_el_aviso_frenado_queda_failed_con_el_motivo_y_no_sale(monkeypatch) -> None:
    """`send_event_notice(frenado=…)` deja un solo `failed` con el motivo bajo el id estable del aviso, sin tocar
    el proveedor: el mismo registro que la pausa (D-76)."""
    from core.gateway import event_notices

    agregados: list = []
    conversacion = SimpleNamespace(id=uuid.uuid4(), tenant_id=TID)

    class _Sesion:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a):
            return conversacion

        def add(self, fila):
            agregados.append(fila)

    async def _no_enviado(_key):
        return False

    async def _no_debe_llamarse(*_a, **_k):
        raise AssertionError("un aviso frenado no mira la conexión ni la pausa ni envía")

    monkeypatch.setattr(event_notices, "get_session", lambda: _Sesion())
    monkeypatch.setattr(event_notices, "_ya_enviado", _no_enviado)
    monkeypatch.setattr(event_notices, "_registrar_aviso_frenado", _no_debe_llamarse)
    monkeypatch.setattr("core.gateway.meta_connection_state.motivo_pausa_de_conversacion", _no_debe_llamarse)
    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _no_debe_llamarse)
    monkeypatch.setattr("core.gateway.sender.send_template_to_conversation", _no_debe_llamarse)
    async with tenant_scope(TID):
        resultado = await event_notices.send_event_notice(
            conversacion.id, "appointment.reminder", {}, notice_key="appt_reminder:x:24",
            frenado="plan_feature_not_included",
        )
    assert resultado.modo == "plan_feature_not_included"
    assert len(agregados) == 1
    fila = agregados[0]
    assert (fila.status, fila.error_reason, fila.client_message_id) == (
        "failed", "plan_feature_not_included", "appt_reminder:x:24",
    )
    assert fila.tenant_id == TID


def test_los_motivos_de_freno_son_los_del_plan() -> None:
    from core.gateway.event_notices import MOTIVOS_FRENADO

    assert campanas.MOTIVO in MOTIVOS_FRENADO


# ---------------------------------------------------------------- seguimiento de leads


@pytest.mark.parametrize(("plan_id", "sale"), [("free", False), ("basic", False), ("premium", True), ("empresarial", True), (None, True)])
async def test_seguimiento_de_leads_solo_con_campanas_completas(monkeypatch, plan_id, sale: bool) -> None:
    """Gherkin «Seguimiento de leads»: en Basic (y Free) no se envía y la tarjeta queda `skipped` con el motivo; en
    Premium sí. Sin suscripción no se bloquea."""
    from core.leads import jobs
    from core.leads.config import ConfigCaptarLeads

    corridos: list[uuid.UUID] = []
    registros: list[dict] = []

    async def _negocios():
        return [TID]

    async def _si(*_a, **_k):
        return True

    async def _no(*_a, **_k):
        return False

    async def _config(_tid):
        return ConfigCaptarLeads()

    async def _del_negocio(tid, _cfg):
        corridos.append(tid)
        return 2, 2

    async def _registrar(tenant_id, cron, **kw):
        registros.append({"tenant_id": tenant_id, "cron": cron, **kw})

    async def _registrar_fallo(*_a, **_k):
        raise AssertionError("no falla")

    monkeypatch.setattr("core.billing.service.get_active_subscription", _sub(plan_id))
    monkeypatch.setattr(jobs, "_negocios_con_captar_leads", _negocios)
    monkeypatch.setattr(jobs, "debe_correr", _si)
    monkeypatch.setattr(jobs.pausa_automatica, "negocio_pausado", _no)
    monkeypatch.setattr(jobs.config_leads, "cargar_del_negocio", _config)
    monkeypatch.setattr(jobs, "_follow_up_leads_del_negocio", _del_negocio)
    monkeypatch.setattr(jobs, "registrar", _registrar)
    monkeypatch.setattr(jobs, "registrar_fallo", _registrar_fallo)
    await jobs.follow_up_leads()

    assert len(registros) == 1
    if sale:
        assert corridos == [TID]
        assert registros[0].get("status", "success") == "success"
        assert registros[0]["acciones"] == 2
    else:
        assert corridos == []
        assert registros[0]["status"] == "skipped"
        assert registros[0]["acciones"] == 0
        assert "plan" in registros[0]["resumen"]

"""HU-SH-10 / HU-AOS-57 · Onboarding obligatorio pero saltable, contra el stack real.

Negocio NUEVO con el General y el Coordinador del alta (SPEC-39) y `AOS_CONNECTION_ROUTING` encendida.
Gherkin:

- «Solo el propietario es abierto»: un negocio creado desde el corte queda `pendiente` y el panel debe
  abrirlo (`abrir`) solo al propietario; el master lo ve sin bloqueo. Uno anterior al corte `no_aplica`.
- «Omitir deja el aviso»: omitir guarda quién y cuándo, `avisar` queda encendido y `abrir` apagado;
  reanudar o avanzar lo retoma.
- «Terminar lo valida el servidor»: `finalizar` responde 409 `faltan_pasos` con lo que falta.
- «Dejar listo»: crea el chat web CERRADO, publica el agente del guion y activa su versión, sin duplicar
  nada al repetir; con un chat web que ya atiende clientes, 409 `canal_en_vivo`.
- «Probar el agente nuevo»: el playground acepta `connection_id` (solo del negocio) y la conversación de
  prueba queda atada a esa conexión; esa primera prueba habilita `finalizar`.
Sin LLM ni red.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PACKS = ["pack_commerce"]
PASSWORD = "clave-segura-123"


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


@pytest.fixture(scope="module", autouse=True)
def _alta_con_agent_os():
    import _alta_agent_os

    _alta_agent_os.ACTIVO = True
    yield
    _alta_agent_os.ACTIVO = False


@pytest.fixture(autouse=True)
def _ruteo_y_corte(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    monkeypatch.setattr(get_settings(), "onboarding_desde", date(2020, 1, 1))


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hush10")
    tid, _ = await onboard_tenant(name=f"HUSH10 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
    return tid


async def _persona(app_client, tid: uuid.UUID, rol: str) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('onb')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=rol))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _recorrido(app_client, h: dict[str, str]) -> dict[str, Any]:
    r = await app_client.get("/api/v1/admin/onboarding", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["recorrido"]


async def test_abre_solo_al_propietario_y_los_negocios_anteriores_al_corte_no_aplican(
    app_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    colaborador = await _persona(app_client, tid, "colaborador")
    master = await api_headers(app_client, tid)

    r = await _recorrido(app_client, dueno)
    assert (r["estado"], r["aplica"], r["abrir"], r["avisar"], r["total_pasos"]) == ("pendiente", True, True, False, 6)
    # El master ve el estado, pero no lo bloquea; el colaborador tampoco es redirigido.
    assert (await _recorrido(app_client, master))["abrir"] is False
    assert (await _recorrido(app_client, colaborador))["abrir"] is False

    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "onboarding_desde", date.today() + timedelta(days=1))
    r = await _recorrido(app_client, dueno)
    assert (r["estado"], r["aplica"], r["abrir"]) == ("no_aplica", False, False)
    # Y sin recorrido no se puede mover.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=dueno)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida")


async def test_avanzar_omitir_y_reanudar_dejan_rastro_y_solo_los_mueve_el_propietario(app_client) -> None:
    tid = await _negocio()
    dueno = await _persona(app_client, tid, "owner")
    colaborador = await _persona(app_client, tid, "colaborador")

    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 3})
    assert r.status_code == 200, r.text
    assert (r.json()["estado"], r.json()["paso"], r.json()["avisar"], r.json()["abrir"]) == ("en_curso", 3, True, False)

    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=dueno)
    assert r.status_code == 200, r.text
    omitido = r.json()
    assert (omitido["estado"], omitido["paso"], omitido["avisar"], omitido["abrir"]) == ("omitido", 3, True, False)
    assert omitido["omitido_en"] is not None
    # Persistido en el negocio (no en el navegador) y auditado.
    assert (await _recorrido(app_client, dueno))["estado"] == "omitido"
    auditoria = await _sql(tid, "select input->>'accion' from audit_log where action = 'onboarding_recorrido' order by created_at")
    assert [a for (a,) in auditoria] == ["avanzar", "omitir"]

    r = await app_client.post("/api/v1/admin/onboarding/reanudar", headers=dueno)
    assert (r.status_code, r.json()["estado"], r.json()["paso"]) == (200, "en_curso", 3)
    r = await app_client.post("/api/v1/admin/onboarding/reanudar", headers=dueno)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida")

    # Fuera de rango: lo frena el esquema.
    r = await app_client.post("/api/v1/admin/onboarding/avanzar", headers=dueno, json={"paso": 9})
    assert r.status_code == 422

    # Un colaborador con permiso de escribir no mueve el recorrido del negocio.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=colaborador)
    assert r.status_code == 403, r.text


async def test_finalizar_lo_valida_el_servidor_y_dejar_listo_publica_y_activa_sin_duplicar(
    app_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = await _negocio()
    h = await _persona(app_client, tid, "owner")

    r = await app_client.post("/api/v1/admin/onboarding/finalizar", headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "faltan_pasos"), r.text
    assert r.json()["error"]["details"]["faltan"] == ["datos_negocio", "agente", "agente_publicado", "prueba"]

    # El negocio se describe (paso 1 del asistente: lo calcula el checklist de siempre).
    r = await app_client.put(
        "/api/v1/admin/settings/identity",
        headers=h,
        json={"assistant_name": "Ana", "business_description": "Vendo ropa deportiva en Manizales."},
    )
    assert r.status_code == 200, r.text

    r = await app_client.post(
        "/api/v1/admin/agents/from-script",
        headers=h,
        json={"template_key": "ventas", "answers": {"que_vendes": "productos", "largo_respuesta": "normal"}},
    )
    assert r.status_code == 201, r.text
    agente = r.json()["agent"]["id"]

    r = await app_client.post("/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": agente})
    assert r.status_code == 200, r.text
    listo = r.json()
    assert listo["listo"] is True and listo["release_id"]
    assert [(p["clave"], p["estado"]) for p in listo["pasos"]] == [
        ("conexion", "hecho"),
        ("publicar", "hecho"),
        ("activar", "hecho"),
    ]
    assert (listo["recorrido"]["agente_id"], listo["recorrido"]["conexion_id"]) == (agente, listo["conexion_id"])

    # El chat web nació cerrado al público y con la release del agente activa.
    conexion = await _sql(
        tid,
        "select active, active_release_id::text, type_id from channel_connections where id = :c",
        c=uuid.UUID(listo["conexion_id"]),
    )
    assert conexion == [(False, listo["release_id"], "webchat")]
    publicado = await _sql(
        tid,
        "select count(*) from agent_instances where connection_id = :c and agent_id = :a and deleted_at is null",
        c=uuid.UUID(listo["conexion_id"]),
        a=uuid.UUID(agente),
    )
    assert publicado == [(1,)]

    # Repetir no duplica el agente en la conexión ni crea otra conexión.
    r = await app_client.post("/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": agente})
    assert r.status_code == 200, r.text
    assert r.json()["conexion_id"] == listo["conexion_id"]
    # La segunda vez nada se crea: conexión y publicación ya estaban, y la versión ya es la activa.
    assert {p["estado"] for p in r.json()["pasos"]} == {"ya_estaba"}
    assert next(p["clave"] for p in r.json()["pasos"]) == "conexion"
    assert await _sql(tid, "select count(*) from channel_connections where type_id = 'webchat' and deleted_at is null") == [(1,)]
    assert await _sql(
        tid, "select count(*) from agent_instances where agent_id = :a and deleted_at is null", a=uuid.UUID(agente)
    ) == [(1,)]

    # Aún falta la prueba con ese agente.
    r = await app_client.post("/api/v1/admin/onboarding/finalizar", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["faltan"] == ["prueba"], r.text

    # Probar por una conexión ajena o inexistente se rechaza; por la del agente, el turno corre atado a ella.
    ajena = await _sql(None, "select id::text from channel_connections where tenant_id <> :t limit 1", t=tid)
    for invalida in (str(uuid.uuid4()), *(a for (a,) in ajena)):
        r = await app_client.post(
            "/api/v1/admin/playground/message", headers=h, json={"text": "Hola", "connection_id": invalida}
        )
        assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_connection"), r.text

    vistas: list[uuid.UUID | None] = []

    async def _turno(conversation_id: uuid.UUID, text_: str) -> Any:
        import types

        fila = await _sql(tid, "select connection_id from conversations where id = :c", c=conversation_id)
        vistas.append(fila[0][0])
        return types.SimpleNamespace(text=f"Respuesta a: {text_}", image_url=None, escalated=False)

    async def _turno_escalado(conversation_id: uuid.UUID, text_: str) -> Any:
        import types

        return types.SimpleNamespace(text="Tuve un inconveniente", image_url=None, escalated=True)

    # Un turno que escaló (p. ej. falló el modelo) no prueba nada: no habilita terminar.
    monkeypatch.setattr("core.orchestrator.loop.process_conversation", _turno_escalado)
    r = await app_client.post(
        "/api/v1/admin/playground/message",
        headers=h,
        json={"text": "¿Qué venden?", "connection_id": listo["conexion_id"]},
    )
    assert r.status_code == 200, r.text
    assert (await _recorrido(app_client, h))["probado_en"] is None

    monkeypatch.setattr("core.orchestrator.loop.process_conversation", _turno)
    r = await app_client.post(
        "/api/v1/admin/playground/message",
        headers=h,
        json={"text": "¿Qué venden?", "connection_id": listo["conexion_id"]},
    )
    assert r.status_code == 200, r.text
    assert vistas == [uuid.UUID(listo["conexion_id"])]
    assert (await _recorrido(app_client, h))["probado_en"] is not None

    # La prueba de siempre (sin conexión) sigue por la conexión interna del playground, no por la del agente.
    r = await app_client.post("/api/v1/admin/playground/message", headers=h, json={"text": "Hola"})
    assert r.status_code == 200, r.text
    assert vistas[-1] != uuid.UUID(listo["conexion_id"])

    r = await app_client.post("/api/v1/admin/onboarding/finalizar", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["estado"], r.json()["avisar"], r.json()["completado_en"] is not None) == ("completo", False, True)
    # Ya completo, no se vuelve a mover.
    r = await app_client.post("/api/v1/admin/onboarding/omitir", headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "transicion_invalida")


async def test_un_chat_web_que_ya_atiende_clientes_no_se_toca_desde_el_primer_uso(app_client) -> None:
    tid = await _negocio()
    h = await _persona(app_client, tid, "owner")
    r = await app_client.put("/api/v1/admin/settings/web-chat", headers=h, json={"enabled": True, "allowed_origins": []})
    assert r.status_code == 200, r.text
    r = await app_client.post(
        "/api/v1/admin/agents/from-script", headers=h, json={"template_key": "ventas", "answers": {}}
    )
    assert r.status_code == 201, r.text

    r = await app_client.post("/api/v1/admin/onboarding/dejar-listo", headers=h, json={"agent_id": r.json()["agent"]["id"]})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "canal_en_vivo"), r.text

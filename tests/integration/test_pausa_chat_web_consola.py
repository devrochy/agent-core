"""HU-AOS-23 · pausar y reanudar el chat web desde la consola de conexiones.

- **Un solo estado:** `POST /admin/connections/{id}/action` sobre la conexión `webchat` escribe el
  mismo interruptor que Configuración → Chat web (`app_settings.web_chat_config.enabled` y
  `channel_connections.active`); `GET /settings/web-chat` y la consola dicen lo mismo, en los dos
  sentidos.
- **Pausado no responde:** el endpoint público contesta `disabled` y un mensaje que ya había
  entrado se guarda sin llamar al LLM; al reanudar vuelve a responder.
- **Auditoría:** `connection.paused` / `connection.resumed` desde la consola y desde Configuración.
- **Permisos:** sin escritura en `connections`, la consola no ofrece el botón (`can_edit`) y la
  acción da 403 sin tocar nada. Primaria y borrar siguen fuera para el chat web (404).
- **Aislamiento:** pausar el chat web de un negocio no toca el de otro.

El LLM es el `FakeProvider` del conftest o uno que falla si lo llaman: nunca hay LLM real.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_agentes_versionados import PASSWORD
from test_consola_de_conexiones import _colaborador
from test_kernel_ingress import _filas, _inmediato, _negocio, _sin_llm, _turnos

pytestmark = [
    pytest.mark.aislamiento,  # pausa por negocio sobre el chat web (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

ORIGEN = "https://tienda.example"
SETTINGS = "/api/v1/admin/settings/web-chat"
CONSOLA = "/api/v1/admin/connections/console"


def _accion(cid: uuid.UUID | str) -> str:
    return f"/api/v1/admin/connections/{cid}/action"


async def _widget(app_client, tid: uuid.UUID) -> tuple[uuid.UUID, str, dict[str, str]]:
    h = await api_headers(app_client, tid)
    r = await app_client.put(SETTINGS, headers=h, json={"enabled": True, "allowed_origins": [ORIGEN]})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    (fila,) = await _filas(
        tid, "SELECT id FROM channel_connections WHERE tenant_id = :t AND type_id = 'webchat' AND deleted_at IS NULL", t=tid
    )
    return fila["id"], key, h


async def _estado(app_client, h: dict[str, str], tid: uuid.UUID, cid: uuid.UUID) -> dict[str, Any]:
    """Lo que dicen Configuración, la consola, `app_settings` y la conexión."""
    config = (await app_client.get(SETTINGS, headers=h)).json()
    (fila,) = [f for f in (await app_client.get(CONSOLA, headers=h)).json()["items"] if f["id"] == str(cid)]
    (legacy,) = await _filas(tid, "SELECT web_chat_config FROM app_settings WHERE tenant_id = :t AND deleted_at IS NULL", t=tid)
    (cc,) = await _filas(tid, "SELECT active FROM channel_connections WHERE tenant_id = :t AND id = :c", t=tid, c=cid)
    return {
        "configuracion": config["enabled"],
        "consola": (fila["active"], fila["health"], fila["pausable"], fila["pause_scope"]),
        "app_settings": legacy["web_chat_config"]["enabled"],
        "conexion": cc["active"],
        "key": config["key"],
    }


async def _auditoria(tid: uuid.UUID, cid: uuid.UUID) -> list[str]:
    filas = await _filas(
        tid,
        "SELECT action FROM audit_log WHERE tenant_id = :t AND input->>'connection_id' = :c "
        "AND action LIKE 'connection.%' ORDER BY created_at",
        t=tid, c=str(cid),
    )
    return [f["action"] for f in filas]


async def _publico(app_client, key: str, visitante: str):
    return await app_client.post(
        f"/api/v1/public/web-chat/{key}/messages", headers={"Origin": ORIGEN}, json={"content": "hola", "visitor_id": visitante}
    )


async def _entrar_web(tid: uuid.UUID, key: str, visitante: str) -> uuid.UUID:
    """Un mensaje del visitante que ya estaba en la cola cuando se pausó: lo procesa el consumer."""
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(
            {
                "tenant_id": str(tid), "provider": "web", "channel": "web",
                "external_message_id": f"web:{uuid.uuid4()}", "customer_external_id": f"web:{visitante}",
                "customer_name": "", "content": "hola, ¿están?", "connection_external_id": key,
                "media": "[]", "media_context": "",
            }
        )
    (fila,) = await _filas(
        tid,
        "SELECT cv.id FROM conversations cv JOIN customers c ON c.id = cv.customer_id "
        "WHERE c.external_id = :e ORDER BY cv.started_at DESC LIMIT 1",
        e=f"web:{visitante}",
    )
    return fila["id"]


# ---------------------------------------------------------------- pausar y reanudar
async def test_pausado_desde_la_consola_no_responde_y_al_reanudar_vuelve(app_client, monkeypatch) -> None:
    import core.tasks.push_tasks as push_tasks

    tid = await _negocio()
    cid, key, h = await _widget(app_client, tid)
    _inmediato(monkeypatch)
    monkeypatch.setattr(push_tasks.notify_owners_push, "delay", lambda *_a, **_k: None)
    visitante = uniq("vis")

    r = await app_client.post(_accion(cid), headers=h, json={"action": "deactivate"})
    assert r.status_code == 204, r.text

    r = await _publico(app_client, key, visitante)
    assert (r.status_code, r.json()) == (403, {"error": "disabled"})
    r = await app_client.get(f"/api/v1/public/web-chat/{key}/config")
    assert (r.status_code, r.json()) == (403, {"error": "disabled"})

    with monkeypatch.context() as m:  # lo que ya estaba en la cola no llama al LLM
        _sin_llm(m)
        conv = await _entrar_web(tid, key, visitante)
    assert await _turnos(tid, conv) == [("blocked", "connection_paused", "silent")]

    r = await app_client.post(_accion(cid), headers=h, json={"action": "activate"})
    assert r.status_code == 204, r.text
    assert (await _publico(app_client, key, visitante)).status_code == 200
    await _entrar_web(tid, key, visitante)
    turnos = await _turnos(tid, conv)
    assert len(turnos) == 2 and turnos[1][1] is None, "reanudado, el turno vuelve a correr"


# ---------------------------------------------------------------- un solo estado
async def test_el_estado_coincide_con_configuracion_en_los_dos_sentidos(app_client) -> None:
    tid = await _negocio()
    cid, key, h = await _widget(app_client, tid)
    assert await _estado(app_client, h, tid, cid) == {
        "configuracion": True, "consola": (True, "ok", True, "connection"),
        "app_settings": True, "conexion": True, "key": key,
    }

    # Consola → Configuración: pausar no rota la clave ni toca los dominios.
    assert (await app_client.post(_accion(cid), headers=h, json={"action": "deactivate"})).status_code == 204
    assert await _estado(app_client, h, tid, cid) == {
        "configuracion": False, "consola": (False, "paused", True, "connection"),
        "app_settings": False, "conexion": False, "key": key,
    }
    assert (await app_client.get(SETTINGS, headers=h)).json()["allowed_origins"] == [ORIGEN]

    # Configuración → consola: activar desde Configuración reanuda.
    r = await app_client.put(SETTINGS, headers=h, json={"enabled": True, "allowed_origins": [ORIGEN]})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert (await _estado(app_client, h, tid, cid))["consola"] == (True, "ok", True, "connection")


async def test_desincronizado_con_app_settings_falla_cerrado_sin_tocar_nada(app_client) -> None:
    """Si `app_settings` no tiene el widget de esa conexión (dato roto), no se repara en silencio
    ni se genera clave: 409 y todo queda como estaba."""
    tid = await _negocio()
    cid, _key, h = await _widget(app_client, tid)
    await _filas(tid, "UPDATE app_settings SET web_chat_config = '{}'::jsonb WHERE tenant_id = :t RETURNING id", t=tid)

    assert (await app_client.post(_accion(cid), headers=h, json={"action": "deactivate"})).status_code == 409
    (cc,) = await _filas(tid, "SELECT active FROM channel_connections WHERE tenant_id = :t AND id = :c", t=tid, c=cid)
    (legacy,) = await _filas(tid, "SELECT web_chat_config FROM app_settings WHERE tenant_id = :t", t=tid)
    assert (cc["active"], legacy["web_chat_config"]) == (True, {})
    assert await _auditoria(tid, cid) == []


# ---------------------------------------------------------------- auditoría
async def test_pausar_y_reanudar_queda_auditado_desde_la_consola_y_desde_configuracion(app_client) -> None:
    tid = await _negocio()
    cid, _key, h = await _widget(app_client, tid)
    assert await _auditoria(tid, cid) == [], "crear el widget no es reanudar"

    for accion in ("deactivate", "activate"):
        assert (await app_client.post(_accion(cid), headers=h, json={"action": accion})).status_code == 204
    for enabled in (False, False, True):  # guardar sin cambiar el interruptor no audita
        r = await app_client.put(SETTINGS, headers=h, json={"enabled": enabled, "allowed_origins": [ORIGEN]})
        assert r.status_code == 200
    assert await _auditoria(tid, cid) == [
        "connection.paused", "connection.resumed", "connection.paused", "connection.resumed",
    ]


# ---------------------------------------------------------------- permisos y alcance
async def test_sin_permiso_de_editar_conexiones_no_hay_boton_ni_accion(app_client) -> None:
    tid = await _negocio()
    cid, _key, h = await _widget(app_client, tid)
    solo_ver = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"connections": "read", "settings": "write"}), password=PASSWORD
    )
    assert (await app_client.get(CONSOLA, headers=solo_ver)).json()["can_edit"] is False
    r = await app_client.post(_accion(cid), headers=solo_ver, json={"action": "deactivate"})
    assert r.status_code == 403
    assert (await _estado(app_client, h, tid, cid))["conexion"] is True
    assert await _auditoria(tid, cid) == []

    editor = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"connections": "write"}), password=PASSWORD
    )
    assert (await app_client.get(CONSOLA, headers=editor)).json()["can_edit"] is True
    assert (await app_client.post(_accion(cid), headers=editor, json={"action": "deactivate"})).status_code == 204


async def test_primaria_y_borrar_siguen_fuera_y_otro_negocio_no_se_entera(app_client) -> None:
    tid_a, tid_b = await _negocio(), await _negocio()
    cid_a, _key_a, h_a = await _widget(app_client, tid_a)
    cid_b, key_b, h_b = await _widget(app_client, tid_b)

    for accion in ("primary", "delete"):
        assert (await app_client.post(_accion(cid_a), headers=h_a, json={"action": accion})).status_code == 404
    # El id del chat web de B con la sesión de A: 404 y B sigue atendiendo.
    assert (await app_client.post(_accion(cid_b), headers=h_a, json={"action": "deactivate"})).status_code == 404
    assert (await app_client.post(_accion(cid_a), headers=h_a, json={"action": "deactivate"})).status_code == 204
    assert (await _estado(app_client, h_b, tid_b, cid_b))["conexion"] is True
    assert (await _publico(app_client, key_b, uniq("vis"))).status_code == 200
    assert await _auditoria(tid_b, cid_b) == []
    assert await _auditoria(tid_a, cid_b) == []

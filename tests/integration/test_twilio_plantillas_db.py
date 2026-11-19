"""ADR-0186 (PR-6) · plantillas de Twilio contra Postgres real, con la Content API simulada.

- Crear: fila local y Content en Twilio; nombre repetido es 409; un corte a mitad deja `creating` y repetir el
  pedido reconcilia por nombre (no duplica el Content).
- Enviar a aprobación (idempotente), polling por el cron (`next_poll_at`), aprobada y rechazada.
- Sync: trae las creadas en la consola de Twilio y adopta la que quedó en `creating`.
- Borrar: lógico aquí y Content borrado en Twilio.
- Aislamiento: otro negocio no ve ni consulta la plantilla; el enumerador del cron solo da `(id, tenant_id)`.
- API: crear, listar, sync con tope de uno por minuto.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

XE = "XE" + "a" * 32


class ContentApi:
    """Cuenta de Twilio con un sender ONLINE y una Content API en memoria."""

    def __init__(self, ac: str, e164: str) -> None:
        self.ac, self.e164 = ac, e164
        self.webhook: dict[str, Any] = {}
        self.contents: dict[str, dict[str, Any]] = {}
        self.fallar_creacion = False
        self.solicitudes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        if path.endswith(f"/Accounts/{self.ac}.json"):
            return httpx.Response(
                200, json={"status": "active", "friendly_name": "C", "type": "Full"}
            )
        if path == f"/v2/Channels/Senders/{XE}":
            if m == "POST":
                self.webhook = json.loads(request.content)["webhook"]
            return httpx.Response(
                200,
                json={
                    "sid": XE,
                    "sender_id": f"whatsapp:{self.e164}",
                    "status": "ONLINE",
                    "webhook": self.webhook,
                },
            )
        if path == "/v1/Content" and m == "POST":
            body = json.loads(request.content)
            sid = "HX" + uuid.uuid4().hex
            self.contents[sid] = {"sid": sid, **body, "approval_requests": {}}
            if self.fallar_creacion:
                return httpx.Response(503)  # Twilio lo creó pero la respuesta no llegó
            return httpx.Response(201, json=self.contents[sid])
        if path == "/v1/ContentAndApprovals":
            return httpx.Response(200, json={"contents": list(self.contents.values()), "meta": {}})
        if path.startswith("/v1/Content/HX"):
            sid = path.split("/")[3]
            if sid not in self.contents:
                return httpx.Response(404, json={"code": 20404})
            c = self.contents[sid]
            if path.endswith("/ApprovalRequests/whatsapp") and m == "POST":
                self.solicitudes += 1
                c["approval_requests"] = {**json.loads(request.content), "status": "received"}
                return httpx.Response(201, json=c["approval_requests"])
            if path.endswith("/ApprovalRequests"):
                return httpx.Response(
                    200, json={"whatsapp": c["approval_requests"] or {"status": "unsubmitted"}}
                )
            if m == "DELETE":
                del self.contents[sid]
                return httpx.Response(204)
        return httpx.Response(404, json={"code": 20404})


@pytest.fixture
def twilio(monkeypatch) -> list[ContentApi]:
    from agentcore_channel_twilio import client as client_mod

    cuentas: list[ContentApi] = []
    original = client_mod.TwilioClient.__init__

    def _init(self, account_sid, auth_token, *, transport=None):
        cuenta = next((c for c in reversed(cuentas) if c.ac == account_sid), cuentas[-1])
        original(
            self,
            account_sid,
            auth_token,
            transport=transport or httpx.MockTransport(cuenta.handler),
        )

    monkeypatch.setattr(client_mod.TwilioClient, "__init__", _init)
    return cuentas


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.gateway.twilio import bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados = []
    for prefijo in ("tpa", "tpb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(
            name=f"Twilio {slug}", slug=slug, packs=[], sembrar_ejemplos=False
        )
        async with tenant_scope(tid):
            await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _conectar(tid: uuid.UUID, cuentas: list[ContentApi]) -> tuple[ContentApi, uuid.UUID]:
    from core.gateway.twilio import alta
    from core.shared.tenant import tenant_scope

    api = ContentApi("AC" + uuid.uuid4().hex, f"+1555{uuid.uuid4().int % 10_000_000:07d}")
    cuentas.append(api)
    async with tenant_scope(tid):
        hecha = await alta.conectar(tid, account_sid=api.ac, auth_token="tok", sender_sid=XE)
    return api, hecha.connection_id


async def _en(tid: uuid.UUID, fn, *a, **kw):
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await fn(*a, **kw)


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            r = await session.execute(text(sql), params)
            return list(r.all()) if r.returns_rows else []


_PLANTILLA = dict(
    nombre="recordatorio_cita",
    idioma="es",
    categoria="utility",
    cuerpo="Hola {{1}}, tu cita es el {{2}}",
    ejemplos={"1": "Ana", "2": "lunes"},
)


async def test_crear_aprobar_y_polling(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    creada = await _en(tid, plantillas.crear, tid, cid, **_PLANTILLA)
    assert (
        creada.estado == "unsubmitted"
        and creada.content_sid in api.contents
        and creada.categoria == "UTILITY"
    )
    with pytest.raises(AltaError) as exc:
        await _en(tid, plantillas.crear, tid, cid, **_PLANTILLA)
    assert exc.value.code == "nombre_en_uso"

    enviada = await _en(tid, plantillas.enviar_a_aprobacion, tid, cid, creada.id)
    assert enviada.estado == "received" and enviada.enviada_at is not None
    again = await _en(tid, plantillas.enviar_a_aprobacion, tid, cid, creada.id)  # idempotente
    assert again.estado == "received" and api.solicitudes == 1

    # El cron la consulta cuando vence su `next_poll_at`.
    await _sql(
        tid,
        "update twilio_content_templates set next_poll_at = now() - interval '1 minute' where id = :i",
        i=creada.id,
    )
    api.contents[creada.content_sid]["approval_requests"]["status"] = "approved"
    assert await plantillas.revisar_pendientes() >= 1
    [(estado, proximo)] = await _sql(
        tid,
        "select approval_status, next_poll_at from twilio_content_templates where id = :i",
        i=creada.id,
    )
    assert (estado, proximo) == ("approved", None)
    assert [p.nombre for p in await _en(tid, plantillas.listar, tid, cid, solo_aprobadas=True)] == [
        "recordatorio_cita"
    ]


async def test_corte_a_mitad_se_reconcilia(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    api.fallar_creacion = True
    with pytest.raises(AltaError):
        await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "aviso_pago"})
    assert await _sql(
        tid,
        "select approval_status, content_sid from twilio_content_templates where friendly_name = 'aviso_pago' "
        "and channel_connection_id = :c",
        c=cid,
    ) == [("creating", None)]
    api.fallar_creacion = False
    hecha = await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "aviso_pago"})
    assert hecha.estado == "unsubmitted" and len(api.contents) == 1  # adoptó el que Twilio ya tenía
    # Adoptada por nombre: no se puede probar que la creó solvershub, así que no se borra en Twilio.
    await _en(tid, plantillas.borrar, tid, cid, hecha.id)
    assert hecha.content_sid in api.contents


async def test_sync_trae_las_de_la_consola_y_borrar(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    sid = "HX" + uuid.uuid4().hex
    api.contents[sid] = {
        "sid": sid,
        "friendly_name": "de_la_consola",
        "language": "es",
        "types": {"twilio/text": {"body": "Hola desde la consola"}},
        "variables": {},
        "approval_requests": {
            "status": "approved",
            "name": "de_la_consola",
            "category": "MARKETING",
        },
    }
    todas = await _en(tid, plantillas.sincronizar, tid, cid)
    [consola] = [p for p in todas if p.nombre == "de_la_consola"]
    assert (consola.estado, consola.categoria, consola.cuerpo) == (
        "approved",
        "MARKETING",
        "Hola desde la consola",
    )

    from core.gateway.twilio.alta import AltaError

    with pytest.raises(AltaError) as exc:  # uno por minuto
        await _en(tid, plantillas.sincronizar, tid, cid)
    assert exc.value.code == "sync_rate_limited"

    # Importada de la consola: se borra aquí, pero NO en Twilio (puede usarse fuera de solvershub).
    await _en(tid, plantillas.borrar, tid, cid, consola.id)
    assert sid in api.contents
    assert [p.nombre for p in await _en(tid, plantillas.listar, tid, cid)] == []
    # Creada desde solvershub: se borra también en Twilio.
    propia = await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "propia"})
    await _en(tid, plantillas.borrar, tid, cid, propia.id)
    assert propia.content_sid not in api.contents


async def test_otro_negocio_no_ve_ni_consulta(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas
    from core.gateway.twilio.alta import AltaError

    tid_a, tid_b = negocios
    _, cid = await _conectar(tid_a, twilio)
    creada = await _en(tid_a, plantillas.crear, tid_a, cid, **{**_PLANTILLA, "nombre": "solo_de_a"})
    assert (
        await _sql(tid_b, "select 1 from twilio_content_templates where id = :i", i=creada.id) == []
    )
    with pytest.raises(AltaError) as exc:
        await _en(tid_b, plantillas.refrescar, tid_b, creada.id)
    assert exc.value.code == "not_found"
    # El enumerador del cron solo expone ids.
    await _sql(
        tid_a, "update twilio_content_templates set next_poll_at = now() where id = :i", i=creada.id
    )
    filas = await _sql(None, "select * from admin_twilio_plantillas_por_revisar(10)")
    assert all(len(f) == 2 for f in filas) and (creada.id, tid_a) in filas


async def test_api_plantillas(negocios, twilio, app_client) -> None:
    from _api_helpers import api_headers
    from test_opciones_api import _headers

    tid, _ = negocios
    _, cid = await _conectar(tid, twilio)
    h = await api_headers(app_client, tid)
    base = f"/api/v1/admin/connections/twilio/{cid}/templates"
    r = await app_client.post(base, headers=h, json={**_PLANTILLA, "nombre": "por_api"})
    assert r.status_code == 201, r.text
    creada = r.json()
    r = await app_client.post(f"{base}/{creada['id']}/submit", headers=h)
    assert r.status_code == 200 and r.json()["estado"] == "received", r.text
    r = await app_client.get(base, headers=h)
    assert [p["nombre"] for p in r.json()] == ["por_api"]
    lector = await _headers(app_client, tid, rol="colaborador", secciones={"connections": "read"})
    assert (
        await app_client.post(base, headers=lector, json={**_PLANTILLA, "nombre": "no"})
    ).status_code == 403
    r = await app_client.post(base, headers=h, json={**_PLANTILLA, "nombre": "Mal Nombre"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "nombre_invalido"


async def test_sync_da_de_baja_lo_borrado_en_twilio_y_refrescar_no_se_traba(
    negocios, twilio
) -> None:
    from core.gateway.twilio import plantillas
    from core.gateway.twilio.alta import AltaError

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    queda = await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "queda"})
    se_va = await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "se_va"})
    del api.contents[se_va.content_sid]  # borrada en la consola de Twilio
    nombres = [p.nombre for p in await _en(tid, plantillas.sincronizar, tid, cid, manual=False)]
    assert nombres == ["queda"]

    # 404 al refrescar: la plantilla se da de baja.
    otra = await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "otra"})
    del api.contents[otra.content_sid]
    with pytest.raises(AltaError):
        await _en(tid, plantillas.refrescar, tid, otra.id)
    assert await _sql(
        tid, "select deleted_at is not null from twilio_content_templates where id = :i", i=otra.id
    ) == [(True,)]

    # Error no transitorio (token revocado): se aplaza una hora, no queda vencida para siempre.
    await _sql(
        tid,
        "update twilio_content_templates set next_poll_at = now() - interval '1 minute' where id = :i",
        i=queda.id,
    )
    original = api.handler

    def _denegado(request):
        if "/ApprovalRequests" in request.url.path:
            return httpx.Response(401, json={"code": 20003})
        return original(request)

    api.handler = _denegado
    with pytest.raises(AltaError):
        await _en(tid, plantillas.refrescar, tid, queda.id)
    [(aplazada,)] = await _sql(
        tid,
        "select next_poll_at > now() + interval '50 minutes' from twilio_content_templates where id = :i",
        i=queda.id,
    )
    assert aplazada is True


async def test_pausada_no_se_consulta(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    _, cid = await _conectar(tid, twilio)
    creada = await _en(tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "en_pausa"})
    await _sql(
        tid, "update twilio_content_templates set next_poll_at = now() where id = :i", i=creada.id
    )
    await _sql(tid, "update channel_connections set active = false where id = :c", c=cid)
    filas = await _sql(None, "select id from admin_twilio_plantillas_por_revisar(500)")
    assert (creada.id,) not in filas


async def test_sync_y_borrar_piden_propietario(negocios, twilio, app_client) -> None:
    from test_opciones_api import _headers

    tid, _ = negocios
    _, cid = await _conectar(tid, twilio)
    colaborador = await _headers(
        app_client, tid, rol="colaborador", secciones={"connections": "write"}
    )
    base = f"/api/v1/admin/connections/twilio/{cid}/templates"
    assert (await app_client.post(f"{base}/sync", headers=colaborador)).status_code == 403
    assert (
        await app_client.delete(f"{base}/{uuid.uuid4()}", headers=colaborador)
    ).status_code == 403
    r = await app_client.post(
        base, headers=colaborador, json={**_PLANTILLA, "nombre": "del_colaborador"}
    )
    assert r.status_code == 201, r.text


async def test_sync_con_lista_cortada_no_da_de_baja(negocios, twilio, monkeypatch) -> None:
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    viva = await _en(
        tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "viva_en_la_pagina_21"}
    )
    otra_sid = "HX" + uuid.uuid4().hex
    api.contents[otra_sid] = {
        "sid": otra_sid,
        "friendly_name": "de_la_pagina_1",
        "language": "es",
        "types": {"twilio/text": {"body": "Hola"}},
        "variables": {},
        "approval_requests": {},
    }

    async def _cortada(client):
        from agentcore_channel_twilio.content import ContentTemplate

        return [ContentTemplate.from_api(api.contents[otra_sid])], False

    monkeypatch.setattr(plantillas, "list_contents_completo", _cortada)
    nombres = {p.nombre for p in await _en(tid, plantillas.sincronizar, tid, cid, manual=False)}
    assert {"viva_en_la_pagina_21", "de_la_pagina_1"} <= nombres
    assert await _sql(
        tid, "select deleted_at is null from twilio_content_templates where id = :i", i=viva.id
    ) == [(True,)]


async def test_borrada_aqui_no_vuelve_y_nombre_raro_no_tumba_el_sync(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    sid = "HX" + uuid.uuid4().hex
    api.contents[sid] = {
        "sid": sid,
        "friendly_name": "importada",
        "language": "es",
        "types": {"twilio/text": {"body": "Hola"}},
        "variables": {},
        "approval_requests": {},
    }
    raro = "HX" + uuid.uuid4().hex
    api.contents[raro] = {
        "sid": raro,
        "friendly_name": "con_salto\n",
        "language": "es",
        "types": {"twilio/text": {"body": "Hola"}},
        "variables": {},
        "approval_requests": {},
    }
    [importada] = [
        p
        for p in await _en(tid, plantillas.sincronizar, tid, cid, manual=False)
        if p.nombre == "importada"
    ]
    await _en(tid, plantillas.borrar, tid, cid, importada.id)
    assert sid in api.contents  # importada: queda en Twilio
    nombres = [p.nombre for p in await _en(tid, plantillas.sincronizar, tid, cid, manual=False)]
    assert "importada" not in nombres and "con_salto\n" not in nombres


async def test_desconectar_da_de_baja_sus_plantillas(negocios, twilio) -> None:
    from core.gateway.connections import delete_connection
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    _, cid = await _conectar(tid, twilio)
    creada = await _en(
        tid, plantillas.crear, tid, cid, **{**_PLANTILLA, "nombre": "de_la_conexion"}
    )
    await _en(tid, delete_connection, cid)
    assert await _sql(
        tid,
        "select deleted_at is not null, next_poll_at from twilio_content_templates where id = :i",
        i=creada.id,
    ) == [(True, None)]


async def test_plantilla_de_otra_conexion_del_mismo_negocio(negocios, twilio, app_client) -> None:
    from _api_helpers import api_headers

    from core.gateway.twilio import plantillas

    tid, _ = negocios
    _, cid_x = await _conectar(tid, twilio)
    _, cid_y = await _conectar(tid, twilio)
    de_x = await _en(tid, plantillas.crear, tid, cid_x, **{**_PLANTILLA, "nombre": "de_x"})
    h = await api_headers(app_client, tid)
    base_y = f"/api/v1/admin/connections/twilio/{cid_y}/templates/{de_x.id}"
    assert (await app_client.post(f"{base_y}/submit", headers=h)).status_code == 404
    assert (await app_client.post(f"{base_y}/refresh", headers=h)).status_code == 404
    assert (await app_client.delete(base_y, headers=h)).status_code == 404


async def test_no_importa_contenidos_con_botones(negocios, twilio) -> None:
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    api, cid = await _conectar(tid, twilio)
    sid = "HX" + uuid.uuid4().hex
    api.contents[sid] = {
        "sid": sid,
        "friendly_name": "con_botones",
        "language": "es",
        "variables": {},
        "types": {
            "twilio/text": {"body": "Hola"},
            "twilio/quick-reply": {"body": "Hola", "actions": []},
        },
        "approval_requests": {"status": "approved"},
    }
    nombres = [p.nombre for p in await _en(tid, plantillas.sincronizar, tid, cid, manual=False)]
    assert "con_botones" not in nombres

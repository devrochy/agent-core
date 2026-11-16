"""HU-CONN-21 · las plantillas del negocio en todas sus conexiones de WhatsApp, contra Postgres real.

Meta con Graph simulado (`graph_fetch`) y Twilio con la Content API en memoria:
- Matriz: cada plantilla una vez (nombre e idioma) con su estado en cada conexión; «distinta» y «no replicable».
- Copiar Meta → Twilio: se crea con el mismo nombre, idioma, texto, variables y categoría y se envía a aprobación.
- Idempotencia: repetir no duplica («ya existía»); lo que quedó creado sin enviar se envía.
- No pisar: mismo nombre e idioma con otro texto queda «distinta» y no se toca.
- No replicable: encabezado con imagen, botones o variables con nombre hacia Twilio.
- Sandbox y conexión pausada no son destino. Copiar todas con resumen. Twilio → Meta.
- Aviso por evento con plantilla que falta en una conexión activa.
- Aislamiento: otro negocio no ve las conexiones ni puede copiar a ellas. API: lectura y copia.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
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

XE = "XE" + "b" * 32


class ContentApi:
    """Cuenta de Twilio con un sender ONLINE y una Content API en memoria."""

    def __init__(self, ac: str, e164: str) -> None:
        self.ac, self.e164 = ac, e164
        self.webhook: dict[str, Any] = {}
        self.contents: dict[str, dict[str, Any]] = {}
        self.solicitudes = 0
        self.fallar_aprobacion = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        if path.endswith(f"/Accounts/{self.ac}.json"):
            return httpx.Response(200, json={"status": "active", "friendly_name": "C", "type": "Full"})
        if path == f"/v2/Channels/Senders/{XE}":
            if m == "POST":
                self.webhook = json.loads(request.content)["webhook"]
            return httpx.Response(
                200,
                json={"sid": XE, "sender_id": f"whatsapp:{self.e164}", "status": "ONLINE", "webhook": self.webhook},
            )
        if path == "/v1/Content" and m == "POST":
            body = json.loads(request.content)
            sid = "HX" + uuid.uuid4().hex
            self.contents[sid] = {"sid": sid, **body, "approval_requests": {}}
            return httpx.Response(201, json=self.contents[sid])
        if path == "/v1/ContentAndApprovals":
            return httpx.Response(200, json={"contents": list(self.contents.values()), "meta": {}})
        if path.startswith("/v1/Content/HX"):
            sid = path.split("/")[3]
            if sid not in self.contents:
                return httpx.Response(404, json={"code": 20404})
            c = self.contents[sid]
            if path.endswith("/ApprovalRequests/whatsapp") and m == "POST":
                if self.fallar_aprobacion:
                    return httpx.Response(503)
                self.solicitudes += 1
                c["approval_requests"] = {**json.loads(request.content), "status": "received"}
                return httpx.Response(201, json=c["approval_requests"])
            if path.endswith("/ApprovalRequests"):
                return httpx.Response(200, json={"whatsapp": c["approval_requests"] or {"status": "unsubmitted"}})
        return httpx.Response(404, json={"code": 20404})


class GraphFalso:
    """WABA de Meta: guarda lo que se crea; `existentes` simula plantillas que Meta tiene y aquí no se sincronizaron."""

    def __init__(self, waba_id: str) -> None:
        self.waba_id = waba_id
        self.creadas: list[dict[str, Any]] = []
        self.existentes: list[dict[str, Any]] = []

    async def fetch(self, path, *, method="GET", body=None, query=None, **kwargs):
        from core.gateway.meta_errors import GraphError

        assert path == f"/{self.waba_id}/message_templates", path
        if method == "POST":
            if any(e["name"] == body["name"] and e["language"] == body["language"] for e in self.existentes):
                raise GraphError("duplicada", category="CONFIG", http_status=400)
            self.creadas.append(body)
            return {"id": f"meta-{uuid.uuid4().hex[:8]}", "status": "PENDING"}
        return {"data": self.existentes}


@pytest.fixture
def twilio(monkeypatch) -> list[ContentApi]:
    from agentcore_channel_twilio import client as client_mod

    cuentas: list[ContentApi] = []
    original = client_mod.TwilioClient.__init__

    def _init(self, account_sid, auth_token, *, transport=None):
        cuenta = next((c for c in reversed(cuentas) if c.ac == account_sid), cuentas[-1])
        original(self, account_sid, auth_token, transport=transport or httpx.MockTransport(cuenta.handler))

    monkeypatch.setattr(client_mod.TwilioClient, "__init__", _init)
    return cuentas


@pytest.fixture
def meta(monkeypatch) -> dict[str, GraphFalso]:
    """Graph simulado por `waba_id` y credenciales de prueba para cualquier cuenta."""
    import core.admin.meta_numbers_service as numeros
    import core.gateway.meta_templates as templates_mod

    grafos: dict[str, GraphFalso] = {}
    resolver = numeros.resolve_waba_and_credentials

    async def _resolver(connection_id, tenant_id):
        connection, waba, _ = await resolver(connection_id, tenant_id)
        creds = SimpleNamespace(access_token="tok", app_secret="sec", graph_api_version="v26.0")
        return connection, waba, (creds if waba is not None else None)

    async def _fetch(path, **kw):
        waba_id = path.split("/")[1]
        return await grafos[waba_id].fetch(path, **kw)

    monkeypatch.setattr(numeros, "resolve_waba_and_credentials", _resolver)
    monkeypatch.setattr(templates_mod, "graph_fetch", _fetch)
    return grafos


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.gateway.twilio import bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados = []
    for prefijo in ("pna", "pnb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Plantillas {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        async with tenant_scope(tid):
            await bandera.fijar(tid, "whatsapp_twilio", True, actor="test")
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _en(tid: uuid.UUID, fn, *a, **kw):
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await fn(*a, **kw)


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            r = await session.execute(text(sql), params)
            return list(r.all()) if r.returns_rows else []


async def _conectar_twilio(tid: uuid.UUID, cuentas: list[ContentApi]) -> tuple[ContentApi, uuid.UUID]:
    from core.gateway.twilio import alta

    api = ContentApi("AC" + uuid.uuid4().hex, f"+1555{uuid.uuid4().int % 10_000_000:07d}")
    cuentas.append(api)
    hecha = await _en(tid, alta.conectar, tid, account_sid=api.ac, auth_token="tok", sender_sid=XE)
    return api, hecha.connection_id


async def _conectar_meta(
    tid: uuid.UUID, grafos: dict[str, GraphFalso], *, status: str = "connected"
) -> tuple[uuid.UUID, uuid.UUID, GraphFalso]:
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(tid):
        async with get_session() as session:
            conn = MetaConnection(
                tenant_id=tid,
                channel="whatsapp",
                origin="embedded_signup",
                meta_app_id=f"app-{uuid.uuid4().hex[:6]}",
                status=status,
            )
            session.add(conn)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=conn.id, waba_id=waba_id, name="Cuenta Meta")
            session.add(waba)
            await session.flush()
            ids = conn.id, waba.id
    grafos[waba_id] = GraphFalso(waba_id)
    return ids[0], ids[1], grafos[waba_id]


async def _plantilla_meta(
    tid: uuid.UUID,
    waba_row: uuid.UUID,
    nombre: str,
    *,
    cuerpo: str = "Hola {{1}}, tu cita es el {{2}}",
    idioma: str = "es",
    status: str = "APPROVED",
    extra: list[dict[str, Any]] | None = None,
    formato: str | None = None,
) -> None:
    from core.db.models import MessageTemplate
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    body: dict[str, Any] = {"type": "BODY", "text": cuerpo}
    if "{{1}}" in cuerpo:
        body["example"] = {"body_text": [["Ana", "lunes"]]}
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                MessageTemplate(
                    tenant_id=tid,
                    waba_account_id=waba_row,
                    meta_template_id=f"mt-{uuid.uuid4().hex[:10]}",
                    name=nombre,
                    language=idioma,
                    category="UTILITY",
                    status=status,
                    parameter_format=formato,
                    components=[*(extra or []), body],
                )
            )


def _ref(proveedor: str, cid: uuid.UUID) -> tuple[str, uuid.UUID]:
    return (proveedor, cid)


# --------------------------------------------------------------------------- tests


async def test_copiar_meta_a_twilio_idempotente_y_matriz(negocios, twilio, meta) -> None:
    from core.admin import plantillas_negocio_service as svc

    tid, _ = negocios
    meta_id, waba_row, _graph = await _conectar_meta(tid, meta)
    api, tw_id = await _conectar_twilio(tid, twilio)
    await _plantilla_meta(tid, waba_row, "recordatorio_cita")

    m = await _en(tid, svc.matriz, tid)
    fila = next(f for f in m.filas if f.nombre == "recordatorio_cita")
    assert fila.celdas[f"meta:{meta_id}"].estado == "aprobada"
    assert fila.celdas[f"twilio:{tw_id}"].estado == "falta"

    [r] = await _en(
        tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre="recordatorio_cita", idioma="es",
        destinos=[_ref("twilio", tw_id)],
    )
    assert r.resultado == "creada", r
    [content] = api.contents.values()
    assert content["friendly_name"] == "recordatorio_cita" and content["language"] == "es"
    assert content["types"] == {"twilio/text": {"body": "Hola {{1}}, tu cita es el {{2}}"}}
    assert content["variables"] == {"1": "Ana", "2": "lunes"}
    assert content["approval_requests"]["category"] == "UTILITY" and api.solicitudes == 1

    # Repetir no duplica ni vuelve a pedir aprobación.
    [r2] = await _en(
        tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre="recordatorio_cita", idioma="es",
        destinos=[_ref("twilio", tw_id)],
    )
    assert r2.resultado == "ya_existia" and len(api.contents) == 1 and api.solicitudes == 1

    fila = next(f for f in (await _en(tid, svc.matriz, tid)).filas if f.nombre == "recordatorio_cita")
    assert fila.celdas[f"twilio:{tw_id}"].estado == "pendiente"
    assert not fila.celdas[f"twilio:{tw_id}"].distinta


async def test_creada_sin_enviar_se_retoma(negocios, twilio, meta) -> None:
    from core.admin import plantillas_negocio_service as svc

    tid, _ = negocios
    meta_id, waba_row, _ = await _conectar_meta(tid, meta)
    api, tw_id = await _conectar_twilio(tid, twilio)
    await _plantilla_meta(tid, waba_row, "aviso_pago", cuerpo="Tu pago de {{1}} llegó el {{2}}")

    api.fallar_aprobacion = True
    [r] = await _en(
        tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre="aviso_pago", idioma="es",
        destinos=[_ref("twilio", tw_id)],
    )
    assert r.resultado == "error" and "Repite la copia" in r.motivo
    api.fallar_aprobacion = False
    [r] = await _en(
        tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre="aviso_pago", idioma="es",
        destinos=[_ref("twilio", tw_id)],
    )
    assert r.resultado == "creada" and len(api.contents) == 1 and api.solicitudes == 1


async def test_no_pisar_no_replicable_y_destinos_fuera(negocios, twilio, meta) -> None:
    from core.admin import plantillas_negocio_service as svc
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    meta_id, waba_row, _ = await _conectar_meta(tid, meta)
    api, tw_id = await _conectar_twilio(tid, twilio)
    _, sandbox_id = await _conectar_twilio(tid, twilio)
    _, pausada_id = await _conectar_twilio(tid, twilio)
    await _sql(
        tid,
        "update twilio_connections set modo = 'sandbox', sender_sid = null where channel_connection_id = :c",
        c=sandbox_id,
    )
    await _sql(tid, "update channel_connections set active = false where id = :c", c=pausada_id)

    await _plantilla_meta(tid, waba_row, "bienvenida", cuerpo="Hola, bienvenido")
    await _en(
        tid, plantillas.crear, tid, tw_id, nombre="bienvenida", idioma="es", categoria="UTILITY",
        cuerpo="Hola, bienvenida a la tienda", ejemplos={},
    )
    await _plantilla_meta(
        tid, waba_row, "promo_foto", cuerpo="Mira la promo",
        extra=[{"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["h"]}}],
    )
    await _plantilla_meta(
        tid, waba_row, "con_botones", cuerpo="¿Confirmas?",
        extra=[{"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Sí"}]}],
    )
    await _plantilla_meta(tid, waba_row, "con_nombre", cuerpo="Hola {{nombre}}", formato="NAMED")

    antes = len(api.contents)
    [r] = await _en(
        tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre="bienvenida", idioma="es",
        destinos=[_ref("twilio", tw_id)],
    )
    assert r.resultado == "distinta"
    assert len(api.contents) == antes  # no se tocó
    [(cuerpo,)] = await _sql(
        tid,
        "select body from twilio_content_templates where channel_connection_id = :c and friendly_name = 'bienvenida'",
        c=tw_id,
    )
    assert cuerpo == "Hola, bienvenida a la tienda"

    fuera = await _en(
        tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre="bienvenida", idioma="es",
        destinos=[_ref("twilio", sandbox_id), _ref("twilio", pausada_id)],
    )
    assert [x.resultado for x in fuera] == ["error", "error"]
    assert "Sandbox" in fuera[0].motivo and "no es destino" in fuera[1].motivo

    for nombre in ("promo_foto", "con_botones", "con_nombre"):
        [r] = await _en(
            tid, svc.copiar, tid, origen=_ref("meta", meta_id), nombre=nombre, idioma="es",
            destinos=[_ref("twilio", tw_id)],
        )
        assert r.resultado == "no_replicable" and r.motivo, (nombre, r)
    assert len(api.contents) == antes

    m = await _en(tid, svc.matriz, tid)
    por = {f.nombre: f for f in m.filas}
    assert por["bienvenida"].celdas[f"twilio:{tw_id}"].distinta
    assert por["promo_foto"].celdas[f"twilio:{tw_id}"].estado == "no_replicable"
    conexiones = {c.clave: c for c in m.conexiones}
    assert conexiones[f"twilio:{sandbox_id}"].sandbox and not conexiones[f"twilio:{sandbox_id}"].destinable
    assert not conexiones[f"twilio:{pausada_id}"].destinable


async def test_copiar_todas_y_twilio_a_meta(negocios, twilio, meta) -> None:
    from core.admin import plantillas_negocio_service as svc
    from core.gateway.twilio import plantillas

    tid, _ = negocios
    meta_id, waba_row, graph = await _conectar_meta(tid, meta)
    api, tw_id = await _conectar_twilio(tid, twilio)
    await _plantilla_meta(tid, waba_row, "uno")
    await _plantilla_meta(tid, waba_row, "dos", cuerpo="Recordatorio para {{1}} el {{2}}")
    await _plantilla_meta(tid, waba_row, "tres_pendiente", status="PENDING")
    await _plantilla_meta(
        tid, waba_row, "cuatro_boton", cuerpo="Hola",
        extra=[{"type": "FOOTER", "text": "pie"}],
    )

    resumen = await _en(tid, svc.copiar_todas, tid, origen=_ref("meta", meta_id), destino=_ref("twilio", tw_id))
    assert (resumen.creadas, resumen.no_replicables, resumen.errores, resumen.restantes) == (2, 1, 0, 0)
    assert {c["friendly_name"] for c in api.contents.values()} == {"uno", "dos"}
    otra = await _en(tid, svc.copiar_todas, tid, origen=_ref("meta", meta_id), destino=_ref("twilio", tw_id))
    assert (otra.creadas, otra.ya_existian) == (0, 2)

    # Tope por pedido: lo demás queda en `restantes`.
    _, tw2 = await _conectar_twilio(tid, twilio)
    corta = await _en(
        tid, svc.copiar_todas, tid, origen=_ref("meta", meta_id), destino=_ref("twilio", tw2), limite=1
    )
    assert (corta.creadas, corta.restantes) == (1, 1)

    # Twilio → Meta: el texto pasa a un BODY con sus ejemplos.
    creada = await _en(
        tid, plantillas.crear, tid, tw_id, nombre="solo_twilio", idioma="es", categoria="MARKETING",
        cuerpo="Oferta para {{1}}", ejemplos={"1": "Ana"},
    )
    await _sql(tid, "update twilio_content_templates set approval_status = 'approved' where id = :i", i=creada.id)
    [r] = await _en(
        tid, svc.copiar, tid, origen=_ref("twilio", tw_id), nombre="solo_twilio", idioma="es",
        destinos=[_ref("meta", meta_id)],
    )
    assert r.resultado == "creada", r
    [body] = [b for b in graph.creadas if b["name"] == "solo_twilio"]
    assert body["category"] == "MARKETING" and body["components"] == [
        {"type": "BODY", "text": "Oferta para {{1}}", "example": {"body_text": [["Ana"]]}}
    ]

    # Meta ya la tiene aunque aquí no se sincronizó: «ya existía», sin duplicar.
    graph.existentes.append({"name": "uno", "language": "es", "id": "x", "status": "APPROVED"})
    await _sql(
        tid, "update twilio_content_templates set approval_status = 'approved' where channel_connection_id = :c", c=tw_id
    )
    [r] = await _en(
        tid, svc.copiar, tid, origen=_ref("twilio", tw_id), nombre="uno", idioma="es",
        destinos=[_ref("meta", meta_id)],
    )
    assert r.resultado == "ya_existia", r


async def test_aviso_por_evento_con_plantilla_que_falta(negocios, twilio, meta) -> None:
    from core.admin import plantillas_negocio_service as svc
    from core.gateway.event_notices import EVENTOS

    tid, _ = negocios
    evento = next(iter(EVENTOS))
    meta_id, waba_row, _ = await _conectar_meta(tid, meta)
    _, tw_id = await _conectar_twilio(tid, twilio)
    _, sandbox_id = await _conectar_twilio(tid, twilio)
    await _sql(
        tid,
        "update twilio_connections set modo = 'sandbox', sender_sid = null where channel_connection_id = :c",
        c=sandbox_id,
    )
    await _plantilla_meta(tid, waba_row, "aviso_evento")
    await _sql(
        tid,
        "update app_settings set message_templates_map = cast(:m as jsonb) where tenant_id = :t and deleted_at is null",
        m=json.dumps({evento: {"template": "aviso_evento", "language": "es", "variables": []}}),
        t=tid,
    )
    m = await _en(tid, svc.matriz, tid)
    faltantes = [(a.evento, a.conexion, a.estado) for a in m.avisos_faltantes if a.nombre == "aviso_evento"]
    assert (evento, f"twilio:{tw_id}", "falta") in faltantes
    assert all(c != f"meta:{meta_id}" and c != f"twilio:{sandbox_id}" for _, c, _ in faltantes)


async def test_aislamiento_entre_negocios(negocios, twilio, meta) -> None:
    from core.admin import plantillas_negocio_service as svc

    tid_a, tid_b = negocios
    meta_a, waba_a, _ = await _conectar_meta(tid_a, meta)
    _, tw_b = await _conectar_twilio(tid_b, twilio)
    await _plantilla_meta(tid_a, waba_a, "privada_a")

    m_b = await _en(tid_b, svc.matriz, tid_b)
    assert all(c.clave != f"meta:{meta_a}" for c in m_b.conexiones)
    assert all(f.nombre != "privada_a" for f in m_b.filas)

    # B no puede usar una conexión de A como origen…
    with pytest.raises(svc.PlantillasNegocioError) as exc:
        await _en(
            tid_b, svc.copiar, tid_b, origen=_ref("meta", meta_a), nombre="privada_a", idioma="es",
            destinos=[_ref("twilio", tw_b)],
        )
    assert exc.value.code == "not_found"
    # …ni A copiar a una de B.
    [r] = await _en(
        tid_a, svc.copiar, tid_a, origen=_ref("meta", meta_a), nombre="privada_a", idioma="es",
        destinos=[_ref("twilio", tw_b)],
    )
    assert r.resultado == "error" and r.motivo == "Conexión no encontrada."
    # …ni a una cuenta de Meta de B.
    meta_b, _, graph_b = await _conectar_meta(tid_b, meta)
    [r] = await _en(
        tid_a, svc.copiar, tid_a, origen=_ref("meta", meta_a), nombre="privada_a", idioma="es",
        destinos=[_ref("meta", meta_b)],
    )
    assert r.resultado == "error" and r.motivo == "Conexión no encontrada." and not graph_b.creadas
    # Y el servicio exige el tenant_scope del mismo negocio.
    with pytest.raises(PermissionError):
        await _en(tid_b, svc.matriz, tid_a)


async def test_api_lectura_y_copia(app_client, negocios, twilio, meta) -> None:
    from _api_helpers import api_headers

    tid, _ = negocios
    meta_id, waba_row, _ = await _conectar_meta(tid, meta)
    _, tw_id = await _conectar_twilio(tid, twilio)
    await _plantilla_meta(tid, waba_row, "por_api")
    headers = await api_headers(app_client, tid)

    r = await app_client.get("/api/v1/admin/whatsapp-templates", headers=headers)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    fila = next(f for f in cuerpo["filas"] if f["nombre"] == "por_api")
    assert fila["celdas"][f"twilio:{tw_id}"]["estado"] == "falta"

    r = await app_client.post(
        "/api/v1/admin/whatsapp-templates/copy",
        headers=headers,
        json={
            "origen": {"proveedor": "meta", "id": str(meta_id)},
            "nombre": "por_api",
            "idioma": "es",
            "destinos": [{"proveedor": "twilio", "id": str(tw_id)}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["resultado"] == "creada"

    r = await app_client.post(
        "/api/v1/admin/whatsapp-templates/copy-all",
        headers=headers,
        json={"origen": {"proveedor": "meta", "id": str(meta_id)}, "destino": {"proveedor": "meta", "id": str(meta_id)}},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "mismo_destino", r.text

    # Un colaborador con Conexiones en lectura ve la matriz pero no copia.
    lector = await _headers_colaborador(app_client, tid, {"connections": "read"})
    assert (await app_client.get("/api/v1/admin/whatsapp-templates", headers=lector)).status_code == 200
    copia = {
        "origen": {"proveedor": "meta", "id": str(meta_id)},
        "nombre": "por_api",
        "idioma": "es",
        "destinos": [{"proveedor": "twilio", "id": str(tw_id)}],
    }
    r = await app_client.post("/api/v1/admin/whatsapp-templates/copy", headers=lector, json=copia)
    assert r.status_code == 403, r.text
    r = await app_client.post(
        "/api/v1/admin/whatsapp-templates/copy-all",
        headers=lector,
        json={"origen": copia["origen"], "destino": copia["destinos"][0]},
    )
    assert r.status_code == 403, r.text


_CLAVE_COLABORADOR = "Plantillas-Negocio-2026!"


async def _headers_colaborador(client, tid: uuid.UUID, secciones: dict[str, str]) -> dict[str, str]:
    from _api_helpers import api_headers, uniq

    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    email = f"{uniq('lector')}@example.test"
    async with tenant_scope(tid):
        async with get_session() as session:
            admin = Admin(email=email, password_hash=hash_password(_CLAVE_COLABORADOR), role="colaborador")
            session.add(admin)
            await session.flush()
            session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
            for seccion, acceso in secciones.items():
                session.add(AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=tid))
    return await api_headers(client, tid, email=email, password=_CLAVE_COLABORADOR)

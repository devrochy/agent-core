"""SPEC-40 · la API admin de plantillas con opciones contra Postgres real (HU-OPC-05, ADR-0172).

Se prueba por HTTP (`/api/v1/plantillas-opciones`) con la base, la RLS y el AgentSpec REALES; solo se intercepta el
borde de Meta (cola de Celery y credenciales), como en `test_opciones_envio.py`.

- Permisos: 403 sin la sección, sin escritura y sin ser dueño o master; 200/201 al dueño y al master.
- Bandera apagada (el valor por defecto): TODA ruta responde 404 `opciones_directas_no_habilitadas` y no escribe nada.
- Borradores, `validar`, `vista-previa`, `publicar` (inmutable, sin cambios, errores sin efecto, concurrencia),
  `retirar` y borrar.
- Catálogo de acciones: solo las tools elegibles del negocio; ni una de otro negocio, ni sin binding, ni la tool de envío.
- `enviar-prueba`: solo a una conversación del propio negocio, idempotente.
- Aislamiento: la clave de otro negocio es un 404 en TODAS las rutas; la misma clave puede existir en dos negocios.
- `GET /admin/settings/message-templates/missing` distingue una plantilla con opciones de un aviso por evento.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_opciones_envio import Negocio, _sql, conversacion, envios, mensajes
from test_opciones_envio import cola as _cola
from test_opciones_toque import PARAMETROS, TOOL
from test_opciones_toque_entorno import _bandera, _binding
from test_opciones_toque_entorno import herramienta as _herramienta

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Las fixtures de los tests hermanos, re-expuestas por asignación (importarlas por nombre las redefiniría como parámetro).
cola = _cola
herramienta = _herramienta

URL = "/api/v1/plantillas-opciones"
PASSWORD = "Opciones-Api-2026!"


# ---------------------------------------------------------------- datos
def definicion(n: int = 2, *, body: str = "Hola {cliente.nombre}, ¿qué hacemos?", tool: str = TOOL) -> dict[str, Any]:
    return {
        "body": body,
        "footer": None,
        "variables": ["cliente.nombre"],
        "exclusivo": True,
        "ttl_horas": 24,
        "opciones": [
            {
                "id": f"o{i}",
                "titulo": f"Opción {i}",
                "accion": {"tool": tool, "args_fijos": {"pedido": f"A-{i}"}, "args_contexto": {}, "confirmar": False},
                "respuesta_ok": "Listo, {ctx.cliente.nombre}.",
                "respuesta_error": "No pude.",
                "result_keys": [],
            }
            for i in range(1, n + 1)
        ],
    }


def con_errores() -> dict[str, Any]:
    d = definicion()
    d["opciones"][0]["accion"]["tool"] = "get_customer_profile"  # existe, pero nadie la marcó apta
    d["opciones"][1]["titulo"] = "x" * 40
    return d


def codigos(hallazgos: list[dict[str, Any]], severidad: str = "error") -> set[str]:
    return {h["code"] for h in hallazgos if h["severity"] == severidad}


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[Negocio, Negocio, Negocio]]:
    """A (bandera encendida, la tool con binding), B (bandera encendida, SIN binding) y C (bandera apagada)."""
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    salida: list[Negocio] = []
    for pref, encendido in (("apia", True), ("apib", True), ("apic", False)):
        slug = f"{pref}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Opciones API {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        if encendido:
            await _bandera(tid, True)
        salida.append(Negocio(tid, slug))
    yield salida[0], salida[1], salida[2]
    for n in salida:
        await deactivate_tenant(str(n.tid))
        await purge_tenant(str(n.tid), f"ELIMINAR {n.slug}")


@pytest.fixture
async def listo(negocios, herramienta):  # type: ignore[no-untyped-def]
    """La tool de prueba registrada en el host y con binding explícito SOLO en A."""
    a, b, _ = negocios
    await _binding(a.tid)
    await _binding(b.tid, ninguno=True)
    return negocios


async def _usuario(tid: uuid.UUID, *, rol: str, secciones: dict[str, str]) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    email = f"{uniq(rol)}@example.test"
    async with tenant_scope(tid):
        async with get_session() as session:
            admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
            session.add(admin)
            await session.flush()
            session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=rol))
            for seccion, acceso in secciones.items():
                session.add(AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=tid))
    return email


async def _headers(client, tid: uuid.UUID, *, rol: str | None = None, secciones: dict[str, str] | None = None) -> dict[str, str]:
    if rol is None:
        return await api_headers(client, tid)  # master
    email = await _usuario(tid, rol=rol, secciones=secciones or {})
    return await api_headers(client, tid, email=email, password=PASSWORD)


async def _contar(tid: uuid.UUID, tabla: str = "option_templates") -> int:
    [(n,)] = await _sql(tid, f"select count(*) from {tabla} where tenant_id = :t", t=tid)
    return n


async def _crear(client, h: dict[str, str], clave: str | None = None, d: dict[str, Any] | None = None) -> str:
    clave = clave or f"t{uuid.uuid4().hex[:10]}"
    r = await client.post(URL, json={"clave": clave, "nombre": "Estado del pedido", "definicion": d or definicion()}, headers=h)
    assert r.status_code == 201, r.text
    return clave


async def _publicada(client, h: dict[str, str], d: dict[str, Any] | None = None) -> str:
    clave = await _crear(client, h, d=d)
    r = await client.post(f"{URL}/{clave}/publicar", headers=h)
    assert r.status_code == 201, r.text
    return clave


# ---------------------------------------------------------------- bandera apagada
async def test_con_la_bandera_apagada_toda_ruta_dice_no_habilitado_y_no_escribe(app_client, listo) -> None:
    _, _, c = listo
    h = await _headers(app_client, c.tid)
    llamadas = [
        ("get", URL, None), ("get", f"{URL}/x", None), ("get", f"{URL}/catalogo-acciones", None),
        ("get", f"{URL}/catalogo-variables", None), ("post", f"{URL}/validar", {"definicion": definicion()}),
        ("post", f"{URL}/vista-previa", {"definicion": definicion()}),
        ("post", URL, {"clave": "x", "definicion": definicion()}), ("put", f"{URL}/x/borrador", {"definicion": definicion()}),
        ("post", f"{URL}/x/publicar", None), ("post", f"{URL}/x/retirar", None), ("delete", f"{URL}/x", None),
        ("post", f"{URL}/x/enviar-prueba", {"conversation_id": str(uuid.uuid4()), "clave_idempotencia": "12345678"}),
    ]
    for metodo, url, cuerpo in llamadas:
        r = await getattr(app_client, metodo)(url, headers=h, **({"json": cuerpo} if cuerpo is not None else {}))
        assert r.status_code == 404 and r.json()["error"]["code"] == "opciones_directas_no_habilitadas", (metodo, url, r.text)
    assert await _contar(c.tid) == 0


# ---------------------------------------------------------------- permisos
async def test_permisos_403_sin_seccion_sin_escritura_y_sin_ser_dueno(app_client, listo) -> None:
    a, _, _ = listo
    sin_seccion = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "none"})
    lector = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "read"})
    escritor = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "write"})
    dueno = await _headers(app_client, a.tid, rol="owner", secciones={})
    cuerpo = {"clave": f"p{uuid.uuid4().hex[:8]}", "definicion": definicion()}

    assert (await app_client.get(URL, headers=sin_seccion)).status_code == 403
    assert (await app_client.get(f"{URL}/catalogo-acciones", headers=sin_seccion)).status_code == 403
    assert (await app_client.post(URL, json=cuerpo, headers=sin_seccion)).status_code == 403
    # Con lectura de Conexiones: lee, pero no escribe.
    assert (await app_client.get(URL, headers=lector)).status_code == 200
    assert (await app_client.get(f"{URL}/catalogo-variables", headers=lector)).status_code == 200
    assert (await app_client.post(f"{URL}/validar", json={"definicion": definicion()}, headers=lector)).status_code == 200
    assert (await app_client.post(URL, json=cuerpo, headers=lector)).status_code == 403
    # Con escritura en Conexiones pero colaborador (no dueño ni master): tampoco escribe.
    assert (await app_client.post(URL, json=cuerpo, headers=escritor)).status_code == 403
    assert await _contar(a.tid) == 0 or all(
        k != cuerpo["clave"] for (k,) in await _sql(a.tid, "select key from option_templates where tenant_id = :t", t=a.tid)
    )
    # El dueño y el master sí.
    assert (await app_client.post(URL, json=cuerpo, headers=dueno)).status_code == 201
    assert (await app_client.post(f"{URL}/{cuerpo['clave']}/publicar", headers=dueno)).status_code == 201
    master = await _headers(app_client, a.tid)
    assert (await app_client.post(URL, json={**cuerpo, "clave": f"m{uuid.uuid4().hex[:8]}"}, headers=master)).status_code == 201
    # Sin sesión: 401.
    assert (await app_client.get(URL)).status_code == 401
    for metodo, ruta in (("post", f"{URL}/{cuerpo['clave']}/publicar"), ("post", f"{URL}/{cuerpo['clave']}/retirar"),
                         ("delete", f"{URL}/{cuerpo['clave']}"), ("put", f"{URL}/{cuerpo['clave']}/borrador")):
        extra = {"json": {"definicion": definicion()}} if metodo == "put" else {}
        assert (await getattr(app_client, metodo)(ruta, headers=escritor, **extra)).status_code == 403, ruta
        assert (await getattr(app_client, metodo)(ruta, headers=lector, **extra)).status_code == 403, ruta


# ---------------------------------------------------------------- borradores
async def test_crud_de_borradores_clave_unica_y_forma_validada(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _crear(app_client, h)

    repetida = await app_client.post(URL, json={"clave": clave}, headers=h)
    assert repetida.status_code == 409 and repetida.json()["error"]["code"] == "clave_repetida"
    assert (await app_client.post(URL, json={"clave": "Con Espacios"}, headers=h)).status_code == 422
    forma_rota = await app_client.post(URL, json={"clave": "rota", "definicion": {"body": "x", "campo_extra": 1}}, headers=h)
    assert forma_rota.status_code == 422 and forma_rota.json()["error"]["code"] == "definicion_invalida"
    assert {x["path"] for x in forma_rota.json()["error"]["details"]["hallazgos"]} >= {"campo_extra", "opciones"}

    detalle = (await app_client.get(f"{URL}/{clave}", headers=h)).json()
    assert detalle["estado"] == "draft" and detalle["version_vigente"] is None and detalle["versiones"] == []
    assert detalle["borrador"]["body"].startswith("Hola") and detalle["definicion_vigente"] is None
    assert clave in [p["clave"] for p in (await app_client.get(URL, headers=h)).json()["plantillas"]]

    nuevo = definicion(3, body="Hola {cliente.nombre}")
    guardado = await app_client.put(f"{URL}/{clave}/borrador", json={"nombre": "Otro nombre", "definicion": nuevo}, headers=h)
    assert guardado.status_code == 200 and len(guardado.json()["borrador"]["opciones"]) == 3 and guardado.json()["nombre"] == "Otro nombre"
    # Un borrador puede estar incompleto (el lint completo corre al publicar), pero no tener otra forma.
    incompleto = copy.deepcopy(nuevo)
    incompleto["opciones"][0]["titulo"] = ""
    assert (await app_client.put(f"{URL}/{clave}/borrador", json={"definicion": incompleto}, headers=h)).status_code == 200
    assert (await app_client.put(f"{URL}/{clave}/borrador", json={"definicion": {"body": 1}}, headers=h)).status_code == 422
    gigante = copy.deepcopy(nuevo)
    gigante["opciones"][0]["accion"]["args_fijos"] = {"k": "x" * 200_000}
    assert (await app_client.put(f"{URL}/{clave}/borrador", json={"definicion": gigante}, headers=h)).status_code == 422

    assert (await app_client.get(f"{URL}/no_existe", headers=h)).status_code == 404
    assert (await app_client.get(f"{URL}/Clave%20Mala", headers=h)).status_code == 404
    assert (await app_client.put(f"{URL}/no_existe/borrador", json={"definicion": nuevo}, headers=h)).status_code == 404

    assert (await app_client.delete(f"{URL}/{clave}", headers=h)).status_code == 204
    assert (await app_client.get(f"{URL}/{clave}", headers=h)).status_code == 404
    assert (await app_client.delete(f"{URL}/{clave}", headers=h)).status_code == 404
    otra = await app_client.post(URL, json={"clave": clave, "definicion": nuevo}, headers=h)
    assert otra.status_code == 201, "borrar libera la clave"


# ---------------------------------------------------------------- validar y vista previa
async def test_validar_devuelve_los_hallazgos_sin_guardar_nada(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    antes = await _contar(a.tid)

    r = await app_client.post(f"{URL}/validar", json={"definicion": con_errores()}, headers=h)

    assert r.status_code == 200 and r.json()["valida"] is False
    hallazgos = r.json()["hallazgos"]
    assert {"path", "code", "severity", "mensaje"} == set(hallazgos[0])
    assert {"tool_no_apta", "titulo_largo"} <= codigos(hallazgos)
    assert {h_["path"] for h_ in hallazgos} >= {"opciones[0].accion.tool", "opciones[1].titulo"}
    ok = (await app_client.post(f"{URL}/validar", json={"definicion": definicion()}, headers=h)).json()
    assert ok["valida"] is True and codigos(ok["hallazgos"]) == set()
    forma = await app_client.post(f"{URL}/validar", json={"definicion": {"body": 5}}, headers=h)
    assert forma.status_code == 200 and codigos(forma.json()["hallazgos"]) == {"esquema"}
    assert (await app_client.post(f"{URL}/validar", json={"definicion": "no es un objeto"}, headers=h)).status_code == 422
    assert await _contar(a.tid) == antes


async def test_vista_previa_da_el_interactivo_los_componentes_de_meta_y_el_respaldo_sin_enviar(app_client, listo, cola) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)

    r = await app_client.post(
        f"{URL}/vista-previa", json={"definicion": definicion(3), "ejemplos": {"cliente.nombre": "Luisa"}}, headers=h
    )

    assert r.status_code == 200, r.text
    v = r.json()
    assert v["interactivo"]["type"] == "button" and v["interactivo"]["body"] == "Hola Luisa, ¿qué hacemos?"
    assert [b["title"] for b in v["interactivo"]["buttons"]] == ["Opción 1", "Opción 2", "Opción 3"] and v["cabe_en_whatsapp"] is True
    assert v["componentes_meta"][0]["text"] == "Hola {{cliente_nombre}}, ¿qué hacemos?"
    assert v["componentes_meta"][-1]["buttons"][0] == {"type": "QUICK_REPLY", "text": "Opción 1"}
    assert v["cuerpo_meta_renderizado"] == "Hola Luisa, ¿qué hacemos?"
    assert v["texto_de_respaldo"] == "Hola Luisa, ¿qué hacemos?\n\n1. Opción 1\n2. Opción 2\n3. Opción 3"
    lista = (await app_client.post(f"{URL}/vista-previa", json={"definicion": definicion(5)}, headers=h)).json()
    assert lista["interactivo"]["type"] == "list" and len(lista["interactivo"]["rows"]) == 5
    assert "Ana" in lista["interactivo"]["body"], "variable base con muestra"

    rota = await app_client.post(f"{URL}/vista-previa", json={"definicion": con_errores()}, headers=h)
    assert rota.status_code == 422 and rota.json()["error"]["code"] == "definicion_con_errores"
    assert "tool_no_apta" in codigos(rota.json()["error"]["details"]["hallazgos"])
    assert cola == [] and await _contar(a.tid, "option_sends") == 0 and await _contar(a.tid, "messages") == 0


# ---------------------------------------------------------------- publicar
async def test_publicar_congela_la_version_y_con_errores_no_cambia_nada(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _crear(app_client, h, d=con_errores())

    rechazada = await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    assert rechazada.status_code == 422 and rechazada.json()["error"]["code"] == "definicion_con_errores"
    assert "tool_no_apta" in codigos(rechazada.json()["error"]["details"]["hallazgos"])
    detalle = (await app_client.get(f"{URL}/{clave}", headers=h)).json()
    assert detalle["estado"] == "draft" and detalle["versiones"] == [] and detalle["version_vigente"] is None

    arreglada = definicion(3)
    await app_client.put(f"{URL}/{clave}/borrador", json={"definicion": arreglada}, headers=h)
    v1 = await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    assert v1.status_code == 201, v1.text
    assert v1.json()["version_n"] == 1 and v1.json()["message_template_id"] is None and len(v1.json()["definition_hash"]) == 64
    assert (await app_client.post(f"{URL}/{clave}/publicar", headers=h)).json()["error"]["code"] == "sin_cambios"

    # Editar el borrador no toca la versión publicada; publicar crea la 2 y la 1 queda intacta y SIN retirar.
    await app_client.put(f"{URL}/{clave}/borrador", json={"definicion": definicion(2, body="Hola {cliente.nombre}")}, headers=h)
    detalle = (await app_client.get(f"{URL}/{clave}", headers=h)).json()
    assert detalle["estado"] == "published" and detalle["version_vigente"] == 1 and detalle["tiene_borrador_sin_publicar"] is True
    assert len(detalle["definicion_vigente"]["opciones"]) == 3
    v2 = await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    assert v2.status_code == 201 and v2.json()["version_n"] == 2
    detalle = (await app_client.get(f"{URL}/{clave}", headers=h)).json()
    assert [(v["version_n"], v["vigente"], v["retirada_en"]) for v in detalle["versiones"]] == [(2, True, None), (1, False, None)]
    assert detalle["tiene_borrador_sin_publicar"] is False and len(detalle["definicion_vigente"]["opciones"]) == 2
    [(definicion_v1, hash_v1)] = await _sql(
        a.tid, "select definition, definition_hash from option_template_versions where option_template_id = "
        "(select id from option_templates where tenant_id = :t and key = :k) and version_n = 1", t=a.tid, k=clave,
    )
    assert len(definicion_v1["opciones"]) == 3 and hash_v1 == v1.json()["definition_hash"]

    # La versión es inmutable: ni siquiera el dueño de la fila la puede cambiar con SQL.
    with pytest.raises(Exception, match="append-only"):
        await _sql(a.tid, "update option_template_versions set definition = '{}'::jsonb where tenant_id = :t", t=a.tid)

    auditoria = await _sql(a.tid, "select action, input, output from audit_log where action like 'option_template_%' and input->>'template_key' = :k", k=clave)
    assert {x[0] for x in auditoria} >= {"option_template_create", "option_template_draft", "option_template_publish"}
    assert "Hola" not in str(auditoria), "la auditoría lleva claves y versiones, nunca el contenido de la definición"


async def test_dos_publicaciones_simultaneas_no_repiten_numero_de_version(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _crear(app_client, h)

    r1, r2 = await asyncio.gather(*(app_client.post(f"{URL}/{clave}/publicar", headers=h) for _ in range(2)))

    assert sorted([r1.status_code, r2.status_code]) == [201, 409], (r1.text, r2.text)
    assert {x["error"]["code"] for x in (r1.json(), r2.json()) if "error" in x} == {"sin_cambios"}
    detalle = (await app_client.get(f"{URL}/{clave}", headers=h)).json()
    assert [v["version_n"] for v in detalle["versiones"]] == [1]


async def test_publicar_sin_borrador_es_422(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = f"vacia{uuid.uuid4().hex[:6]}"
    assert (await app_client.post(URL, json={"clave": clave}, headers=h)).status_code == 201
    r = await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sin_borrador"


# ---------------------------------------------------------------- retirar y borrar
async def test_retirar_marca_las_versiones_y_se_puede_volver_a_publicar(app_client, listo, cola) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)
    assert (await app_client.delete(f"{URL}/{clave}", headers=h)).json()["error"]["code"] == "plantilla_publicada"

    r = await app_client.post(f"{URL}/{clave}/retirar", headers=h)

    assert r.status_code == 200 and r.json()["estado"] == "retired"
    assert all(v["retirada_en"] for v in r.json()["versiones"])
    assert (await app_client.post(f"{URL}/{clave}/retirar", headers=h)).json()["error"]["code"] == "no_publicada"
    conv = await conversacion(a.tid)
    prueba = await app_client.post(
        f"{URL}/{clave}/enviar-prueba", json={"conversation_id": str(conv), "clave_idempotencia": uniq("idem")}, headers=h
    )
    assert prueba.status_code == 409 and prueba.json()["error"]["code"] == "no_publicada" and cola == []

    nueva = await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    assert nueva.status_code == 201 and nueva.json()["version_n"] == 2
    detalle = (await app_client.get(f"{URL}/{clave}", headers=h)).json()
    assert detalle["estado"] == "published"


async def test_borrar_una_retirada_libera_la_clave_y_conserva_la_historia(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)
    await app_client.post(f"{URL}/{clave}/retirar", headers=h)

    assert (await app_client.delete(f"{URL}/{clave}", headers=h)).status_code == 204

    assert (await app_client.get(f"{URL}/{clave}", headers=h)).status_code == 404
    [(n,)] = await _sql(
        a.tid, "select count(*) from option_template_versions v join option_templates t on t.id = v.option_template_id "
        "where t.tenant_id = :t and t.key = :k and t.deleted_at is not null", t=a.tid, k=clave,
    )
    assert n == 1, "la versión publicada sigue ahí (historia de solo avance)"
    assert (await app_client.post(URL, json={"clave": clave}, headers=h)).status_code == 201


# ---------------------------------------------------------------- catálogos
async def test_catalogo_de_acciones_solo_las_elegibles_del_negocio(app_client, listo, monkeypatch) -> None:
    from core.mcp.host import mcp_host
    from core.mcp.types import MCPTool

    a, b, _ = listo

    async def _h(args, ctx):
        return {}

    extra = {
        "opc_sensible": MCPTool(name="opc_sensible", description="Borra algo.", parameters={"type": "object", "properties": {}},
                                handler=_h, plugin_id="core", apta_para_opcion=True, sensible=True),
        "opc_no_apta": MCPTool(name="opc_no_apta", description="No apta.", parameters={}, handler=_h, plugin_id="core"),
        "opc_sin_binding": MCPTool(name="opc_sin_binding", description="Sin binding.", parameters={}, handler=_h, plugin_id="core",
                                   apta_para_opcion=True),
    }
    for nombre, tool in extra.items():
        monkeypatch.setitem(mcp_host._tools, nombre, tool)
    await _binding(a.tid)
    await _agregar_binding(a.tid, "opc_sensible")
    await _agregar_binding(a.tid, "opc_no_apta")
    h = await _headers(app_client, a.tid)

    r = await app_client.get(f"{URL}/catalogo-acciones", headers=h)

    assert r.status_code == 200, r.text
    por_nombre = {x["tool"]: x for x in r.json()["acciones"]}
    assert set(por_nombre) == {TOOL, "opc_sensible"}, "ni la no apta, ni la apta sin binding, ni la tool de envío, ni las del core sin marca"
    assert por_nombre[TOOL]["parameters"] == PARAMETROS and por_nombre[TOOL]["sensible"] is False
    assert por_nombre[TOOL]["confirmacion_obligatoria"] is False and por_nombre[TOOL]["descripcion"]
    assert por_nombre["opc_sensible"]["sensible"] is True and por_nombre["opc_sensible"]["confirmacion_obligatoria"] is True
    assert "enviar_plantilla_opciones" not in por_nombre

    # El binding deshabilitado la saca del catálogo; y B (sin binding) no ve la tool de A.
    await _binding(a.tid, enabled=False)
    sin = (await app_client.get(f"{URL}/catalogo-acciones", headers=h)).json()["acciones"]
    assert TOOL not in {x["tool"] for x in sin}
    hb = await _headers(app_client, b.tid)
    assert (await app_client.get(f"{URL}/catalogo-acciones", headers=hb)).json() == {"acciones": []}
    await _binding(a.tid)


async def _agregar_binding(tid: uuid.UUID, tool: str) -> None:
    from sqlalchemy import select

    from core.agents.spec import load_active_spec
    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        datos = (await load_active_spec()).model_dump(mode="json")
        datos["tools"].append({"tool": tool, "enabled": True, "scopes": [], "maxCallsPerConversation": None, "requiresConfirmation": False})
        async with get_session() as s:
            fila = (await s.execute(select(AgentSpecRecord).where(AgentSpecRecord.tenant_id == tid, AgentSpecRecord.is_active.is_(True)))).scalar_one()
            fila.spec = datos


async def test_la_tool_de_otro_negocio_no_es_elegible_en_el_lint_ni_al_publicar(app_client, listo) -> None:
    _, b, _ = listo
    h = await _headers(app_client, b.tid)
    clave = await _crear(app_client, h)  # B no tiene binding para TOOL

    r = await app_client.post(f"{URL}/{clave}/publicar", headers=h)

    assert r.status_code == 422 and "tool_sin_binding" in codigos(r.json()["error"]["details"]["hallazgos"])


async def test_catalogo_de_variables_es_el_del_servidor(app_client, listo) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    r = await app_client.get(f"{URL}/catalogo-variables", headers=h)
    assert r.status_code == 200
    assert {(v["ruta"], v["origen"]) for v in r.json()["variables"]} == {("cliente.nombre", "conversacion"), ("negocio.nombre", "conversacion")}


# ---------------------------------------------------------------- enviar prueba
async def test_enviar_prueba_solo_a_una_conversacion_del_negocio_e_idempotente(app_client, listo, cola) -> None:
    a, b, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)
    conv = await conversacion(a.tid, ventana=timedelta(hours=3))
    ajena = await conversacion(b.tid, ventana=timedelta(hours=3))
    idem = uniq("idem")

    r = await app_client.post(f"{URL}/{clave}/enviar-prueba", json={"conversation_id": str(conv), "clave_idempotencia": idem}, headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["via"] == "interactivo" and r.json()["ejecucion_directa"] is True and r.json()["ya_enviado"] is False
    [(send_id, via, contexto_, _, _)] = await envios(a.tid, conv)
    assert str(send_id) == r.json()["send_id"] and via == "interactive" and contexto_ == {"cliente.nombre": "Ana"}
    assert len(cola) == 1 and len(await mensajes(a.tid, conv)) == 1
    repetida = await app_client.post(f"{URL}/{clave}/enviar-prueba", json={"conversation_id": str(conv), "clave_idempotencia": idem}, headers=h)
    assert repetida.status_code == 200 and repetida.json()["ya_enviado"] is True and repetida.json()["send_id"] == r.json()["send_id"]
    assert len(cola) == 1, "la misma clave de idempotencia no duplica el mensaje"

    # Una conversación de otro negocio, una inexistente y un número libre: 404/422, y NO sale nada.
    for destino in (ajena, uuid.uuid4()):
        x = await app_client.post(f"{URL}/{clave}/enviar-prueba", json={"conversation_id": str(destino), "clave_idempotencia": uniq("idem")}, headers=h)
        assert x.status_code == 404 and x.json()["error"]["code"] == "conversacion_no_encontrada", x.text
    libre = await app_client.post(f"{URL}/{clave}/enviar-prueba", json={"conversation_id": "573001234567", "clave_idempotencia": "12345678"}, headers=h)
    assert libre.status_code == 422
    sin_clave = await app_client.post(f"{URL}/{clave}/enviar-prueba", json={"conversation_id": str(conv)}, headers=h)
    assert sin_clave.status_code == 422
    assert len(cola) == 1 and await envios(b.tid, ajena) == []
    inexistente = await app_client.post(f"{URL}/no_existe/enviar-prueba", json={"conversation_id": str(conv), "clave_idempotencia": "12345678"}, headers=h)
    assert inexistente.status_code == 404 and inexistente.json()["error"]["code"] == "not_found"
    # Sin publicar: 409.
    borrador = await _crear(app_client, h)
    sin_publicar = await app_client.post(f"{URL}/{borrador}/enviar-prueba", json={"conversation_id": str(conv), "clave_idempotencia": "12345678"}, headers=h)
    assert sin_publicar.status_code == 409 and sin_publicar.json()["error"]["code"] == "no_publicada"
    # Queda la auditoría del envío con el actor que lo pidió.
    actores = await _sql(a.tid, "select actor from audit_log where action = 'option_send' and conversation_id = :c", c=conv)
    assert actores and all(actor.startswith("admin:") for (actor,) in actores)


async def test_fuera_de_la_ventana_sin_plantilla_aprobada_es_409_y_la_alerta_distingue_la_opcion(app_client, listo, cola) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _publicada(app_client, h)
    cerrada = await conversacion(a.tid, ventana=None)

    r = await app_client.post(f"{URL}/{clave}/enviar-prueba", json={"conversation_id": str(cerrada), "clave_idempotencia": uniq("idem")}, headers=h)

    assert r.status_code == 409 and r.json()["error"]["code"] == "sin_plantilla", r.text
    assert r.json()["error"]["details"]["motivo"] == "version_sin_plantilla_de_meta" and cola == []
    # Un aviso por evento que tampoco salió, para ver los dos tipos.
    await _sql(
        a.tid, "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, status, error_reason, client_message_id) "
        "values (gen_random_uuid(), :t, :c, 'outbound', 'agent', 'Aviso «subscription.charge_created» sin enviar: evento sin plantilla', "
        "'failed', 'sin_plantilla', 'aviso:1')", t=a.tid, c=cerrada,
    )

    faltantes = await app_client.get("/api/v1/admin/settings/message-templates/missing", headers=h)

    assert faltantes.status_code == 200, faltantes.text
    j = faltantes.json()
    por_tipo = {(x["tipo"], x["clave"]) for x in j["recent"]}
    assert ("opcion", clave) in por_tipo and ("evento", None) in por_tipo
    assert j["total_7d"] == 2 and j["total_opciones_7d"] == 1 and j["total_eventos_7d"] == 1


# ---------------------------------------------------------------- aislamiento
async def test_la_clave_de_otro_negocio_es_404_en_toda_ruta_y_la_misma_clave_puede_existir_en_dos(app_client, listo, cola) -> None:
    a, b, _ = listo
    ha, hb = await _headers(app_client, a.tid), await _headers(app_client, b.tid)
    clave = await _publicada(app_client, ha)
    conv_b = await conversacion(b.tid, ventana=timedelta(hours=3))

    for metodo, ruta, cuerpo in (
        ("get", f"{URL}/{clave}", None), ("put", f"{URL}/{clave}/borrador", {"definicion": definicion()}),
        ("post", f"{URL}/{clave}/publicar", None), ("post", f"{URL}/{clave}/retirar", None), ("delete", f"{URL}/{clave}", None),
        ("post", f"{URL}/{clave}/enviar-prueba", {"conversation_id": str(conv_b), "clave_idempotencia": "12345678"}),
    ):
        r = await getattr(app_client, metodo)(ruta, headers=hb, **({"json": cuerpo} if cuerpo is not None else {}))
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", (metodo, ruta, r.text)
    assert clave not in [p["clave"] for p in (await app_client.get(URL, headers=hb)).json()["plantillas"]]
    assert cola == [] and await _contar(b.tid, "option_sends") == 0

    # La misma clave existe en B sin chocar con la de A (el índice único es por negocio).
    assert (await app_client.post(URL, json={"clave": clave, "definicion": definicion()}, headers=hb)).status_code == 201
    # Y A sigue intacto.
    detalle = (await app_client.get(f"{URL}/{clave}", headers=ha)).json()
    assert detalle["estado"] == "published" and detalle["version_vigente"] == 1
    # RLS directa: con el tenant de B no se ve ninguna fila de A aunque se pida por clave.
    assert await _sql(b.tid, "select 1 from option_templates where tenant_id = :a", a=a.tid) == []


async def test_un_header_de_otro_negocio_ajeno_a_la_cuenta_es_403(app_client, listo) -> None:
    a, b, _ = listo
    dueno_a = await _headers(app_client, a.tid, rol="owner")
    r = await app_client.get(URL, headers={**dueno_a, "X-Tenant-Id": str(b.tid)})
    assert r.status_code == 403


async def test_la_respuesta_de_otro_negocio_es_identica_a_la_de_una_clave_inexistente(app_client, listo) -> None:
    a, b, _ = listo
    ha, hb = await _headers(app_client, a.tid), await _headers(app_client, b.tid)
    clave = await _publicada(app_client, ha)
    conv_b = await conversacion(b.tid, ventana=timedelta(hours=3))
    prueba = {"conversation_id": str(conv_b), "clave_idempotencia": "12345678"}
    for metodo, sufijo, cuerpo in (("get", "", None), ("post", "/publicar", None), ("post", "/retirar", None),
                                   ("delete", "", None), ("put", "/borrador", {"definicion": definicion()}),
                                   ("post", "/enviar-prueba", prueba)):
        ajena = await getattr(app_client, metodo)(f"{URL}/{clave}{sufijo}", headers=hb, **({"json": cuerpo} if cuerpo else {}))
        falsa = await getattr(app_client, metodo)(f"{URL}/nada_{uuid.uuid4().hex[:8]}{sufijo}", headers=hb, **({"json": cuerpo} if cuerpo else {}))
        assert (ajena.status_code, ajena.json()) == (falsa.status_code, falsa.json()), (metodo, sufijo)


async def test_el_desglose_de_avisos_sin_plantilla_no_cruza_negocios(app_client, listo) -> None:
    a, b, _ = listo
    hb = await _headers(app_client, b.tid)
    conv_a = await conversacion(a.tid, ventana=None)
    base = (await app_client.get("/api/v1/admin/settings/message-templates/missing", headers=hb)).json()
    await _sql(
        a.tid, "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, status, error_reason, client_message_id) "
        "values (gen_random_uuid(), :t, :c, 'outbound', 'agent', 'Aviso «opciones:solo_de_a» sin enviar: x', 'failed', 'sin_plantilla', "
        "'opt_send:abc:f1')", t=a.tid, c=conv_a,
    )
    despues = (await app_client.get("/api/v1/admin/settings/message-templates/missing", headers=hb)).json()
    assert despues == base and "solo_de_a" not in str(despues)


async def test_sin_permiso_gana_el_403_aunque_la_bandera_este_apagada(app_client, listo) -> None:
    _, _, c = listo
    sin_permiso = await _headers(app_client, c.tid, rol="colaborador", secciones={"connections": "none"})
    assert (await app_client.get(URL, headers=sin_permiso)).status_code == 403
    lector = await _headers(app_client, c.tid, rol="colaborador", secciones={"connections": "read"})
    assert (await app_client.get(URL, headers=lector)).json()["error"]["code"] == "opciones_directas_no_habilitadas"

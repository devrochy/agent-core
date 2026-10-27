"""SPEC-40 · publicar en Meta desde la API admin (HU-OPC-04 sobre HU-OPC-05), por HTTP y con Postgres/Redis REALES.

Solo se simula Meta (`graph_fetch`) y las credenciales de la conexión. Se prueba el encadenado opcional de
`POST /plantillas-opciones/{clave}/publicar` (cuerpo `meta`), el reintento `POST …/versiones/{n}/publicar-en-meta`, el
estado de Meta en el detalle, los permisos y el aislamiento entre negocios. La mecánica de la reserva, el reintento y
los conflictos está en `test_opciones_meta_publicacion_db.py`.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from test_opciones_api import (  # noqa: F401  (negocios: fixture del módulo hermano)
    URL,
    _crear,
    _headers,
    _sql,
    definicion,
    negocios,
)
from test_opciones_api import herramienta as _herramienta
from test_opciones_api import listo as _listo
from test_opciones_meta_publicacion_db import MetaFalsa, conexion, corte, webhook

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

herramienta = _herramienta
listo = _listo


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> MetaFalsa:
    falsa = MetaFalsa()
    monkeypatch.setattr("core.gateway.meta_templates.graph_fetch", falsa)

    async def credenciales(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(access_token="token-de-prueba", app_secret="secreto-de-prueba", graph_api_version="v21.0")

    monkeypatch.setattr("core.gateway.meta_credentials.credentials_for", credenciales)

    async def alerta(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert", alerta)
    return falsa


def cuerpo_meta(conn: uuid.UUID, categoria: str = "UTILITY", **extra: Any) -> dict[str, Any]:
    return {"meta": {"connection_id": str(conn), "categoria": categoria, "ejemplos": {"cliente.nombre": "Mariana"}, **extra}}


async def detalle(client, h: dict[str, str], clave: str) -> dict[str, Any]:
    r = await client.get(f"{URL}/{clave}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def versiones(tid: uuid.UUID, clave: str) -> int:
    [(n,)] = await _sql(
        tid,
        "select count(*) from option_template_versions v join option_templates t on t.id = v.option_template_id "
        "where t.key = :k and t.tenant_id = :t",
        k=clave, t=tid,
    )
    return n


# ---------------------------------------------------------------- publicar encadenado
async def test_publicar_sin_cuerpo_sigue_solo_congelando_la_version(app_client, listo, meta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    clave = await _crear(app_client, h)
    r = await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    assert r.status_code == 201 and r.json()["meta"] is None and r.json()["message_template_id"] is None
    assert not meta.llamadas
    assert (await detalle(app_client, h, clave))["versiones"][0]["meta"] is None


async def test_publicar_con_meta_crea_la_plantilla_y_el_detalle_muestra_su_estado(app_client, listo, meta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    conn, _, _ = await conexion(a.tid)
    clave = await _crear(app_client, h)

    r = await app_client.post(f"{URL}/{clave}/publicar", json=cuerpo_meta(conn, "MARKETING"), headers=h)

    assert r.status_code == 201, r.text
    m = r.json()["meta"]
    assert m["ok"] and m["outcome"] == "published" and m["nombre"] == f"{clave}_v1" and m["categoria"] == "MARKETING"
    assert m["estado"] == "PENDING" and r.json()["message_template_id"] == m["message_template_id"]
    v = (await detalle(app_client, h, clave))["versiones"][0]
    assert v["meta"]["estado"] == "PENDING" and v["meta"]["categoria"] == "MARKETING" and v["meta"]["nombre"] == f"{clave}_v1"

    await webhook(a.tid, m["meta_template_id"], f"{clave}_v1", "APPROVED")
    assert (await detalle(app_client, h, clave))["versiones"][0]["meta"]["estado"] == "APPROVED"


async def test_la_version_nueva_es_otra_plantilla_y_la_aprobada_no_se_toca(app_client, listo, meta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    conn, _, _ = await conexion(a.tid)
    clave = await _crear(app_client, h)
    r1 = await app_client.post(f"{URL}/{clave}/publicar", json=cuerpo_meta(conn), headers=h)
    await webhook(a.tid, r1.json()["meta"]["meta_template_id"], f"{clave}_v1", "APPROVED")
    d2 = definicion(3)
    assert (await app_client.put(f"{URL}/{clave}/borrador", json={"definicion": d2}, headers=h)).status_code == 200

    r2 = await app_client.post(f"{URL}/{clave}/publicar", json=cuerpo_meta(conn), headers=h)

    assert r2.json()["meta"]["nombre"] == f"{clave}_v2" and r2.json()["version_n"] == 2
    vs = {v["version_n"]: v for v in (await detalle(app_client, h, clave))["versiones"]}
    assert vs[1]["meta"]["estado"] == "APPROVED" and vs[2]["meta"]["estado"] == "PENDING"
    assert all(p[1].endswith("/message_templates") for p in meta.posts) and len(meta.posts) == 2  # ninguna edición


async def test_una_categoria_o_un_idioma_invalidos_no_congelan_ninguna_version(app_client, listo, meta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    conn, _, _ = await conexion(a.tid)
    clave = await _crear(app_client, h)
    r = await app_client.post(f"{URL}/{clave}/publicar", json=cuerpo_meta(conn, "AUTHENTICATION"), headers=h)
    assert r.status_code == 422
    r = await app_client.post(f"{URL}/{clave}/publicar", json=cuerpo_meta(conn, idioma="ES-ar"), headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "idioma_invalido"
    assert await versiones(a.tid, clave) == 0 and not meta.llamadas


async def test_si_meta_falla_la_version_sigue_publicada_y_el_reintento_no_duplica(app_client, listo, meta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    conn, _, _ = await conexion(a.tid)
    clave = await _crear(app_client, h)
    meta.falla_despues.append(corte())  # Meta crea la plantilla y la respuesta se pierde

    r = await app_client.post(f"{URL}/{clave}/publicar", json=cuerpo_meta(conn), headers=h)
    assert r.status_code == 201 and r.json()["version_n"] == 1
    assert r.json()["meta"]["ok"] is False and r.json()["meta"]["outcome"] == "network_error" and r.json()["meta"]["reintentable"]
    assert (await detalle(app_client, h, clave))["estado"] == "published"

    ok = await app_client.post(f"{URL}/{clave}/versiones/1/publicar-en-meta", json=cuerpo_meta(conn)["meta"], headers=h)
    assert ok.status_code == 200 and ok.json()["ok"] and ok.json()["outcome"] == "already_published"
    otra = await app_client.post(f"{URL}/{clave}/versiones/1/publicar-en-meta", json=cuerpo_meta(conn)["meta"], headers=h)
    assert otra.json()["outcome"] == "already_published"
    assert len(meta.posts) == 1 and len(meta.plantillas) == 1


# ---------------------------------------------------------------- reintento por versión: errores y permisos
async def test_publicar_en_meta_una_version_inexistente_o_retirada(app_client, listo, meta) -> None:
    a, _, _ = listo
    h = await _headers(app_client, a.tid)
    conn, _, _ = await conexion(a.tid)
    clave = await _crear(app_client, h)
    await app_client.post(f"{URL}/{clave}/publicar", headers=h)
    url = f"{URL}/{clave}/versiones/{{n}}/publicar-en-meta"
    assert (await app_client.post(url.format(n=9), json=cuerpo_meta(conn)["meta"], headers=h)).status_code == 404
    assert (await app_client.post(f"{URL}/no_existe/versiones/1/publicar-en-meta", json=cuerpo_meta(conn)["meta"], headers=h)).status_code == 404
    assert (await app_client.post(f"{URL}/{clave}/retirar", headers=h)).status_code == 200
    r = await app_client.post(url.format(n=1), json=cuerpo_meta(conn)["meta"], headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_retirada" and not meta.llamadas


async def test_publicar_en_meta_pide_dueno_o_master_y_escritura(app_client, listo, meta) -> None:
    a, _, _ = listo
    master = await _headers(app_client, a.tid)
    conn, _, _ = await conexion(a.tid)
    clave = await _crear(app_client, master)
    await app_client.post(f"{URL}/{clave}/publicar", headers=master)
    lector = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "read"})
    escritor = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "write"})
    dueno = await _headers(app_client, a.tid, rol="owner", secciones={})
    url = f"{URL}/{clave}/versiones/1/publicar-en-meta"
    cuerpo = cuerpo_meta(conn)["meta"]
    assert (await app_client.post(url, json=cuerpo, headers=lector)).status_code == 403
    assert (await app_client.post(url, json=cuerpo, headers=escritor)).status_code == 403
    assert (await app_client.post(url, json=cuerpo)).status_code == 401
    assert not meta.llamadas
    assert (await app_client.post(url, json=cuerpo, headers=dueno)).json()["outcome"] == "published"


async def test_otro_negocio_no_publica_en_meta_la_version_ajena_ni_con_su_propia_conexion(app_client, listo, meta) -> None:
    a, b, _ = listo
    ha, hb = await _headers(app_client, a.tid), await _headers(app_client, b.tid)
    conn_b, _, _ = await conexion(b.tid)
    clave = await _crear(app_client, ha)
    await app_client.post(f"{URL}/{clave}/publicar", headers=ha)
    r = await app_client.post(f"{URL}/{clave}/versiones/1/publicar-en-meta", json=cuerpo_meta(conn_b)["meta"], headers=hb)
    assert r.status_code == 404 and not meta.llamadas
    # Y la conexión de OTRO negocio no sirve para el propio: la plantilla no se crea.
    conn_ajena, _, _ = await conexion(b.tid)
    r = await app_client.post(f"{URL}/{clave}/versiones/1/publicar-en-meta", json=cuerpo_meta(conn_ajena)["meta"], headers=ha)
    assert r.status_code == 200 and r.json()["outcome"] == "connection_not_ready" and not meta.llamadas


async def test_con_la_bandera_apagada_publicar_en_meta_dice_no_habilitado(app_client, listo, meta) -> None:
    _, _, c = listo
    h = await _headers(app_client, c.tid)
    conn, _, _ = await conexion(c.tid)
    r = await app_client.post(f"{URL}/x/versiones/1/publicar-en-meta", json=cuerpo_meta(conn)["meta"], headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "opciones_directas_no_habilitadas"
    r = await app_client.post(f"{URL}/x/publicar", json=cuerpo_meta(conn), headers=h)
    assert r.status_code == 404 and not meta.llamadas

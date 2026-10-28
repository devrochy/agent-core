"""Pista D4 · los mensajes guardados (`/api/v1/mensajes/guardados`) contra Postgres real (ADR-0178).

Se prueba por HTTP con la base, la RLS y la auditoría REALES. Solo almacenamiento: nada se envía a un cliente.

- Permisos: 403 sin la sección, sin escritura y sin ser dueño o master; 200/201 al dueño y al master; 401 sin sesión.
- CRUD con estados, borrado lógico que libera la clave, unicidad por negocio, tope por negocio, auditoría sin el texto.
- Validación con el módulo común (variables permitidas, `{{n}}` fuera, largo, canales) y vista previa sin escribir.
- Aislamiento: la clave de otro negocio es 404 en toda ruta; la misma clave puede existir en dos negocios; la RLS misma
  (sin el filtro del servicio) no deja leer ni escribir entre negocios.
- Catálogo D0: los guardados salen como `tipo=guardado` con estado, canales y variables, sin cambiar el contrato.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from test_opciones_api import _headers
from test_opciones_envio import Negocio, _sql

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/mensajes/guardados"
CATALOGO = "/api/v1/mensajes/catalogo"
TEXTO = "Hola {cliente.nombre},\ngracias por escribir a {negocio.nombre}."


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[Negocio, Negocio, Negocio]]:
    """A y B para el aislamiento; C solo para el tope por negocio."""
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    salida: list[Negocio] = []
    for pref in ("guaa", "guab", "guac"):
        slug = f"{pref}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Guardados {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        salida.append(Negocio(tid, slug))
    yield salida[0], salida[1], salida[2]
    for n in salida:
        await deactivate_tenant(str(n.tid))
        await purge_tenant(str(n.tid), f"ELIMINAR {n.slug}")


def _clave() -> str:
    return f"m{uuid.uuid4().hex[:10]}"


def _cuerpo(clave: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"clave": clave or _clave(), "titulo": "Saludo", "texto": TEXTO, **extra}


async def _crear(client, h: dict[str, str], **extra: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = await client.post(URL, json=_cuerpo(**extra), headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _codigo(r: Any) -> str:
    return r.json()["error"]["code"]


# ---------------------------------------------------------------- permisos
async def test_permisos_403_sin_seccion_sin_escritura_y_sin_ser_dueno(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    sin_seccion = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "none"})
    lector = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "read"})
    escritor = await _headers(app_client, a.tid, rol="colaborador", secciones={"connections": "write"})
    dueno = await _headers(app_client, a.tid, rol="owner", secciones={})
    master = await _headers(app_client, a.tid)
    cuerpo = _cuerpo()

    for ruta in (URL, f"{URL}/catalogo-opciones", f"{URL}/x"):
        assert (await app_client.get(ruta, headers=sin_seccion)).status_code == 403, ruta
    assert (await app_client.post(f"{URL}/vista-previa", json={"texto": "hola"}, headers=sin_seccion)).status_code == 403
    assert (await app_client.post(URL, json=cuerpo, headers=sin_seccion)).status_code == 403
    # Con lectura de Conexiones: lee y previsualiza, pero no escribe.
    assert (await app_client.get(URL, headers=lector)).status_code == 200
    assert (await app_client.get(f"{URL}/catalogo-opciones", headers=lector)).status_code == 200
    assert (await app_client.post(f"{URL}/vista-previa", json={"texto": "hola"}, headers=lector)).status_code == 200
    assert (await app_client.post(URL, json=cuerpo, headers=lector)).status_code == 403
    # Con escritura en Conexiones pero colaborador (ni dueño ni master): tampoco escribe.
    assert (await app_client.post(URL, json=cuerpo, headers=escritor)).status_code == 403
    assert await _sql(a.tid, "select 1 from mensajes_guardados where tenant_id = :t and clave = :c", t=a.tid, c=cuerpo["clave"]) == []
    # El dueño y el master sí.
    assert (await app_client.post(URL, json=cuerpo, headers=dueno)).status_code == 201
    assert (await app_client.post(URL, json=_cuerpo(), headers=master)).status_code == 201
    for metodo, ruta, extra in (("patch", f"{URL}/{cuerpo['clave']}", {"json": {"titulo": "x"}}), ("delete", f"{URL}/{cuerpo['clave']}", {})):
        assert (await getattr(app_client, metodo)(ruta, headers=escritor, **extra)).status_code == 403, ruta
        assert (await getattr(app_client, metodo)(ruta, headers=lector, **extra)).status_code == 403, ruta
    # Sin sesión: 401.
    assert (await app_client.get(URL)).status_code == 401
    assert (await app_client.post(URL, json=cuerpo)).status_code == 401


# ---------------------------------------------------------------- CRUD
async def test_ciclo_de_vida_crear_leer_editar_archivar_y_borrar(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    h = await _headers(app_client, a.tid)
    clave = _clave()

    creado = await _crear(app_client, h, clave=clave, canales=["webchat", "whatsapp_meta", "webchat"])
    assert creado["estado"] == "borrador" and creado["clave"] == clave
    assert creado["texto"] == TEXTO, "los saltos de línea se conservan"
    assert creado["variables"] == ["cliente.nombre", "negocio.nombre"]
    assert creado["todos_los_canales"] is False and creado["canales"] == ["whatsapp_meta", "webchat"], "sin repetir y en el orden del catálogo"
    assert (await app_client.get(f"{URL}/{clave}", headers=h)).json() == creado

    # Editar solo lo que llega.
    r = await app_client.patch(f"{URL}/{clave}", json={"titulo": "  Saludo   corto ", "estado": "activo"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["titulo"] == "Saludo corto" and r.json()["estado"] == "activo" and r.json()["texto"] == TEXTO
    assert r.json()["actualizado_en"] >= creado["actualizado_en"]
    r = await app_client.patch(f"{URL}/{clave}", json={"texto": "Hola {cliente.nombre}", "canales": ["todos"]}, headers=h)
    assert r.json()["canales"] == [] and r.json()["todos_los_canales"] is True and r.json()["variables"] == ["cliente.nombre"]

    # Archivar y volver a activar.
    assert (await app_client.patch(f"{URL}/{clave}", json={"estado": "archivado"}, headers=h)).json()["estado"] == "archivado"
    en_lista = {m["clave"]: m["estado"] for m in (await app_client.get(URL, headers=h)).json()["mensajes"]}
    assert en_lista[clave] == "archivado"
    solo_activos = (await app_client.get(URL, params={"estado": "activo"}, headers=h)).json()["mensajes"]
    assert clave not in {m["clave"] for m in solo_activos}
    assert (await app_client.patch(f"{URL}/{clave}", json={"estado": "activo"}, headers=h)).json()["estado"] == "activo"

    # Borrar: 404 después, y la clave queda libre (borrado lógico, la fila sigue).
    assert (await app_client.delete(f"{URL}/{clave}", headers=h)).status_code == 204
    assert (await app_client.get(f"{URL}/{clave}", headers=h)).status_code == 404
    assert (await app_client.delete(f"{URL}/{clave}", headers=h)).status_code == 404
    assert (await app_client.patch(f"{URL}/{clave}", json={"titulo": "x"}, headers=h)).status_code == 404
    [(n, borrados)] = await _sql(a.tid, "select count(*), count(deleted_at) from mensajes_guardados where tenant_id = :t and clave = :c", t=a.tid, c=clave)
    assert (n, borrados) == (1, 1), "sin borrado físico"
    otra = await _crear(app_client, h, clave=clave, texto="Otro texto")
    assert otra["texto"] == "Otro texto" and otra["estado"] == "borrador"


async def test_clave_repetida_en_el_mismo_negocio_es_409_y_en_otro_negocio_es_valida(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b, _ = negocios
    ha, hb = await _headers(app_client, a.tid), await _headers(app_client, b.tid)
    clave = _clave()
    await _crear(app_client, ha, clave=clave)
    r = await app_client.post(URL, json=_cuerpo(clave), headers=ha)
    assert r.status_code == 409 and _codigo(r) == "clave_repetida"
    await _crear(app_client, hb, clave=clave, texto="Distinto en B")
    assert (await app_client.get(f"{URL}/{clave}", headers=ha)).json()["texto"] == TEXTO
    assert (await app_client.get(f"{URL}/{clave}", headers=hb)).json()["texto"] == "Distinto en B"


async def test_la_base_tambien_impone_la_unicidad_y_las_formas(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    clave = _clave()
    await _crear(app_client, await _headers(app_client, a.tid), clave=clave)
    insertar = (
        "insert into mensajes_guardados (id, tenant_id, clave, titulo, texto, estado) "
        "values (:i, :t, :c, :ti, :tx, :e)"
    )
    base = {"t": a.tid, "c": _clave(), "ti": "T", "tx": "x", "e": "borrador"}
    def con_canales(literal: str) -> str:  # literal de prueba (constante), no entrada de nadie
        return (
            "insert into mensajes_guardados (id, tenant_id, clave, titulo, texto, canales) "
            f"values (:i, :t, :c, 'T', 'x', '{literal}'::text[])"
        )

    for canales in ("{NULL}", "{\"\"}", "{{a},{b}}", "{" + ",".join(f"c{i}" for i in range(21)) + "}"):
        with pytest.raises(DBAPIError) as exc:
            await _sql(a.tid, con_canales(canales), i=uuid.uuid4(), t=a.tid, c=_clave())
        assert "ck_mensajes_guardados_canales" in str(exc.value), canales
    await _sql(a.tid, con_canales("{webchat,whatsapp_meta}"), i=uuid.uuid4(), t=a.tid, c=_clave())
    for malo in (
        {"c": clave},  # clave viva repetida
        {"c": "Mayúsculas"}, {"c": ""}, {"c": "x" * 41},
        {"ti": "   "}, {"ti": "t" * 121}, {"tx": "  "}, {"tx": "x" * 4097}, {"e": "publicado"},
        {"tx": "\n\t \r"}, {"ti": "\n"},  # solo espacios en blanco, no solo U+0020
    ):
        with pytest.raises(DBAPIError) as exc:
            await _sql(a.tid, insertar, i=uuid.uuid4(), **{**base, **malo})
        assert "violates" in str(exc.value), malo  # una restricción de la base, no un error de tipos


async def test_tope_de_mensajes_vivos_por_negocio(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    from core.admin.mensajes_guardados_service import MAX_GUARDADOS

    _, _, c = negocios
    h = await _headers(app_client, c.tid)
    await _sql(
        c.tid,
        "insert into mensajes_guardados (id, tenant_id, clave, titulo, texto) "
        "select gen_random_uuid(), :t, 'r' || g, 'T', 'x' from generate_series(1, :n) g",
        t=c.tid, n=MAX_GUARDADOS,
    )
    r = await app_client.post(URL, json=_cuerpo(), headers=h)
    assert r.status_code == 409 and _codigo(r) == "limite_de_mensajes"
    # Borrar uno libera cupo.
    assert (await app_client.delete(f"{URL}/r1", headers=h)).status_code == 204
    assert (await app_client.post(URL, json=_cuerpo(), headers=h)).status_code == 201


# ---------------------------------------------------------------- validación
async def test_validacion_del_texto_las_variables_y_los_canales(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    h = await _headers(app_client, a.tid)

    async def intentar(**campos: Any) -> Any:
        return await app_client.post(URL, json=_cuerpo(**campos), headers=h)

    r = await intentar(texto="Tu cita es el {cita.fecha}")
    assert r.status_code == 422 and _codigo(r) == "variable_no_permitida"
    assert r.json()["error"]["details"] == {"desconocidas": ["cita.fecha"], "permitidas": ["cliente.nombre", "negocio.nombre"]}
    assert _codigo(await intentar(texto="Hola {{1}}")) == "variable_no_permitida"
    assert _codigo(await intentar(texto="   ")) == "texto_invalido"
    assert _codigo(await intentar(texto="x" * 1025)) == "texto_invalido"
    assert _codigo(await intentar(titulo="  ")) == "titulo_invalido"
    r = await intentar(canales=["whatsapp_meta", "telegram", "playground"])
    assert r.status_code == 422 and _codigo(r) == "canales_invalidos" and r.json()["error"]["details"]["invalidos"] == ["playground", "telegram"]
    assert (await intentar(clave="Mal Formada")).status_code == 422
    assert (await intentar(estado="archivado")).status_code == 422, "un mensaje nuevo es borrador o activo"
    assert (await intentar(estado="publicado")).status_code == 422
    assert not await _sql(a.tid, "select 1 from mensajes_guardados where tenant_id = :t and texto like '%cita.fecha%'", t=a.tid), "lo inválido no se guarda"

    clave = _clave()
    await _crear(app_client, h, clave=clave)
    assert _codigo(await app_client.patch(f"{URL}/{clave}", json={}, headers=h)) == "sin_cambios"
    r = await app_client.patch(f"{URL}/{clave}", json={"texto": "{otro}"}, headers=h)
    assert r.status_code == 422 and _codigo(r) == "variable_no_permitida"
    assert (await app_client.get(f"{URL}/{clave}", headers=h)).json()["texto"] == TEXTO, "un PATCH inválido no cambia nada"


async def test_catalogo_de_opciones_del_formulario(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    r = await app_client.get(f"{URL}/catalogo-opciones", headers=await _headers(app_client, a.tid))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert "playground" not in cuerpo["canales"] and {"whatsapp_meta", "webchat"} <= set(cuerpo["canales"])
    assert [v["ruta"] for v in cuerpo["variables"]] == ["cliente.nombre", "negocio.nombre"]
    assert (cuerpo["max_texto"], cuerpo["max_titulo"]) == (1024, 120)


# ---------------------------------------------------------------- vista previa
async def test_vista_previa_renderiza_sin_guardar(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    h = await _headers(app_client, a.tid)
    antes = await _sql(a.tid, "select count(*) from mensajes_guardados where tenant_id = :t", t=a.tid)
    r = await app_client.post(f"{URL}/vista-previa", json={"texto": TEXTO}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "texto_renderizado": "Hola Ana,\ngracias por escribir a Mi negocio.",
        "variables": ["cliente.nombre", "negocio.nombre"],
        "ejemplos_usados": {"cliente.nombre": "Ana", "negocio.nombre": "Mi negocio"},
    }
    r = await app_client.post(
        f"{URL}/vista-previa", json={"texto": TEXTO, "ejemplos": {"cliente.nombre": "Luis {negocio.nombre}", "ruta.ajena": "x"}}, headers=h
    )
    assert r.json()["texto_renderizado"] == "Hola Luis {negocio.nombre},\ngracias por escribir a Mi negocio."
    r = await app_client.post(f"{URL}/vista-previa", json={"texto": "Hola {nadie}"}, headers=h)
    assert r.status_code == 422 and _codigo(r) == "variable_no_permitida"
    assert await _sql(a.tid, "select count(*) from mensajes_guardados where tenant_id = :t", t=a.tid) == antes


# ---------------------------------------------------------------- auditoría
async def test_cada_escritura_queda_en_audit_log_sin_el_texto(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    h = await _headers(app_client, a.tid)
    secreto = f"texto-privado-{uuid.uuid4().hex}"
    clave = _clave()
    await _crear(app_client, h, clave=clave, texto=secreto)
    await app_client.patch(f"{URL}/{clave}", json={"estado": "activo", "titulo": "Otro"}, headers=h)
    await app_client.patch(f"{URL}/{clave}", json={"estado": "archivado"}, headers=h)
    await app_client.delete(f"{URL}/{clave}", headers=h)
    await app_client.patch(f"{URL}/{clave}", json={"titulo": "ya borrado"}, headers=h)  # 404: no audita
    filas = await _sql(
        a.tid,
        "select action, actor, input, output from audit_log where action like 'mensaje_guardado_%' and input->>'clave' = :c order by created_at, id",
        c=clave,
    )
    assert [f[0] for f in filas] == ["mensaje_guardado_create", "mensaje_guardado_update", "mensaje_guardado_archive", "mensaje_guardado_delete"]
    assert all(f[1].startswith("admin:") for f in filas)
    assert filas[0][3] == {"estado": "borrador"} and filas[2][3] == {"estado": "archivado"}
    assert secreto not in json.dumps([[f[2], f[3]] for f in filas]), "la auditoría guarda la clave y el estado, nunca el texto"


# ---------------------------------------------------------------- aislamiento
async def test_la_clave_de_otro_negocio_es_404_en_toda_ruta_y_no_se_lista(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b, _ = negocios
    ha, hb = await _headers(app_client, a.tid), await _headers(app_client, b.tid)
    de_a = (await _crear(app_client, ha, texto="Texto exclusivo de A"))["clave"]

    assert (await app_client.get(f"{URL}/{de_a}", headers=hb)).status_code == 404
    assert (await app_client.patch(f"{URL}/{de_a}", json={"titulo": "robado"}, headers=hb)).status_code == 404
    assert (await app_client.delete(f"{URL}/{de_a}", headers=hb)).status_code == 404
    assert de_a not in {m["clave"] for m in (await app_client.get(URL, headers=hb)).json()["mensajes"]}
    assert "Texto exclusivo de A" not in json.dumps((await app_client.get(URL, headers=hb)).json())
    intacto = (await app_client.get(f"{URL}/{de_a}", headers=ha)).json()
    assert intacto["titulo"] == "Saludo" and intacto["texto"] == "Texto exclusivo de A"


async def test_la_rls_aisla_por_si_sola_sin_el_filtro_del_servicio(negocios) -> None:
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, b, _ = negocios
    clave = _clave()
    async with tenant_scope(a.tid), get_session() as s:
        await s.execute(
            text("insert into mensajes_guardados (id, tenant_id, clave, titulo, texto) values (:i, :t, :c, 'T', 'x')"),
            {"i": uuid.uuid4(), "t": a.tid, "c": clave},
        )
    # Desde B no se ve ni se toca, aunque se pida por clave y sin filtrar por negocio.
    async with tenant_scope(b.tid), get_session() as s:
        assert (await s.execute(text("select count(*) from mensajes_guardados where clave = :c"), {"c": clave})).scalar_one() == 0
        assert (await s.execute(text("update mensajes_guardados set titulo = 'robado' where clave = :c"), {"c": clave})).rowcount == 0
    # Desde A no se puede escribir en nombre de B (WITH CHECK).
    with pytest.raises(DBAPIError):
        async with tenant_scope(a.tid), get_session() as s:
            await s.execute(
                text("insert into mensajes_guardados (id, tenant_id, clave, titulo, texto) values (:i, :t, :c, 'T', 'x')"),
                {"i": uuid.uuid4(), "t": b.tid, "c": _clave()},
            )
    # Sin negocio en contexto la consulta falla cerrada: 0 filas, nunca un error por `''::uuid`.
    async with get_session() as s:
        assert (await s.execute(text("select count(*) from mensajes_guardados"))).scalar_one() == 0
    [(titulo,)] = await _sql(a.tid, "select titulo from mensajes_guardados where clave = :c", c=clave)
    assert titulo == "T"


async def test_app_runtime_no_puede_borrar_fisicamente(negocios) -> None:
    a, _, _ = negocios
    with pytest.raises(DBAPIError):
        await _sql(a.tid, "delete from mensajes_guardados where tenant_id = :t", t=a.tid)


# ---------------------------------------------------------------- catálogo D0
def _guardados(cuerpo: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["clave"]: m for m in cuerpo["mensajes"] if m["tipo"] == "guardado"}


async def test_el_catalogo_incluye_los_guardados_con_estado_canales_y_variables(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b, _ = negocios
    h = await _headers(app_client, a.tid)
    k_activo, k_arch, k_borrado = _clave(), _clave(), _clave()
    await _crear(app_client, h, clave=k_activo, titulo="Horario", texto="Abrimos de 8 a 5, {cliente.nombre}.", estado="activo", canales=["whatsapp_meta", "whatsapp_waweb"])
    await _crear(app_client, h, clave=k_arch, canales=["webchat"])
    await app_client.patch(f"{URL}/{k_arch}", json={"estado": "archivado"}, headers=h)
    await _crear(app_client, h, clave=k_borrado)
    await app_client.delete(f"{URL}/{k_borrado}", headers=h)

    r = await app_client.get(CATALOGO, headers=h)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    g = _guardados(cuerpo)
    assert k_borrado not in g, "el borrado no sale"
    activo = g[k_activo]
    assert activo["id"] == f"guardado:{k_activo}" and activo["nombre"] == "Horario" and activo["estado"] == "activo"
    assert activo["canales_soportados"] == ["whatsapp"], "dos tipos de WhatsApp, un solo canal"
    assert activo["variables"] == ["cliente.nombre"] and activo["dialecto_variables"] == "llaves_con_ruta"
    assert activo["resumen"] == "Abrimos de 8 a 5, {cliente.nombre}." and activo["idioma"] is None and activo["estado_detalle"] is None
    assert activo["actualizado_en"] is not None
    assert activo["fuente"]["editor"] == "guardado" and activo["fuente"]["clave"] == k_activo
    assert g[k_arch]["estado"] == "archivado" and g[k_arch]["canales_soportados"] == ["web"]
    assert len({m["id"] for m in cuerpo["mensajes"]}) == len(cuerpo["mensajes"]), "los ids siguen siendo únicos"
    assert {"guardado", "evento"} <= {m["tipo"] for m in cuerpo["mensajes"]}
    assert cuerpo["truncado"] == []
    assert cuerpo["omitidas"] == [{"tipo": "opciones", "motivo": "bandera_apagada"}], "los guardados no dependen de la bandera de opciones"

    # Un guardado para todos los canales lista todos los de clientes.
    k_todos = _clave()
    await _crear(app_client, h, clave=k_todos)
    todos = _guardados((await app_client.get(CATALOGO, headers=h)).json())[k_todos]["canales_soportados"]
    assert {"whatsapp", "web", "email"} <= set(todos) and len(todos) == len(set(todos))

    # El otro negocio no ve nada de A.
    de_b = (await app_client.get(CATALOGO, headers=await _headers(app_client, b.tid))).json()
    assert not {k_activo, k_arch, k_todos} & set(_guardados(de_b))


async def test_el_catalogo_exige_connections_para_los_guardados(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = negocios
    clave = _clave()
    await _crear(app_client, await _headers(app_client, a.tid), clave=clave)
    todo_ninguno = {"connections": "none", "settings": "none", "plugins": "none"}

    async def ver(secciones: dict[str, str]) -> tuple[set[str], set[tuple[str, str]]]:
        h = await _headers(app_client, a.tid, rol="colaborador", secciones={**todo_ninguno, **secciones})
        cuerpo = (await app_client.get(CATALOGO, headers=h)).json()
        return set(_guardados(cuerpo)), {(o["tipo"], o["motivo"]) for o in cuerpo["omitidas"]}

    claves, omitidas = await ver({"settings": "read"})
    assert clave not in claves and ("guardado", "sin_permiso") in omitidas
    claves, omitidas = await ver({"connections": "read"})
    assert clave in claves and ("guardado", "sin_permiso") not in omitidas

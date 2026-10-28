"""Pista D0 · el catálogo unificado de mensajes (`GET /api/v1/mensajes/catalogo`) contra Postgres real (ADR-0178).

Solo lectura. Se siembran los cuatro orígenes (plantillas de Meta, avisos por evento, plantillas con opciones y textos
de packs) en un negocio y se comprueba que cada uno sale con su forma común y su puntero al editor; también el negocio
vacío, la bandera apagada, los permisos por sección y el aislamiento entre negocios.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from test_opciones_api import _headers
from test_opciones_envio import Negocio, _sql
from test_opciones_meta_publicacion_db import HASH, conexion, definicion

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/mensajes/catalogo"
TEXTO_PROPIO = "Hola {cliente.nombre}, tu cobro de {cargo.monto} (propio de este negocio)"


async def _plantilla_meta(tid: uuid.UUID, waba_fila: uuid.UUID, nombre: str, estado: str, cuerpo: str) -> uuid.UUID:
    mid = uuid.uuid4()
    componentes = [{"type": "BODY", "text": cuerpo, "example": {"body_text": [["DATO-REAL-DE-MUESTRA"]]}}]
    await _sql(
        tid,
        "insert into message_templates (id, tenant_id, waba_account_id, name, language, category, status, components) "
        "values (:i, :t, :w, :n, 'es', 'UTILITY', :s, cast(:c as jsonb))",
        i=mid, t=tid, w=waba_fila, n=nombre, s=estado, c=json.dumps(componentes),
    )
    return mid


async def _opciones(tid: uuid.UUID, clave: str, versiones: int = 1, ligar: dict[int, uuid.UUID] | None = None) -> None:
    pid = uuid.uuid4()
    await _sql(
        tid,
        "insert into option_templates (id, tenant_id, key, name, status, current_version_n) values (:i, :t, :k, 'Estado', 'published', :n)",
        i=pid, t=tid, k=clave, n=versiones,
    )
    for n in range(1, versiones + 1):
        await _sql(
            tid,
            "insert into option_template_versions (id, tenant_id, option_template_id, version_n, definition, definition_hash, message_template_id) "
            "values (:i, :t, :p, :n, cast(:d as jsonb), :h, :m)",
            i=uuid.uuid4(), t=tid, p=pid, n=n, d=json.dumps(definicion(2)), h=HASH, m=(ligar or {}).get(n),
        )


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[dict[str, Negocio]]:
    """`a`: las cuatro fuentes pobladas; `b`: vacío con bandera; `c`: con plantillas con opciones y la bandera apagada."""
    from core.alta_negocio import onboard_tenant
    from core.kernel.opciones import bandera
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    salida: dict[str, Negocio] = {}
    for letra, packs in (("a", ["pack_subscriptions", "pack_calendar"]), ("b", []), ("c", [])):
        slug = f"cat{letra}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Catálogo {slug}", slug=slug, packs=packs, sembrar_ejemplos=False)
        salida[letra] = Negocio(tid, slug)
        if letra != "c":
            async with tenant_scope(tid):
                await bandera.fijar(tid, True, actor="test")

    a, c = salida["a"].tid, salida["c"].tid
    # Meta: una plantilla aprobada, una pendiente; y la versión vigente y la anterior de una plantilla con opciones.
    _conn, waba, _ = await conexion(a)
    salida["conexion_a"] = Negocio(_conn, "x")
    await _plantilla_meta(a, waba, "cobro_nuevo", "APPROVED", "Hola {{1}}, tu cobro es {{2}}")
    await _plantilla_meta(a, waba, "promo_pendiente", "PENDING", "Hola {{1}}")
    vigente = await _plantilla_meta(a, waba, "estado_v2", "APPROVED", "Hola {{1}}, ¿qué hacemos?")
    anterior = await _plantilla_meta(a, waba, "estado_v1", "APPROVED", "Hola {{1}}")
    await _opciones(a, "estado", versiones=2, ligar={1: anterior, 2: vigente})
    # Evento mapeado a una plantilla aprobada.
    await _sql(
        a,
        "update app_settings set message_templates_map = cast(:m as jsonb) where tenant_id = :t",
        m=json.dumps({"subscription.charge_created": {"template": "cobro_nuevo", "language": "es", "variables": ["cliente.nombre"], "fallback_text": TEXTO_PROPIO}}),
        t=a,
    )
    # Texto de pack personalizado (los demás quedan con el de fábrica).
    async with tenant_scope(a):
        await plugin_manager.save_config("pack_subscriptions", {"aviso_cargo_texto": TEXTO_PROPIO})
    # El negocio C tiene una plantilla con opciones, pero la bandera apagada la oculta.
    await _opciones(c, "no_visible")
    yield salida
    for k in ("a", "b", "c"):
        await deactivate_tenant(str(salida[k].tid))
        await purge_tenant(str(salida[k].tid), f"ELIMINAR {salida[k].slug}")


def _por_tipo(cuerpo: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grupos: dict[str, list[dict[str, Any]]] = {}
    for m in cuerpo["mensajes"]:
        grupos.setdefault(m["tipo"], []).append(m)
    return grupos


async def test_las_cuatro_fuentes_con_forma_comun_y_puntero_al_editor(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a = negocios["a"]
    r = await app_client.get(URL, headers=await _headers(app_client, a.tid))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    g = _por_tipo(cuerpo)
    assert cuerpo["omitidas"] == [] and cuerpo["truncado"] == []

    # meta: las ligadas a la versión VIGENTE de la plantilla con opciones no salen; la versión anterior sí.
    meta = {m["clave"]: m for m in g["meta"]}
    assert set(meta) == {"cobro_nuevo", "promo_pendiente", "estado_v1"}
    cobro = meta["cobro_nuevo"]
    assert cobro["estado"] == "APPROVED" and cobro["estado_detalle"] == "UTILITY" and cobro["idioma"] == "es"
    assert cobro["variables"] == ["1", "2"] and cobro["dialecto_variables"] == "meta_posicionales"
    assert cobro["canales_soportados"] == ["whatsapp"]
    assert cobro["fuente"]["editor"] == "meta" and cobro["fuente"]["connection_id"] == str(negocios["conexion_a"].tid)
    assert cobro["resumen"] == "Hola {{1}}, tu cobro es {{2}}"
    assert "DATO-REAL-DE-MUESTRA" not in json.dumps(cuerpo), "el resumen sale del cuerpo, nunca de los ejemplos de Meta"
    assert meta["promo_pendiente"]["estado"] == "PENDING"

    # evento: uno por cada evento conocido; el mapeado a una plantilla aprobada lo dice.
    from core.gateway.event_notices import EVENTOS

    eventos = {m["clave"]: m for m in g["evento"]}
    assert set(eventos) == set(EVENTOS)
    cargo = eventos["subscription.charge_created"]
    assert cargo["estado"] == "mapeado" and cargo["estado_detalle"] == "cobro_nuevo"
    assert cargo["fuente"] == {**cargo["fuente"], "editor": "evento", "evento": "subscription.charge_created"}
    assert "cliente.nombre" in cargo["variables"] and cargo["resumen"].startswith("Hola {cliente.nombre}")
    assert eventos["appointment.reminder"]["estado"] == "sin_mapear"

    # opciones: la clave, su estado y las variables de la versión vigente.
    [op] = g["opciones"]
    assert (op["clave"], op["estado"], op["fuente"]["clave"]) == ("estado", "published", "estado")
    assert op["variables"] == ["cliente.nombre"] and op["resumen"].startswith("Hola {cliente.nombre}")
    assert op["estado_detalle"] == "APPROVED", "el estado de su plantilla de Meta (estado_v2) va en la entrada de opciones"

    # texto_pack: solo los campos que el pack marcó; el personalizado se distingue del de fábrica; el correo trae su canal.
    textos = {(m["fuente"]["plugin_id"], m["fuente"]["campo"]): m for m in g["texto_pack"]}
    propio = textos[("pack_subscriptions", "aviso_cargo_texto")]
    assert propio["estado"] == "personalizado" and propio["resumen"] == TEXTO_PROPIO
    assert propio["variables"] == ["cliente.nombre", "cargo.monto"]
    assert textos[("pack_subscriptions", "recordatorio_texto")]["estado"] == "por_defecto"
    assert textos[("pack_calendar", "email_body_confirmed")]["canales_soportados"] == ["email"]
    assert textos[("pack_calendar", "confirmacion_texto")]["canales_soportados"] == ["whatsapp"]
    assert ("pack_calendar", "source") not in textos, "un campo que el pack no marcó como mensaje no entra"
    assert len({m["id"] for m in cuerpo["mensajes"]}) == len(cuerpo["mensajes"]), "los ids son únicos"


async def test_negocio_vacio_solo_trae_los_eventos_sin_mapear_y_nada_ajeno(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    b = negocios["b"]
    r = await app_client.get(URL, headers=await _headers(app_client, b.tid))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    g = _por_tipo(cuerpo)
    assert set(g) == {"evento"} and all(m["estado"] == "sin_mapear" for m in g["evento"])
    assert cuerpo["omitidas"] == []
    assert "cobro_nuevo" not in json.dumps(cuerpo) and TEXTO_PROPIO not in json.dumps(cuerpo), "nada del negocio A"


async def test_bandera_apagada_oculta_las_opciones_y_lo_dice(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    c = negocios["c"]
    r = await app_client.get(URL, headers=await _headers(app_client, c.tid))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert "opciones" not in _por_tipo(cuerpo) and "no_visible" not in json.dumps(cuerpo)
    assert cuerpo["omitidas"] == [{"tipo": "opciones", "motivo": "bandera_apagada"}]


async def test_cada_tipo_exige_la_seccion_de_su_fuente(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a = negocios["a"].tid

    async def tipos(secciones: dict[str, str]) -> tuple[int, set[str], list[dict[str, str]]]:
        h = await _headers(app_client, a, rol="colaborador", secciones=secciones)
        r = await app_client.get(URL, headers=h)
        if r.status_code != 200:
            return r.status_code, set(), []
        cuerpo = r.json()
        return 200, set(_por_tipo(cuerpo)), cuerpo["omitidas"]

    todo_ninguno = {"connections": "none", "settings": "none", "plugins": "none"}
    assert (await tipos(todo_ninguno))[0] == 403

    estado, vistos, omitidas = await tipos({**todo_ninguno, "settings": "read"})
    assert (estado, vistos) == (200, {"evento"})
    assert {(o["tipo"], o["motivo"]) for o in omitidas} == {(t, "sin_permiso") for t in ("meta", "opciones", "texto_pack")}

    estado, vistos, _ = await tipos({**todo_ninguno, "connections": "read"})
    assert (estado, vistos) == (200, {"meta", "opciones"})

    estado, vistos, _ = await tipos({**todo_ninguno, "plugins": "read"})
    assert (estado, vistos) == (200, {"texto_pack"})

    master = await app_client.get(URL, headers=await _headers(app_client, a))
    assert set(_por_tipo(master.json())) == {"meta", "evento", "opciones", "texto_pack"}


async def test_sin_sesion_responde_401(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    assert (await app_client.get(URL)).status_code == 401


async def test_es_de_solo_lectura(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a = negocios["a"].tid

    async def huella() -> list[Any]:
        return await _sql(
            a,
            "select (select count(*) from message_templates), (select count(*) from option_templates), "
            "(select count(*) from option_template_versions), (select count(*) from plugin_configs where tenant_id = :t), "
            "(select count(*) from agent_specs), (select message_templates_map::text from app_settings where tenant_id = :t), "
            "(select count(*) from audit_log)",
            t=a,
        )

    antes = await huella()
    for _ in range(2):
        assert (await app_client.get(URL, headers=await _headers(app_client, a))).status_code == 200
    despues = await huella()
    # El login escribe en `audit_log`; el catálogo no escribe en ninguna de las tablas de las fuentes.
    assert antes[0][:-1] == despues[0][:-1]

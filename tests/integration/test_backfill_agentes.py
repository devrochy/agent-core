"""HU-AOS-13 · Instancias por conexión: Coordinador + General (backfill B2, SPEC-26 §4.4).

Criterios de la HU contra el stack real (sin LLM ni red):
- **Backfill:** cada conexión conversacional tiene Coordinador (KernelSpec derivado de la
  `persona` v2) y General (el AgentSpec v2 activo), y las integraciones no reciben agentes.
  Idempotente por `legacy_agent_spec_id`, con dry-run, precondición del backfill de conexiones
  (HU-AOS-11) y los secretos literales copiados a la instalación `core` antes de la primera
  versión (ninguna versión lleva literales).
- **Uno y solo uno:** un segundo Coordinador en la conexión lo rechaza el índice único; con
  release activa, quedarse sin Coordinador lo impide el trigger diferido.
- **Equivalencia (con `aos_runtime = legacy`):** el backfill no cambia lo que lee el loop
  (`agent_specs`, `app_settings`). La equivalencia del golden con el runtime v3 se mide en
  HU-AOS-14/15.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_api_registrar_numero import _negocio_con_byok

pytestmark = [
    pytest.mark.aislamiento,  # aislamiento entre negocios del backfill y de la API (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SECRETO = "Bearer literal-del-negocio-que-no-entra-a-versiones"


# ---------------------------------------------------------------- helpers
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


async def _conexion(tid: uuid.UUID, tipo: str | None, *, channel: str = "whatsapp", provider: str = "meta") -> uuid.UUID:
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, :ch, :pr, :ext, :tipo)",
        id=cid, t=tid, ch=channel, pr=provider, ext=uniq("ext"), tipo=tipo,
    )
    return cid


def _tool(headers: dict[str, Any], dominios: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": "consultar_envio",
        "url": "https://api.transportadora.co/g/{g}",
        "allowed_domains": ["api.transportadora.co"] if dominios is None else dominios,
        "headers": headers,
    }


async def _poner_tools(tid: uuid.UUID, tools: list[dict[str, Any]], persona: dict[str, Any] | None = None) -> None:
    extra = {"extensions": {"tools": tools}}
    if persona is not None:
        extra["persona"] = persona
    await _sql(
        tid,
        "update agent_specs set spec = spec || cast(:e as jsonb) where tenant_id = :t and is_active",
        e=json.dumps(extra), t=tid,
    )


async def _negocio_migrable() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Negocio dado de alta (con su AgentSpec v2), un número de antes del despliegue (sin
    conexión canónica) y una integración de calendario. Devuelve (negocio, cuenta, calendario)."""
    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    calendario = await _conexion(tid, "google_calendar", channel="calendar", provider="google")
    return tid, cuenta, calendario


async def _backfill(tid: uuid.UUID, *, dry_run: bool):  # type: ignore[no-untyped-def]
    from core.agents.backfill_agentes import backfill_agentes

    return await backfill_agentes(dry_run=dry_run, tenant_id=tid, modules=_modules())


async def _conteos(tid: uuid.UUID) -> dict[str, int]:
    (fila,) = await _sql(
        tid,
        "select (select count(*) from agents where tenant_id = :t), "
        "(select count(*) from agent_versions where tenant_id = :t), "
        "(select count(*) from agent_instances where tenant_id = :t), "
        "(select count(*) from pack_installations where tenant_id = :t and pack_id = 'core')",
        t=tid,
    )
    return dict(zip(("agentes", "versiones", "instancias", "instalacion_core"), fila, strict=True))


async def _legacy_y_settings(tid: uuid.UUID) -> list[Any]:
    """Lo que lee el loop legacy: debe quedar byte a byte igual tras el backfill."""
    specs = await _sql(tid, "select id, is_active, spec::text from agent_specs where tenant_id = :t order by id", t=tid)
    settings = await _sql(tid, "select to_jsonb(s)::text from app_settings s where tenant_id = :t", t=tid)
    return [specs, settings]


# ---------------------------------------------------------------- backfill
async def test_backfill_coordinador_y_general_en_cada_conexion_conversacional() -> None:
    from core.agents.secretos import secretos_configurados
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.shared.tenant import tenant_scope

    tid, cuenta, calendario = await _negocio_migrable()
    await _poner_tools(tid, [_tool({"Authorization": SECRETO, "X-Cliente": "demo"})])
    otro, _, _ = await _negocio_migrable()
    antes = await _legacy_y_settings(tid)

    # Precondición: el número de antes del despliegue no tiene conexión canónica todavía.
    bloqueado = await _backfill(tid, dry_run=False)
    assert bloqueado.fallo and not bloqueado.errores
    (problema,) = bloqueado.bloqueados
    assert problema.tenant_id == tid and "agent-core db backfill-conexiones" in problema.mensaje
    assert await _conteos(tid) == {"agentes": 0, "versiones": 0, "instancias": 0, "instalacion_core": 0}

    await backfill_conexiones(dry_run=False, tenant_id=tid)
    segundo = uniq("ph-2")
    await _sql(
        tid, "insert into phone_numbers (id, tenant_id, connection_id, phone_number_id) values (gen_random_uuid(), :t, :c, :p)",
        t=tid, c=cuenta, p=segundo,
    )
    await backfill_conexiones(dry_run=False, tenant_id=tid)
    conversacionales = [
        cid
        for (cid,) in await _sql(
            tid, "select id from channel_connections where tenant_id = :t and type_id = 'whatsapp_meta'", t=tid
        )
    ]
    assert len(conversacionales) == 2

    # Dry-run: dice lo que haría y no escribe nada.
    seco = await _backfill(tid, dry_run=True)
    assert not seco.fallo and seco.migrados == 1
    assert seco.agentes == {"creado": 2}
    assert seco.versiones == {"creada": 2}
    assert seco.instancias == {"creada": 4}
    assert seco.secretos == {"migrado": 1}
    assert (seco.conexiones_conversacionales, seco.conexiones_sin_agentes) == (2, 1)
    assert await _conteos(tid) == {"agentes": 0, "versiones": 0, "instancias": 0, "instalacion_core": 0}

    reporte = await _backfill(tid, dry_run=False)
    assert not reporte.fallo, (reporte.bloqueados, reporte.errores)
    assert reporte.agentes == {"creado": 2} and reporte.versiones == {"creada": 2}
    assert reporte.instancias == {"creada": 4}

    agentes = {
        key: (aid, kind)
        for key, aid, kind in await _sql(tid, "select key, id, kind from agents where tenant_id = :t", t=tid)
    }
    assert {k: v[1] for k, v in agentes.items()} == {"coordinador": "core", "general": "specialist"}
    (spec_id,) = (await _sql(tid, "select id from agent_specs where tenant_id = :t and is_active", t=tid))[0]
    versiones = await _sql(
        tid,
        "select a.key, v.version, v.contract, v.legacy_agent_spec_id, v.content::text from agent_versions v "
        "join agents a on a.id = v.agent_id where v.tenant_id = :t order by a.key",
        t=tid,
    )
    assert [(k, n, c, legacy) for k, n, c, legacy, _ in versiones] == [
        ("coordinador", 1, "kernelspec/1", spec_id),
        ("general", 1, "agentspec/3", spec_id),
    ]
    kernel, general = json.loads(versiones[0][4]), json.loads(versiones[1][4])
    assert kernel["routing"]["defaultAgent"] == "general"
    assert general["declarativeTools"][0]["headers"] == {
        "Authorization": {"secret": "consultar_envio_authorization"},
        "X-Cliente": "demo",
    }
    assert any(p["version"] == "=0.0.0" for p in general["procedures"]) or general["procedures"] == []

    instancias = await _sql(
        tid,
        "select connection_id, agent_id, role, is_default from agent_instances where tenant_id = :t",
        t=tid,
    )
    for cid in conversacionales:
        de_la_conexion = {(a, r, d) for c, a, r, d in instancias if c == cid}
        assert de_la_conexion == {
            (agentes["coordinador"][0], "core", False),
            (agentes["general"][0], "specialist", True),
        }
    assert all(c != calendario for c, *_ in instancias)  # la integración no recibe agentes

    # El secreto quedó en la instalación core, y ningún literal en versiones ni borradores.
    async with tenant_scope(tid):
        assert await secretos_configurados(tid) == {"consultar_envio_authorization": ["api.transportadora.co"]}
    (texto,) = (
        await _sql(
            tid,
            "select coalesce(string_agg(content::text, ''), '') from ("
            "select content from agent_versions where tenant_id = :t "
            "union all select content from agent_drafts where tenant_id = :t) x",
            t=tid,
        )
    )[0]
    assert SECRETO not in texto and "literal-del-negocio" not in texto

    # Sin cambio de conducta: lo que lee el loop legacy no cambió.
    assert await _legacy_y_settings(tid) == antes
    # Solo el negocio pedido: el otro sigue sin nada (y no se ven sus filas desde aquí).
    assert (await _conteos(otro))["agentes"] == 0

    # Re-correr no crea nada.
    otra_vez = await _backfill(tid, dry_run=False)
    assert not otra_vez.fallo
    assert otra_vez.agentes == {"ya_estaba": 2} and otra_vez.versiones == {"ya_estaba": 2}
    assert otra_vez.instancias == {"ya_estaba": 4} and otra_vez.secretos == {"ya_estaba": 1}
    assert (await _conteos(tid))["instancias"] == 4


async def test_recorrida_cubre_conexiones_nuevas_y_un_v2_nuevo_publica_la_version_siguiente() -> None:
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    tid, _cuenta, _cal = await _negocio_migrable()
    await backfill_conexiones(dry_run=False, tenant_id=tid)
    assert not (await _backfill(tid, dry_run=False)).fallo

    # Una conexión nacida después (Instagram) recibe sus instancias en la re-corrida.
    nueva = await _conexion(tid, "instagram_meta", channel="instagram")
    # Un AgentSpec v2 nuevo activo con otra persona: versión 2 del Coordinador; el General no
    # cambia de contenido (la voz es del KernelSpec) y el dedupe por hash lo deja en la 1.
    (spec_id, spec_text) = (
        await _sql(tid, "select id, spec::text from agent_specs where tenant_id = :t and is_active", t=tid)
    )[0]
    spec = json.loads(spec_text)
    spec["version"] = "1.1.0"
    spec["persona"]["tone"] = "formal"
    await _sql(tid, "update agent_specs set is_active = false where id = :i", i=spec_id)
    await _sql(
        tid,
        "insert into agent_specs (id, tenant_id, version, is_active, spec) "
        "values (gen_random_uuid(), :t, '1.1.0', true, cast(:s as jsonb))",
        t=tid, s=json.dumps(spec),
    )
    reporte = await _backfill(tid, dry_run=False)
    assert not reporte.fallo
    assert reporte.versiones == {"creada": 1, "ya_estaba": 1}
    assert reporte.instancias == {"ya_estaba": 2, "creada": 2}
    numeros = await _sql(
        tid,
        "select a.key, max(v.version) from agent_versions v join agents a on a.id = v.agent_id "
        "where v.tenant_id = :t group by a.key order by a.key",
        t=tid,
    )
    assert numeros == [("coordinador", 2), ("general", 1)]
    (kernel,) = (
        await _sql(
            tid,
            "select v.content::text from agent_versions v join agents a on a.id = v.agent_id "
            "where v.tenant_id = :t and a.key = 'coordinador' and v.version = 2",
            t=tid,
        )
    )[0]
    assert json.loads(kernel)["voice"]["tone"] == "formal"
    assert len(await _sql(tid, "select 1 from agent_instances where connection_id = :c", c=nueva)) == 2


async def test_sin_spec_activo_se_siembra_el_default_y_dry_run_no_escribe() -> None:
    from core.db.session import get_session

    tid = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, 'AOS-13', :s)"), {"id": tid, "s": uniq("aos13")}
        )
    await _conexion(tid, "instagram_meta", channel="instagram")

    seco = await _backfill(tid, dry_run=True)
    assert not seco.fallo and seco.specs_sembrados == 1 and seco.instancias == {"creada": 2}
    assert await _sql(tid, "select 1 from agent_specs where tenant_id = :t", t=tid) == []

    real = await _backfill(tid, dry_run=False)
    assert not real.fallo and real.specs_sembrados == 1
    assert len(await _sql(tid, "select 1 from agent_specs where tenant_id = :t and is_active", t=tid)) == 1
    assert (await _conteos(tid))["instancias"] == 2


async def test_secretos_en_conflicto_bloquean_sin_escribir_y_el_header_sin_dominio_se_poda() -> None:
    from pydantic import SecretStr

    from core.agents.secretos import SecretoDeclarativo, guardar_secretos_declarativos
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.shared.tenant import tenant_scope

    # Conflicto: el editor ya guardó ese nombre con OTRO valor → error, nada escrito.
    tid, _cuenta, _cal = await _negocio_migrable()
    await backfill_conexiones(dry_run=False, tenant_id=tid)
    await _poner_tools(tid, [_tool({"Authorization": SECRETO})])
    async with tenant_scope(tid):
        await guardar_secretos_declarativos(
            tid,
            {
                "consultar_envio_authorization": SecretoDeclarativo(
                    valor=SecretStr("otro-valor"), dominios=("api.transportadora.co",)
                )
            },
        )
    reporte = await _backfill(tid, dry_run=False)
    (error,) = reporte.errores
    assert "consultar_envio_authorization" in error.mensaje and "otro valor" in error.mensaje
    assert SECRETO not in error.mensaje and "otro-valor" not in error.mensaje
    assert (await _conteos(tid))["agentes"] == 0

    # Una tool sin allowed_domains hoy no llama a nadie: su header sensible se poda (con
    # aviso), el negocio migra y la versión no lleva el literal.
    tid2, _c2, _cal2 = await _negocio_migrable()
    await backfill_conexiones(dry_run=False, tenant_id=tid2)
    await _poner_tools(tid2, [_tool({"Authorization": SECRETO, "X-Cliente": "demo"}, dominios=[])])
    reporte = await _backfill(tid2, dry_run=False)
    assert not reporte.fallo and reporte.avisos >= 1 and not reporte.secretos
    (contenido,) = (
        await _sql(
            tid2,
            "select v.content::text from agent_versions v join agents a on a.id = v.agent_id "
            "where v.tenant_id = :t and a.key = 'general'",
            t=tid2,
        )
    )[0]
    assert json.loads(contenido)["declarativeTools"][0]["headers"] == {"X-Cliente": "demo"}
    assert SECRETO not in contenido


async def test_un_secreto_ya_migrado_no_se_amplia_solo_a_dominios_nuevos() -> None:
    """Si el v2 activo suma dominios a una tool cuyo secreto ya se migró (por ejemplo, un
    rollback a una fila vieja), el backfill no ata solo el secreto a esos dominios: error del
    negocio con el nombre y los dominios, sin escribir. Y nunca pisa un secreto existente."""
    from pydantic import SecretStr

    from core.agents.secretos import (
        SecretoDeclarativo,
        SecretoExistente,
        guardar_secretos_declarativos,
        secretos_configurados,
    )
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.shared.tenant import tenant_scope

    tid, _cuenta, _cal = await _negocio_migrable()
    await backfill_conexiones(dry_run=False, tenant_id=tid)
    await _poner_tools(tid, [_tool({"Authorization": SECRETO})])
    assert not (await _backfill(tid, dry_run=False)).fallo

    (spec_id, spec_text) = (
        await _sql(tid, "select id, spec::text from agent_specs where tenant_id = :t and is_active", t=tid)
    )[0]
    spec = json.loads(spec_text)
    spec["extensions"]["tools"][0]["allowed_domains"] = ["api.transportadora.co", "api2.transportadora.co"]
    await _sql(tid, "update agent_specs set is_active = false where id = :i", i=spec_id)
    await _sql(
        tid,
        "insert into agent_specs (id, tenant_id, version, is_active, spec) "
        "values (gen_random_uuid(), :t, '1.2.0', true, cast(:s as jsonb))",
        t=tid, s=json.dumps(spec | {"version": "1.2.0"}),
    )
    versiones_antes = (await _conteos(tid))["versiones"]
    reporte = await _backfill(tid, dry_run=False)
    (error,) = reporte.errores
    assert "consultar_envio_authorization" in error.mensaje and "api2.transportadora.co" in error.mensaje
    assert SECRETO not in error.mensaje
    assert (await _conteos(tid))["versiones"] == versiones_antes
    async with tenant_scope(tid):
        assert await secretos_configurados(tid) == {"consultar_envio_authorization": ["api.transportadora.co"]}
        # Guardado del backfill (`solo_nuevos`): con la fila bloqueada, no pisa uno existente.
        with pytest.raises(SecretoExistente):
            await guardar_secretos_declarativos(
                tid,
                {
                    "consultar_envio_authorization": SecretoDeclarativo(
                        valor=SecretStr("rotado"), dominios=("api.transportadora.co",)
                    )
                },
                solo_nuevos=True,
            )


async def test_corrida_sobre_todos_los_negocios_no_mezcla_agentes_ni_secretos() -> None:
    from core.agents.backfill_agentes import backfill_agentes
    from core.agents.secretos import secretos_configurados
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.shared.tenant import tenant_scope

    negocios = []
    for dominio in ("api.uno.co", "api.dos.co"):
        tid, _cuenta, _cal = await _negocio_migrable()
        await backfill_conexiones(dry_run=False, tenant_id=tid)
        tool = _tool({"Authorization": f"Bearer {dominio}"}, dominios=[dominio]) | {"url": f"https://{dominio}/x"}
        await _poner_tools(tid, [tool])
        negocios.append((tid, dominio))

    # Todos los negocios vivos de la base (los de otros tests pueden quedar bloqueados o con error).
    reporte = await backfill_agentes(dry_run=False, modules=_modules())
    assert not any(p.tenant_id in {t for t, _ in negocios} for p in reporte.bloqueados + reporte.errores)
    for tid, dominio in negocios:
        # Las 2 instancias del negocio apuntan a agentes y conexiones del MISMO negocio.
        propias = await _sql(
            tid,
            "select count(*) from agent_instances i "
            "join agents a on a.id = i.agent_id and a.tenant_id = i.tenant_id "
            "join channel_connections c on c.id = i.connection_id and c.tenant_id = i.tenant_id "
            "where i.tenant_id = :t",
            t=tid,
        )
        assert propias == [(2,)]
        assert (await _conteos(tid))["instancias"] == 2
        async with tenant_scope(tid):
            configurados = await secretos_configurados(tid)
        assert configurados == {"consultar_envio_authorization": [dominio]}


async def test_credenciales_fuera_de_la_lista_url_y_body_pasan_a_la_instalacion_core() -> None:
    """Decisión del dueño (2026-10-11): un header no listado, el userinfo y la query de la URL y
    un token del body también se copian a la instalación `core`; ninguna versión ni borrador
    queda con el literal."""
    from core.agents.secretos import secretos_configurados
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.shared.tenant import tenant_scope

    tid, _cuenta, _cal = await _negocio_migrable()
    await backfill_conexiones(dry_run=False, tenant_id=tid)
    literales = {"goog-literal-1", "clave-userinfo-2", "query-literal-3", "body-literal-4"}
    tool = _tool({"X-Goog-Api-Key": "goog-literal-1", "Content-Type": "application/json"}) | {
        "method": "POST",
        "url": "https://ana:clave-userinfo-2@api.transportadora.co/g/{g}?api_key=query-literal-3&formato=json",
        "body": {"guia": "{g}", "access_token": "body-literal-4"},
    }
    await _poner_tools(tid, [tool])
    reporte = await _backfill(tid, dry_run=False)
    assert not reporte.fallo, reporte.errores
    assert reporte.secretos == {"migrado": 4}

    (contenido,) = (
        await _sql(
            tid,
            "select v.content::text from agent_versions v join agents a on a.id = v.agent_id "
            "where v.tenant_id = :t and a.key = 'general'",
            t=tid,
        )
    )[0]
    (t,) = json.loads(contenido)["declarativeTools"]
    assert t["headers"] == {
        "X-Goog-Api-Key": {"secret": "consultar_envio_x_goog_api_key"},
        "Content-Type": "application/json",
        "Authorization": {"secret": "consultar_envio_authorization"},
    }
    assert t["url"] == (
        "https://api.transportadora.co/g/{g}?api_key={secret:consultar_envio_api_key}&formato=json"
    )
    assert t["body"] == {"guia": "{g}", "access_token": {"secret": "consultar_envio_access_token"}}
    (texto,) = (
        await _sql(
            tid,
            "select coalesce(string_agg(content::text, ''), '') from ("
            "select content from agent_versions where tenant_id = :t "
            "union all select content from agent_drafts where tenant_id = :t) x",
            t=tid,
        )
    )[0]
    assert not any(literal in texto for literal in literales)
    async with tenant_scope(tid):
        configurados = await secretos_configurados(tid)
    assert set(configurados) == {
        "consultar_envio_x_goog_api_key",
        "consultar_envio_authorization",
        "consultar_envio_api_key",
        "consultar_envio_access_token",
    }
    assert all(d == ["api.transportadora.co"] for d in configurados.values())

    # La API de agentes rechaza al guardar un literal nuevo en esos lugares (mismo validador).
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones

    (aid,) = (await _sql(tid, "select id from agents where tenant_id = :t and key = 'general'", t=tid))[0]
    t["body"]["api_secret"] = "nuevo-literal-5"
    general = json.loads(contenido) | {"declarativeTools": [t]}
    async with tenant_scope(tid):
        with pytest.raises(versiones.ErroresDeContrato) as exc:
            await versiones.publicar(tid, aid, catalogo=catalogo_de_acciones(_modules()), contenido=general)
    assert [(i.code, i.path) for i in exc.value.issues] == [
        ("credencial_literal", "declarativeTools[0].body.api_secret")
    ]
    assert "nuevo-literal-5" not in str(exc.value.issues)


# ---------------------------------------------------------------- uno y solo uno
async def test_uno_y_solo_uno_tras_el_backfill() -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, 'AOS-13', :s)"), {"id": tid, "s": uniq("aos13")}
        )
    cid = await _conexion(tid, "instagram_meta", channel="instagram")
    assert not (await _backfill(tid, dry_run=False)).fallo

    # Un segundo Coordinador en la conexión: lo rechaza el índice único.
    otro = uuid.uuid4()
    await _sql(tid, "insert into agents (id, tenant_id, key, kind, name) values (:a, :t, 'otro_core', 'core', 'X')", a=otro, t=tid)
    with pytest.raises(IntegrityError, match="uq_agent_instances_one_core"):
        await _sql(
            tid,
            "insert into agent_instances (tenant_id, connection_id, agent_id, role) values (:t, :c, :a, 'core')",
            t=tid, c=cid, a=otro,
        )

    # Con su Coordinador, la conexión se activa (release activa); dejarla sin él lo impide el
    # trigger diferido al COMMIT.
    rel = uuid.uuid4()
    await _sql(
        tid,
        "insert into connection_releases (id, tenant_id, connection_id, number, manifest, manifest_hash) "
        "values (:r, :t, :c, 1, '{}', :h)",
        r=rel, t=tid, c=cid, h="a" * 64,
    )
    await _sql(tid, "update channel_connections set active_release_id = :r where id = :c", r=rel, c=cid)
    with pytest.raises(DBAPIError, match="sin Coordinador"):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("update agent_instances set enabled = false where connection_id = :c and role = 'core'"),
                    {"c": cid},
                )
    # Y una conexión que nunca tuvo Coordinador no se puede activar.
    sin_core = await _conexion(tid, "instagram_meta", channel="instagram")
    rel2 = uuid.uuid4()
    with pytest.raises(DBAPIError, match="release activa sin Coordinador"):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text(
                        "insert into connection_releases (id, tenant_id, connection_id, number, manifest, manifest_hash) "
                        "values (:r, :t, :c, 1, '{}', :h)"
                    ),
                    {"r": rel2, "t": tid, "c": sin_core, "h": "b" * 64},
                )
                await session.execute(
                    text("update channel_connections set active_release_id = :r where id = :c"), {"r": rel2, "c": sin_core}
                )


async def test_instancias_base_rechaza_integraciones_y_conexiones_de_otro_negocio() -> None:
    from core.agents.instancias import (
        ConexionNoConversacional,
        ConexionNoEncontrada,
        asegurar_instancias_base,
    )
    from core.db.session import get_session
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.shared.tenant import tenant_scope

    tid, _cuenta, calendario = await _negocio_migrable()
    otro, _c, _cal = await _negocio_migrable()
    ajena = await _conexion(otro, "instagram_meta", channel="instagram")
    async with tenant_scope(tid):
        async with get_session() as session:
            with pytest.raises(ConexionNoConversacional):
                await asegurar_instancias_base(session, tid, calendario)
            with pytest.raises(ConexionNoEncontrada):
                await asegurar_instancias_base(session, tid, ajena)
            with pytest.raises(PermissionError):
                await asegurar_instancias_base(session, otro, ajena)
    # Un agente de otro negocio no entra aunque llegue su id: la FK compuesta lo rechaza.
    propia = await _conexion(tid, "instagram_meta", channel="instagram")
    await backfill_conexiones(dry_run=False, tenant_id=otro)
    assert not (await _backfill(otro, dry_run=False)).fallo
    ajenos = {k: a for k, a in await _sql(otro, "select key, id from agents where tenant_id = :t", t=otro)}
    assert ajenos, "el otro negocio tiene sus agentes"
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid):
            async with get_session() as session:
                await asegurar_instancias_base(session, tid, propia, agentes=ajenos)


# ---------------------------------------------------------------- API
async def test_api_lista_las_instancias_de_la_conexion_y_404_para_otro_negocio(app_client) -> None:
    tid = uuid.uuid4()
    from core.db.session import get_session

    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, 'AOS-13', :s)"), {"id": tid, "s": uniq("aos13")}
        )
    cid = await _conexion(tid, "instagram_meta", channel="instagram")
    otro = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, 'AOS-13b', :s)"), {"id": otro, "s": uniq("aos13")}
        )
    ajena = await _conexion(otro, "instagram_meta", channel="instagram")
    assert not (await _backfill(tid, dry_run=False)).fallo
    assert not (await _backfill(otro, dry_run=False)).fallo

    h = await api_headers(app_client, tid)
    r = await app_client.get(f"/api/v1/admin/connections/{cid}/agents", headers=h)
    assert r.status_code == 200, r.text
    filas = r.json()
    assert [(f["key"], f["role"], f["is_default"], f["latest_version"]) for f in filas] == [
        ("coordinador", "core", False, 1),
        ("general", "specialist", True, 1),
    ]
    # La conexión de otro negocio no existe para este.
    r = await app_client.get(f"/api/v1/admin/connections/{ajena}/agents", headers=h)
    assert r.status_code == 404
    r = await app_client.get(f"/api/v1/admin/connections/{uuid.uuid4()}/agents", headers=h)
    assert r.status_code == 404

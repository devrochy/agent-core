"""HU-AOS-14 · Releases por conexión con rollback en un clic (ADR-0117), contra el stack real.

Sin LLM ni red. Criterios de la HU:
- **Build:** congela versiones, habilidades con su versión exacta registrada en `pack_versions`,
  overrides de playbooks, secciones del negocio y los campos de `AppSettings` del prompt, con
  `manifest_hash`; no se activa sola; idempotente por hash.
- **Release 1:** por conexión conversacional, desde el AgentSpec v2 activo, idempotente; con
  `aos_runtime = legacy` no cambia lo que lee el loop.
- **Sticky por episodio:** con la 2 publicada, una conversación en la 1 sigue en ella hasta el
  fin del episodio, y su siguiente episodio usa la 2.
- **Rollback:** cambio de puntero; las conversaciones de la release retirada cambian en su
  siguiente turno, las tareas se revalidan (`revalidated`) o, sin su procedimiento, se cancelan
  o se escalan si tuvieron un efecto irreversible; queda un `release_event`; dos clics no rebotan.
- **Sync de `pack_versions`:** la función SECURITY DEFINER recalcula el hash, rechaza plugins de
  un negocio y no la ejecuta `app_runtime`.
- **API:** permisos, 404 entre negocios, 409.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.aislamiento,  # releases de una conexión: aislamiento entre negocios (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"


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


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte


async def _negocio() -> tuple[uuid.UUID, str, uuid.UUID]:
    """Negocio dado de alta (AgentSpec v2) con una conexión conversacional. (tid, slug, conexión)."""
    from core.alta_negocio import onboard_tenant

    slug = uniq("aos14")
    tid, _ = await onboard_tenant(name=f"AOS-14 {slug}", slug=slug, persona={"tone": "cercano"})
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid,
        t=tid,
        ext=uniq("ig"),
    )
    return tid, slug, cid


async def _release_uno(tid: uuid.UUID) -> Any:
    from core.releases.release_uno import backfill_releases

    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    return reporte


async def _construir(tid: uuid.UUID, cid: uuid.UUID) -> Any:
    from core.releases.build import construir
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await construir(tid, cid, actor="test", modules=_modules())


async def _promover(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID) -> Any:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await promover(tid, cid, rid, actor="test")


async def _rollback(tid: uuid.UUID, cid: uuid.UUID, **kw: Any) -> Any:
    from core.releases.promocion import rollback
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await rollback(tid, cid, actor="test", **kw)


async def _activa(tid: uuid.UUID, cid: uuid.UUID) -> uuid.UUID | None:
    (fila,) = await _sql(
        tid, "select active_release_id from channel_connections where id = :c", c=cid
    )
    return fila[0]


async def _cambiar_nombre(tid: uuid.UUID, nombre: str) -> None:
    await _sql(
        tid, "update app_settings set assistant_name = :n where tenant_id = :t", n=nombre, t=tid
    )


async def _conversacion(tid: uuid.UUID, cid: uuid.UUID) -> uuid.UUID:
    cliente, conv = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid,
        "insert into customers (id, tenant_id, channel, external_id) values (:id, :t, 'instagram', :e)",
        id=cliente,
        t=tid,
        e=uniq("cli"),
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id) "
        "values (:id, :t, :cli, 'instagram', :c)",
        id=conv,
        t=tid,
        cli=cliente,
        c=cid,
    )
    return conv


async def _mensaje(
    tid: uuid.UUID, conv: uuid.UUID, cuando: datetime, direccion: str = "inbound"
) -> None:
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, created_at) "
        "values (:id, :t, :c, :d, :s, 'hola', :w)",
        id=uuid.uuid4(),
        t=tid,
        c=conv,
        d=direccion,
        s="customer" if direccion == "inbound" else "agent",
        w=cuando,
    )


async def _asignar(tid: uuid.UUID, cid: uuid.UUID, conv: uuid.UUID) -> Any:
    from core.db.session import get_session
    from core.kernel.release_del_turno import asignar
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await asignar(
                session, tenant_id=tid, conversation_id=conv, connection_id=cid
            )
    assert resultado is not None
    return resultado[0]


# ---------------------------------------------------------------- release 1 y build
async def test_release_uno_desde_el_v2_activo_idempotente_y_sin_cambio_legacy() -> None:
    tid, slug, cid = await _negocio()
    from core.onboarding import tenant_dir

    playbooks = tenant_dir(slug) / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    (playbooks / "tono").with_suffix(".md").write_text(
        "Override del negocio para el tono.", encoding="utf-8"
    )
    (playbooks / "no_permitido.md").write_text("Fuera de la allowlist.", encoding="utf-8")
    legacy = await _sql(
        tid,
        "select id, is_active, spec::text from agent_specs where tenant_id = :t order by id",
        t=tid,
    )

    reporte = await _release_uno(tid)
    assert reporte.conexiones["construida"] == 1
    rid = await _activa(tid, cid)
    assert rid is not None
    (fila,) = await _sql(
        tid,
        "select number, origin, manifest, manifest_hash from connection_releases where id = :r",
        r=rid,
    )
    numero, origen, manifest, huella = fila
    assert (numero, origen) == (1, "owner")
    from core.releases.manifiesto import hash_de

    assert hash_de(manifest) == huella
    assert [c["role"] for c in manifest["components"]] == ["core", "specialist"]
    assert {s["pack_id"] for s in manifest["skills"]} <= {m.id for m in _modules()}
    lock = {s["pack_id"]: s["version"] for s in manifest["skills"]}
    for modulo in _modules():
        if modulo.id in lock:
            assert lock[modulo.id] == modulo.version
    assert [p["name"] for p in manifest["playbook_overrides"]] == ["tono"]  # solo la allowlist
    assert set(manifest["app_settings"]) == {
        "assistant_name",
        "business_description",
        "language",
        "brand_voice",
    }
    componentes = await _sql(
        tid, "select count(*) from release_components where release_id = :r", r=rid
    )
    assert componentes[0][0] == 2
    tipos = [
        t
        for (t,) in await _sql(
            tid, "select type from release_events where release_id = :r order by created_at", r=rid
        )
    ]
    assert tipos == ["built", "promoted"]

    # Idempotente: una re-corrida no construye otra; el loop legacy lee lo mismo.
    reporte = await _release_uno(tid)
    assert reporte.conexiones["construida"] == 0 and reporte.conexiones["ya_estaba"] == 1
    assert legacy == await _sql(
        tid,
        "select id, is_active, spec::text from agent_specs where tenant_id = :t order by id",
        t=tid,
    )


async def test_build_congela_no_se_activa_y_es_idempotente_por_hash() -> None:
    tid, _, cid = await _negocio()
    await _release_uno(tid)
    uno = await _activa(tid, cid)

    mismo = await _construir(tid, cid)
    assert (mismo.release_id, mismo.creada) == (uno, False)

    await _cambiar_nombre(tid, "Asistente nuevo")
    dos = await _construir(tid, cid)
    assert dos.creada and dos.number == 2
    assert await _activa(tid, cid) == uno  # no se activa sola
    (fila,) = await _sql(
        tid,
        "select manifest->'app_settings'->>'assistant_name' from connection_releases where id = :r",
        r=dos.release_id,
    )
    assert fila[0] == "Asistente nuevo"

    # Inmutable.
    with pytest.raises(DBAPIError):
        await _sql(
            tid, "update connection_releases set number = 99 where id = :r", r=dos.release_id
        )


async def test_un_override_ilegible_hace_fallar_el_build() -> None:
    from core.onboarding import tenant_dir
    from core.releases.build import ErrorDelBuild

    tid, slug, cid = await _negocio()
    await _release_uno(tid)
    playbooks = tenant_dir(slug) / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    (playbooks / "tono.md").write_text("x" * (9 * 1024), encoding="utf-8")
    with pytest.raises(ErrorDelBuild) as error:
        await _construir(tid, cid)
    assert error.value.codigo == "override_invalido"


# ---------------------------------------------------------------- rollback
async def test_rollback_en_un_clic_no_rebota_y_deja_eventos() -> None:
    from core.releases.promocion import ConflictoDeRelease

    tid, _, cid = await _negocio()
    await _release_uno(tid)
    uno = await _activa(tid, cid)
    await _cambiar_nombre(tid, "Dos")
    dos = (await _construir(tid, cid)).release_id
    await _promover(tid, cid, dos)
    await _cambiar_nombre(tid, "Tres")
    tres = (await _construir(tid, cid)).release_id
    await _promover(tid, cid, tres)

    with pytest.raises(ConflictoDeRelease):
        await _promover(tid, cid, tres)  # ya activa

    assert (await _rollback(tid, cid)).to_release_id == dos
    assert await _activa(tid, cid) == dos
    # Un segundo clic va a la 1, no vuelve a la 3.
    assert (await _rollback(tid, cid)).to_release_id == uno
    with pytest.raises(ConflictoDeRelease):
        await _rollback(tid, cid)  # no queda una anterior
    eventos = await _sql(
        tid,
        "select type, data->>'to_release_id' from release_events where release_id = :r and type = 'rolled_back'",
        r=tres,
    )
    assert eventos == [("rolled_back", str(dos))]


# ---------------------------------------------------------------- sticky por episodio y revalidación
async def test_sticky_por_episodio_y_rollback_en_el_siguiente_turno() -> None:
    tid, _, cid = await _negocio()
    await _release_uno(tid)
    uno = await _activa(tid, cid)
    conv = await _conversacion(tid, cid)
    t0 = datetime.now(UTC) - timedelta(days=3)

    await _mensaje(tid, conv, t0)
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (uno, "primera")

    await _cambiar_nombre(tid, "Dos")
    dos = (await _construir(tid, cid)).release_id
    await _promover(tid, cid, dos)

    # Mismo episodio (minutos después): sigue en la 1.
    await _mensaje(tid, conv, t0 + timedelta(minutes=1), "outbound")
    await _mensaje(tid, conv, t0 + timedelta(minutes=5))
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (uno, None)

    # Pasa más que `episode.idleMinutes` (720 por defecto): episodio nuevo con la 2.
    await _mensaje(tid, conv, t0 + timedelta(minutes=6), "outbound")
    await _mensaje(tid, conv, t0 + timedelta(hours=14))
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (dos, "episodio_nuevo")

    # Rollback a la 1: la conversación cambia en su siguiente turno, a mitad de episodio.
    await _rollback(tid, cid)
    await _mensaje(tid, conv, t0 + timedelta(hours=14, minutes=1), "outbound")
    await _mensaje(tid, conv, t0 + timedelta(hours=14, minutes=2))
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (uno, "retirada")
    (fila,) = await _sql(
        tid, "select release_id, release_lane from conversations where id = :c", c=conv
    )
    assert fila == (uno, "stable")


async def test_al_cambiar_de_release_las_tareas_se_revalidan_cancelan_o_escalan() -> None:
    from core.db.session import get_session
    from core.kernel import state
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    tid, _, cid = await _negocio()
    await _release_uno(tid)
    uno = await _activa(tid, cid)
    assert uno is not None
    conv = await _conversacion(tid, cid)
    t0 = datetime.now(UTC) - timedelta(days=2)
    await _mensaje(tid, conv, t0)
    await _asignar(tid, cid, conv)

    async with tenant_scope(tid):
        async with get_session() as session:
            release = await cargar_release(session, tid, uno)
            assert release is not None
            existente = sorted(release.procedimientos())[0]
            proc = release.procedimientos()[existente]
            vigente = await state.iniciar(
                session,
                tenant_id=tid,
                conversation_id=conv,
                procedure_id=existente,
                procedure_version=proc.version,
                stage=proc.stages[0].id,
                actor="kernel",
                now=t0,
            )
            sin_efecto = await state.iniciar(
                session,
                tenant_id=tid,
                conversation_id=conv,
                procedure_id="tenant.retirado",
                procedure_version="0.0.0",
                stage="unica",
                actor="kernel",
                stack_position=1,
                now=t0,
            )
            con_efecto = await state.iniciar(
                session,
                tenant_id=tid,
                conversation_id=conv,
                procedure_id="tenant.cobro",
                procedure_version="0.0.0",
                stage="unica",
                actor="kernel",
                stack_position=2,
                now=t0,
            )
            await state.registrar(
                session,
                tenant_id=tid,
                conversation_id=conv,
                task_id=con_efecto.id,
                tipo="action_succeeded",
                data={"action": "crear_link_de_pago"},
                actor="tool",
                now=t0,
            )
            ids = (vigente.id, sin_efecto.id, con_efecto.id)

    await _cambiar_nombre(tid, "Dos")
    dos = (await _construir(tid, cid)).release_id
    await _promover(tid, cid, dos)
    await _rollback(tid, cid, to_release_id=uno)  # retira la 2 (nadie en ella)
    await _promover(tid, cid, dos)
    # La conversación (en la 1) cambia a la 2 al empezar su siguiente episodio, con actividad
    # reciente en las tareas para que no expiren por episodio.
    await _sql(
        tid, "update conversation_tasks set updated_at = now() where conversation_id = :c", c=conv
    )
    await _mensaje(tid, conv, t0 + timedelta(minutes=1), "outbound")
    await _mensaje(tid, conv, datetime.now(UTC))
    a = await _asignar(tid, cid, conv)
    assert a.release_id == dos
    assert a.revalidadas == (ids[0],) and a.canceladas == (ids[1],) and a.escaladas == (ids[2],)
    filas = dict(
        (r[0], (r[1], r[2]))
        for r in await _sql(
            tid,
            "select id, status, status_reason from conversation_tasks where conversation_id = :c",
            c=conv,
        )
    )
    assert filas[ids[0]] == ("active", None)
    assert filas[ids[1]] == ("cancelled", "release_changed")
    assert filas[ids[2]] == ("escalated", "release_changed")
    (evento,) = await _sql(
        tid, "select data from task_events where task_id = :t and type = 'revalidated'", t=ids[0]
    )
    assert evento[0]["from_release_id"] == str(uno) and evento[0]["to_release_id"] == str(dos)
    async with tenant_scope(tid):
        async with get_session() as session:
            # La proyección sigue reconstruible desde los eventos (ADR-0114).
            for tarea in ids:
                reconstruida = await state.reconstruir(
                    session, tenant_id=tid, conversation_id=conv, task_id=tarea
                )
                assert reconstruida.status == filas[tarea][0]


# ---------------------------------------------------------------- equivalencia del prompt
async def test_con_la_release_1_el_prompt_es_el_mismo_que_en_legacy() -> None:
    from core.agents.spec import load_active_spec
    from core.db.session import get_session
    from core.kernel.release_del_turno import con_release
    from core.llm.prompt_builder import PromptContext, build_system_blocks
    from core.onboarding import tenant_dir
    from core.releases.cargar import cargar_release
    from core.shared import release_vigente
    from core.shared.tenant import tenant_scope

    tid, slug, cid = await _negocio()
    playbooks = tenant_dir(slug) / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    (playbooks / "tono.md").write_text("Override congelado del tono.", encoding="utf-8")
    await _release_uno(tid)
    rid = await _activa(tid, cid)
    assert rid is not None
    ahora = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    async with tenant_scope(tid):
        spec = await load_active_spec(tid)
        # Como el loop: los campos salen de `app_settings` en caliente.
        ((nombre, descripcion, idioma, voz),) = await _sql(
            tid,
            "select assistant_name, business_description, language, brand_voice from app_settings where tenant_id = :t",
            t=tid,
        )
        ctx = PromptContext(
            assistant_name=nombre,
            business_description=descripcion,
            language=idioma,
            brand_voice=voz,
            now=ahora,
        )
        legacy = await build_system_blocks(spec, ctx)

        @con_release
        async def turno_v3() -> list[str]:
            async with get_session() as session:
                release = await cargar_release(session, tid, rid)
            assert release is not None
            release_vigente.fijar(release.artefactos())
            return await build_system_blocks(spec, release_vigente.con_valores_de_release(ctx))

        assert await turno_v3() == legacy
        # Un override o un nombre editados en caliente no entran hasta la siguiente release.
        (playbooks / "tono.md").write_text("Editado después del build.", encoding="utf-8")
        ctx.assistant_name = "Editado en caliente"
        congelado = await turno_v3()
        assert congelado == legacy
        assert any("Override congelado" in b for b in congelado)
        assert not any("Editado" in b for b in congelado)


# ---------------------------------------------------------------- pack_versions
async def test_register_pack_version_valida_en_la_base_y_no_es_de_app_runtime() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings
    from core.releases.packs import sha256_texto, texto_canonico

    dueno = create_async_engine(get_settings().migrations_database_url)
    pid, ajeno = uniq("pack_test").replace("-", "_"), uniq("plugin_negocio").replace("-", "_")
    texto = texto_canonico({"provides": {"agent_templates": []}})
    try:
        async with dueno.begin() as conn:
            await conn.execute(
                text(
                    "insert into plugins (id, name, version) values (:a, :a, '1.0.0'), (:b, :b, '1.0.0')"
                ),
                {"a": pid, "b": ajeno},
            )
            await conn.execute(
                text("update plugins set owner_tenant_id = gen_random_uuid() where id = :b"),
                {"b": ajeno},
            )

            async def registrar(p: str, v: str, t: str, h: str) -> str:
                return (
                    await conn.execute(
                        text("select register_pack_version(:p, :v, 'pack/2', :t, :h)"),
                        {"p": p, "v": v, "t": t, "h": h},
                    )
                ).scalar_one()

            assert await registrar(pid, "1.0.0", texto, sha256_texto(texto)) == "inserted"
            assert await registrar(pid, "1.0.0", texto, sha256_texto(texto)) == "exists"
            otro = texto_canonico({"provides": {"agent_templates": [], "x": 1}})
            assert await registrar(pid, "1.0.0", otro, sha256_texto(otro)) == "hash_mismatch"
            assert await registrar(pid, "1.0.1", texto, "0" * 64) == "hash_invalid"
            assert await registrar(ajeno, "1.0.0", texto, sha256_texto(texto)) == "not_platform"
            assert (
                await registrar(uniq("nada"), "1.0.0", texto, sha256_texto(texto)) == "unknown_pack"
            )
            roto = '{"provides": {"agent_templates": {}}}'  # plantillas que no son lista (CHECK)
            assert await registrar(pid, "2.0.0", roto, sha256_texto(roto)) == "invalid_manifest"
            assert (
                await registrar(pid, "2.0.1", "no es json", sha256_texto("no es json"))
                == "invalid_manifest"
            )
            nulos = text("select register_pack_version(:p, null, null, null, null)")
            assert (await conn.execute(nulos, {"p": pid})).scalar_one() == "invalid_input"
            await conn.execute(
                text(
                    "update pack_versions set yanked = true where pack_id = :p and version = '1.0.0'"
                ),
                {"p": pid},
            )
            assert await registrar(pid, "1.0.0", texto, sha256_texto(texto)) == "yanked"
            # Un plugin con versiones en el catálogo no se vuelve privado.
            with pytest.raises(DBAPIError):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "update plugins set owner_tenant_id = gen_random_uuid() where id = :p"
                        ),
                        {"p": pid},
                    )
    finally:
        await dueno.dispose()

    with pytest.raises(DBAPIError):
        await _sql(None, "select register_pack_version('x', '1', 'pack/2', '{}', 'h')")


# ---------------------------------------------------------------- API
async def _dueno(tid: uuid.UUID, role: str = "owner") -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=role))
    return email


async def test_api_releases_permisos_aislamiento_y_rollback(app_client) -> None:
    tid, _, cid = await _negocio()
    otro, _, ajena = await _negocio()
    await _release_uno(tid)
    await _release_uno(otro)
    uno = await _activa(tid, cid)
    master = await api_headers(app_client, tid)
    base = f"/api/v1/admin/connections/{cid}"

    r = await app_client.get(f"{base}/releases", headers=master)
    assert r.status_code == 200, r.text
    assert [(f["number"], f["active"], f["last_event"]) for f in r.json()] == [
        (1, True, "promoted")
    ]

    r = await app_client.get(f"{base}/releases/{uno}", headers=master)
    assert r.status_code == 200
    detalle = r.json()
    assert [e["type"] for e in detalle["events"]] == ["built", "promoted"]
    assert "text" not in str(detalle["manifest"]["playbook_overrides"])

    # Otro negocio: su conexión y su release no existen para este.
    assert (
        await app_client.get(f"/api/v1/admin/connections/{ajena}/releases", headers=master)
    ).status_code == 404
    ajena_rid = await _activa(otro, ajena)
    assert (await app_client.get(f"{base}/releases/{ajena_rid}", headers=master)).status_code == 404
    r = await app_client.post(
        f"{base}/rollback", headers=master, json={"to_release_id": str(ajena_rid)}
    )
    assert r.status_code == 404

    # El dueño en legacy no escribe (403); con v3 sí.
    dueno = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    assert (await app_client.post(f"{base}/releases", headers=dueno, json={})).status_code == 403
    await _sql(None, "update tenants set aos_runtime = 'v3' where id = :t", t=tid)
    await _cambiar_nombre(tid, "Desde la API")
    r = await app_client.post(f"{base}/releases", headers=dueno, json={"notes": "cambio de nombre"})
    assert r.status_code == 201, r.text
    dos = r.json()["release"]
    assert (dos["number"], dos["active"], r.json()["created"]) == (2, False, True)

    # Un colaborador con escritura no promueve ni vuelve atrás: solo propietario o master.
    colaborador = await api_headers(
        app_client, tid, email=await _dueno(tid, "colaborador"), password=PASSWORD
    )
    r = await app_client.post(f"{base}/releases/{dos['id']}/promote", headers=colaborador, json={})
    assert r.status_code == 403

    r = await app_client.post(f"{base}/releases/{dos['id']}/promote", headers=dueno, json={})
    assert r.status_code == 200, r.text
    assert r.json()["from_release_id"] == str(uno)
    assert (
        await app_client.post(f"{base}/releases/{dos['id']}/promote", headers=dueno, json={})
    ).status_code == 409
    r = await app_client.post(f"{base}/rollback", headers=dueno, json={"reason": "rollback"})
    assert r.status_code == 422  # motivo reservado al sistema
    r = await app_client.post(f"{base}/rollback", headers=dueno, json={"reason": "regresion"})
    assert r.status_code == 200 and r.json()["to_release_id"] == str(uno)
    assert await _activa(tid, cid) == uno
    assert (await app_client.post(f"{base}/rollback", headers=dueno, json={})).status_code == 409


async def test_fijar_release_del_turno_solo_con_v3(monkeypatch) -> None:
    from core.config import get_settings
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.kernel.release_del_turno import con_release, fijar_release_del_turno
    from core.shared import release_vigente
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    tid, _, cid = await _negocio()
    await _release_uno(tid)
    uno = await _activa(tid, cid)
    conv = await _conversacion(tid, cid)
    await _mensaje(tid, conv, datetime.now(UTC))

    @con_release
    async def turno() -> tuple[Any, Any]:
        async with get_session() as session:
            conversacion = await session.get(Conversation, conv)
        asignacion = await fijar_release_del_turno(conversacion)
        return asignacion, release_vigente.vigente()

    async with tenant_scope(tid):
        asignacion, vigente = await turno()
    assert asignacion is None and vigente is None  # legacy: nada
    assert (await _sql(tid, "select release_id from conversations where id = :c", c=conv))[0][
        0
    ] is None

    await _sql(None, "update tenants set aos_runtime = 'v3' where id = :t", t=tid)
    async with tenant_scope(tid):
        asignacion, vigente = await turno()
    assert asignacion is not None and asignacion.release_id == uno
    assert vigente is not None and vigente.release_id == uno and vigente.idle_minutes == 720

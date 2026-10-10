"""Ciclo de vida de tenants: suspensión y purga (ADR-0001, invariante #8).

Genéricos: se crean tenants desechables por test, nunca por cliente. La suspensión
bloquea webhook y consumer; la purga es borrado físico irreversible (frase exacta
"ELIMINAR <slug>") que vacía core + packs + Redis y la fila tenants.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from urllib.parse import unquote

import pytest
from conftest import _meta_request
from sqlalchemy import func, select, text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "tenants" / "moto" / "catalog.json"
)
INBOUND_STREAM = "agent_core:inbound"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _onboard(slug: str, whatsapp_phone: str = "") -> tuple[uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant desechable {slug}",
        slug=slug,
        business_description="Ciclo de vida (test genérico).",
        persona={"tone": "cercano"},
        packs=["pack_commerce"],
        whatsapp={
            "label": slug,
            "phone_number_id": whatsapp_phone,
        }
        if whatsapp_phone
        else None,
    )
    return tid, slug


async def _seed_catalog(tid: uuid.UUID) -> None:
    from cli.commands.tenant import seed_catalog

    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    await seed_catalog(tid, data)


async def _tenant_tables() -> list[str]:
    from core.db.session import get_session

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "select table_name from information_schema.columns "
                    "where column_name = 'tenant_id' and table_schema = 'public'"
                )
            )
        ).scalars().all()
    return sorted(rows)


async def _count_rows(table: str, tenant_id: uuid.UUID) -> int:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return (
                await session.execute(
                    text(f"select count(*) from {table} where tenant_id = :tid"),
                    {"tid": str(tenant_id)},
                )
            ).scalar()


def _meta_payload(phone_id: str, wa_id: str, message_id: str, body: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "e1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": phone_id},
                    "contacts": [{"wa_id": wa_id}],
                    "messages": [{
                        "from": wa_id,
                        "id": message_id,
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": body},
                    }],
                },
            }],
        }],
    }


# ------------------------------------------------------------------ suspensión
async def test_suspendido_no_procesa_webhook(app_client) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant

    phone = _uniq("555")
    tid, _slug = await _onboard(_uniq("susp-webhook"), whatsapp_phone=phone)
    await deactivate_tenant(str(tid))

    body, headers = _meta_request(
        _meta_payload(phone, "wa-susp", f"wamid.susp.{uuid.uuid4().hex[:6]}", "hola")
    )
    r = await app_client.post(
        "/webhooks/whatsapp",
        content=body,
        headers=headers,
    )
    assert r.json().get("status") == "ignored", "un tenant suspendido no encola mensajes"

    async with tenant_scope(tid):
        async with get_session() as session:
            n = (
                await session.execute(select(func.count()).select_from(Conversation))
            ).scalar()
    assert n == 0, "no debe crearse ninguna conversación para el tenant suspendido"


async def test_suspendido_descarta_eventos_ya_encolados(app_client, recorded) -> None:
    """Mensajes en el stream de un tenant suspendido se ACKean sin procesar."""
    from core.memory.working import get_redis
    from core.orchestrator.consumer import consume_loop
    from core.tenants import deactivate_tenant

    tid, _slug = await _onboard(_uniq("susp-consumer"))
    await deactivate_tenant(str(tid))

    await get_redis().xadd(
        INBOUND_STREAM,
        {
            "tenant_id": str(tid),
            "provider": "meta",
            "channel": "whatsapp",
            "external_message_id": f"wamid.queued.{uuid.uuid4().hex[:6]}",
            "customer_external_id": "wa-queued",
            "content": "mensaje encolado antes de la suspensión",
            "media": "null",
        },
    )
    recorded.clear()

    consumer = asyncio.create_task(consume_loop())
    await asyncio.sleep(2)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert recorded == [], "el consumer no debe procesar mensajes de un tenant suspendido"
    # El evento se consumió (ACK): no debe quedar PENDIENTE en el grupo.
    from core.orchestrator.consumer import GROUP

    summary = await get_redis().xpending(INBOUND_STREAM, GROUP)
    assert summary.get("pending", 0) == 0, "el evento del tenant suspendido debe descartarse (ACK), no reintentarse"


# ------------------------------------------------------------------ purga
async def test_suspendido_no_responde_por_debounce(recorded) -> None:
    """Un mensaje con debounce ya programado no genera respuesta si el tenant se suspendió."""
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.orchestrator.debounce import poll_debounce_once, schedule_debounce
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant

    tid, slug = await _onboard(_uniq("susp-debounce"))
    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id="wa-deb")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp")
            session.add(conv)
            await session.commit()
            conv_id = conv.id

    # debounce programado con delay corto (como si el mensaje llegara pre-suspensión)
    await schedule_debounce(conv_id, 500, {"latest_message": "hola", "tenant_id": str(tid)})
    await deactivate_tenant(slug)
    recorded.clear()

    await poll_debounce_once()

    assert recorded == [], "un tenant suspendido no debe generar turnos por el debounce"
    from core.db.models import Message
    async with tenant_scope(tid):
        async with get_session() as session:
            outbound = (
                await session.execute(
                    select(func.count()).select_from(Message).where(Message.direction == "outbound")
                )
            ).scalar()
    assert outbound == 0, "no debe enviarse ninguna respuesta del tenant suspendido"


async def test_purge_frase_incorrecta_no_borra() -> None:
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.tenants import TenantNotSuspendedError, deactivate_tenant, purge_tenant

    tid, _slug = await _onboard(_uniq("purge-nofrase"))
    await _seed_catalog(tid)
    await deactivate_tenant(str(tid))

    with pytest.raises(ValueError) as exc:
        await purge_tenant(str(tid), "ELIMINAR OTRACOSA")
    assert not isinstance(exc.value, TenantNotSuspendedError)
    assert "Frase de confirmación" in str(exc.value)

    async with get_session() as session:
        still = await session.get(Tenant, tid)
    assert still is not None, "una frase incorrecta no borra nada"


async def test_purge_borra_core_packs_redis_y_tenant() -> None:
    from core.db.models import Admin, Conversation, Customer, Message, Tenant
    from core.db.session import get_session
    from core.memory.working import append_turn, get_redis
    from core.shared.tenant import admin_tenants, tenant_scope
    from core.tenants import purge_tenant

    tid, slug = await _onboard(_uniq("purge-todo"))
    await _seed_catalog(tid)

    # cliente + conversación + mensaje + working memory en Redis
    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id="wa-purge")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp", status="open")
            session.add(conv)
            await session.flush()
            session.add(
                Message(
                    tenant_id=tid, conversation_id=conv.id, direction="inbound",
                    sender_type="customer", content="hola",
                )
            )
            await session.commit()
            conv_id = conv.id
    await append_turn(conv_id, "user", "hola")

    tables = await _tenant_tables()
    assert tables, "debe haber tablas con tenant_id"
    assert sum([await _count_rows(t, tid) for t in tables]) > 0, "el tenant debe tener datos"

    from core.tenants import deactivate_tenant

    await deactivate_tenant(slug)
    await purge_tenant(str(tid), f"ELIMINAR {slug}")

    # cero filas en TODAS las tablas (core + packs)
    for t in tables:
        assert await _count_rows(t, tid) == 0, f"{t} debería estar vacío tras el purge"

    # la fila tenants ya no existe
    async with get_session() as session:
        assert await session.get(Tenant, tid) is None, "la fila tenants debe desaparecer"

    # Redis: la working memory de la conversación se borró
    assert await get_redis().exists(f"wm:{conv_id}") == 0, "working memory del tenant purgada"

    # el admin pierde acceso al tenant
    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == "admin@local"))
        ).scalar_one_or_none()
    if admin is not None:
        ids = {str(t[0]) for t in await admin_tenants(admin.id)}
        assert str(tid) not in ids, "tras el purge, el admin no puede ver el tenant"


async def test_purge_de_un_negocio_activo_falla_y_no_borra_nada() -> None:
    """Solo se purga un negocio suspendido: en Python (con un mensaje que dice cómo
    suspenderlo) y, por defensa en profundidad, en la función SQL."""
    import sqlalchemy.exc

    from core.db.models import Tenant
    from core.db.session import get_session
    from core.memory.working import append_turn, get_redis
    from core.shared.tenant import tenant_scope
    from core.tenants import TenantNotSuspendedError, purge_tenant

    tid, slug = await _onboard(_uniq("purge-activo"))
    await _seed_catalog(tid)
    await _sembrar_conversacion(tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            conv_id = (
                await session.execute(text("select id from conversations where tenant_id = :t"),
                                      {"t": str(tid)})
            ).scalar_one()
    await append_turn(conv_id, "user", "hola")

    tables = await _tenant_tables()
    antes = {t: await _count_rows(t, tid) for t in tables}
    assert sum(antes.values()) > 0

    with pytest.raises(TenantNotSuspendedError, match=f"tenant deactivate {slug}"):
        await purge_tenant(str(tid), f"ELIMINAR {slug}")

    # La función SQL, llamada directo por el runtime, también se niega.
    async with get_session() as session:
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="no está suspendido"):
            await session.execute(text("select purge_tenant(:tid)"), {"tid": str(tid)})
        await session.rollback()

    assert {t: await _count_rows(t, tid) for t in tables} == antes, "no se borró nada"
    async with get_session() as session:
        assert (await session.get(Tenant, tid)).status == "active"
    assert await get_redis().exists(f"wm:{conv_id}") == 1, "Redis intacto"


# ------------------------------------------------------------------ purga sin superusuario
_DUENO_SIN_SUPER = "purge_dueno_sin_superusuario"


def _url_dueno() -> str:
    url = os.environ.get("MIGRATIONS_DATABASE_URL", "")
    if not url:
        pytest.skip("requiere MIGRATIONS_DATABASE_URL (el rol dueño de las tablas)")
    return url.replace("+asyncpg", "", 1)


async def _limpiar_dueno_de_prueba(conn) -> None:
    """Devuelve la función al rol que migra y borra el rol de prueba, si quedó de una corrida
    anterior que murió a medias."""
    if not await conn.fetchval("select 1 from pg_roles where rolname = $1", _DUENO_SIN_SUPER):
        return
    dueno = await conn.fetchval(
        "select proowner::regrole::text from pg_proc "
        "where oid = 'public.purge_tenant(uuid)'::regprocedure"
    )
    if dueno == _DUENO_SIN_SUPER:
        await conn.execute("alter function public.purge_tenant(uuid) owner to current_user")
    # DROP OWNED revoca también el permiso sobre el parámetro (es un objeto compartido).
    await conn.execute(f"drop owned by {_DUENO_SIN_SUPER}")
    await conn.execute(f"drop role {_DUENO_SIN_SUPER}")


@pytest.fixture
async def purge_con_dueno_sin_superusuario():
    """`purge_tenant` en manos de un dueño NOSUPERUSER BYPASSRLS, como en producción.

    En producción el dueño es `agent_core_migrations` (runbook de producción, 4.3); en el CI y
    en local las migraciones corren como superusuario y el bug de la `0011` no se veía. Con el
    superusuario de `MIGRATIONS_DATABASE_URL` se crea un rol sin superusuario con lo que tiene el
    dueño en producción (BYPASSRLS, borrar en las tablas, `SET` sobre `session_replication_role`)
    y se le pasa la función; al final se devuelve al dueño original. Si la base ya se migró con
    un dueño sin superusuario, se usa tal cual.

    Devuelve una función para quitar y reponer el permiso sobre el parámetro, o `None` si la
    base ya venía migrada así (el dueño real no se toca).
    """
    import asyncpg

    conn = await asyncpg.connect(_url_dueno())
    try:
        if await conn.fetchval("select rolsuper from pg_roles where rolname = current_user"):
            await _limpiar_dueno_de_prueba(conn)
        dueno, dueno_es_super = await conn.fetchrow(
            "select quote_ident(r.rolname), r.rolsuper from pg_proc p "
            "join pg_roles r on r.oid = p.proowner "
            "where p.oid = 'public.purge_tenant(uuid)'::regprocedure"
        )
        if not dueno_es_super:
            yield None
            return
        if not await conn.fetchval("select rolsuper from pg_roles where rolname = current_user"):
            pytest.skip("el rol de MIGRATIONS_DATABASE_URL no es superusuario: no puede crear roles")

        rol = _DUENO_SIN_SUPER
        try:
            await conn.execute(
                f"create role {rol} nologin nosuperuser bypassrls nocreatedb nocreaterole"
            )
            await conn.execute(f"grant usage on schema public, meta_private to {rol}")
            await conn.execute(
                f"grant select, update, delete on all tables in schema public, meta_private to {rol}"
            )
            await conn.execute(f"grant set on parameter session_replication_role to {rol}")
            await conn.execute(f"alter function public.purge_tenant(uuid) owner to {rol}")

            async def permiso(conceder: bool) -> None:
                accion, prep = ("grant", "to") if conceder else ("revoke", "from")
                await conn.execute(
                    f"{accion} set on parameter session_replication_role {prep} {rol}"
                )

            yield permiso
        finally:
            await conn.execute(f"alter function public.purge_tenant(uuid) owner to {dueno}")
            await _limpiar_dueno_de_prueba(conn)
    finally:
        await conn.close()


async def _sembrar_conversacion(tid: uuid.UUID) -> None:
    """Cadena de FKs dentro del tenant: customer ← conversation ← message."""
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"wa-{tid.hex[:6]}")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp")
            session.add(conv)
            await session.flush()
            session.add(
                Message(
                    tenant_id=tid, conversation_id=conv.id, direction="inbound",
                    sender_type="customer", content="hola",
                )
            )
            await session.commit()


async def _todas_las_tablas_con_tenant(conn) -> list[str]:
    """Como `_tenant_tables`, pero en todos los esquemas propios (incluye `meta_private`)."""
    return [
        r[0]
        for r in await conn.fetch(
            "select format('%I.%I', n.nspname, c.relname) from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "join pg_attribute a on a.attrelid = c.oid "
            "where a.attname = 'tenant_id' and not a.attisdropped and c.relkind in ('r', 'p') "
            "and not c.relispartition and n.nspname not in ('pg_catalog', 'information_schema') "
            "and n.nspname not like 'pg\\_%' order by 1"
        )
    ]


async def _conteos(conn, tablas: list[str], tid: uuid.UUID) -> dict[str, int]:
    # El dueño (BYPASSRLS) o el superusuario cuentan sin `tenant_scope`.
    return {
        t: await conn.fetchval(f"select count(*) from {t} where tenant_id = $1", tid)
        for t in tablas
    }


async def test_purge_funciona_con_dueno_sin_superusuario(purge_con_dueno_sin_superusuario) -> None:
    """Con la `0011`, un dueño sin superusuario no podía apagar los triggers de las FKs.

    Además: borra `meta_private` (tokens de Meta), deja intacto a otro negocio, aplica el
    `ON DELETE SET NULL` de `provisioning_requests.negocio_tenant_id` del vendedor, hace rollback
    completo si falla y no filtra `session_replication_role` al llamador.
    """
    import asyncpg
    import sqlalchemy.exc

    from core.db.session import get_session
    from core.tenants import purge_tenant

    tid, slug = await _onboard(_uniq("purge-nosuper"))
    await _seed_catalog(tid)
    await _sembrar_conversacion(tid)
    otro, _ = await _onboard(_uniq("purge-otro"))
    await _seed_catalog(otro)
    await _sembrar_conversacion(otro)

    conn = await asyncpg.connect(_url_dueno())
    try:
        # Un token de Meta del negocio (meta_private) y una solicitud de alta del otro negocio
        # (el vendedor) que apunta al que se purga.
        conexion = uuid.uuid4()
        await conn.execute(
            "insert into public.meta_connections (id, tenant_id, channel, meta_app_id, origin) "
            "values ($1, $2, 'whatsapp', 'app-prueba', 'manual')",
            conexion, tid,
        )
        await conn.execute(
            "insert into meta_private.meta_tokens (id, tenant_id, connection_id, kind, subject_id, "
            "ciphertext, iv, auth_tag, key_id, token_hash) "
            "values ($1, $2, $3, 'system_user', 's', '\\x00', '\\x00', '\\x00', 'k1', 'h')",
            uuid.uuid4(), tid, conexion,
        )
        solicitud = uuid.uuid4()
        await conn.execute(
            "insert into public.provisioning_requests (id, tenant_id, referencia, "
            "negocio_tenant_id) values ($1, $2, $3, $4)",
            solicitud, otro, f"ref-{solicitud.hex[:8]}", tid,
        )

        tablas = await _todas_las_tablas_con_tenant(conn)
        assert "meta_private.meta_tokens" in tablas
        antes = await _conteos(conn, tablas, tid)
        assert antes["meta_private.meta_tokens"] == 1
        otro_antes = await _conteos(conn, tablas, otro)
        assert sum(otro_antes.values()) > 0, "el otro negocio debe tener datos"

        from core.tenants import deactivate_tenant

        await deactivate_tenant(str(tid))
        permiso = purge_con_dueno_sin_superusuario
        if permiso is not None:
            # Sin el permiso sobre el parámetro la purga falla entera: rollback, nada borrado.
            await permiso(False)
            try:
                with pytest.raises(sqlalchemy.exc.DBAPIError, match="session_replication_role"):
                    await purge_tenant(str(tid), f"ELIMINAR {slug}")
            finally:
                await permiso(True)
            assert await _conteos(conn, tablas, tid) == antes
            assert await conn.fetchval("select count(*) from public.tenants where id = $1", tid)

        await purge_tenant(str(tid), f"ELIMINAR {slug}")

        despues = await _conteos(conn, tablas, tid)
        assert {t: n for t, n in despues.items() if n} == {}, "quedaron filas del negocio"
        assert not await conn.fetchval("select count(*) from public.tenants where id = $1", tid)
        assert await _conteos(conn, tablas, otro) == otro_antes, "el otro negocio no se toca"
        assert await conn.fetchval(
            "select negocio_tenant_id from public.provisioning_requests where id = $1", solicitud
        ) is None, "ON DELETE SET NULL de la solicitud del vendedor"
        # La cláusula SET de la función no se filtra a la transacción del llamador. Basta una
        # fila `tenants` suspendida y sin datos.
        vacio = uuid.uuid4()
        await conn.execute(
            "insert into public.tenants (id, name, slug, status) values ($1, 'x', $2, 'suspended')",
            vacio, f"purge-vacio-{vacio.hex[:6]}",
        )
        async with get_session() as session:
            await session.execute(text("select purge_tenant(:tid)"), {"tid": str(vacio)})
            rol = (await session.execute(text("show session_replication_role"))).scalar()
            await session.rollback()
        assert rol == "origin"
        await conn.execute("delete from public.tenants where id = $1", vacio)
    finally:
        await conn.close()


# ------------------------------------------------------------------ panel
async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def test_panel_desactivar_reactivar_y_purge(app_client) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    tid, slug = await _onboard(_uniq("panel-lifecycle"))
    cookie = await _login(app_client)
    headers = {"Cookie": cookie}

    r = await app_client.post(f"/admin/tenants/{tid}/deactivate", headers=headers)
    assert r.status_code == 303
    async with get_session() as session:
        assert (await session.get(Tenant, tid)).status == "suspended"

    r = await app_client.post(f"/admin/tenants/{tid}/activate", headers=headers)
    assert r.status_code == 303
    async with get_session() as session:
        assert (await session.get(Tenant, tid)).status == "active"

    # activo → no se purga aunque la frase sea correcta, y el aviso dice cómo suspenderlo
    r = await app_client.post(
        f"/admin/tenants/{tid}/purge", headers=headers, data={"confirm_phrase": f"ELIMINAR {slug}"}
    )
    assert r.status_code == 303 and "danger" in r.headers["location"]
    assert "deactivate" in unquote(r.headers["location"])
    async with get_session() as session:
        assert await session.get(Tenant, tid) is not None

    r = await app_client.post(f"/admin/tenants/{tid}/deactivate", headers=headers)
    assert r.status_code == 303

    # frase incorrecta → no borra
    r = await app_client.post(
        f"/admin/tenants/{tid}/purge", headers=headers, data={"confirm_phrase": "ELIMINAR OTRACOSA"}
    )
    assert r.status_code == 303 and "danger" in r.headers["location"]
    async with get_session() as session:
        assert await session.get(Tenant, tid) is not None

    # frase correcta → borra
    r = await app_client.post(
        f"/admin/tenants/{tid}/purge",
        headers=headers,
        data={"confirm_phrase": f"ELIMINAR {slug}"},
    )
    assert r.status_code == 303 and "success" in r.headers["location"]
    async with get_session() as session:
        assert await session.get(Tenant, tid) is None

async def test_api_purge_exige_negocio_suspendido(app_client) -> None:
    """`POST /api/v1/admin/platform/tenants/{id}/purge`: 409 si el negocio está activo."""
    from _api_helpers import api_headers

    from core.db.models import Tenant
    from core.db.session import get_session

    tid, slug = await _onboard(_uniq("api-purge"))
    # El negocio activo del panel es otro (un suspendido no se puede abrir).
    async with get_session() as session:
        otro = (
            await session.execute(text("select id from tenants where slug = 'tenant-1'"))
        ).scalar_one()
    headers = await api_headers(app_client, otro)
    url = f"/api/v1/admin/platform/tenants/{tid}/purge"

    r = await app_client.post(url, headers=headers, json={"confirm_phrase": f"ELIMINAR {slug}"})
    assert r.status_code == 409, r.text
    assert "tenant_not_suspended" in r.text and "deactivate" in r.text
    async with get_session() as session:
        assert await session.get(Tenant, tid) is not None

    r = await app_client.post(f"/api/v1/admin/platform/tenants/{tid}/deactivate", headers=headers)
    assert r.status_code == 204, r.text
    r = await app_client.post(url, headers=headers, json={"confirm_phrase": f"ELIMINAR {slug}"})
    assert r.status_code == 204, r.text
    async with get_session() as session:
        assert await session.get(Tenant, tid) is None

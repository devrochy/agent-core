"""HU-AOS-70 · Tablas de leads del core y copia desde commerce (ADR-0193, migración 0146).

Con la BD real: un lead vivo por cliente; la copia conserva ids, historia y seguimientos; entre varios leads de un
mismo cliente queda vivo el más reciente y los demás entran borrados (sin borrado físico); un lead vivo del core gana
sobre el de commerce; dos corridas dan el mismo resultado; `--dry-run` no escribe y avisa de lo que ocultaría; la
config de intake pasa cifrada sin pisar la del core; nada cruza negocios.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import uniq
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AHORA = datetime.now(UTC)


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("lco")
    tid, _ = await onboard_tenant(
        name=f"Leads {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            c = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
            session.add(c)
            await session.flush()
            return c.id


# Columnas de `pack_commerce_leads` que un test puede fijar (packages/pack-commerce/alembic/versions/0001..0019). Los
# modelos ORM de commerce ya no existen (ADR-0193): se siembra con SQL crudo, como lo lee `core.leads.copia`.
_COLUMNAS_DEL_LEAD = frozenset({
    "conversation_id", "phone", "email", "interest", "score", "status", "idempotency_key", "last_activity_at",
    "stage_changed_at", "next_action_at", "next_action_note", "loss_reason", "loss_note", "estimated_value",
})


async def _lead_de_commerce(tid: uuid.UUID, cliente: uuid.UUID | None, **campos) -> uuid.UUID:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    assert set(campos) <= _COLUMNAS_DEL_LEAD, set(campos) - _COLUMNAS_DEL_LEAD
    lead_id = uuid.uuid4()
    valores = {"id": lead_id, "tenant_id": tid, "customer_id": cliente, "name": "Ana", **campos}
    columnas = ", ".join(valores)
    marcas = ", ".join(f":{c}" for c in valores)
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(text(f"insert into pack_commerce_leads ({columnas}) values ({marcas})"), valores)
            await session.execute(
                text(
                    "insert into pack_commerce_lead_stage_changes "
                    "(id, tenant_id, lead_id, customer_id, from_stage, to_stage, actor, idempotency_key) "
                    "values (:id, :t, :l, :c, 'nuevo', 'calificado', 'agente', :k)"
                ),
                {"id": uuid.uuid4(), "t": tid, "l": lead_id, "c": cliente, "k": uniq("idem")},
            )
            await session.execute(
                text(
                    "insert into pack_commerce_lead_followups "
                    "(id, tenant_id, lead_id, customer_id, note, due_at, outcome, actor) "
                    "values (:id, :t, :l, :c, 'llamar', :due, 'hecho', 'persona')"
                ),
                {"id": uuid.uuid4(), "t": tid, "l": lead_id, "c": cliente, "due": AHORA},
            )
    return lead_id


async def _leads_del_core(tid: uuid.UUID) -> dict[uuid.UUID, tuple[uuid.UUID | None, bool]]:
    from core.db.models import Lead
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (await session.execute(select(Lead.id, Lead.customer_id, Lead.deleted_at))).all()
    return {f.id: (f.customer_id, f.deleted_at is None) for f in filas}


async def _contar(tid: uuid.UUID, tabla: str) -> int:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await session.execute(text(f"select count(*) from {tabla}"))).scalar()


async def test_copia_conserva_historia_y_deja_un_lead_vivo_por_cliente():
    from core.leads.copia import copiar_leads

    tid = await _negocio()
    ana = await _cliente(tid)
    viejo = await _lead_de_commerce(tid, ana, status="contactado", last_activity_at=AHORA - timedelta(days=3),
                                    next_action_at=AHORA + timedelta(days=1))
    reciente = await _lead_de_commerce(tid, ana, status="nuevo", last_activity_at=AHORA)
    sin_cliente = await _lead_de_commerce(tid, None, status="nuevo")

    seco = await copiar_leads(dry_run=True, tenant_id=tid)
    assert seco.leads == 3 and seco.cambios == 3 and seco.seguimientos == 3
    assert [o.lead_id for o in seco.ocultos] == [viejo] and seco.ocultos[0].seguimiento_vigente
    assert await _contar(tid, "leads") == 0  # el dry-run no escribe

    reporte = await copiar_leads(dry_run=False, tenant_id=tid, lote=1)  # lote 1: varios lotes por tabla
    assert (reporte.leads, reporte.cambios, reporte.seguimientos) == (3, 3, 3)
    leads = await _leads_del_core(tid)
    assert leads[reciente] == (ana, True)
    assert leads[viejo] == (ana, False)  # oculto, no borrado: su historia sigue apuntando a él
    assert leads[sin_cliente] == (None, True)
    assert await _contar(tid, "lead_stage_changes") == 3 and await _contar(tid, "lead_followups") == 3

    otra = await copiar_leads(dry_run=False, tenant_id=tid)
    assert (otra.leads, otra.cambios, otra.seguimientos, otra.configs) == (0, 0, 0, 0)
    assert await _leads_del_core(tid) == leads
    assert await _contar(tid, "pack_commerce_leads") == 3  # los originales no se tocan

    # La copia es una foto: un cambio posterior en commerce no se copia, pero el reporte lo cuenta.
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text("update pack_commerce_leads set status = 'perdido' where id = :id"), {"id": reciente}
            )
    assert (await copiar_leads(dry_run=True, tenant_id=tid)).desactualizados == 1


async def test_un_lead_vivo_del_core_gana_sobre_el_de_commerce():
    from core.db.models import Lead
    from core.db.session import get_session
    from core.leads.copia import copiar_leads
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana = await _cliente(tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            del_core = Lead(tenant_id=tid, customer_id=ana, origen="whatsapp")
            session.add(del_core)
    de_commerce = await _lead_de_commerce(tid, ana, status="calificado")

    await copiar_leads(dry_run=False, tenant_id=tid)
    leads = await _leads_del_core(tid)
    assert leads[del_core.id] == (ana, True)
    assert leads[de_commerce] == (ana, False)


async def test_un_cliente_no_puede_tener_dos_leads_vivos():
    from core.db.models import Lead
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana = await _cliente(tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(Lead(tenant_id=tid, customer_id=ana))
        with pytest.raises(IntegrityError):
            async with get_session() as session:
                session.add(Lead(tenant_id=tid, customer_id=ana))
        async with get_session() as session:  # uno borrado no cuenta
            session.add(Lead(tenant_id=tid, customer_id=ana, deleted_at=AHORA))
        with pytest.raises(IntegrityError):
            async with get_session() as session:
                session.add(Lead(tenant_id=tid, customer_id=None, origen="facebook"))  # fuera del catálogo


async def test_la_config_de_commerce_pasa_cifrada_sin_pisar_la_del_core():
    from core.db.models import LeadSettings
    from core.db.session import get_session
    from core.leads import config as config_leads
    from core.leads.copia import copiar_leads
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        await plugin_manager.save_config(
            "pack_commerce",
            {"lead_intake_enabled": True, "lead_intake_secret": "s3cr3t", "follow_up_hours": 6,
             "lead_intake_source": "facebook", "auto_create_lead": False},
        )
    await copiar_leads(dry_run=False, tenant_id=tid)
    config = await _config(tid)
    assert config.lead_intake_enabled and config.follow_up_hours == 6
    assert config.lead_intake_secret.get_secret_value() == "s3cr3t"
    async with tenant_scope(tid):
        async with get_session() as session:
            cifrada = (await session.execute(select(LeadSettings.config_encrypted))).scalar_one()
            assert "s3cr3t" not in cifrada
            # Editada en el core, una nueva corrida no la pisa.
            await config_leads.guardar(session, tid, config.model_copy(update={"follow_up_hours": 2}))
    await copiar_leads(dry_run=False, tenant_id=tid)
    assert (await _config(tid)).follow_up_hours == 2


async def test_la_copia_completa_el_intake_de_una_config_por_defecto():
    """Un lead del chat creó la config por defecto (intake apagado) antes de la copia: la copia le pone el intake de
    commerce y no toca lo demás; una config con intake propio no se pisa."""
    from core.db.session import get_session
    from core.leads import config as config_leads
    from core.leads.copia import copiar_leads
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        await plugin_manager.save_config(
            "pack_commerce",
            {"lead_intake_enabled": True, "lead_intake_secret": "s3cr3t", "lead_intake_turnstile": "0xT",
             "lead_intake_allowed_origins": ["https://www.ejemplo.co"], "follow_up_hours": 6},
        )
        async with get_session() as session:
            await config_leads.asegurar_por_defecto(session, tid)
    await copiar_leads(dry_run=False, tenant_id=tid)
    config = await _config(tid)
    assert config.lead_intake_enabled and config.lead_intake_secret.get_secret_value() == "s3cr3t"
    assert config.lead_intake_turnstile.get_secret_value() == "0xT"
    assert config.lead_intake_allowed_origins == ["https://www.ejemplo.co"]
    assert config.follow_up_hours == config_leads.FOLLOW_UP_HORAS_POR_DEFECTO  # lo demás es la del core

    async with tenant_scope(tid):
        async with get_session() as session:
            await config_leads.guardar(
                session, tid, config.model_copy(update={"lead_intake_secret": SecretStr("otro")})
            )
    await copiar_leads(dry_run=False, tenant_id=tid)
    assert (await _config(tid)).lead_intake_secret.get_secret_value() == "otro"


async def _config(tid: uuid.UUID):
    from core.leads import config as config_leads
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await config_leads.cargar_del_negocio(tid)


async def test_aislamiento_entre_negocios():
    from core.leads.copia import copiar_leads

    a, b = await _negocio(), await _negocio()
    lead_a = await _lead_de_commerce(a, await _cliente(a))
    lead_b = await _lead_de_commerce(b, await _cliente(b))

    await copiar_leads(dry_run=False, tenant_id=a)
    assert set(await _leads_del_core(a)) == {lead_a}
    assert await _leads_del_core(b) == {}  # B no se copió y no ve el lead de A
    await copiar_leads(dry_run=False, tenant_id=b)
    assert set(await _leads_del_core(b)) == {lead_b}
    assert await _contar(b, "lead_stage_changes") == 1


async def test_una_config_ilegible_no_corta_la_copia():
    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.leads.copia import copiar_leads
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    lead = await _lead_de_commerce(tid, await _cliente(tid))
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = await session.get(PluginConfig, ("pack_commerce", tid))
            if fila is None:
                session.add(PluginConfig(plugin_id="pack_commerce", tenant_id=tid, config_encrypted="no-es-cifrado"))
            else:
                fila.config_encrypted = "no-es-cifrado"

    reporte = await copiar_leads(dry_run=False, tenant_id=tid)
    assert reporte.configs_ilegibles == [tid] and reporte.configs == 0
    assert set(await _leads_del_core(tid)) == {lead}


async def test_rls_de_las_tablas_de_leads():
    from core.db.models import Lead
    from core.db.session import get_session
    from core.leads import config as config_leads
    from core.leads.copia import copiar_leads
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    lead_a = await _lead_de_commerce(a, await _cliente(a))
    lead_b = await _lead_de_commerce(b, await _cliente(b))
    await copiar_leads(dry_run=False)  # el bucle real, todos los negocios
    assert lead_a in await _leads_del_core(a) and lead_a not in await _leads_del_core(b)
    assert lead_b in await _leads_del_core(b) and lead_b not in await _leads_del_core(a)

    async with tenant_scope(a):
        with pytest.raises(DBAPIError):  # WITH CHECK: no se escribe un lead de B desde A
            async with get_session() as session:
                session.add(Lead(tenant_id=b))
        async with get_session() as session:
            await config_leads.guardar(session, a, config_leads.ConfigCaptarLeads(lead_intake_enabled=True))
        with pytest.raises(PermissionError):  # la config de B no se toca desde A
            async with get_session() as session:
                await config_leads.cargar(session, b)
    async with tenant_scope(b):
        assert await config_leads.cargar_del_negocio(b) is None or not (
            await config_leads.cargar_del_negocio(b)
        ).lead_intake_enabled
    async with get_session() as session:  # sin negocio no se ve nada
        assert (await session.execute(text("select count(*) from leads"))).scalar() == 0

    async with get_session() as session:
        for tabla in ("leads", "lead_stage_changes", "lead_followups", "lead_settings"):
            puede_borrar = (
                await session.execute(text("select has_table_privilege('app_runtime', :t, 'DELETE')"), {"t": tabla})
            ).scalar()
            assert puede_borrar is False, tabla
            politica = (
                await session.execute(
                    text("select qual, with_check from pg_policies where tablename = :t and policyname = "
                         "'tenant_isolation'"),
                    {"t": tabla},
                )
            ).one()
            assert "app.tenant_id" in politica.qual and "app.tenant_id" in politica.with_check

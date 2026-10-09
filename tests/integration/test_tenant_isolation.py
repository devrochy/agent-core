"""Aislamiento multi-tenant genérico (ADR-0001/0018) contra el stack real.

Un solo test por propiedad, usando DOS tenants genéricos (t1 y tenant-moto):
los asserts no dependen del cliente ni de un tenant concreto — crecer en tenants
nunca exige añadir tests.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

MOTO_CATALOG_SIZE = 3  # tests/fixtures/tenants/moto/catalog.json


async def _product_count(tenant_id) -> int:
    from pack_commerce.models import Product

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return (
                await session.execute(select(func.count()).select_from(Product))
            ).scalar()


async def test_catalogo_aislado_por_tenant(moto_setup) -> None:
    """Cada tenant ve SOLO su catálogo (RLS + filtro por tenant, ADR-0018)."""
    t1, moto = moto_setup
    assert await _product_count(moto) == MOTO_CATALOG_SIZE, "el tenant de ejemplo tiene su catálogo"

    # el SKU del tenant de ejemplo no existe en t1
    from pack_commerce.models import ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        async with get_session() as session:
            leak = (
                await session.execute(
                    select(func.count())
                    .select_from(ProductVariant)
                    .where(ProductVariant.sku.like("TST-%"))
                )
            ).scalar()
    assert leak == 0, "los productos de un tenant no filtran hacia otro"


async def test_spec_enablement_y_escalacion_por_tenant(moto_setup) -> None:
    """El enablement de packs y las palabras de escalación son por tenant.

    Las palabras de escalación viven en `AppSettings.escalation_config`
    (lo que el orchestrator REALMENTE lee, `core/orchestrator/consumer.py`)
    -- NO en `AgentSpec.policies.escalation.triggers`, que dejó de ser
    personalizable por tenant (fila 170 ítem 7 del ROADMAP, bug real
    corregido: ese campo del contrato nunca se leía en runtime).

    Se setea `escalation_config` explícitamente acá (no se confía en el seed
    de `onboard_tenant` dentro de `moto_setup`): ese seed solo corre la
    PRIMERA vez que se crea la fila de `AppSettings` -- en una BD local
    reusada entre corridas, `tenant-moto` puede ya existir desde antes de
    que `escalation_config` existiera como concepto, y `onboard_tenant` es
    idempotente a propósito (no pisa un tenant existente)."""
    t1, moto = moto_setup
    from sqlalchemy import select

    from core.agents.spec import enabled_packs, load_active_spec
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(moto):
        async with get_session() as session:
            settings_moto = (
                await session.execute(select(AppSettings).where(AppSettings.tenant_id == moto))
            ).scalar_one()
            original_config = dict(settings_moto.escalation_config or {})
            settings_moto.escalation_config = {"keywords": ["asesor", "compatibilidad"]}
    try:
        async with tenant_scope(moto):
            spec = await load_active_spec()
            async with get_session() as session:
                settings_moto = (
                    await session.execute(select(AppSettings).where(AppSettings.tenant_id == moto))
                ).scalar_one()
        assert "pack_commerce" in enabled_packs(spec), "el tenant de ejemplo tiene el pack habilitado"
        assert "compatibilidad" in settings_moto.escalation_config.get("keywords", [])

        # t1 no debe haberse contagiado la palabra específica del otro tenant
        async with tenant_scope(t1):
            async with get_session() as session:
                settings_t1 = (
                    await session.execute(select(AppSettings).where(AppSettings.tenant_id == t1))
                ).scalar_one_or_none()
        t1_keywords = settings_t1.escalation_config.get("keywords", []) if settings_t1 else []
        assert "compatibilidad" not in t1_keywords
    finally:
        async with tenant_scope(moto):
            async with get_session() as session:
                settings_moto = (
                    await session.execute(select(AppSettings).where(AppSettings.tenant_id == moto))
                ).scalar_one()
                settings_moto.escalation_config = original_config


async def test_meta_setup_checklist_aislado_por_tenant(moto_setup) -> None:
    """Un paso marcado en un tenant no aparece ni se puede pisar desde otro
    (RLS + UNIQUE(tenant_id, step_key), migración 0013)."""
    from core.db.models import MetaSetupChecklist
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    t1, moto = moto_setup

    # BD de integración compartida entre corridas: limpiar antes de insertar
    # para que el UNIQUE(tenant_id, step_key) no choque en una segunda corrida.
    for tid in (t1, moto):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    MetaSetupChecklist.__table__.delete().where(
                        MetaSetupChecklist.tenant_id == tid, MetaSetupChecklist.step_key == "crear_app"
                    )
                )

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(MetaSetupChecklist(tenant_id=t1, step_key="crear_app", done=True))

    async with tenant_scope(moto):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(MetaSetupChecklist).where(MetaSetupChecklist.step_key == "crear_app")
                )
            ).scalar_one_or_none()
            assert row is None, "el paso marcado en t1 no debe verse desde moto"

            # mismo step_key, tenant distinto: no debe chocar con el UNIQUE(tenant_id, step_key)
            session.add(MetaSetupChecklist(tenant_id=moto, step_key="crear_app", done=False))

    async with tenant_scope(t1):
        async with get_session() as session:
            row_t1 = (
                await session.execute(
                    select(MetaSetupChecklist).where(MetaSetupChecklist.step_key == "crear_app")
                )
            ).scalar_one()
            assert row_t1.done is True, "el paso de t1 sigue como se dejó, sin contagiarse del de moto"


async def test_meta_schema_nuevo_aislado_por_tenant(moto_setup) -> None:
    """Las 9 tablas nuevas de la integración Meta (migraciones 0014-0019) no
    cruzan tenants -- un query desde el tenant equivocado trae 0 filas, no una
    excepción (mismo criterio que el resto de la suite)."""
    import uuid as uuid_mod
    from datetime import UTC, datetime

    from core.db.models import (
        FbPage,
        GraphApiCall,
        GraphApiUsage,
        IgAccount,
        MessageTemplate,
        MetaConnection,
        MetaToken,
        PhoneNumber,
        WabaAccount,
    )
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    t1, moto = moto_setup
    suffix = uuid_mod.uuid4().hex[:8]

    async with tenant_scope(t1):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=t1, channel="whatsapp", environment="production", origin="byok", meta_app_id=f"app-{suffix}"
            )
            session.add(connection)
            await session.flush()
            conn_id = connection.id

            waba = WabaAccount(tenant_id=t1, connection_id=conn_id, waba_id=f"waba-{suffix}")
            session.add(waba)
            await session.flush()
            waba_id = waba.id

            session.add(PhoneNumber(tenant_id=t1, connection_id=conn_id, phone_number_id=f"phone-{suffix}"))
            session.add(FbPage(tenant_id=t1, connection_id=conn_id, page_id=f"page-{suffix}"))
            session.add(IgAccount(tenant_id=t1, connection_id=conn_id, ig_user_id=f"ig-{suffix}"))
            session.add(
                MessageTemplate(tenant_id=t1, waba_account_id=waba_id, name=f"tpl-{suffix}", language="es")
            )
            session.add(GraphApiCall(tenant_id=t1, connection_id=conn_id, endpoint="/messages"))
            session.add(
                GraphApiUsage(
                    tenant_id=t1, connection_id=conn_id, usage_date=datetime.now(UTC), call_count=1
                )
            )

        await meta_token_store.put_token(conn_id, "app_access", f"app-{suffix}", "token-de-prueba")

    models = (WabaAccount, PhoneNumber, FbPage, IgAccount, MessageTemplate, GraphApiCall, GraphApiUsage)

    async with tenant_scope(t1):
        async with get_session() as session:
            for model in (MetaConnection, *models):
                count = (
                    await session.execute(select(func.count()).select_from(model).where(model.tenant_id == t1))
                ).scalar()
                assert count and count >= 1, f"{model.__name__} debería tener al menos 1 fila en t1"

    async with tenant_scope(moto):
        async with get_session() as session:
            for model in (MetaConnection, *models):
                leak = (await session.execute(select(func.count()).select_from(model))).scalar()
                assert leak == 0, f"{model.__name__} de t1 no debe verse desde moto"

            leak_tokens = (await session.execute(select(func.count()).select_from(MetaToken))).scalar()
            assert leak_tokens == 0, "meta_tokens de t1 no debe verse desde moto"

        # RLS ya esconde la fila (recién verificado arriba); además, aunque
        # alguien pasara el connection_id correcto pero un tenant_id explícito
        # equivocado a get_token (ej. un bug de contexto), el WHERE filtra
        # `tenant_id` (ADR-0125) y, si no, el AAD no matchea (tercer cinturón,
        # tests/test_crypto_envelope.py::test_aad_ata_el_ciphertext_a_la_fila).
        async with tenant_scope(t1):
            leido = await meta_token_store.get_token(conn_id, "app_access", f"app-{suffix}", tenant_id=moto)
            assert leido is None, "get_token no debería poder leer con un tenant_id distinto al de la fila"

    async with tenant_scope(t1):
        # revoke_token con tenant_id equivocado: filtra por tenant_id en el
        # WHERE (no solo RLS) -> no encuentra la fila, no revoca nada.
        await meta_token_store.revoke_token(conn_id, "app_access", f"app-{suffix}", tenant_id=moto)
        assert await meta_token_store.get_token(conn_id, "app_access", f"app-{suffix}") == "token-de-prueba"

        # con el tenant_id correcto sí revoca.
        await meta_token_store.revoke_token(conn_id, "app_access", f"app-{suffix}", tenant_id=t1)
        assert await meta_token_store.get_token(conn_id, "app_access", f"app-{suffix}") is None


async def test_numeracion_independiente_por_tenant(moto_setup) -> None:
    """ADR-0018: cada tenant arranca su numeración de pedidos en 1 (sin colisión)."""
    t1, moto = moto_setup
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    for tid in (t1, moto):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("delete from pack_commerce_order_counters where tenant_id = :t"),
                    {"t": tid},
                )
    async with tenant_scope(t1):
        async with get_session() as session:
            s1 = (
                await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t1})
            ).scalar()
    async with tenant_scope(moto):
        async with get_session() as session:
            s2 = (
                await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": moto})
            ).scalar()
    assert s1 == 1 and s2 == 1, "ambos tenants arrancan su numeración en 1, de forma independiente"


async def test_onboarding_idempotente(moto_setup) -> None:
    """Correr el onboarding dos veces no duplica filas."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import AgentSpecRecord, AppSettings, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup

    async def _counts() -> tuple[int, int, int]:
        async with get_session() as session:
            tenants = (
                await session.execute(select(func.count()).select_from(Tenant).where(Tenant.slug == "tenant-moto"))
            ).scalar()
        async with tenant_scope(moto):
            async with get_session() as session:
                specs = (
                    await session.execute(
                        select(func.count())
                        .select_from(AgentSpecRecord)
                        .where(AgentSpecRecord.tenant_id == moto)
                    )
                ).scalar()
                settings = (
                    await session.execute(
                        select(func.count())
                        .select_from(AppSettings)
                        .where(AppSettings.tenant_id == moto)
                    )
                ).scalar()
        return tenants, specs, settings

    antes = await _counts()
    await onboard_tenant(
        name="Tenant de ejemplo (moto)",
        slug="tenant-moto",
        business_description="Idempotente",
        packs=["pack_commerce"],
    )
    despues = await _counts()
    assert antes == despues, "el onboarding repetido no duplica filas"


class _ConstantEmbeddings:
    """Embeddings fake: mismo vector siempre (distancia coseno == 0).

    No probamos precisión semántica acá, solo qué filas devuelve `recall()`
    según el filtro de aislamiento -- un embedding constante hace que
    cualquier contenido "matchee" con cualquier query, aislando la variable
    bajo prueba.
    """

    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 383


async def _customer_and_conversation(tenant_id, *, external_id: str):
    """`external_id` se sufija con un UUID corto: la BD de integración es
    compartida entre corridas de pytest y `customers` tiene un índice único
    parcial por (tenant_id, channel, external_id) -- mismo motivo que
    `test_wizard_escalation_triggers_va_a_escalation_config` usa nombres
    únicos por corrida."""
    import uuid as uuid_mod

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    unique_external_id = f"{external_id}-{uuid_mod.uuid4().hex[:8]}"
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            customer = Customer(
                tenant_id=tenant_id, channel="whatsapp", external_id=unique_external_id
            )
            session.add(customer)
            await session.flush()
            conversation = Conversation(tenant_id=tenant_id, customer_id=customer.id)
            session.add(conversation)
            await session.flush()
            return customer.id, conversation.id


async def test_recall_no_cruza_tenants_via_conversation_id(moto_setup, monkeypatch) -> None:
    """`recall()` no debe devolver hechos de otro tenant aunque se le pase un
    `conversation_id` ajeno. Corre con RLS activo (igual que
    `test_pack_commerce_tenant_id_explicito.py`, mismo criterio de la suite):
    no aísla si el bloqueo es RLS o el filtro explícito de `tenant_id` +
    `get_owned()` -- prueba que HOY no hay cruce, no que cada capa por
    separado alcance."""
    monkeypatch.setattr(
        "core.memory.vector.get_embedding_provider", lambda: _ConstantEmbeddings()
    )
    from core.memory.vector import recall, store_memory
    from core.shared.tenant import tenant_scope

    t1, moto = moto_setup
    t1_customer, t1_conversation = await _customer_and_conversation(t1, external_id="wa:recall-t1")
    moto_customer, _moto_conversation = await _customer_and_conversation(
        moto, external_id="wa:recall-moto"
    )

    async with tenant_scope(t1):
        await store_memory(
            "Cliente t1: le interesa la moto XYZ",
            customer_id=t1_customer,
            conversation_id=t1_conversation,
        )

    async with tenant_scope(moto):
        leaked = await recall(
            "moto", customer_id=moto_customer, conversation_id=t1_conversation
        )
    assert leaked == [], "conversation_id de otro tenant no debe traer memoria ajena"


async def test_recall_funciona_entre_conversaciones_del_mismo_cliente(
    moto_setup, monkeypatch
) -> None:
    """La validación por `conversation_id` es un cinturón extra, no un filtro:
    recall() debe seguir trayendo hechos guardados en OTRA conversación del
    mismo cliente (memoria de largo plazo cross-conversación)."""
    monkeypatch.setattr(
        "core.memory.vector.get_embedding_provider", lambda: _ConstantEmbeddings()
    )
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.memory.vector import recall, store_memory
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup
    customer_id, conversation_a = await _customer_and_conversation(
        moto, external_id="wa:recall-cross-convo"
    )

    async with tenant_scope(moto):
        await store_memory(
            "Cliente: su moto favorita es la XYZ",
            customer_id=customer_id,
            conversation_id=conversation_a,
        )
        async with get_session() as session:
            conversation_b = Conversation(tenant_id=moto, customer_id=customer_id)
            session.add(conversation_b)
            await session.flush()
            conversation_b_id = conversation_b.id

        resultados = await recall(
            "moto", customer_id=customer_id, conversation_id=conversation_b_id
        )
    assert resultados, "debe recuperar memoria de otra conversación del mismo cliente"


async def test_recall_conversation_id_inexistente_no_rompe(moto_setup, monkeypatch) -> None:
    """`conversation_id` que no existe (o de otro tenant) -> [] sin excepción."""
    import uuid as uuid_mod

    monkeypatch.setattr(
        "core.memory.vector.get_embedding_provider", lambda: _ConstantEmbeddings()
    )
    from core.memory.vector import recall
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup
    async with tenant_scope(moto):
        resultados = await recall("moto", conversation_id=uuid_mod.uuid4())
    assert resultados == []


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def test_panel_lista_tenants_y_crea_uno_nuevo(app_client, moto_setup) -> None:
    """El panel (solo master) lista tenants y el wizard crea uno nuevo."""
    _t1, _moto = moto_setup
    cookie = await _login(app_client)
    headers = {"Cookie": cookie}

    r = await app_client.get("/admin/tenants", headers=headers)
    assert r.status_code == 200
    assert "tenant-moto" in r.text, "la lista muestra el tenant de ejemplo"

    r_form = await app_client.get("/admin/tenants/nuevo", headers=headers)
    assert r_form.status_code == 200
    assert "Nuevo tenant" in r_form.text

    r = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={
            "name": "Panadería del Barrio",
            "business": "Panadería de ejemplo",
            "language": "es",
            "tone": "cercano",
            "escalation_triggers": "asesor, queja",
        },
    )
    assert r.status_code == 303
    assert "ac_tenant" in r.headers.get("set-cookie", ""), "el tenant nuevo se activa en la cookie"
    assert "panaderia-del-barrio" in r.headers["location"], "redirige al spec con flash"

    # slug duplicado: idempotente, no rompe
    r2 = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={"name": "Panadería del Barrio", "slug": "panaderia-del-barrio"},
    )
    assert r2.status_code == 303


async def test_alta_con_whatsapp_usa_flujo_byok(app_client, monkeypatch) -> None:
    """El wizard de alta conecta WhatsApp con el flujo BYOK
    (`save_byok_connection`: MetaConnection + suscripción), no con el
    `ChannelConnection` legacy."""
    import uuid

    cookie = await _login(app_client)
    headers = {"Cookie": cookie}
    calls: list[tuple] = []

    async def _fake_save(
        tid, admin_id, app_id, app_secret, phone, token, waba, *, environment="sandbox", verify_token=None
    ):
        calls.append((tid, app_id, waba, environment, verify_token))

    monkeypatch.setattr("core.gateway.meta_byok.save_byok_connection", _fake_save)

    r = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={
            "name": f"Byok Alta {uuid.uuid4().hex[:6]}",
            "language": "es",
            "tone": "cercano",
            "connect_whatsapp": "true",
            "wa_app_id": "app-x",
            "wa_app_secret": "secret-x",
            "wa_waba_id": "waba-x",
            "wa_phone_number_id": "phone-x",
            "wa_access_token": "token-x",
            "wa_verify_token": "vt-x",
        },
    )
    assert r.status_code == 303
    assert "/admin/configuracion" in r.headers["location"]
    assert calls and calls[0][1] == "app-x" and calls[0][2] == "waba-x" and calls[0][3] == "sandbox"


async def test_alta_con_whatsapp_fallido_deja_tenant_y_avisa(app_client, monkeypatch) -> None:
    """Si el WhatsApp falla, el tenant YA creado se conserva y se avisa
    (redirige a Conexiones), en vez de abortar el alta."""
    import uuid

    from core.gateway.meta_byok import ByokError

    cookie = await _login(app_client)
    headers = {"Cookie": cookie}

    async def _fail(*a, **k):
        raise ByokError("El access token está vencido o es inválido")

    monkeypatch.setattr("core.gateway.meta_byok.save_byok_connection", _fail)

    slug = f"byokfail-{uuid.uuid4().hex[:6]}"
    r = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={
            "name": "Byok Fail",
            "slug": slug,
            "language": "es",
            "tone": "cercano",
            "connect_whatsapp": "true",
            "wa_app_id": "a",
            "wa_app_secret": "s",
            "wa_waba_id": "w",
            "wa_phone_number_id": "p",
            "wa_access_token": "t",
        },
    )
    assert r.status_code == 303
    assert "/admin/conexiones" in r.headers["location"]
    assert "creado" in r.headers["location"]

    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        row = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    assert row is not None, "el tenant debe quedar creado aunque WhatsApp falle"


async def test_wizard_escalation_triggers_va_a_escalation_config(app_client) -> None:
    """`escalation_triggers` del wizard debe terminar en
    `AppSettings.escalation_config` (lo que el orchestrator REALMENTE lee,
    `core/orchestrator/consumer.py`) -- no en el campo muerto del AgentSpec
    (bug real corregido, fila 170 ítem 7 del ROADMAP).

    Tenant con slug único por corrida (no reusa `panaderia-del-barrio`,
    que es un fixture idempotente de otro test y en una BD compartida entre
    corridas de pytest puede haberse creado ANTES de este fix, sin
    `escalation_config`)."""
    import uuid

    from core.db.models import AppSettings, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    cookie = await _login(app_client)
    headers = {"Cookie": cookie}
    unique_name = f"Panadería Escalation {uuid.uuid4().hex[:8]}"

    r = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={
            "name": unique_name,
            "business": "Panadería de ejemplo",
            "language": "es",
            "tone": "cercano",
            "escalation_triggers": "asesor, queja",
        },
    )
    assert r.status_code == 303

    async with get_session() as session:
        nuevo = (
            await session.execute(select(Tenant).where(Tenant.name == unique_name))
        ).scalar_one()
    async with tenant_scope(nuevo.id):
        async with get_session() as session:
            settings_row = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == nuevo.id)
                )
            ).scalar_one()
    assert settings_row.escalation_config == {"keywords": ["asesor", "queja"]}


async def test_wizard_sin_escalation_triggers_deja_escalation_config_vacio(app_client) -> None:
    """Sin triggers en el form (caso del wizard "rápido", solo nombre/slug --
    ver el segundo POST de `test_panel_lista_tenants_y_crea_uno_nuevo`), el
    contrato es `escalation_config == {}` (implícito, no antes cubierto por
    ningún test) -- en runtime cae a `DEFAULT_ESCALATION_KEYWORDS`
    (`matches_escalation_keywords`, `core/orchestrator/escalation.py`)."""
    import uuid

    from core.db.models import AppSettings, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    cookie = await _login(app_client)
    headers = {"Cookie": cookie}
    unique_name = f"Panadería Sin Triggers {uuid.uuid4().hex[:8]}"

    r = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={"name": unique_name, "language": "es", "tone": "cercano"},
    )
    assert r.status_code == 303

    async with get_session() as session:
        nuevo = (
            await session.execute(select(Tenant).where(Tenant.name == unique_name))
        ).scalar_one()
    async with tenant_scope(nuevo.id):
        async with get_session() as session:
            settings_row = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == nuevo.id)
                )
            ).scalar_one()
    assert settings_row.escalation_config == {}
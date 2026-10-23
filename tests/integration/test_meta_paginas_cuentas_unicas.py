"""Una Página de Facebook o una cuenta de Instagram pertenecen a un solo negocio (ADR-0152), con BD real.

Antes de la migración 0104 `resolve_tenant_for_page` e `…_for_ig_user` devolvían el primer negocio que
encontraran (`LIMIT 1` sin orden) y contaban también las conexiones borradas: dos negocios con la misma
Página se disputaban los mensajes. Estos tests corren las funciones SQL como `app_runtime` (sin bypass
de RLS), igual que el runtime.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, uniq
from sqlalchemy import update

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ------------------------------------------------------------------ helpers
async def _new_tenant(prefix: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de Páginas y cuentas de Meta.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL, whatsapp=None,
    )
    return tid


async def _seed(
    tid: uuid.UUID, *, page_id: str | None = None, ig_user_id: str | None = None, status: str = "connected"
) -> uuid.UUID:
    """Conexión de Meta viva con su Página o su cuenta, escrita directo (sin pasar por las altas)."""
    from core.db.models import FbPage, IgAccount, MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="messenger" if page_id else "instagram", origin="manual",
                meta_app_id=uniq("app"), status=status,
            )
            session.add(connection)
            await session.flush()
            if page_id:
                session.add(FbPage(tenant_id=tid, connection_id=connection.id, page_id=page_id))
            if ig_user_id:
                session.add(IgAccount(tenant_id=tid, connection_id=connection.id, ig_user_id=ig_user_id))
            return connection.id


async def _soft_delete(tid: uuid.UUID, connection_id: uuid.UUID) -> None:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.id == connection_id).values(deleted_at=utcnow())
            )


async def _resolve(fn: str, value: str) -> uuid.UUID | None:
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(text(f"select {fn}(:v)"), {"v": value})).scalar_one_or_none()


def _evento(object_type: str, entry_id: str):
    from core.gateway.webhook_flatten import FlattenedEvent

    return FlattenedEvent(
        object_type=object_type, field="messages", external_id=uniq("m"), entry_id=entry_id, sub_id=None, value={}
    )


# ------------------------------------------------------------------ resolvedores
async def test_una_pagina_con_un_dueno_rutea_a_su_negocio() -> None:
    page_id, ig_id = uniq("page"), uniq("ig")
    tenant = await _new_tenant("msg-uno")
    await _seed(tenant, page_id=page_id)
    await _seed(tenant, ig_user_id=ig_id)

    assert await _resolve("resolve_tenant_for_page", page_id) == tenant
    assert await _resolve("resolve_tenant_for_ig_user", ig_id) == tenant


async def test_un_activo_de_nadie_no_rutea() -> None:
    assert await _resolve("resolve_tenant_for_page", uniq("page-sin-dueno")) is None
    assert await _resolve("resolve_tenant_for_ig_user", uniq("ig-sin-dueno")) is None


async def test_una_pagina_con_dos_duenos_no_rutea_a_ninguno() -> None:
    """Antes iba al primero que devolviera la BD: el que registrara la Página de otro recibía sus mensajes."""
    page_id, ig_id = uniq("page-doble"), uniq("ig-doble")
    a, b = await _new_tenant("msg-a"), await _new_tenant("msg-b")
    await _seed(a, page_id=page_id)
    await _seed(b, page_id=page_id)
    await _seed(a, ig_user_id=ig_id)
    await _seed(b, ig_user_id=ig_id)

    assert await _resolve("resolve_tenant_for_page", page_id) is None
    assert await _resolve("resolve_tenant_for_ig_user", ig_id) is None


async def test_la_conexion_borrada_deja_de_contar() -> None:
    """Al borrar la conexión que sobra, el activo vuelve a rutear al dueño que queda."""
    page_id = uniq("page-borrada")
    a, b = await _new_tenant("borra-a"), await _new_tenant("borra-b")
    await _seed(a, page_id=page_id)
    conexion_de_b = await _seed(b, page_id=page_id)
    assert await _resolve("resolve_tenant_for_page", page_id) is None

    await _soft_delete(b, conexion_de_b)

    assert await _resolve("resolve_tenant_for_page", page_id) == a


async def test_una_conexion_en_draft_no_cuenta() -> None:
    page_id = uniq("page-draft")
    a, b = await _new_tenant("draft-a"), await _new_tenant("draft-b")
    await _seed(a, page_id=page_id)
    await _seed(b, page_id=page_id, status="draft")

    assert await _resolve("resolve_tenant_for_page", page_id) == a


# ------------------------------------------------------------------ ruteo y reclamo
async def test_un_evento_de_un_activo_con_dos_duenos_es_un_conflicto() -> None:
    from core.gateway.webhook_route_tenant import route_event

    page_id, ig_id = uniq("page-conflicto"), uniq("ig-conflicto")
    a, b = await _new_tenant("conf-a"), await _new_tenant("conf-b")
    for tenant in (a, b):
        await _seed(tenant, page_id=page_id)
        await _seed(tenant, ig_user_id=ig_id)

    for object_type, activo in (("page", page_id), ("instagram", ig_id)):
        route = await route_event(_evento(object_type, activo))
        assert (route.tenant_id, route.conflict) == (None, True), object_type


async def test_un_evento_de_un_activo_sin_dueno_no_es_un_conflicto() -> None:
    from core.gateway.webhook_route_tenant import route_event

    route = await route_event(_evento("page", uniq("page-libre")))

    assert (route.tenant_id, route.conflict) == (None, False)


async def test_el_reclamo_distingue_al_dueno_de_otro_negocio() -> None:
    from core.gateway.meta_asset_claims import messaging_asset_claimed_by_other_tenant

    page_id, ig_id = uniq("page-reclamo"), uniq("ig-reclamo")
    duena, otra = await _new_tenant("rec-duena"), await _new_tenant("rec-otra")
    await _seed(duena, page_id=page_id)
    await _seed(duena, ig_user_id=ig_id)

    assert not await messaging_asset_claimed_by_other_tenant(duena, page_id=page_id, ig_user_id=ig_id)
    assert await messaging_asset_claimed_by_other_tenant(otra, page_id=page_id)
    assert await messaging_asset_claimed_by_other_tenant(otra, ig_user_id=ig_id)
    assert await messaging_asset_claimed_by_other_tenant(None, page_id=page_id)  # tenant sin crear: cualquier dueño cuenta
    assert not await messaging_asset_claimed_by_other_tenant(otra, page_id=uniq("otra-page"))


async def test_claim_messaging_assets_rechaza_el_activo_de_otro_negocio() -> None:
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import (
        CLAIMED_MESSAGING_MESSAGE,
        MetaAssetError,
        claim_messaging_assets,
    )

    page_id = uniq("page-claim")
    duena, otra = await _new_tenant("claim-duena"), await _new_tenant("claim-otra")
    await _seed(duena, page_id=page_id)

    async with get_session() as session:
        await claim_messaging_assets(session, duena, page_id=page_id)  # la dueña puede volver a guardarla
        with pytest.raises(MetaAssetError) as exc:
            await claim_messaging_assets(session, otra, page_id=page_id)

    assert exc.value.code == "asset_claimed"
    assert exc.value.message == CLAIMED_MESSAGING_MESSAGE

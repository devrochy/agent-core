"""Siembra un escenario de DEMO para el login unificado y las invitaciones (ADR-0081).

Crea (idempotente):
- Dos negocios con `pack_cobranza`: `demo-cobranza` y `demo-cobranza-norte`.
- Un **gestor compartido** (`gestor@demo.test`) con perfil `Gestor` en AMBOS →
  su login pide elegir negocio.
- Un **invitado** (`invitado@demo.test`) con una invitación pendiente al negocio
  norte → su login devuelve el token de membresía y la invitación.

Uso: python scripts/seed_demo_login.py
Credenciales: todas usan la contraseña `demo12345`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PW = "demo12345"
A = ("demo-cobranza", "Cobranza Demo")
B = ("demo-cobranza-norte", "Cobranza Demo Norte")


async def _asegurar_admin(email: str, role: str = "colaborador") -> None:
    from sqlalchemy import select

    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        existente = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()
        if existente is None:
            session.add(Admin(email=email, password_hash=hash_password(PW), role=role))


async def _gestor_en(tenant_id, admin_email: str) -> None:
    from pack_cobranza.models import Gestor
    from sqlalchemy import select

    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == admin_email))
        ).scalar_one()
        admin_id = admin.id

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            link = (
                await session.execute(
                    select(TenantUser).where(
                        TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role="colaborador"))
            gestor = (
                await session.execute(
                    select(Gestor).where(Gestor.tenant_id == tenant_id, Gestor.admin_id == admin_id)
                )
            ).scalar_one_or_none()
            if gestor is None:
                session.add(Gestor(tenant_id=tenant_id, admin_id=admin_id, nombre="Gestor Demo"))


async def _habilitar_pack(tenant_id, pack: str) -> None:
    """El negocio tiene la habilidad si un agente activo la tiene instalada (HU-AOS-91): crea el agente de la
    plantilla del pack si todavía no existe."""
    from core.agents.editor import ErrorDelEditor, crear_desde_plantilla
    from core.agents.instalaciones_agente import packs_del_negocio
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plantilla = {"pack_cobranza": "cobranza"}[pack]
    async with tenant_scope(tenant_id):
        if pack in await packs_del_negocio(tenant_id):
            return
        try:
            await crear_desde_plantilla(
                tenant_id,
                plantilla,
                nombre=None,
                catalogo=catalogo_de_acciones(plugin_manager.list_modules()),
                actor=None,
                puede_habilitar=True,
            )
        except ErrorDelEditor as exc:
            if exc.code != "key_taken":
                raise


async def _tenant(nombre: str, slug: str, owner_email: str):
    from core.alta_negocio import onboard_tenant

    tenant_id, _ = await onboard_tenant(
        name=nombre,
        slug=slug,
        business_description="Negocio de demostración del login unificado.",
        persona={"tone": "cercano"},
        admin_email=owner_email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    await _habilitar_pack(tenant_id, "pack_cobranza")
    return tenant_id


async def main() -> None:
    ta = await _tenant(A[1], A[0], "dueno-a@demo.test")
    tb = await _tenant(B[1], B[0], "dueno-b@demo.test")

    await _asegurar_admin("gestor@demo.test")
    await _gestor_en(ta, "gestor@demo.test")
    await _gestor_en(tb, "gestor@demo.test")

    await _asegurar_admin("invitado@demo.test")
    await _invitar(tb, "invitado@demo.test")

    await _asegurar_admin("master@demo.test", role="master")

    print(f"✓ Demo lista. Contraseña para todos: {PW}")
    print(f"  Negocios: {A[0]}, {B[0]}")
    print("  Owners:   dueno-a@demo.test · dueno-b@demo.test")
    print("  Gestor compartido (selector de negocio): gestor@demo.test")
    print("  Invitado (aceptar invitación):           invitado@demo.test")
    print("  Master (ve todos los negocios):          master@demo.test")


async def _invitar(tenant_id, email: str) -> None:
    from sqlalchemy import select

    from core.admin.invitations_service import crear_invitacion
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one()
        admin_id = admin.id
    await crear_invitacion(
        tenant_id=tenant_id,
        admin_id=admin_id,
        invited_by=None,
        role="colaborador",
        pack_roles=["pack_cobranza:gestor"],
    )


if __name__ == "__main__":
    asyncio.run(main())

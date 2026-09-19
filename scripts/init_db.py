"""Inicializa la BD: migraciones + admin por defecto + fila de configuración.

Uso: python scripts/init_db.py  (o `agent-core db seed`)
Idempotente: no duplica nada si ya existe.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_EMAIL = "admin@local"


async def seed() -> None:
    from sqlalchemy import select

    from core.admin.auth import hash_password
    from core.db.models import Admin, AppSettings, Tenant, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    # ADMIN_PASSWORD: solo para CI (login determinístico en tests de integración).
    # Sin setear, se genera una aleatoria (comportamiento seguro por defecto).
    password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(10)
    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == DEFAULT_EMAIL))
        ).scalar_one_or_none()
        created = False
        if admin is None:
            admin = Admin(email=DEFAULT_EMAIL, password_hash=hash_password(password), role="master")
            session.add(admin)
            await session.flush()
            created = True

        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "tenant-1"))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name="Tenant #1", slug="tenant-1")
            session.add(tenant)
            await session.flush()

        binding = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.tenant_id == tenant.id,
                    TenantUser.admin_id == admin.id,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            session.add(TenantUser(tenant_id=tenant.id, admin_id=admin.id, role="owner"))

    async with tenant_scope(tenant.id):
        async with get_session() as session:
            settings_row = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == tenant.id)
                )
            ).scalar_one_or_none()
            if settings_row is None:
                session.add(AppSettings(tenant_id=tenant.id, assistant_name="Mi Asistente"))

    if created:
        print(f"✓ Admin creado -> email: {DEFAULT_EMAIL} | contraseña: {password}")
        print("  (cámbiala después del primer login)")
    else:
        print(f"✓ Admin ya existía ({DEFAULT_EMAIL}) — sin cambios")
    print("✓ Configuración base lista")


if __name__ == "__main__":
    print("Aplicando migraciones...")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    asyncio.run(seed())

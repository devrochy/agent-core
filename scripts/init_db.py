"""Inicializa la BD: migraciones + admin por defecto + fila de configuración.

Uso: python scripts/init_db.py  (o `agent-core db seed`)
Idempotente: no duplica nada si ya existe.
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_EMAIL = "admin@local"


async def seed() -> None:
    from sqlalchemy import select

    from core.admin.auth import hash_password
    from core.db.models import Admin, AppSettings
    from core.db.session import get_session

    password = secrets.token_urlsafe(10)
    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == DEFAULT_EMAIL))
        ).scalar_one_or_none()
        created = False
        if admin is None:
            session.add(Admin(email=DEFAULT_EMAIL, password_hash=hash_password(password), role="master"))
            created = True

        settings_row = (await session.execute(select(AppSettings).limit(1))).scalar_one_or_none()
        if settings_row is None:
            session.add(AppSettings(assistant_name="Mi Asistente"))

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

"""Segunda base de los tests del gate de pass^k (HU-AOS-21 parte 3b): `agentcore_eval_test`.

Misma instancia Postgres que la base del test, base aparte y migrada con el mismo `agent-core db migrate`
(en un subproceso con SUS URLs). Se crea una vez por corrida; si ya existe y está migrada, se reutiliza.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

NOMBRE = "agentcore_eval_test"
RAIZ = Path(__file__).resolve().parents[2]


def urls() -> tuple[str, str]:
    """(runtime, dueño) de la base de eval de prueba, derivadas de las del test."""
    from core.config import get_settings

    s = get_settings()
    runtime = make_url(s.database_url).set(database=NOMBRE).render_as_string(hide_password=False)
    dueno = make_url(s.migrations_database_url or s.database_url).set(database=NOMBRE).render_as_string(hide_password=False)
    return runtime, dueno


async def asegurar() -> tuple[str, str]:
    """Crea y migra la base si hace falta. Devuelve sus URLs `(runtime, dueño)`.

    Bajo pytest-xdist varios workers llaman a esto a la vez sobre una base que aún no existe (en CI el
    Postgres de cada job es nuevo). Sin exclusión, todos creaban y migraban la misma base y los que perdían
    la carrera fallaban en el `db migrate` (visto 4 veces el 2026-11-02, siempre en los tests del gate).
    Un candado de aviso de Postgres (`pg_advisory_lock`) serializa crear + comprobar + migrar: el primero
    migra y los demás, al entrar, ven la base ya migrada. Se toma en la base `postgres`, común a todos; si
    el proceso muere, Postgres lo suelta al cerrarse la conexión.
    """
    runtime, dueno = urls()
    admin = create_async_engine(
        make_url(dueno).set(database="postgres").render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text("select pg_advisory_lock(hashtext(:n))"), {"n": NOMBRE})
            try:
                existe = (await conn.execute(text("select 1 from pg_database where datname = :n"), {"n": NOMBRE})).first()
                if existe is None:
                    await conn.execute(text(f'create database "{NOMBRE}"'))
                await _migrar_si_falta(runtime, dueno)
            finally:
                await conn.execute(text("select pg_advisory_unlock(hashtext(:n))"), {"n": NOMBRE})
    finally:
        await admin.dispose()
    return runtime, dueno


async def _migrar_si_falta(runtime: str, dueno: str) -> None:
    motor = create_async_engine(dueno)
    try:
        async with motor.connect() as conn:
            migrada = (await conn.execute(text("select to_regclass('public.alembic_version')"))).scalar_one()
    finally:
        await motor.dispose()
    if migrada is None:
        entorno = {**os.environ, "DATABASE_URL": runtime, "MIGRATIONS_DATABASE_URL": dueno}
        resultado = subprocess.run(
            [sys.executable, "-m", "cli.main", "db", "migrate"], env=entorno, cwd=RAIZ, capture_output=True, text=True, timeout=900
        )
        assert resultado.returncode == 0, resultado.stdout[-500:] + resultado.stderr[-500:]

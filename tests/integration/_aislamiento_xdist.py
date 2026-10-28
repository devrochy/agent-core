"""Aislamiento por worker para correr la integración con pytest-xdist (ADR-0120, adenda 2026-10-28).

Los tests de integración usan tenants de prueba con UUID fijo, contactos fijos y limpian el
Redis global antes de cada test, así que dos procesos sobre la misma base y el mismo Redis se
pisarían. Con `-n N` cada worker `gwK` recibe:

- su PROPIA base, `<base>_gwK`, clonada de la base plantilla ya migrada y sembrada
  (`CREATE DATABASE ... TEMPLATE`): arranca idéntica a la de un job serial, en segundos;
- su PROPIA base de Redis (`/K+1`; la `/0` queda para la corrida serial);
- su propio `TENANTS_ROOT` temporal.

Sin xdist (corrida serial, local o en el CI de hoy) no hace nada. Tampoco corre si
`AGENT_CORE_INTEGRATION` no es `1`, porque en ese caso los tests se saltan y no hay base.

Las URLs se cambian en `os.environ` ANTES de que `core.config` se importe, así que el pool, el
engine y las tareas de Celery que lean `get_settings()` ven ya la base del worker.
"""

from __future__ import annotations

import atexit
import os
import re
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

# Redis ofrece 16 bases (0-15). La 0 es de la corrida serial; la 15, de `EVAL_REDIS_DB`.
_MAX_WORKERS = 14
# Un solo lock de aviso para serializar el clonado entre workers (clave arbitraria, fija).
_LOCK_CLONADO = 7_120_001


def _numero_de_worker() -> int | None:
    """`gw3` -> 3; None si no estamos bajo xdist."""
    m = re.fullmatch(r"gw(\d+)", os.environ.get("PYTEST_XDIST_WORKER", ""))
    return int(m.group(1)) if m else None


def _con_base(url: str, base: str) -> str:
    partes = urlsplit(url)
    return urlunsplit(partes._replace(path="/" + quote(base)))


def _dsn_psycopg(url: str, base: str) -> str:
    """URL de SQLAlchemy (`postgresql+asyncpg://...`) -> DSN de psycopg2 hacia `base`."""
    partes = urlsplit(url)
    credenciales = ""
    if partes.username:
        credenciales = quote(unquote(partes.username), safe="")
        if partes.password:
            credenciales += ":" + quote(unquote(partes.password), safe="")
        credenciales += "@"
    return f"postgresql://{credenciales}{partes.hostname}:{partes.port or 5432}/{quote(base)}"


def _clonar_base(url_migraciones: str, plantilla: str, destino: str) -> None:
    """Crea `destino` como copia de `plantilla`. Exige el rol de migraciones (superusuario en el CI)."""
    import psycopg2

    # Se conecta a `postgres`, no a la plantilla: Postgres no clona una base con conexiones abiertas.
    admin = psycopg2.connect(_dsn_psycopg(url_migraciones, "postgres"))
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            # Serializa el clonado: dos CREATE DATABASE a la vez sobre la misma plantilla se estorban.
            cur.execute("select pg_advisory_lock(%s)", (_LOCK_CLONADO,))
            try:
                cur.execute(f'drop database if exists "{destino}" with (force)')
                limite = time.monotonic() + 60
                while True:
                    try:
                        cur.execute(f'create database "{destino}" template "{plantilla}"')
                        break
                    except psycopg2.errors.ObjectInUse:
                        if time.monotonic() > limite:
                            raise
                        time.sleep(1)  # algo aún conectado a la plantilla (p. ej. el seed recién terminado)
            finally:
                cur.execute("select pg_advisory_unlock(%s)", (_LOCK_CLONADO,))
    finally:
        admin.close()


def _borrar_base(url_migraciones: str, destino: str) -> None:
    try:
        import psycopg2

        admin = psycopg2.connect(_dsn_psycopg(url_migraciones, "postgres"))
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'drop database if exists "{destino}" with (force)')
        admin.close()
    except Exception:
        pass


def aislar_worker() -> None:
    """Apunta este proceso a la base, el Redis y el TENANTS_ROOT de su worker. No-op sin xdist."""
    worker = _numero_de_worker()
    if worker is None or os.environ.get("AGENT_CORE_INTEGRATION") != "1":
        return
    if worker >= _MAX_WORKERS:
        raise RuntimeError(f"Hay {_MAX_WORKERS} bases de Redis para workers; se pidió el worker gw{worker}.")

    url_migraciones = os.environ.get("MIGRATIONS_DATABASE_URL", "")
    url_runtime = os.environ.get("DATABASE_URL", "")
    if not url_migraciones or not url_runtime:
        raise RuntimeError("xdist en integración exige DATABASE_URL y MIGRATIONS_DATABASE_URL.")

    plantilla = unquote(urlsplit(url_migraciones).path.lstrip("/"))
    destino = f"{plantilla}_gw{worker}"
    _clonar_base(url_migraciones, plantilla, destino)
    atexit.register(_borrar_base, url_migraciones, destino)

    os.environ["MIGRATIONS_DATABASE_URL"] = _con_base(url_migraciones, destino)
    os.environ["DATABASE_URL"] = _con_base(url_runtime, destino)
    os.environ["REDIS_URL"] = _con_base(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), str(worker + 1))
    os.environ["TENANTS_ROOT"] = tempfile.mkdtemp(prefix=f"tenants-gw{worker}-")

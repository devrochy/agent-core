"""Verifica que una base de agent-core aísla a los negocios (HU-ARCH-01).

Corre con la conexión del RUNTIME, `DATABASE_URL` (la misma que usan la API, el worker y el
beat), y comprueba:

1. `rls_forced`: el check del checklist de producción
   (`core/gateway/meta_production_readiness.py::check_rls_forced`), reutilizado tal cual:
   ninguna tabla de `public` con RLS habilitada y sin FORCE, y `meta_private` sin USAGE
   para PUBLIC.
2. `rls_otros_esquemas`: la misma regla fuera de `public` (p. ej. `meta_private`), que
   `rls_forced` no mira.
3. `rol_runtime`: el rol de la conexión no es superusuario, no tiene BYPASSRLS y no es dueño
   (ni miembro del rol dueño) de la base, de un esquema ni de una tabla. Un superusuario o
   un rol con BYPASSRLS se salta RLS siempre; el dueño de una tabla se la salta si no tiene
   FORCE, y además puede quitárselo.
4. `rol_esperado`: el rol es `app_runtime` (o el que se pase con `--rol-esperado`).
5. `contrasena_runtime`: la contraseña de la URL no es la de desarrollo con la que la
   migración 0003 crea `app_runtime` (está en el repo). Sin contraseña en la URL queda en `?`.

`DATABASE_URL` se lee SOLO del entorno: si falta, el script termina con código 2 y no cae al
`.env` ni al valor por defecto de `core/config.py`. Dentro del contenedor de la API ya está:

    docker exec <contenedor-api> python scripts/verificar_aislamiento.py

Solo lee (consultas al catálogo de Postgres).

Código de salida: 0 si ningún check falla (un `?` es una advertencia, no una falla), 1 si
alguno falla y 2 si falta la variable o no se puede consultar la base.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if TYPE_CHECKING:
    from core.gateway.meta_preflight import PreflightCheck

ENV_URL = "DATABASE_URL"
ROL_ESPERADO = "app_runtime"
# Contraseña con la que `alembic/versions/0003_multitenant_core.py` crea `app_runtime` si el
# rol no existe. Es pública (está en el repo): en producción no puede ser esta.
CONTRASENA_DE_DESARROLLO = "app_runtime"
MAX_LISTADO = 10

SALIDA_OK = 0
SALIDA_FALLA = 1
SALIDA_ERROR = 2

_SQL_ROL = """
select r.rolname, r.rolsuper, r.rolbypassrls
from pg_roles r
where r.rolname = current_user
"""

# Lo que el rol posee directamente o por pertenecer al rol dueño (`MEMBER` cubre también
# la pertenencia sin INHERIT: con SET ROLE el runtime pasaría a ser el dueño).
_SQL_PROPIEDADES = """
select 'base ' || d.datname as objeto
from pg_database d
where d.datname = current_database() and pg_has_role(current_user, d.datdba, 'MEMBER')
union all
select 'esquema ' || n.nspname
from pg_namespace n
where n.nspname <> 'information_schema' and n.nspname !~ '^pg_'
  and pg_has_role(current_user, n.nspowner, 'MEMBER')
union all
select 'tabla ' || n.nspname || '.' || c.relname
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p') and n.nspname <> 'information_schema' and n.nspname !~ '^pg_'
  and pg_has_role(current_user, c.relowner, 'MEMBER')
order by 1
"""

_SQL_RLS_OTROS_ESQUEMAS = """
select n.nspname || '.' || c.relname
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p')
  and n.nspname not in ('public', 'information_schema') and n.nspname !~ '^pg_'
  and c.relrowsecurity and not c.relforcerowsecurity
order by 1
"""


def _check(key: str, ok: bool | None, detail: str) -> PreflightCheck:
    from core.gateway.meta_preflight import PreflightCheck

    return PreflightCheck(key=key, ok=ok, detail=detail, checked_at=datetime.now(UTC))


def _listar(nombres: list[str]) -> str:
    if len(nombres) <= MAX_LISTADO:
        return ", ".join(nombres)
    return ", ".join(nombres[:MAX_LISTADO]) + f" y {len(nombres) - MAX_LISTADO} más"


def describir_url(url: str) -> str:
    """`base en host:puerto como usuario`, sin la contraseña."""
    partes = urlsplit(url)
    try:
        puerto = partes.port or 5432
    except ValueError:
        puerto = "?"
    base = partes.path.lstrip("/") or "?"
    return f"{base} en {partes.hostname or '?'}:{puerto} como {partes.username or '?'}"


def sin_clave(texto: str, url: str) -> str:
    """`texto` sin la contraseña de `url`, por si un error del driver la repite."""
    try:
        clave = urlsplit(url).password
    except ValueError:
        clave = None
    for variante in {clave, unquote(clave)} if clave else ():
        texto = texto.replace(variante, "***")
    return texto


def revisar_contrasena(url: str) -> PreflightCheck:
    """La contraseña de la URL del runtime no puede ser la de desarrollo (pública)."""
    try:
        crudo = urlsplit(url).password
    except ValueError:
        crudo = None
    if not crudo:
        return _check(
            "contrasena_runtime", None, "La URL no trae contraseña: no se puede revisar desde aquí"
        )
    if unquote(crudo) == CONTRASENA_DE_DESARROLLO:
        return _check(
            "contrasena_runtime",
            False,
            "Es la contraseña de desarrollo de la migración 0003 (está en el repo): "
            "cambiarla con ALTER ROLE y actualizar DATABASE_URL",
        )
    return _check("contrasena_runtime", True, "No es la contraseña de desarrollo")


async def revisar_rol(session: Any, rol_esperado: str = ROL_ESPERADO) -> list[PreflightCheck]:
    """`rol_runtime` y `rol_esperado` sobre la conexión de `session`."""
    from sqlalchemy import text

    rol, superusuario, bypassrls = (await session.execute(text(_SQL_ROL))).one()
    if superusuario:
        rol_runtime = _check("rol_runtime", False, f"{rol} es superusuario: se salta RLS siempre")
    elif bypassrls:
        rol_runtime = _check("rol_runtime", False, f"{rol} tiene BYPASSRLS: se salta RLS siempre")
    else:
        propias = list((await session.execute(text(_SQL_PROPIEDADES))).scalars())
        if propias:
            rol_runtime = _check(
                "rol_runtime", False, f"{rol} es dueño (o miembro del dueño) de: {_listar(propias)}"
            )
        else:
            rol_runtime = _check(
                "rol_runtime",
                True,
                f"{rol} no es superusuario, no tiene BYPASSRLS y no es dueño de ninguna "
                "base, esquema ni tabla",
            )
    detalle = f"Conecta como {rol}"
    if rol != rol_esperado:
        detalle += f"; se esperaba {rol_esperado}"
    return [rol_runtime, _check("rol_esperado", rol == rol_esperado, detalle)]


async def revisar_rls_otros_esquemas(session: Any) -> PreflightCheck:
    """Tablas con RLS habilitada y sin FORCE fuera de `public`."""
    from sqlalchemy import text

    tablas = list((await session.execute(text(_SQL_RLS_OTROS_ESQUEMAS))).scalars())
    if tablas:
        return _check(
            "rls_otros_esquemas", False, f"RLS sin FORCE fuera de public: {_listar(tablas)}"
        )
    return _check("rls_otros_esquemas", True, "Fuera de public no hay tablas con RLS sin FORCE")


def _simbolo(ok: bool | None) -> str:
    if ok is True:
        return "OK"
    if ok is None:
        return "? "
    return "X "


async def ejecutar(rol_esperado: str = ROL_ESPERADO) -> int:
    """Corre los checks contra `DATABASE_URL` e imprime el resultado. Devuelve el código."""
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        print(
            f"ERROR: falta {ENV_URL} en el entorno. Se lee solo de variables de entorno (nunca "
            "de .env): es la URL del runtime (rol app_runtime).",
            file=sys.stderr,
        )
        return SALIDA_ERROR

    from core.db.session import get_engine, get_session
    from core.gateway import meta_production_readiness as readiness

    try:
        checks = [await readiness.check_rls_forced()]
        async with get_session() as session:
            checks.append(await revisar_rls_otros_esquemas(session))
            checks.extend(await revisar_rol(session, rol_esperado))
    except Exception as exc:  # sin conexión no hay nada que verificar
        mensaje = f"no se pudo consultar {describir_url(url)}: {type(exc).__name__}: {exc}"
        print(f"ERROR: {sin_clave(mensaje, url)}", file=sys.stderr)
        return SALIDA_ERROR
    finally:
        await get_engine().dispose()
    checks.append(revisar_contrasena(url))

    print(f"Base: {describir_url(url)}")
    for check in checks:
        print(f"[{_simbolo(check.ok)}] {check.key}: {check.detail}")
    return SALIDA_FALLA if any(check.ok is False for check in checks) else SALIDA_OK


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rol-esperado",
        default=ROL_ESPERADO,
        help=f"Rol con el que debe conectar el runtime (por defecto {ROL_ESPERADO}).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(ejecutar(args.rol_esperado)))


if __name__ == "__main__":
    main()

"""Compara el conteo de filas por tabla entre dos bases Postgres: origen y destino.

Es la verificación de la migración de datos de HU-ARCH-01 (runbook
`docs/runbooks/produccion-vps.md`): después de restaurar en la base nueva, cada tabla
tiene que tener las mismas filas que en la base de origen y las versiones de esquema
(`alembic_version*`) tienen que ser las mismas.

Conexiones: SOLO de variables de entorno, nunca de `.env` ni de otro archivo.

- `ORIGEN_DATABASE_URL`: la base de la que se copiaron los datos.
- `DESTINO_DATABASE_URL`: la base nueva.

Aceptan el formato de la app (`postgresql+asyncpg://…`) o el de libpq (`postgresql://…`).
El rol de cada URL tiene que poder saltarse RLS (superusuario o `BYPASSRLS`): las tablas de
negocio tienen RLS forzada y, sin ese permiso, `count(*)` daría 0 en las dos bases y el
conteo "coincidiría" sin haber comparado nada. Por eso el script lo comprueba antes de
contar y además fija `row_security = off`: si el rol no puede saltarse RLS, la consulta
falla en vez de filtrar filas.

Qué se cuenta: las tablas (`relkind` r y p) de todos los esquemas que no son del sistema.
Se omiten las tablas que pertenecen a una extensión (p. ej. `spatial_ref_sys` de PostGIS):
las crea la extensión, no la migración de datos, y se listan aparte.

Solo lee. Cada base se cuenta dentro de una transacción `REPEATABLE READ READ ONLY`, así el
resultado es una foto consistente aunque alguien escriba mientras tanto.

Uso:
    export ORIGEN_DATABASE_URL=…  DESTINO_DATABASE_URL=…
    python scripts/comparar_conteos.py

Código de salida:
    0  todas las tablas y las versiones de esquema coinciden;
    1  hay diferencias (conteo, tabla que falta o versión de esquema distinta);
    2  error de uso: falta una variable, no se puede conectar, el rol no puede saltarse RLS
       o las dos URL llegan a la misma base.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

ENV_ORIGEN = "ORIGEN_DATABASE_URL"
ENV_DESTINO = "DESTINO_DATABASE_URL"

SALIDA_IGUALES = 0
SALIDA_DIFERENCIAS = 1
SALIDA_ERROR = 2

IGUAL = "igual"
DIFIERE = "difiere"
FALTA_EN_DESTINO = "falta en destino"
FALTA_EN_ORIGEN = "falta en origen"
VERSION_DISTINTA = "versión distinta"

# Tablas de usuario de todos los esquemas que no son del sistema. `de_extension` marca las
# que crea una extensión (dependencia 'e' en pg_depend); `tiene_version` marca las tablas
# de versiones de Alembic (la del core y una por pack: `alembic_version_<pack>`).
_SQL_TABLAS = """
select n.nspname as esquema,
       c.relname as tabla,
       exists (
           select 1 from pg_depend d
           where d.classid = 'pg_class'::regclass and d.objid = c.oid and d.deptype = 'e'
       ) as de_extension,
       c.relname like 'alembic\\_version%' and exists (
           select 1 from pg_attribute a
           where a.attrelid = c.oid and a.attname = 'version_num' and not a.attisdropped
       ) as tiene_version
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p')
  and n.nspname <> 'information_schema'
  and n.nspname !~ '^pg_'
order by 1, 2
"""

_SQL_ROL = """
select current_user as rol,
       current_database() as base,
       current_setting('server_version') as version,
       r.rolsuper as superusuario,
       r.rolbypassrls as bypassrls,
       pg_postmaster_start_time()::text as arranque
from pg_roles r
where r.rolname = current_user
"""


class ErrorDeUso(Exception):
    """Falta configuración o la conexión no sirve para comparar: salida 2."""


@dataclass(frozen=True)
class Foto:
    """Lo que se leyó de una base: conteos por tabla y versiones de esquema."""

    descripcion: str
    conteos: Mapping[str, int]
    versiones: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    omitidas: tuple[str, ...] = ()
    # (identificador del clúster, arranque del servidor, base): igual en las dos fotos
    # solo si las dos URL llegan a la misma base del mismo servidor.
    identidad: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class Fila:
    tabla: str
    origen: int | None
    destino: int | None
    estado: str
    detalle: str = ""


def normalizar_url(url: str) -> str:
    """URL de SQLAlchemy (`postgresql+asyncpg://`) → URL de libpq, que es la que usa asyncpg."""
    esquema, separador, resto = url.strip().partition("://")
    if not separador:
        raise ErrorDeUso("La URL no tiene el formato postgresql://usuario@host:puerto/base.")
    esquema = esquema.split("+", 1)[0]
    if esquema not in ("postgresql", "postgres"):
        raise ErrorDeUso(f"Esquema de URL no soportado: {esquema!r} (se espera postgresql).")
    return f"postgresql://{resto}"


def servidor_de(url: str) -> str:
    """`host:puerto` de la URL (nunca la contraseña)."""
    partes = urlsplit(url)
    try:
        puerto = partes.port or 5432
    except ValueError:
        puerto = "?"
    return f"{partes.hostname or '?'}:{puerto}"


def sin_clave(texto: str, url: str) -> str:
    """`texto` sin la contraseña de `url`, por si un error del driver la repite."""
    try:
        clave = urlsplit(url).password
    except ValueError:
        clave = None
    for variante in {clave, unquote(clave)} if clave else ():
        texto = texto.replace(variante, "***")
    return texto


def _error(contexto: str, exc: Exception, url: str) -> ErrorDeUso:
    return ErrorDeUso(sin_clave(f"{contexto}: {type(exc).__name__}: {exc}", url))


def leer_urls(entorno: Mapping[str, str]) -> tuple[str, str]:
    faltan = [nombre for nombre in (ENV_ORIGEN, ENV_DESTINO) if not entorno.get(nombre, "").strip()]
    if faltan:
        raise ErrorDeUso(
            "Falta " + " y ".join(faltan) + " en el entorno. Las URL se leen solo de variables "
            "de entorno, nunca de .env (ver el docstring del script)."
        )
    origen = normalizar_url(entorno[ENV_ORIGEN])
    destino = normalizar_url(entorno[ENV_DESTINO])
    if origen == destino:
        raise ErrorDeUso(
            f"{ENV_ORIGEN} y {ENV_DESTINO} son la misma URL: no hay nada que comparar."
        )
    return origen, destino


def _ident(nombre: str) -> str:
    return '"' + nombre.replace('"', '""') + '"'


async def tomar_foto(url: str) -> Foto:
    """Conecta, comprueba que el rol se salta RLS y cuenta las filas de cada tabla."""
    import asyncpg

    servidor = servidor_de(url)
    try:
        conn = await asyncpg.connect(url, timeout=20)
    except Exception as exc:  # red, DNS, autenticación: no hay con qué comparar
        raise _error(f"No se pudo conectar a {servidor}", exc, url) from exc
    try:
        rol = await conn.fetchrow(_SQL_ROL)
        if rol is None or not (rol["superusuario"] or rol["bypassrls"]):
            raise ErrorDeUso(
                f"El rol {rol['rol'] if rol else '?'} de {servidor} no es superusuario ni tiene "
                "BYPASSRLS: con RLS forzada contaría 0 filas en las tablas de negocio. Usar el "
                "superusuario de la base (o un rol con BYPASSRLS)."
            )
        descripcion = f"{rol['base']} en {servidor} como {rol['rol']} (PostgreSQL {rol['version']})"
        try:
            sistema = str(await conn.fetchval("select system_identifier from pg_control_system()"))
        except Exception:  # sin permiso: la comparación de identidad queda en manos de la URL
            sistema = "?"

        conteos: dict[str, int] = {}
        versiones: dict[str, tuple[str, ...]] = {}
        omitidas: list[str] = []
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("set local row_security = off")
            for fila in await conn.fetch(_SQL_TABLAS):
                nombre = f"{fila['esquema']}.{fila['tabla']}"
                if fila["de_extension"]:
                    omitidas.append(nombre)
                    continue
                tabla = f"{_ident(fila['esquema'])}.{_ident(fila['tabla'])}"
                conteos[nombre] = int(await conn.fetchval(f"select count(*) from {tabla}"))
                if fila["tiene_version"]:
                    filas = await conn.fetch(f"select version_num from {tabla}")
                    versiones[nombre] = tuple(sorted(str(f["version_num"]) for f in filas))
    except ErrorDeUso:
        raise
    except Exception as exc:
        raise _error(f"Falló la lectura de {servidor}", exc, url) from exc
    finally:
        await conn.close()
    return Foto(
        descripcion=descripcion,
        conteos=conteos,
        versiones=versiones,
        omitidas=tuple(omitidas),
        identidad=(sistema, str(rol["arranque"]), str(rol["base"])),
    )


def comparar(origen: Foto, destino: Foto) -> list[Fila]:
    """Una fila por tabla de cualquiera de las dos bases, ordenadas por nombre."""
    filas: list[Fila] = []
    for tabla in sorted(set(origen.conteos) | set(destino.conteos)):
        n_origen = origen.conteos.get(tabla)
        n_destino = destino.conteos.get(tabla)
        if n_destino is None:
            filas.append(Fila(tabla, n_origen, None, FALTA_EN_DESTINO))
        elif n_origen is None:
            filas.append(Fila(tabla, None, n_destino, FALTA_EN_ORIGEN))
        elif n_origen != n_destino:
            filas.append(Fila(tabla, n_origen, n_destino, DIFIERE))
        elif origen.versiones.get(tabla) != destino.versiones.get(tabla):
            detalle = (
                f"origen {', '.join(origen.versiones.get(tabla, ())) or '-'}; "
                f"destino {', '.join(destino.versiones.get(tabla, ())) or '-'}"
            )
            filas.append(Fila(tabla, n_origen, n_destino, VERSION_DISTINTA, detalle))
        else:
            filas.append(Fila(tabla, n_origen, n_destino, IGUAL))
    return filas


def _numero(valor: int | None) -> str:
    return "-" if valor is None else str(valor)


def informe(origen: Foto, destino: Foto, filas: list[Fila]) -> list[str]:
    """Las líneas que se imprimen: encabezado, una línea por tabla, omitidas y resumen."""
    ancho = max([len("tabla"), *(len(f.tabla) for f in filas)])
    lineas = [
        f"Origen:  {origen.descripcion}",
        f"Destino: {destino.descripcion}",
        "",
        f"{'tabla':<{ancho}}  {'origen':>9}  {'destino':>9}  estado",
    ]
    for fila in filas:
        estado = f"{fila.estado} ({fila.detalle})" if fila.detalle else fila.estado
        lineas.append(
            f"{fila.tabla:<{ancho}}  {_numero(fila.origen):>9}  {_numero(fila.destino):>9}  {estado}"
        )
    omitidas = sorted(set(origen.omitidas) | set(destino.omitidas))
    if omitidas:
        lineas += ["", "Omitidas por ser de una extensión: " + ", ".join(omitidas)]
    distintas = [f for f in filas if f.estado != IGUAL]
    resumen = (
        f"Resumen: {len(filas)} tablas comparadas, {len(filas) - len(distintas)} iguales, "
        f"{len(distintas)} con diferencias"
    )
    lineas += ["", resumen + (":" if distintas else ".")]
    for fila in distintas:
        detalle = fila.detalle or f"origen {_numero(fila.origen)}, destino {_numero(fila.destino)}"
        lineas.append(f"  - {fila.tabla}: {fila.estado} ({detalle})")
    return lineas


async def ejecutar(entorno: Mapping[str, str]) -> int:
    """Lee las URL de `entorno`, compara e imprime. Devuelve el código de salida."""
    try:
        url_origen, url_destino = leer_urls(entorno)
    except ErrorDeUso as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return SALIDA_ERROR

    resultados = await asyncio.gather(
        tomar_foto(url_origen), tomar_foto(url_destino), return_exceptions=True
    )
    errores = [r for r in resultados if isinstance(r, BaseException)]
    for nombre, resultado in zip(("origen", "destino"), resultados, strict=True):
        if isinstance(resultado, ErrorDeUso):
            print(f"ERROR ({nombre}): {resultado}", file=sys.stderr)
        elif isinstance(resultado, BaseException):
            raise resultado
    if errores:
        return SALIDA_ERROR

    origen, destino = resultados
    assert isinstance(origen, Foto) and isinstance(destino, Foto)
    if origen.identidad is not None and origen.identidad == destino.identidad:
        print(
            f"ERROR: {ENV_ORIGEN} y {ENV_DESTINO} llegan a la misma base del mismo servidor "
            f"({origen.descripcion}): no hay nada que comparar.",
            file=sys.stderr,
        )
        return SALIDA_ERROR

    filas = comparar(origen, destino)
    for linea in informe(origen, destino, filas):
        print(linea)
    return SALIDA_DIFERENCIAS if any(f.estado != IGUAL for f in filas) else SALIDA_IGUALES


def main() -> None:
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    sys.exit(asyncio.run(ejecutar(os.environ)))


if __name__ == "__main__":
    main()

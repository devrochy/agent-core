"""Scripts del runbook de producción en VPS contra un Postgres real (HU-ARCH-01).

- `scripts/comparar_conteos.py`: dos bases creadas por el test, con RLS forzada.
- `scripts/verificar_aislamiento.py`: la base migrada (con el rol del runtime) y una base
  creada por el test con roles mal configurados a propósito.
- `scripts/vaciar_base_nueva.sql`: vacía solo una base con la marca de la base nueva.

Crear bases y roles exige un superusuario: se usa `MIGRATIONS_DATABASE_URL` (en el CI y en
el stack local es `agent_core`). Cada test borra lo que crea; la base migrada solo se lee.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]


def _cargar(nombre: str):
    spec = importlib.util.spec_from_file_location(nombre, RAIZ / "scripts" / f"{nombre}.py")
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


cc = _cargar("comparar_conteos")
va = _cargar("verificar_aislamiento")
SQL_VACIAR = (RAIZ / "scripts" / "vaciar_base_nueva.sql").read_text(encoding="utf-8")
MARCA = "solvershub-produccion-vps"

ESQUEMA_DE_PRUEBA = """
create schema privado;
create table public.negocio (id serial primary key, tenant_id uuid not null);
alter table public.negocio enable row level security;
alter table public.negocio force row level security;
create policy aislamiento on public.negocio
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
create table privado.secreto (id integer primary key);
create table public.alembic_version (version_num varchar(32) primary key);
insert into public.alembic_version values ('0058_evals_pass_k');
insert into public.negocio (tenant_id) select gen_random_uuid() from generate_series(1, 5);
insert into privado.secreto values (1), (2);
"""


def _dsn(
    base_dsn: str,
    *,
    base: str | None = None,
    usuario: str | None = None,
    clave: str | None = None,
    driver: str = "postgresql",
) -> str:
    """La URL de `base_dsn` con otra base, otro usuario o otro driver."""
    partes = urlsplit(base_dsn)
    usuario = usuario or unquote(partes.username or "")
    clave = clave if clave is not None else unquote(partes.password or "")
    netloc = (
        f"{quote(usuario, safe='')}:{quote(clave, safe='')}@{partes.hostname}:{partes.port or 5432}"
    )
    return urlunsplit((driver, netloc, "/" + (base or partes.path.lstrip("/")), "", ""))


@dataclass
class Admin:
    conn: object
    dsn: str
    bases: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)

    async def crear_base(self, prefijo: str, sql: str = "") -> str:
        nombre = f"{prefijo}_{uuid.uuid4().hex[:10]}"
        await self.conn.execute(f'create database "{nombre}"')
        self.bases.append(nombre)
        if sql:
            await self.en(nombre, sql)
        return nombre

    async def crear_rol(self, prefijo: str, atributos: str = "nologin") -> tuple[str, str]:
        nombre = f"{prefijo}_{uuid.uuid4().hex[:10]}"
        clave = secrets.token_urlsafe(16)
        await self.conn.execute(f"create role \"{nombre}\" {atributos} password '{clave}'")
        self.roles.append(nombre)
        return nombre, clave

    async def en(self, base: str, sql: str):
        import asyncpg

        conn = await asyncpg.connect(_dsn(self.dsn, base=base))
        try:
            return await conn.execute(sql)
        finally:
            await conn.close()

    async def valor(self, base: str, sql: str):
        import asyncpg

        conn = await asyncpg.connect(_dsn(self.dsn, base=base))
        try:
            return await conn.fetchval(sql)
        finally:
            await conn.close()


@pytest.fixture
async def admin() -> AsyncIterator[Admin]:
    import asyncpg

    url = os.environ.get("MIGRATIONS_DATABASE_URL", "")
    if not url:
        pytest.skip("requiere MIGRATIONS_DATABASE_URL (un superusuario que cree bases y roles)")
    dsn = cc.normalizar_url(url)
    conn = await asyncpg.connect(dsn)
    if not await conn.fetchval("select rolsuper from pg_roles where rolname = current_user"):
        await conn.close()
        pytest.skip("el rol de MIGRATIONS_DATABASE_URL no es superusuario")
    administrador = Admin(conn=conn, dsn=dsn)
    try:
        yield administrador
    finally:
        for base in administrador.bases:
            await conn.execute(f'drop database if exists "{base}" with (force)')
        for rol in administrador.roles:
            await conn.execute(f'drop role if exists "{rol}"')
        await conn.close()


async def _hay_postgis(admin: Admin) -> bool:
    return bool(
        await admin.conn.fetchval(
            "select count(*) from pg_available_extensions where name = 'postgis'"
        )
    )


# ------------------------------------------------------------------ comparar_conteos.py


async def test_comparar_conteos_cuenta_tablas_con_rls_forzada_y_detecta_diferencias(
    admin: Admin, capsys
) -> None:
    origen = await admin.crear_base("cmp_origen", ESQUEMA_DE_PRUEBA)
    destino = await admin.crear_base("cmp_destino", ESQUEMA_DE_PRUEBA)
    entorno = {
        cc.ENV_ORIGEN: _dsn(admin.dsn, base=origen, driver="postgresql+asyncpg"),
        cc.ENV_DESTINO: _dsn(admin.dsn, base=destino),
    }

    assert await cc.ejecutar(entorno) == cc.SALIDA_IGUALES
    salida = capsys.readouterr().out
    # 5 filas en una tabla con RLS forzada: el superusuario las ve sin `app.tenant_id`.
    assert any(linea.split()[:3] == ["public.negocio", "5", "5"] for linea in salida.splitlines())
    assert "Resumen: 3 tablas comparadas, 3 iguales, 0 con diferencias." in salida

    await admin.en(
        destino,
        "delete from public.negocio where id = 1;"
        "update public.alembic_version set version_num = '0057_plantillas';"
        "create table public.extra (id integer);",
    )

    assert await cc.ejecutar(entorno) == cc.SALIDA_DIFERENCIAS
    salida = capsys.readouterr().out
    assert "public.negocio: difiere (origen 5, destino 4)" in salida
    assert "public.alembic_version: versión distinta" in salida
    assert "public.extra: falta en origen" in salida


async def test_comparar_conteos_se_niega_con_un_rol_sin_bypassrls(admin: Admin, capsys) -> None:
    """Sin BYPASSRLS, `count(*)` daría 0 en las dos bases y "coincidiría": es un error de uso."""
    origen = await admin.crear_base("cmp_origen", ESQUEMA_DE_PRUEBA)
    destino = await admin.crear_base("cmp_destino", ESQUEMA_DE_PRUEBA)
    lector, clave = await admin.crear_rol("cmp_lector", "login")
    await admin.en(
        origen,
        f'grant usage on schema privado to "{lector}";'
        f'grant select on all tables in schema public, privado to "{lector}";',
    )
    entorno = {
        cc.ENV_ORIGEN: _dsn(admin.dsn, base=origen, usuario=lector, clave=clave),
        cc.ENV_DESTINO: _dsn(admin.dsn, base=destino),
    }

    assert await cc.ejecutar(entorno) == cc.SALIDA_ERROR
    captura = capsys.readouterr()
    assert "BYPASSRLS" in captura.err
    assert clave not in captura.err + captura.out


async def test_comparar_conteos_omite_las_tablas_de_extensiones(admin: Admin, capsys) -> None:
    if not await _hay_postgis(admin):
        pytest.skip("este Postgres no trae PostGIS")
    sql = "create extension postgis; create table public.zona (id integer);"
    origen = await admin.crear_base("cmp_origen", sql)
    destino = await admin.crear_base("cmp_destino", sql)
    entorno = {
        cc.ENV_ORIGEN: _dsn(admin.dsn, base=origen),
        cc.ENV_DESTINO: _dsn(admin.dsn, base=destino),
    }

    assert await cc.ejecutar(entorno) == cc.SALIDA_IGUALES
    salida = capsys.readouterr().out
    assert "Omitidas por ser de una extensión: public.spatial_ref_sys" in salida
    assert "Resumen: 1 tablas comparadas" in salida


# ------------------------------------------------------------------ vaciar_base_nueva.sql


async def test_vaciar_base_nueva_solo_vacia_la_base_marcada(admin: Admin) -> None:
    import asyncpg

    sin_marca = await admin.crear_base("vaciar_origen", ESQUEMA_DE_PRUEBA)
    with pytest.raises(asyncpg.PostgresError, match="no tiene la marca"):
        await admin.en(sin_marca, SQL_VACIAR)
    assert await admin.valor(sin_marca, "select count(*) from public.negocio") == 5

    marcada = await admin.crear_base("vaciar_nueva", ESQUEMA_DE_PRUEBA)
    postgis = await _hay_postgis(admin)
    if postgis:
        await admin.en(marcada, "create extension postgis")
    await admin.conn.execute(f"comment on database \"{marcada}\" is '{MARCA}'")

    await admin.en(marcada, SQL_VACIAR)

    for tabla in ("public.negocio", "public.alembic_version", "privado.secreto"):
        assert await admin.valor(marcada, f"select count(*) from {tabla}") == 0, tabla
    # El esquema queda intacto y las secuencias vuelven a empezar.
    assert (
        await admin.valor(
            marcada,
            f"insert into public.negocio (tenant_id) values ('{uuid.uuid4()}') returning id",
        )
        == 1
    )
    if postgis:
        assert await admin.valor(marcada, "select count(*) from public.spatial_ref_sys") > 0


# ------------------------------------------------------------------ verificar_aislamiento.py


@pytest.mark.aislamiento  # subconjunto de las PRs a develop (ADR-0120)
async def test_el_runtime_de_la_base_migrada_no_es_dueno_y_rls_forced_pasa(capsys) -> None:
    """Con la `DATABASE_URL` del CI (rol `app_runtime` sobre la base migrada)."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("requiere DATABASE_URL del runtime")

    resultado = await va.ejecutar()

    salida = capsys.readouterr().out
    assert resultado != va.SALIDA_ERROR, salida
    assert "[OK] rls_forced:" in salida
    assert "[OK] rol_runtime:" in salida
    assert "[OK] rol_esperado: Conecta como app_runtime" in salida


@pytest.mark.aislamiento  # subconjunto de las PRs a develop (ADR-0120)
async def test_verificar_aislamiento_detecta_dueno_miembro_superusuario_y_rls_sin_force(
    admin: Admin,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    dueno, _ = await admin.crear_rol("ais_dueno")
    runtime, clave = await admin.crear_rol("ais_runtime", "login")
    base = await admin.crear_base(
        "ais",
        f"""
        create schema privado;
        create table privado.tokens (tenant_id uuid not null);
        alter table privado.tokens enable row level security;
        create table public.negocio (tenant_id uuid not null);
        alter table public.negocio enable row level security;
        alter table public.negocio force row level security;
        alter table public.negocio owner to "{dueno}";
        grant usage on schema privado to "{runtime}";
        """,
    )

    async def _revisar(usuario: str, clave_usuario: str | None, esperado: str):
        motor = create_async_engine(
            _dsn(
                admin.dsn,
                base=base,
                usuario=usuario,
                clave=clave_usuario,
                driver="postgresql+asyncpg",
            )
        )
        try:
            async with AsyncSession(motor) as session:
                otros = await va.revisar_rls_otros_esquemas(session)
                rol_runtime, rol_esperado = await va.revisar_rol(session, esperado)
        finally:
            await motor.dispose()
        return otros, rol_runtime, rol_esperado

    otros, rol_runtime, rol_esperado = await _revisar(runtime, clave, runtime)
    assert otros.ok is False and "privado.tokens" in otros.detail
    assert rol_runtime.ok is True, rol_runtime.detail
    assert rol_esperado.ok is True

    # Miembro del rol dueño: con SET ROLE pasaría a ser el dueño de public.negocio.
    await admin.conn.execute(f'grant "{dueno}" to "{runtime}"')
    _, rol_runtime, _ = await _revisar(runtime, clave, runtime)
    assert rol_runtime.ok is False and "tabla public.negocio" in rol_runtime.detail
    await admin.conn.execute(f'revoke "{dueno}" from "{runtime}"')

    # Dueño directo de una tabla y de la base.
    await admin.en(base, f'alter table privado.tokens owner to "{runtime}"')
    await admin.conn.execute(f'alter database "{base}" owner to "{runtime}"')
    _, rol_runtime, _ = await _revisar(runtime, clave, runtime)
    assert rol_runtime.ok is False
    assert f"base {base}" in rol_runtime.detail and "tabla privado.tokens" in rol_runtime.detail
    await admin.conn.execute(f'alter database "{base}" owner to current_user')

    # FORCE fuera de public: el check pasa.
    await admin.en(base, "alter table privado.tokens force row level security")
    otros, _, _ = await _revisar(runtime, clave, runtime)
    assert otros.ok is True, otros.detail

    # Superusuario: se salta RLS siempre y no es el rol esperado.
    _, rol_runtime, rol_esperado = await _revisar(
        unquote(urlsplit(admin.dsn).username or ""), None, va.ROL_ESPERADO
    )
    assert rol_runtime.ok is False and "superusuario" in rol_runtime.detail
    assert rol_esperado.ok is False

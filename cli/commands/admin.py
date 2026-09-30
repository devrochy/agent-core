"""agent-core admin: alta y permisos de administradores del panel (ADR-0047).

El panel se autentica con la tabla global `admins` (no hay credenciales por
tenant). La autorización se resuelve por permisos de sección en
`admin_permissions`: `tenant_id` nulo = el permiso vale para todos los tenants
(admin global, ej. marketing); con valor = solo ese tenant. Sin permiso
explícito, el rol del `tenant_user` del tenant activo aplica por defecto
(`owner` → escritura, `colaborador` → lectura).

Uso:
  agent-core admin create --email marketing@x.com --password ...
  agent-core admin grant --email marketing@x.com --section costs --access read
  agent-core admin link --email dueno@x.com --tenant mi-negocio --role owner
  agent-core admin password --email dueno@x.com
  agent-core admin list
"""

from __future__ import annotations

import asyncio
import secrets
import uuid

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
console = Console()


def _sections() -> list[str]:
    from core.admin.permissions import catalog
    from core.plugin_manager import plugin_manager

    return sorted(catalog(plugin_manager.active_ids()))


def _validate_section(section: str) -> None:
    valid = _sections()
    if section not in valid:
        console.print(f"[red]Sección inválida:[/red] {section}. Válidas: {', '.join(valid)}")
        raise typer.Exit(1)


async def _get_admin(email: str):
    from sqlalchemy import select

    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()


async def _get_tenant(slug: str) -> uuid.UUID:
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()
    if tenant_id is None:
        console.print(f"[red]Tenant no encontrado:[/red] {slug}")
        raise typer.Exit(1)
    return tenant_id


@app.command()
def create(
    email: str = typer.Option(..., "--email", help="Email del admin (identificador de login)"),
    password: str = typer.Option("", "--password", help="Contraseña (vacío = se genera)"),
    role: str = typer.Option("colaborador", "--role", help="master | colaborador"),
    username: str = typer.Option("", "--username", help="Usuario alternativo de login (opcional)"),
) -> None:
    """Crea un admin. `master` es superusuario (bypass); el resto usa permisos."""
    from sqlalchemy import select

    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    if role not in ("master", "colaborador"):
        console.print("[red]--role debe ser master o colaborador[/red]")
        raise typer.Exit(1)
    generated = not password
    password = password or secrets.token_urlsafe(12)

    async def _run() -> None:
        async with get_session() as session:
            existing = (
                await session.execute(select(Admin).where(Admin.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                console.print(f"[yellow]El admin {email} ya existe (sin cambios).[/yellow]")
                return
            session.add(
                Admin(
                    email=email,
                    username=username or None,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
        console.print(f"✓ [green]Admin creado:[/green] {email} (rol {role})")
        if generated:
            console.print(f"  Contraseña generada: [bold]{password}[/bold]")

    asyncio.run(_run())


@app.command()
def password(
    email: str = typer.Option(..., "--email", help="Email del admin"),
    password: str = typer.Option("", "--password", help="Nueva contraseña (vacío = se genera)"),
) -> None:
    """Resetea la contraseña de un admin."""
    from core.admin.auth import hash_password
    from core.db.session import get_session

    generated = not password
    password = password or secrets.token_urlsafe(12)

    async def _run() -> None:
        admin = await _get_admin(email)
        if admin is None:
            console.print(f"[red]Admin no encontrado:[/red] {email}")
            raise typer.Exit(1)
        async with get_session() as session:
            stored = await session.get(type(admin), admin.id)
            stored.password_hash = hash_password(password)
        console.print(f"✓ [green]Contraseña actualizada:[/green] {email}")
        if generated:
            console.print(f"  Nueva contraseña: [bold]{password}[/bold]")

    asyncio.run(_run())


@app.command()
def link(
    email: str = typer.Option(..., "--email", help="Email del admin"),
    tenant: str = typer.Option(..., "--tenant", help="Slug del tenant"),
    role: str = typer.Option("owner", "--role", help="owner | colaborador"),
) -> None:
    """Vincula un admin a un tenant (rol por defecto de acceso a ese tenant)."""
    from sqlalchemy import select

    from core.db.models import TenantUser
    from core.db.session import get_session

    if role not in ("owner", "colaborador"):
        console.print("[red]--role debe ser owner o colaborador[/red]")
        raise typer.Exit(1)

    async def _run() -> None:
        admin = await _get_admin(email)
        if admin is None:
            console.print(f"[red]Admin no encontrado:[/red] {email}")
            raise typer.Exit(1)
        tenant_id = await _get_tenant(tenant)
        async with get_session() as session:
            row = (
                await session.execute(
                    select(TenantUser).where(
                        TenantUser.admin_id == admin.id, TenantUser.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(TenantUser(tenant_id=tenant_id, admin_id=admin.id, role=role))
            else:
                row.role = role
        console.print(f"✓ [green]{email} → {tenant} ({role})[/green]")

    asyncio.run(_run())


@app.command()
def grant(
    email: str = typer.Option(..., "--email", help="Email del admin"),
    section: str = typer.Option(..., "--section", help="Sección (ej. costs, conversations)"),
    access: str = typer.Option("read", "--access", help="read | write"),
    tenant: str = typer.Option("", "--tenant", help="Slug del tenant (vacío = global, todos)"),
) -> None:
    """Concede un permiso de sección. Sin `--tenant` el permiso es global."""
    from sqlalchemy import select

    from core.db.models import AdminPermission
    from core.db.session import get_session

    _validate_section(section)
    if access not in ("read", "write"):
        console.print("[red]--access debe ser read o write[/red]")
        raise typer.Exit(1)

    async def _run() -> None:
        admin = await _get_admin(email)
        if admin is None:
            console.print(f"[red]Admin no encontrado:[/red] {email}")
            raise typer.Exit(1)
        tenant_id = await _get_tenant(tenant) if tenant else None
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AdminPermission).where(
                        AdminPermission.admin_id == admin.id,
                        AdminPermission.section == section,
                        AdminPermission.tenant_id.is_(None)
                        if tenant_id is None
                        else AdminPermission.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    AdminPermission(
                        admin_id=admin.id, section=section, access=access, tenant_id=tenant_id
                    )
                )
            else:
                row.access = access
        scope = tenant or "global (todos los tenants)"
        console.print(f"✓ [green]{email}: {section} → {access} en {scope}[/green]")

    asyncio.run(_run())


@app.command()
def revoke(
    email: str = typer.Option(..., "--email", help="Email del admin"),
    section: str = typer.Option(..., "--section", help="Sección a revocar"),
    tenant: str = typer.Option("", "--tenant", help="Slug del tenant (vacío = global)"),
) -> None:
    """Revoca un permiso explícito de sección."""
    from sqlalchemy import delete

    from core.db.models import AdminPermission
    from core.db.session import get_session

    async def _run() -> None:
        admin = await _get_admin(email)
        if admin is None:
            console.print(f"[red]Admin no encontrado:[/red] {email}")
            raise typer.Exit(1)
        tenant_id = await _get_tenant(tenant) if tenant else None
        async with get_session() as session:
            await session.execute(
                delete(AdminPermission).where(
                    AdminPermission.admin_id == admin.id,
                    AdminPermission.section == section,
                    AdminPermission.tenant_id.is_(None)
                    if tenant_id is None
                    else AdminPermission.tenant_id == tenant_id,
                )
            )
        scope = tenant or "global"
        console.print(f"✓ [green]{email}: permiso {section} revocado en {scope}[/green]")

    asyncio.run(_run())


@app.command(name="list")
def list_admins(
    email: str = typer.Option("", "--email", help="Filtrar por email (opcional)"),
) -> None:
    """Lista admins con su rol y sus permisos explícitos."""
    from sqlalchemy import select

    from core.db.models import Admin, AdminPermission, Tenant
    from core.db.session import get_session

    async def _run() -> None:
        async with get_session() as session:
            query = select(Admin).order_by(Admin.email)
            if email:
                query = query.where(Admin.email == email)
            admins = (await session.execute(query)).scalars().all()
            rows = (
                await session.execute(
                    select(
                        AdminPermission.admin_id,
                        AdminPermission.section,
                        AdminPermission.access,
                        Tenant.slug,
                    ).join(Tenant, Tenant.id == AdminPermission.tenant_id, isouter=True)
                )
            ).all()

        perms: dict[uuid.UUID, list[str]] = {}
        for admin_id, section, access, slug in rows:
            scope = slug or "*"
            perms.setdefault(admin_id, []).append(f"{section}:{access}@{scope}")

        table = Table(title="Admins")
        table.add_column("Email")
        table.add_column("Rol")
        table.add_column("Activo")
        table.add_column("Permisos")
        for admin in admins:
            table.add_row(
                admin.email,
                admin.role,
                "sí" if admin.active else "no",
                ", ".join(sorted(perms.get(admin.id, []))) or "—",
            )
        console.print(table)

    asyncio.run(_run())

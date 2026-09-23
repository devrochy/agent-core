"""agent-core tenant: alta y operación de tenants (multi-tenant, ADR-0001).

Cada cliente es un tenant con su propio asistente (AgentSpec). El onboarding es
100% configuración: `tenant create` da de alta el tenant; `tenant seed` importa
los datos del tenant (catalog.json) que viven en TENANTS_ROOT (gitignored).

Uso:
  agent-core tenant create                # wizard interactivo paso a paso
  agent-core tenant create --non-interactive --name "Mi Bot" --packs pack_commerce
  agent-core tenant seed <slug>           # importa TENANTS_ROOT/<slug>/catalog.json
  agent-core tenant list
  agent-core tenant docs <slug>
"""

from __future__ import annotations

import asyncio
import uuid

import questionary
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select

from cli.preflight import slugify

app = typer.Typer(no_args_is_help=True)
console = Console()

DEFAULT_TRIGGERS = ["asesor", "humano", "persona real", "reclamo", "queja"]


# ------------------------------------------------------------------ create
@app.command()
def create(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "-y", help="Sin preguntas: usa las banderas y sus defaults"
    ),
    name: str = typer.Option("", "--name", help="Nombre del negocio / asistente"),
    slug: str = typer.Option("", "--slug", help="Slug del tenant (vacío = derivado del nombre)"),
    business: str = typer.Option("", "--business", help="Descripción del negocio"),
    language: str = typer.Option("es", "--language", help="Idioma principal (es/en/pt)"),
    tone: str = typer.Option("cercano", "--tone", help="formal | neutral | cercano | informal"),
    packs: str = typer.Option("", "--packs", help="Coma-separado, ej: pack_commerce"),
    escalation_triggers: str = typer.Option("", "--escalation-triggers",
                                            help="Coma-separado (default: asesor, humano, persona real, reclamo, queja)"),
    admin_email: str = typer.Option("", "--admin-email", help="Email del admin del tenant (opcional)"),
    admin_password: str = typer.Option(
        "", "--admin-password", help="Contraseña del admin (crea el admin si no existe)"
    ),
    wa_label: str = typer.Option("", "--wa-label", help="Etiqueta de la conexión WhatsApp"),
    wa_phone_number_id: str = typer.Option("", "--wa-phone-number-id", help="phone_number_id de Meta"),
    wa_app_secret: str = typer.Option("", "--wa-app-secret", help="App secret de Meta"),
    wa_access_token: str = typer.Option("", "--wa-access-token", help="Access token de Meta"),
    wa_verify_token: str = typer.Option("", "--wa-verify-token", help="Verify token del webhook"),
) -> None:
    """Wizard paso a paso: da de alta un cliente nuevo como tenant."""
    console.print(Panel.fit("🤖 [bold]agent-core tenant create[/bold] — nuevo cliente", border_style="cyan"))

    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    available = [m.id for m in plugin_manager.list_modules() if m.id != "core"]

    if non_interactive:
        answers = {
            "name": name,
            "slug": slug or slugify(name or "tenant"),
            "business": business,
            "language": language,
            "tone": tone,
            "packs": [p.strip() for p in packs.split(",") if p.strip()],
            "escalation_triggers": [t.strip() for t in escalation_triggers.split(",") if t.strip()],
            "admin_email": admin_email,
            "admin_password": admin_password,
            "whatsapp": {
                "label": wa_label,
                "phone_number_id": wa_phone_number_id,
                "app_secret": wa_app_secret,
                "access_token": wa_access_token,
                "verify_token": wa_verify_token,
            }
            if wa_phone_number_id
            else None,
        }
    else:
        answers = _ask_interactive(available)

    if not answers["escalation_triggers"]:
        answers["escalation_triggers"] = list(DEFAULT_TRIGGERS)

    if answers["whatsapp"]:
        ok, msg, _info = asyncio.run(_verify_whatsapp(answers["whatsapp"]))
        if not ok:
            console.print(f"[red]Credenciales WhatsApp rechazadas: {msg}[/red]")
            raise typer.Exit(1)

    tid, steps = asyncio.run(_onboard(answers))
    console.print("✓ [green]Onboarding completado[/green]")
    for s in steps:
        console.print(f"  · {s}")

    _maybe_seed(answers["slug"], tid)

    from core.tenants import tenant_webhook_url

    lines = [
        f"[bold]{answers['name']}[/bold] dado de alta.",
        f"  · tenant id: [bold]{tid}[/bold]",
        f"  · slug: [bold]{answers['slug']}[/bold]",
        "",
        "Siguientes pasos:",
        "  1. Abre el panel y cambia al tenant en el selector (http://localhost:8000/admin).",
        "  2. Configura el AgentSpec: /admin/spec (persona, playbooks, políticas).",
        "  3. Conecta WhatsApp en /admin/conexiones (o repite con el wizard + credenciales).",
        f"  4. Registra el webhook en Meta: {tenant_webhook_url(answers['slug'])} "
        "(ver: agent-core tenant webhook)",
        f"  5. Crea el golden set del tenant en {answers['slug']}/evals/ (eval-author).",
        "  6. Canary 5% → revisión 24 h → 100% (ADR-0016).",
    ]
    console.print(Panel("\n".join(lines), border_style="green", title="✓ tenant listo"))


def _ask_interactive(available_packs: list[str]) -> dict:
    name = questionary.text("Nombre del negocio / asistente:").ask() or ""
    business = questionary.text("Describe el negocio (1-2 frases):").ask() or ""
    language = questionary.select("Idioma principal:", choices=["es", "en", "pt"], default="es").ask()
    tone = questionary.select(
        "Tono del asistente:",
        choices=["formal", "neutral", "cercano", "informal"],
        default="cercano",
    ).ask()

    slug = questionary.text("Slug del tenant:", default=slugify(name)).ask() or slugify(name)

    pack_choices = [questionary.Choice(title=p, value=p) for p in available_packs]
    selected_packs = (
        questionary.checkbox("Packs a habilitar (espacio para marcar):", choices=pack_choices).ask() or []
    )

    admin_email = questionary.text(
        "Email del administrador del tenant (Enter para omitir):"
    ).ask() or ""
    admin_password = ""
    if admin_email:
        admin_password = (
            questionary.password("Contraseña del administrador (Enter para generar):").ask() or ""
        )

    connect_wa = questionary.confirm("¿Conectar WhatsApp ahora? (puedes hacerlo luego en /admin/conexiones)", default=False).ask()
    whatsapp = None
    if connect_wa:
        whatsapp = {
            "label": questionary.text("Etiqueta (ej: Ventas WhatsApp):", default=name).ask() or name,
            "phone_number_id": questionary.text("phone_number_id de Meta:").ask() or "",
            "app_secret": questionary.password("App secret de Meta:").ask() or "",
            "access_token": questionary.password("Access token de Meta:").ask() or "",
            "verify_token": questionary.password("Verify token del webhook:").ask() or "",
        }

    return {
        "name": name,
        "slug": slug,
        "business": business,
        "language": language,
        "tone": tone,
        "packs": selected_packs,
        "escalation_triggers": list(DEFAULT_TRIGGERS),
        "admin_email": admin_email,
        "admin_password": admin_password,
        "whatsapp": whatsapp,
    }


async def _verify_whatsapp(whatsapp: dict) -> tuple[bool, str, dict]:
    from core.gateway.connections import verify_meta_credentials

    return await verify_meta_credentials(
        whatsapp.get("phone_number_id", ""), whatsapp.get("access_token", ""), whatsapp.get("app_secret", "")
    )


async def _onboard(answers: dict) -> tuple[uuid.UUID, list[str]]:
    import secrets

    from core.onboarding import onboard_tenant

    admin_email = answers.get("admin_email") or None
    admin_password = answers.get("admin_password") or None
    generated = False
    if admin_email and not admin_password:
        admin_password = secrets.token_urlsafe(12)
        generated = True

    tid, steps = await onboard_tenant(
        name=answers["name"],
        slug=answers["slug"],
        business_description=answers["business"],
        language=answers["language"],
        persona={"role": "asistente", "tone": answers["tone"]},
        escalation_triggers=answers["escalation_triggers"],
        packs=answers["packs"],
        admin_email=admin_email,
        admin_password=admin_password,
        whatsapp=answers["whatsapp"],
    )
    if generated:
        console.print(
            f"  [green]Admin del tenant:[/green] {admin_email} / [bold]{admin_password}[/bold]"
        )
    return tid, steps


def _maybe_seed(slug: str, tenant_id: uuid.UUID) -> None:
    from core.onboarding import load_tenant_catalog

    data = load_tenant_catalog(slug)
    if not data:
        return
    if data.get("pack") != "pack_commerce":
        console.print(f"[dim]catalog.json de {slug}: pack '{data.get('pack')}' sin seed conocido[/dim]")
        return
    asyncio.run(seed_catalog(tenant_id, data))
    console.print(f"✓ [green]Catálogo importado desde {slug}/catalog.json[/green]")


# ------------------------------------------------------------------ seed
async def seed_catalog(tenant_id: uuid.UUID, data: dict) -> list[str]:
    """Importa un catálogo pack_commerce a la BD del tenant. Idempotente.

    Los productos del tenant viven en `TENANTS_ROOT/<slug>/catalog.json` (dato,
    no código). Formato: {"category_paths": [[...]], "products": [...],
    "complement_pairs": [[...],[...]]}. Las filas se miran SIEMPRE acotadas al
    tenant (defensa más allá de RLS, ADR-0018).
    """
    from pack_commerce.models import (
        Category,
        CategoryComplement,
        Inventory,
        Product,
        ProductVariant,
    )

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    steps: list[str] = []
    category_ids: dict[tuple[str, ...], uuid.UUID] = {}
    order = 0

    async def category_id(path: tuple[str, ...]) -> uuid.UUID:
        nonlocal order
        if path in category_ids:
            return category_ids[path]
        parent = await category_id(path[:-1]) if len(path) > 1 else None
        stmt = select(Category).where(
            Category.tenant_id == tenant_id,
            Category.name == path[-1],
            Category.parent_id.is_(None) if parent is None else Category.parent_id == parent,
        )
        cat = (await session.execute(stmt)).scalars().first()
        if cat is None:
            cat = Category(parent_id=parent, name=path[-1], sort_order=order, active=True)
            session.add(cat)
            await session.flush()
            order += 1
        category_ids[path] = cat.id
        return cat.id

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for path in data.get("category_paths", []):
                await category_id(tuple(path))
            await session.flush()

            for producto in data.get("products", []):
                cat = await category_id(tuple(producto["category_path"]))
                has_variants = len(producto.get("variants", [])) > 1
                base_sku = producto["variants"][0]["sku"]
                prod_stmt = (
                    select(Product)
                    .join(ProductVariant, ProductVariant.product_id == Product.id)
                    .where(ProductVariant.sku == base_sku, Product.tenant_id == tenant_id)
                )
                product = (await session.execute(prod_stmt)).scalars().first()
                if product is None:
                    product = Product(
                        name=producto["name"],
                        description=producto.get("description", ""),
                        image_url=producto.get("image_url", ""),
                        category_id=cat,
                        has_variants=has_variants,
                    )
                    session.add(product)
                    await session.flush()

                for variante in producto.get("variants", []):
                    vstmt = select(ProductVariant).where(
                        ProductVariant.sku == variante["sku"],
                        ProductVariant.tenant_id == tenant_id,
                        ProductVariant.product_id == product.id,
                    )
                    variant = (await session.execute(vstmt)).scalars().first()
                    if variant is None:
                        variant = ProductVariant(
                            product_id=product.id,
                            sku=variante["sku"],
                            attributes=variante.get("attributes", {}),
                            price=variante["price"],
                            active=True,
                        )
                        session.add(variant)
                        await session.flush()
                    existing_stock = (
                        await session.execute(
                            select(Inventory).where(
                                Inventory.variant_id == variant.id,
                                Inventory.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_stock is None:
                        session.add(
                            Inventory(
                                variant_id=variant.id,
                                stock_quantity=variante.get("stock", 0),
                                source="import",
                            )
                        )
                await session.flush()

            for path_a, path_b in data.get("complement_pairs", []):
                id_a = category_ids.get(tuple(path_a))
                id_b = category_ids.get(tuple(path_b))
                if id_a is None or id_b is None:
                    continue
                for (a, b) in ((id_a, id_b), (id_b, id_a)):
                    exists = (
                        await session.execute(
                            select(CategoryComplement).where(
                                CategoryComplement.tenant_id == tenant_id,
                                CategoryComplement.category_id == a,
                                CategoryComplement.complementary_category_id == b,
                            )
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        session.add(CategoryComplement(category_id=a, complementary_category_id=b))
            await session.commit()
    steps.append(f"catálogo sembrado ({len(data.get('products', []))} productos)")
    return steps


@app.command()
def seed(slug: str = typer.Argument(..., help="Slug del tenant (ej: formoto)")) -> None:
    """Importa TENANTS_ROOT/<slug>/catalog.json a la BD del tenant."""
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.onboarding import load_tenant_catalog

    async def _run() -> None:
        async with get_session() as session:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.slug == slug))
            ).scalar_one_or_none()
        if tenant is None:
            raise typer.BadParameter(f"Tenant no existe: {slug}. Crea el tenant primero con `tenant create`.")
        data = load_tenant_catalog(slug)
        if data is None:
            console.print(f"[yellow]No hay catalog.json para {slug} (TENANTS_ROOT/{slug}/)[/yellow]")
            return
        if data.get("pack") != "pack_commerce":
            console.print(f"[yellow]catalog.json de {slug}: pack '{data.get('pack')}' sin seed conocido[/yellow]")
            return
        steps = await seed_catalog(tenant.id, data)
        for s in steps:
            console.print(f"  · {s}")

    asyncio.run(_run())


# ------------------------------------------------------------------ list
@app.command("list")
def list_() -> None:
    """Lista los tenants del sistema con su estado y packs."""
    from core.db.models import AgentSpecRecord, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async def _run() -> None:
        async with get_session() as session:
            tenants = (
                await session.execute(select(Tenant).order_by(Tenant.created_at))
            ).scalars().all()

        table = Table(title=f"Tenants ({len(tenants)})")
        table.add_column("Slug")
        table.add_column("Nombre")
        table.add_column("Estado")
        table.add_column("Packs")
        for t in tenants:
            packs: list[str] = []
            async with tenant_scope(t.id):
                async with get_session() as session:
                    row = (
                        await session.execute(
                            select(AgentSpecRecord)
                            .where(AgentSpecRecord.tenant_id == t.id, AgentSpecRecord.is_active.is_(True))
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        packs = (row.spec or {}).get("packs", []) or []
            table.add_row(t.slug, t.name or "—", t.status, ", ".join(packs) or "—")
        console.print(table)

    asyncio.run(_run())


# ------------------------------------------------------------------ docs
@app.command()
def docs(slug: str = typer.Argument(..., help="Slug del tenant")) -> None:
    """Abre la carpeta de documentación del tenant (TENANTS_ROOT/<slug>/docs/)."""
    import subprocess
    import sys

    from core.onboarding import ensure_tenant_docs, tenant_dir

    folder = tenant_dir(slug)
    if not folder.exists():
        raise typer.BadParameter(f"No existe el directorio del tenant: {folder}")
    ensure_tenant_docs(slug, slug, "")
    console.print(f"Documentación del tenant: [bold]{folder / 'docs'}[/bold]")
    if sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", str(folder / "docs")])
    elif sys.platform == "darwin":
        subprocess.run(["open", str(folder / "docs")])
    elif sys.platform == "win32":
        subprocess.run(["explorer", str(folder / "docs")])


# ------------------------------------------------------------------ webhook
@app.command()
def webhook(slug: str = typer.Argument(..., help="Slug del tenant")) -> None:
    """Imprime la URL del webhook del tenant + verify token (para Meta)."""
    token = asyncio.run(_tenant_verify_token(slug))
    print_webhook_panel(slug, token)


def print_webhook_panel(slug: str, verify_token: str) -> None:
    """Panel con la Callback URL + verify token a registrar en Meta.

    Compartido por `tenant webhook` y `meta configure` (mismo texto, una sola vez).
    """
    from core.tenants import tenant_webhook_url

    url = tenant_webhook_url(slug)
    console.print(Panel(
        "\n".join([
            "Regístralo en Meta: WhatsApp → Configuration → Webhook:",
            f"  Callback URL:  [bold cyan]{url}[/bold cyan]",
            f"  Verify token:  [bold]{verify_token or '— (no configurado: /admin/conexiones o WHATSAPP_VERIFY_TOKEN)'}[/bold]",
            "  Fields: subscribe a [bold]messages[/bold]",
            "",
            "[dim]Desarrollo: expón un túnel y úsalo como PUBLIC_BASE_URL:[/dim]",
            "  cloudflared tunnel --url http://localhost:8001",
        ]),
        border_style="cyan",
        title=f"webhook · {slug}",
    ))


async def _tenant_verify_token(slug: str) -> str:
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.gateway.connections import effective_credentials
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
    if tenant is None:
        return ""
    async with tenant_scope(tenant.id):
        creds = await effective_credentials()
    return creds.get("verify_token", "")


# ------------------------------------------------------------------ ciclo de vida
@app.command()
def deactivate(slug: str = typer.Argument(..., help="Slug o UUID del tenant")) -> None:
    """Suspende un tenant: deja de procesar mensajes, conserva sus datos."""
    from core.tenants import deactivate_tenant

    tenant = asyncio.run(deactivate_tenant(slug))
    console.print(f"✓ [yellow]{tenant.slug} suspendido[/yellow] — no procesa mensajes (reactivar: tenant activate)")


@app.command()
def activate(slug: str = typer.Argument(..., help="Slug o UUID del tenant")) -> None:
    """Re-activa un tenant suspendido."""
    from core.tenants import activate_tenant

    tenant = asyncio.run(activate_tenant(slug))
    console.print(f"✓ [green]{tenant.slug} activo[/green] — vuelve a procesar mensajes")


@app.command()
def purge(
    slug: str = typer.Argument(..., help="Slug o UUID del tenant"),
    confirm: str = typer.Option("", "--confirm", help='Frase exacta: "ELIMINAR <slug>" (non-interactive)'),
) -> None:
    """Elimina por completo un tenant: borrado físico IRREVERSIBLE.

    Exige escribir exactamente: ELIMINAR <slug>. Borra todos los datos del tenant
    (core + packs + Redis) y la fila tenants. No se puede deshacer.
    """

    phrase = confirm
    if not phrase:
        phrase = questionary.text(
            f"[bold red]Irreversible[/bold red]. Escribe exactamente [bold]ELIMINAR {slug}[/bold] para confirmar:",
        ).ask() or ""

    async def _run() -> None:
        from core.memory.working import get_redis
        from core.tenants import purge_tenant

        await purge_tenant(slug, phrase)
        try:
            await get_redis().aclose()
        except Exception:
            pass

    try:
        asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"✓ [red]{slug} eliminado por completo[/red] (irreversible)")


@app.command()
def admins(
    all_tenants: bool = typer.Option(
        False, "--all", help="Todos los tenants (default: solo los que no tienen admin)"
    ),
    slug: str = typer.Option("", "--slug", help="Un tenant puntual"),
    include_with_admin: bool = typer.Option(
        False, "--incluir-con-admin", help="Crear aunque el tenant ya tenga admin"
    ),
    email_domain: str = typer.Option(
        "gmail.com", "--email-domain", help="Dominio del email generado (<slug>@<dominio>)"
    ),
    password_length: int = typer.Option(16, "--password-length", help="Largo de la contraseña"),
    out: str = typer.Option("", "--out", help="Ruta de CSV para volcar las credenciales"),
) -> None:
    """Crea un administrador owner por tenant y devuelve sus credenciales (ADR-0048).

    Idempotente: por defecto solo a tenants SIN admin; con --incluir-con-admin también
    a los que ya tienen. Si el email `<slug>@<dominio>` ya existe, se omite.
    """
    import csv
    import secrets

    from core.admin.auth import hash_password
    from core.db.models import Admin, Tenant, TenantUser
    from core.db.session import get_session

    async def _run() -> list[tuple[str, str, str, str]]:
        async with get_session() as session:
            query = select(Tenant).where(Tenant.deleted_at.is_(None))
            if slug:
                query = query.where(Tenant.slug == slug)
            tenants = (await session.execute(query.order_by(Tenant.created_at))).scalars().all()
            with_admin = set(
                (await session.execute(select(TenantUser.tenant_id).distinct())).scalars().all()
            )

        rows: list[tuple[str, str, str, str]] = []
        for tenant in tenants:
            if not include_with_admin and tenant.id in with_admin:
                continue
            email = f"{tenant.slug}@{email_domain}"
            async with get_session() as session:
                if (
                    await session.execute(select(Admin).where(Admin.email == email))
                ).scalar_one_or_none() is not None:
                    continue
                password = secrets.token_urlsafe(password_length)[:password_length]
                new_admin = Admin(
                    email=email, password_hash=hash_password(password), role="colaborador"
                )
                session.add(new_admin)
                await session.flush()
                session.add(TenantUser(tenant_id=tenant.id, admin_id=new_admin.id, role="owner"))
            rows.append((tenant.slug, tenant.name or "", email, password))
        return rows

    rows = asyncio.run(_run())
    if not rows:
        console.print("[yellow]Nada para crear (ya tenían admin o el email ya existía).[/yellow]")
        return
    table = Table(title="Credenciales de administradores de tenant")
    table.add_column("Tenant")
    table.add_column("Nombre")
    table.add_column("Email")
    table.add_column("Contraseña")
    for slug_, name_, email_, password_ in rows:
        table.add_row(slug_, name_, email_, password_)
    console.print(table)
    if out:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["tenant", "nombre", "email", "password"])
            writer.writerows(rows)
        console.print(f"[dim]Credenciales escritas en {out}[/dim]")
    console.print("[yellow]Guardá estas contraseñas: no se vuelven a mostrar.[/yellow]")
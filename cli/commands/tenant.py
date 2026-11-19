"""agent-core tenant: alta y operación de tenants (multi-tenant, ADR-0001).

Cada cliente es un tenant con su propio asistente (AgentSpec). El onboarding es
100% configuración: `tenant create` da de alta el tenant; `tenant seed` importa
los datos del tenant (catalog.json) que viven en TENANTS_ROOT (gitignored).

Uso:
  agent-core tenant create                # wizard interactivo paso a paso
  agent-core tenant create --non-interactive --name "Mi Bot" --packs pack_commerce
  agent-core tenant create -y --name "Mi Restaurante" --preset restaurante --plan basic
  agent-core tenant presets               # presets de negocio disponibles (ADR-0110)
  agent-core tenant seed <slug>           # importa TENANTS_ROOT/<slug>/catalog.json
  agent-core tenant list
  agent-core tenant docs <slug>
  agent-core tenant embudo --dias 30       # embudo de activación de las altas (ADR-0124)
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
    preset: str = typer.Option("", "--preset", help="Preset de negocio (ver `tenant presets`), ej: restaurante"),
    plan: str = typer.Option("", "--plan", help="Plan inicial (free, basic, premium, empresarial)"),
    sin_ejemplos: bool = typer.Option(
        False, "--sin-ejemplos", help="No cargar los datos de ejemplo del preset (el negocio trae su catálogo)"
    ),
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
            "preset": preset.strip() or None,
            "plan": plan.strip() or None,
            "sembrar_ejemplos": not sin_ejemplos,
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

    from core.presets import PresetError

    # Alta y seed en un solo event loop: el engine async queda atado al loop en el
    # que abre conexiones (dos `asyncio.run` seguidos rompen con "different loop").
    async def _alta() -> uuid.UUID:
        from core.db.session import get_engine

        try:
            tid, steps = await _onboard(answers)
            console.print("✓ [green]Onboarding completado[/green]")
            for s in steps:
                console.print(f"  · {s}")
            await _maybe_seed(answers["slug"], tid)
            return tid
        finally:
            await get_engine().dispose()

    try:
        tid = asyncio.run(_alta())
    except PresetError as exc:
        console.print(f"[red]Preset: {exc}[/red]")
        raise typer.Exit(1) from exc

    from core.tenants import tenant_webhook_url

    lines = [
        f"[bold]{answers['name']}[/bold] dado de alta.",
        f"  · tenant id: [bold]{tid}[/bold]",
        f"  · slug: [bold]{answers['slug']}[/bold]",
        "",
        "Siguientes pasos:",
        "  1. Abre el panel y cambia al tenant en el selector (http://localhost:8000/admin).",
        "  2. Configura el agente: /admin/configuracion (identidad, cerebro, memoria, habilidades).",
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

    from core.alta_negocio import onboard_tenant

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
        preset=answers.get("preset"),
        plan=answers.get("plan"),
        sembrar_ejemplos=answers.get("sembrar_ejemplos", True),
    )
    if generated:
        console.print(
            f"  [green]Admin del tenant:[/green] {admin_email} / [bold]{admin_password}[/bold]"
        )
    return tid, steps


async def _maybe_seed(slug: str, tenant_id: uuid.UUID) -> None:
    from core.onboarding import ensure_brand_voice, load_tenant_catalog

    data = load_tenant_catalog(slug)
    if data:
        if data.get("pack") != "pack_commerce":
            console.print(f"[dim]catalog.json de {slug}: pack '{data.get('pack')}' sin seed conocido[/dim]")
        else:
            await seed_catalog(tenant_id, data)
            console.print(f"✓ [green]Catálogo importado desde {slug}/catalog.json[/green]")
    await seed_pack_plugins(slug, tenant_id)
    console.print(f"✓ [green]Seed de packs completado para {slug}[/green]")
    if await ensure_brand_voice(tenant_id, slug):
        console.print(f"✓ [green]brand_voice sembrada desde {slug}/docs/voz-de-marca.md[/green]")


async def seed_pack_plugins(slug: str, tenant_id: uuid.UUID) -> list[str]:
    """Seed genérico por pack (ADR-0083, issue #154): `core.onboarding.sembrar_packs`."""
    from core.onboarding import sembrar_packs

    pasos = await sembrar_packs(tenant_id, slug)
    for paso in pasos:
        if paso.endswith(".json importado"):
            console.print(f"✓ [green]{paso} desde {slug}/[/green]")
    return pasos


# ------------------------------------------------------------------ seed
async def seed_catalog(tenant_id: uuid.UUID, data: dict) -> list[str]:
    """Importa un catálogo de pack_commerce a la BD del tenant (`core.onboarding.sembrar_catalogo`)."""
    from core.onboarding import sembrar_catalogo

    return await sembrar_catalogo(tenant_id, data)


@app.command()
def presets() -> None:
    """Lista los presets de negocio disponibles (ADR-0110)."""
    from core.presets import list_presets

    table = Table(title="Presets de negocio")
    for columna in ("id", "versión", "plan mínimo", "packs", "pasos de activación"):
        table.add_column(columna)
    for preset in list_presets():
        table.add_row(
            preset.id, str(preset.version), preset.plan_minimo,
            ", ".join(preset.packs), ", ".join(preset.pasos_de_activacion),
        )
    console.print(table)


@app.command("aplicar-preset")
def aplicar_preset(
    slug: str = typer.Argument(..., help="Slug del tenant"),
    preset: str = typer.Argument(..., help="Id del preset (ver `tenant presets`)"),
    con_ejemplos: bool = typer.Option(
        False, "--con-ejemplos", help="Cargar también los datos de ejemplo del preset (por defecto no)"
    ),
) -> None:
    """Aplica un preset a un negocio que ya existe (HU-SH-01). Idempotente: no pisa
    la config ni la persona que el negocio ya cambió (ADR-0110). Por defecto NO carga
    los datos de ejemplo: un negocio existente ya tiene su catálogo."""
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.presets import PresetError
    from core.presets.apply import apply_preset

    async def _run() -> None:
        async with get_session() as session:
            tenant = (
                await session.execute(select(Tenant).where(Tenant.slug == slug, Tenant.deleted_at.is_(None)))
            ).scalar_one_or_none()
        if tenant is None:
            raise typer.BadParameter(f"Tenant no existe: {slug}")
        try:
            resultado = await apply_preset(tenant.id, preset, sembrar_ejemplos=con_ejemplos)
        except PresetError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        for paso in resultado.pasos:
            console.print(f"  · {paso}")
        for pendiente in resultado.pendientes:
            console.print(f"  [yellow]pendiente[/yellow] {pendiente['pack']}: {pendiente['detalle']}")

    asyncio.run(_run())


@app.command()
def seed(
    slug: str = typer.Argument(..., help="Slug del tenant (ej: formoto)"),
    voz_de_marca: bool = typer.Option(
        False,
        "--voz-de-marca",
        help="Reemplaza la voz de marca guardada por docs/voz-de-marca.md (HU-SH-01).",
    ),
) -> None:
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
        for s in await seed_pack_plugins(slug, tenant.id):
            console.print(f"  · {s}")
        from core.onboarding import ensure_brand_voice

        if await ensure_brand_voice(tenant.id, slug, sobrescribir=voz_de_marca):
            console.print("  · brand_voice sembrada desde docs/voz-de-marca.md")

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


# ------------------------------------------------------------------ embudo de activación (ADR-0124)
def _duracion(segundos: int | None) -> str:
    if segundos is None:
        return "—"
    if segundos < 3600:
        return f"{segundos // 60} min"
    if segundos < 48 * 3600:
        return f"{segundos / 3600:.1f} h"
    return f"{segundos / 86400:.1f} días"


@app.command("embudo")
def embudo(
    dias: int = typer.Option(30, "--dias", min=1, max=365, help="Altas de los últimos N días"),
    solo_automaticas: bool = typer.Option(
        False, "--solo-automaticas", help="Solo los negocios del alta automática (ADR-0121)"
    ),
    sin_barrer: bool = typer.Option(
        False, "--sin-barrer", help="No evaluar los negocios antes (solo lo ya registrado)"
    ),
) -> None:
    """Embudo de activación (HU-SH-06): de las altas de la ventana, cuántas llegaron a cada
    primer paso, en cuánto tiempo (mediana desde el alta) y cuántas dentro de las 24 horas.
    Antes evalúa cada negocio, así cuenta también lo que nadie vio en el Resumen."""
    from datetime import UTC, datetime, timedelta

    async def _run():  # type: ignore[no-untyped-def]
        from core.plugin_manager import plugin_manager
        from core.primeros_pasos.hitos import embudo as _embudo

        plugin_manager.scan()  # el paso «catálogo o servicios» pregunta a cada habilidad
        hasta = datetime.now(UTC)
        return await _embudo(
            hasta - timedelta(days=dias), hasta, solo_automaticas=solo_automaticas, barrer=not sin_barrer
        )

    from core.primeros_pasos.hitos import etiqueta

    resultado = asyncio.run(_run())
    tabla = Table(
        "Paso",
        "Negocios",
        "% de las altas",
        "Mediana desde el alta",
        "En 24 h",
        title=f"Embudo de activación · {resultado.altas} altas en {dias} días",
    )
    for fila in resultado.filas:
        tabla.add_row(
            etiqueta(fila.paso),
            str(fila.negocios),
            f"{fila.porcentaje:.1f} %",
            _duracion(fila.mediana_segundos),
            str(fila.en_24_horas),
        )
    console.print(tabla)


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

    Exige que el tenant esté suspendido (`tenant deactivate <slug>`) y escribir
    exactamente: ELIMINAR <slug>. Borra todos los datos del tenant (core + packs) y
    la fila tenants y, después, su estado Redis. No se puede deshacer. Si Redis no se
    pudo limpiar, sale con código 2 y muestra el comando de reintento (`purge-redis`).
    """
    # Un solo event loop para validar, pedir la frase y purgar: el engine async de
    # SQLAlchemy (y el cliente Redis) quedan atados al loop en el que abren conexiones,
    # así que dos `asyncio.run` seguidos fallaban con "attached to a different loop".
    async def _run() -> tuple[str, bool]:
        from core.db.session import get_engine
        from core.memory.working import get_redis
        from core.tenants import ensure_purgeable, purge_tenant

        try:
            # Antes de pedir la frase: no tiene sentido confirmar algo que se va a rechazar.
            tenant_id = str((await ensure_purgeable(slug)).id)
            phrase = confirm
            if not phrase:
                phrase = await questionary.text(
                    f"[bold red]Irreversible[/bold red]. Escribe exactamente [bold]ELIMINAR {slug}[/bold] "
                    "para confirmar:",
                ).ask_async() or ""
            return tenant_id, await purge_tenant(slug, phrase)
        finally:
            try:
                await get_redis().aclose()
            except Exception:
                pass
            await get_engine().dispose()

    try:
        tenant_id, redis_limpio = asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    if not redis_limpio:
        console.print(
            f"⚠ [yellow]{slug} eliminado de la base (irreversible), pero su estado Redis no se "
            f"pudo limpiar.[/yellow] Reintenta con: agent-core tenant purge-redis {tenant_id}"
        )
        raise typer.Exit(2)
    console.print(f"✓ [red]{slug} eliminado por completo[/red] (irreversible)")


@app.command("purge-redis")
def purge_redis(
    tenant_id: str = typer.Argument(..., help="UUID del tenant YA purgado (el slug ya no existe)"),
) -> None:
    """Reintento: limpia el estado Redis de un tenant ya purgado. Idempotente.

    Úsalo cuando `tenant purge` borró la base pero avisó que Redis no se limpió
    (log `tenant_purge_redis_cleanup_failed`). Se niega si el tenant todavía existe.
    """
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError as exc:
        console.print(f"[red]✗ No es un UUID válido: {tenant_id}[/red]")
        raise typer.Exit(1) from exc

    async def _run() -> int:
        from core.memory.working import get_redis
        from core.tenants import purge_tenant_redis

        try:
            return await purge_tenant_redis(tid)
        finally:
            try:
                await get_redis().aclose()
            except Exception:
                pass

    try:
        borradas = asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"✓ Redis del tenant {tid} limpio ({borradas} claves borradas)")


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

@app.command()
def memoria_purgar(
    slug: str = typer.Option(..., "--slug", help="Slug del tenant"),
    email: str = typer.Option("", "--email", help="Email del cliente a olvidar (opcional)"),
    conversation: str = typer.Option("", "--conversation", help="UUID de conversación (opcional)"),
    customer: str = typer.Option("", "--customer-id", help="UUID del cliente (cualquier canal, p. ej. WhatsApp)"),
) -> None:
    """Olvida la memoria de un cliente o de una conversación (ADR-0053, ADR-0114 §4).

    Borrado LÓGICO (invariante #8) del tenant indicado. Con `--email` (solo): los recuerdos
    de `memory_vectors`, los hechos de `memory_facts`, su perfil en `customers.attributes` y la
    memoria de trabajo de todas sus conversaciones. Con `--conversation`: los recuerdos y la
    memoria de trabajo de esa conversación. Sus mensajes y tareas quedan (registro del negocio).
    """
    import uuid as _uuid

    from sqlalchemy import select

    from core.db.models import Customer, Tenant
    from core.db.session import get_session

    async def _run() -> int:
        from core.memory.vector import purge_memory
        from core.shared.tenant import tenant_scope

        async with get_session() as session:
            tenant_id = (
                await session.execute(select(Tenant.id).where(Tenant.slug == slug))
            ).scalar_one_or_none()
        if tenant_id is None:
            console.print(f"[red]Tenant no encontrado:[/red] {slug}")
            raise typer.Exit(1)

        async with tenant_scope(tenant_id):
            customer_id = _uuid.UUID(customer) if customer else None
            if customer_id is not None:
                async with get_session() as session:
                    existe = (
                        await session.execute(
                            select(Customer.id).where(Customer.tenant_id == tenant_id, Customer.id == customer_id)
                        )
                    ).scalar_one_or_none()
                if existe is None:
                    console.print(f"[red]Cliente no encontrado:[/red] {customer}")
                    raise typer.Exit(1)
            elif email:
                async with get_session() as session:
                    encontrados = (
                        await session.execute(
                            select(Customer.id).where(
                                Customer.tenant_id == tenant_id,
                                Customer.external_id == email,
                                Customer.deleted_at.is_(None),
                            )
                        )
                    ).scalars().all()
                if len(encontrados) > 1:
                    console.print("[red]Hay varios clientes con ese email (uno por canal): usá --customer-id[/red]")
                    raise typer.Exit(1)
                customer_id = encontrados[0] if encontrados else None
                if customer_id is None:
                    console.print(f"[red]Cliente no encontrado:[/red] {email}")
                    raise typer.Exit(1)
            conv_id = _uuid.UUID(conversation) if conversation else None
            return await purge_memory(customer_id=customer_id, conversation_id=conv_id)

    if not (email or conversation or customer):
        console.print("[red]Indicá --customer-id, --email o --conversation[/red]")
        raise typer.Exit(1)
    n = asyncio.run(_run())
    console.print(f"✓ [green]{n} registros de memoria olvidados[/green] (recuerdos y hechos) para {slug}")


@app.command()
def memoria_importar_perfil() -> None:
    """Copia como hechos `perfil` las claves de `customers.attributes` sin hecho (HU-AOS-36).

    Para correr una vez después del deploy de la migración 0080: completa lo que el código
    anterior escribió en `attributes` mientras corría el deploy. Idempotente, todos los negocios.
    """
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async def _run() -> int:
        from core.memory import facts
        from core.shared.tenant import tenant_scope

        async with get_session() as session:
            tenant_ids = (await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))).scalars().all()
        total = 0
        for tid in tenant_ids:
            async with tenant_scope(tid):
                while True:
                    async with get_session() as session:
                        n = await facts.importar_perfil(session, tenant_id=tid)
                    if not n:
                        break
                    total += n
        return total

    n = asyncio.run(_run())
    console.print(f"✓ [green]{n} claves del perfil copiadas como hechos[/green]")


# ------------------------------------------------------------------ altas automáticas (ADR-0121)
def _uuid_del_vendedor(valor: str) -> uuid.UUID:
    try:
        return uuid.UUID(valor)
    except ValueError as exc:
        console.print("[red]✗ El vendedor va por UUID (igual que PROVISIONING_SELLER_TENANTS).[/red]")
        raise typer.Exit(1) from exc


@app.command("altas")
def altas(
    vendedor: str = typer.Argument(..., help="UUID del negocio vendedor"),
    estado: str = typer.Option("revision", "--estado", help="en_curso | completado | revision | omitido | todas"),
) -> None:
    """Altas automáticas de negocios de un vendedor (por defecto, las que están en revisión)."""
    vendedor_id = _uuid_del_vendedor(vendedor)

    async def _run() -> list:
        from core.db.models import ProvisioningRequest
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        async with tenant_scope(vendedor_id):
            async with get_session() as session:
                consulta = select(ProvisioningRequest).where(
                    ProvisioningRequest.tenant_id == vendedor_id,
                    ProvisioningRequest.deleted_at.is_(None),
                )
                if estado != "todas":
                    consulta = consulta.where(ProvisioningRequest.estado == estado)
                return list(
                    (await session.execute(consulta.order_by(ProvisioningRequest.created_at.desc()).limit(200)))
                    .scalars()
                    .all()
                )

    filas = asyncio.run(_run())
    tabla = Table("Referencia", "Estado", "Intentos", "Negocio", "Motivo", "Creada")
    for fila in filas:
        tabla.add_row(
            fila.referencia,
            fila.estado,
            str(fila.intentos),
            fila.negocio_slug or "—",
            (fila.motivo or "")[:80],
            fila.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(tabla)


@app.command("reintentar-alta")
def reintentar_alta(
    vendedor: str = typer.Argument(..., help="UUID del negocio vendedor"),
    referencia: str = typer.Argument(..., help="Referencia del alta (número del pedido)"),
) -> None:
    """Vuelve a correr un alta en revisión, después de corregir lo que faltaba. Retoma el
    mismo negocio (id y slug deterministas). Runbook: docs/runbooks/altas-en-revision.md."""
    vendedor_id = _uuid_del_vendedor(vendedor)

    async def _run():  # type: ignore[no-untyped-def]
        from core.plugin_manager import plugin_manager
        from core.provisioning.consumer import reintentar_alta as _reintentar

        # La fuente de los datos del pedido la registra commerce al activarse.
        await plugin_manager.ensure_process_activation()
        return await _reintentar(vendedor_id, referencia)

    try:
        fila = asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"✓ Alta {referencia}: [bold]{fila.estado}[/bold] {fila.motivo or ''}")


@app.command("aprobar-alta")
def aprobar_alta(
    vendedor: str = typer.Argument(..., help="UUID del negocio vendedor"),
    referencia: str = typer.Argument(..., help="Referencia del alta (número del pedido)"),
) -> None:
    """Aprueba un alta en revisión después de revisarla: corre sin los controles de riesgo
    que la frenaron (HU-SH-07). Runbook: docs/runbooks/altas-en-revision.md."""
    vendedor_id = _uuid_del_vendedor(vendedor)

    async def _run():  # type: ignore[no-untyped-def]
        from core.plugin_manager import plugin_manager
        from core.provisioning.consumer import aprobar_y_procesar

        await plugin_manager.ensure_process_activation()
        return await aprobar_y_procesar(vendedor_id, referencia, por="cli")

    try:
        fila = asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"✓ Alta {referencia} aprobada: [bold]{fila.estado}[/bold] {fila.motivo or ''}")


@app.command("interruptor-altas")
def interruptor_altas(
    vendedor: str = typer.Argument(..., help="UUID del negocio vendedor"),
    pausar: bool | None = typer.Option(None, "--pausar/--reanudar", help="Pausa o reanuda el alta automática"),
    motivo: str = typer.Option("", "--motivo", help="Por qué (obligatorio al pausar)"),
) -> None:
    """Interruptor del alta automática (HU-SH-07). Pausada, cada alta nueva queda en revisión.
    Sin --pausar ni --reanudar, muestra el estado."""
    vendedor_id = _uuid_del_vendedor(vendedor)
    from core.provisioning.vendedores import vendedores_configurados

    if vendedor_id not in vendedores_configurados():
        console.print("[red]✗ Ese negocio no está en PROVISIONING_SELLER_TENANTS.[/red]")
        raise typer.Exit(1)
    if pausar and not motivo.strip():
        console.print("[red]✗ Indica --motivo al pausar.[/red]")
        raise typer.Exit(1)

    async def _run() -> dict:
        from core.provisioning import interruptor

        if pausar is None:
            return await interruptor.estado(vendedor_id)
        return await interruptor.fijar(vendedor_id, pausar=pausar, motivo=motivo, por="cli")

    try:
        estado = asyncio.run(_run())
    except LookupError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    texto = "[red]pausada[/red]" if estado["pausado"] else "[green]activa[/green]"
    console.print(f"Alta automática {texto} {estado.get('motivo') or ''}")


@app.command("descartar-alta")
def descartar_alta(
    vendedor: str = typer.Argument(..., help="UUID del negocio vendedor"),
    referencia: str = typer.Argument(..., help="Referencia del alta (número del pedido)"),
    motivo: str = typer.Option(..., "--motivo", help="Por qué se descarta (queda en el alta y en audit_log)"),
) -> None:
    """Cierra un alta en revisión sin crear el negocio (queda `omitido` con el motivo)."""
    vendedor_id = _uuid_del_vendedor(vendedor)

    async def _run():  # type: ignore[no-untyped-def]
        from core.provisioning.consumer import descartar_alta as _descartar

        return await _descartar(vendedor_id, referencia, motivo)

    try:
        fila = asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"✓ Alta {referencia} descartada: {fila.motivo}")

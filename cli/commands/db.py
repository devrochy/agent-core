"""agent-core db: migraciones y seed."""

from __future__ import annotations

import subprocess
import sys

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def migrate() -> None:
    """Corre las migraciones del core + las de todos los plugins instalados."""
    from core.db.migrations import upgrade_all

    results = upgrade_all()
    console.print("✓ [green]Migraciones del core aplicadas[/green]")

    failed = False
    for plugin_id, status in results.items():
        if status.startswith("error:"):
            failed = True
            console.print(f"[red]✗ {plugin_id}: {status[7:][:150]}[/red]")
        elif status == "aplicadas":
            console.print(f"✓ [green]{plugin_id}: migraciones aplicadas[/green]")
        else:
            console.print(f"[dim]· {plugin_id}: sin migraciones propias[/dim]")

    if failed:
        console.print("\n[red]Alguna migración de plugin falló (ver arriba).[/red]")
        raise typer.Exit(1)


@app.command()
def seed() -> None:
    """Crea el usuario admin por defecto y la fila de configuración (si no existen)."""
    subprocess.run([sys.executable, "scripts/init_db.py"], check=True)


@app.command("backfill-conexiones")
def backfill_conexiones(
    dry_run: bool = typer.Option(False, "--dry-run", help="No escribe: reporta lo que haría y los choques."),
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    lote: int = typer.Option(500, "--lote", min=1, help="Filas por transacción."),
) -> None:
    """Conexión canónica (ADR-0115): una conexión por número vivo de WhatsApp, el chat web como
    conexión `webchat`, `type_id` en las conexiones viejas y `connection_id` en las conversaciones. Idempotente; correr primero con
    `--dry-run` y revisar los choques con conexiones manuales del mismo número."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.gateway.conexion_canonica_backfill import ReporteBackfill
    from core.gateway.conexion_canonica_backfill import backfill_conexiones as correr

    async def _run() -> ReporteBackfill:
        tenant_id = await resolve_tenant(tenant) if tenant else None
        return await correr(dry_run=dry_run, tenant_id=tenant_id, lote=lote)

    reporte = asyncio.run(_run())
    tabla = Table(title="Backfill de conexiones" + (" (dry-run: no se escribió nada)" if dry_run else ""))
    tabla.add_column("Concepto")
    tabla.add_column("Cantidad", justify="right")
    filas = [
        ("Negocios", reporte.negocios),
        ("Conexiones viejas con tipo puesto", reporte.tipos_puestos),
        ("Conexiones sin tipo resoluble", reporte.sin_tipo_resoluble),
        *((f"Números: {k}", v) for k, v in sorted(reporte.numeros.items())),
        ("Conversaciones ligadas a su conexión", reporte.conversaciones_ligadas),
        ("Conversaciones ambiguas (cuenta con varios números)", reporte.conversaciones_ambiguas),
        ("Conversaciones sin conexión (cuenta sin números vivos)", reporte.conversaciones_sin_conexion),
        *((f"Chat web: {k}", v) for k, v in sorted(reporte.chat_web.items())),
        ("Conversaciones web ligadas a su widget", reporte.conversaciones_web_ligadas),
        ("Conversaciones web sin conexión (sin widget o con varios)", reporte.conversaciones_web_sin_conexion),
        ("Abiertas que quedan sin conexión (revisar antes de AOS_CONNECTION_ROUTING)", reporte.abiertas_sin_ligar),
    ]
    for concepto, cantidad in filas:
        tabla.add_row(concepto, str(cantidad))
    console.print(tabla)
    for choque in reporte.choques:
        console.print(
            f"[yellow]Choque:[/yellow] negocio {choque.tenant_id}, número {choque.phone_number_id} "
            "ya tiene una conexión manual: no se crea otra."
        )


@app.command("sync-packs")
def sync_packs(
    database_url: str | None = typer.Option(
        None, "--database-url", help="Por defecto MIGRATIONS_DATABASE_URL (el rol dueño)."
    ),
) -> None:
    """Agent OS (HU-AOS-14): registra la versión de cada habilidad del binario en `pack_versions`
    con `register_pack_version()` (recalcula el hash en la base). Idempotente. Corre en el
    arranque del contenedor `api` después de migrar; si la misma versión llega con otro
    contenido, alerta y no la registra. Sale con 1 si hubo algo que alertar (el entrypoint no
    frena el arranque por eso)."""
    import asyncio

    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs as correr

    reporte = asyncio.run(correr(plugin_manager.scan(), database_url=database_url))
    for resultado, cantidad in sorted(reporte.resultados.items()):
        console.print(f"{resultado}: {cantidad}")
    for error in reporte.errores:
        console.print(f"[red]Error:[/red] {error}")
    if reporte.fallo:
        raise typer.Exit(1)


@app.command("backfill-releases")
def backfill_releases(
    dry_run: bool = typer.Option(False, "--dry-run", help="No escribe: cuenta las conexiones que recibirían su release 1."),
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
) -> None:
    """Agent OS (HU-AOS-14): la release 1 de cada conexión conversacional que no tiene ninguna,
    construida desde el AgentSpec v2 activo (refresca el Coordinador y el General con el backfill
    de agentes) y los artefactos del disco, y promovida. Idempotente. Exige `sync-packs` antes.
    No cambia la conducta: con `aos_runtime = legacy` el runtime ignora la release."""
    import asyncio

    from cli.tenant_ref import resolve_tenant
    from core.plugin_manager import plugin_manager
    from core.releases.release_uno import ReporteReleases
    from core.releases.release_uno import backfill_releases as correr

    async def _run() -> ReporteReleases:
        tenant_id = await resolve_tenant(tenant) if tenant else None
        return await correr(dry_run=dry_run, tenant_id=tenant_id, modules=plugin_manager.scan())

    reporte = asyncio.run(_run())
    console.print(f"Negocios: {reporte.negocios}" + (" (dry-run: no se escribió nada)" if dry_run else ""))
    for concepto, cantidad in sorted(reporte.conexiones.items()):
        console.print(f"Conexiones {concepto}: {cantidad}")
    for problema in reporte.errores:
        donde = f" conexión {problema.connection_id}" if problema.connection_id else ""
        console.print(f"[red]Error:[/red] negocio {problema.tenant_id}{donde}: {problema.mensaje}")
    if reporte.fallo:
        raise typer.Exit(1)


@app.command("backfill-agentes")
def backfill_agentes(
    dry_run: bool = typer.Option(False, "--dry-run", help="No escribe: reporta lo que haría."),
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    lote: int = typer.Option(500, "--lote", min=1, help="Conexiones por transacción."),
) -> None:
    """Agent OS, paso B2 (HU-AOS-13): por negocio, el Coordinador y el General (desde el
    AgentSpec v2 activo) con sus versiones y una instancia de cada uno en cada conexión
    conversacional. Idempotente. Exige el backfill de conexiones (`backfill-conexiones`)
    hecho antes; correr primero con `--dry-run`. No cambia la conducta: el runtime sigue en
    `aos_runtime = legacy`."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.agents.backfill_agentes import ReporteAgentes
    from core.agents.backfill_agentes import backfill_agentes as correr
    from core.plugin_manager import plugin_manager

    async def _run() -> ReporteAgentes:
        tenant_id = await resolve_tenant(tenant) if tenant else None
        return await correr(dry_run=dry_run, tenant_id=tenant_id, lote=lote, modules=plugin_manager.scan())

    reporte = asyncio.run(_run())
    tabla = Table(title="Backfill de agentes" + (" (dry-run: no se escribió nada)" if dry_run else ""))
    tabla.add_column("Concepto")
    tabla.add_column("Cantidad", justify="right")
    filas = [
        ("Negocios", reporte.negocios),
        ("Negocios migrados", reporte.migrados),
        ("Negocios bloqueados (falta el backfill de conexiones)", len(reporte.bloqueados)),
        ("Negocios con error", len(reporte.errores)),
        ("AgentSpec por defecto sembrado", reporte.specs_sembrados),
        *((f"Agentes: {k}", v) for k, v in sorted(reporte.agentes.items())),
        *((f"Versiones: {k}", v) for k, v in sorted(reporte.versiones.items())),
        *((f"Instancias: {k}", v) for k, v in sorted(reporte.instancias.items())),
        *((f"Secretos: {k}", v) for k, v in sorted(reporte.secretos.items())),
        ("Conexiones conversacionales", reporte.conexiones_conversacionales),
        ("Conexiones sin agentes (integraciones o sin tipo)", reporte.conexiones_sin_agentes),
        ("Choques con una conexión manual del mismo número", reporte.choques_manuales),
        ("Avisos de la conversión (ver el log)", reporte.avisos),
    ]
    for concepto, cantidad in filas:
        tabla.add_row(concepto, str(cantidad))
    console.print(tabla)
    for problema in reporte.bloqueados:
        console.print(f"[yellow]Bloqueado:[/yellow] negocio {problema.tenant_id}: {problema.mensaje}")
    for problema in reporte.errores:
        console.print(f"[red]Error:[/red] negocio {problema.tenant_id}: {problema.mensaje}")
    if reporte.fallo:
        raise typer.Exit(1)

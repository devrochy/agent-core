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
    """Conexión canónica (ADR-0115): una conexión por número vivo de WhatsApp, `type_id` en las
    conexiones viejas y `connection_id` en las conversaciones. Idempotente; correr primero con
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
        ("Negocios con chat web activo sin conexión (aún no es tipo de conexión)", reporte.chat_web_sin_conexion),
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

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


@app.command("migrar-a-v3")
def migrar_a_v3(
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    todos: bool = typer.Option(False, "--todos", help="Todos los negocios vivos."),
    dry_run: bool = typer.Option(False, "--dry-run", help="No escribe: reporta precondiciones, bloqueos y avisos."),
    volver: bool = typer.Option(False, "--volver", help="Devuelve el negocio a `aos_runtime = legacy`."),
    construir: bool = typer.Option(
        False, "--construir", help="Si la release activa está desactualizada, construye y promueve una nueva."
    ),
    apagar_procedimientos: bool = typer.Option(
        False, "--apagar-procedimientos", help="Con --volver: apaga también `aos_procedures` si estaba encendida."
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Para escribir las banderas; por defecto MIGRATIONS_DATABASE_URL (el rol dueño)."
    ),
) -> None:
    """Agent OS (HU-AOS-15): enciende `tenants.aos_runtime = 'v3'` (nunca `aos_procedures`) en un
    negocio listo: backfills de conexiones, agentes y releases hechos, `AOS_CONNECTION_ROUTING`
    encendida y la release activa de cada conexión al día y ejecutable. Un negocio que no cumple
    queda bloqueado con el motivo. Corre primero con `--dry-run`. Precondición de producto
    (runbook del PR): el pass^4 del negocio medido antes; si después de migrar empeora, `--volver`."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.plugin_manager import plugin_manager
    from core.releases.migracion_v3 import ReporteMigracion
    from core.releases.migracion_v3 import migrar_a_v3 as correr

    if bool(tenant) == todos:
        raise typer.BadParameter("Indica --tenant <slug> o --todos (uno de los dos).")
    if construir and todos:
        raise typer.BadParameter("--construir promueve releases: solo con --tenant, nunca con --todos.")

    async def _run() -> ReporteMigracion:
        ids = [await resolve_tenant(tenant)] if tenant else None
        return await correr(
            tenant_ids=ids,
            dry_run=dry_run,
            volver=volver,
            construir=construir,
            apagar_procedimientos=apagar_procedimientos,
            modules=plugin_manager.scan(),
            database_url=database_url,
        )

    reporte = asyncio.run(_run())
    titulo = "Migración a v3" if not volver else "Vuelta a legacy"
    tabla = Table(title=titulo + (" (dry-run: no se escribió nada)" if dry_run else ""))
    for columna in ("Negocio", "Antes", "Acción", "Conexiones"):
        tabla.add_column(columna)
    for n in reporte.negocios:
        antes = n.runtime_antes + (" + procedimientos" if n.procedimientos else "")
        tabla.add_row(n.slug, antes, n.accion, str(n.conexiones))
    console.print(tabla)
    for n in reporte.negocios:
        for aviso in n.avisos:
            console.print(f"[yellow]Aviso:[/yellow] {n.slug}: {aviso}")
        for bloqueo in n.bloqueos:
            console.print(f"[red]Bloqueado:[/red] {n.slug}: {bloqueo}")
    if reporte.fallo:
        raise typer.Exit(1)


@app.command("contract-aos")
def contract_aos(
    aplicar: bool = typer.Option(False, "--aplicar", help="Aplica el contract. Sin esta opción solo reporta (en seco)."),
    confirmo: bool = typer.Option(
        False, "--confirmo-sin-vuelta-atras", help="Con --aplicar: confirma que después no hay camino de vuelta."
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Solo en seco: revisa si ESTE negocio está listo (el contract es global)."
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Para el DDL; por defecto MIGRATIONS_DATABASE_URL (el rol dueño)."
    ),
) -> None:
    """Agent OS (HU-AOS-15, C1): cierra la migración. Deduplica las conversaciones abiertas repetidas
    por (conexión, cliente) sin cerrarlas, crea el índice único, vuelve `conversations.connection_id`
    NOT NULL, exige conexión y release en `core_decisions` y valida lo que el expand dejó `NOT VALID`.
    **Punto sin retorno.** Solo aplica con TODOS los negocios en `v3`, `AOS_CONNECTION_ROUTING` encendida,
    ninguna conversación sin conexión y 2 releases estables sin rollback por conexión. Corre primero en
    seco y sigue el runbook `docs/runbooks/migracion-agent-os.md`."""
    import asyncio

    from cli.tenant_ref import resolve_tenant
    from core.releases.contrato import ReporteContrato, contraer

    if tenant and aplicar:
        raise typer.BadParameter("--tenant solo vale en seco: el contract es global.")

    async def _run() -> ReporteContrato:
        ids = [await resolve_tenant(tenant)] if tenant else None
        return await contraer(dry_run=not aplicar, confirmado=confirmo, database_url=database_url, tenant_ids=ids)

    reporte = asyncio.run(_run())
    titulo = "Contract del Agent OS" + (" (en seco: no se escribió nada)" if reporte.dry_run else "")
    console.print(f"[bold]{titulo}[/bold] · negocios: {reporte.negocios}")
    for paso in reporte.pasos:
        console.print(f"  · {paso}")
    for validada in reporte.validadas:
        console.print(f"    validada: {validada}")
    for aviso in reporte.avisos:
        console.print(f"[yellow]Aviso:[/yellow] {aviso}")
    for bloqueo in reporte.bloqueos:
        console.print(f"[red]Bloqueado:[/red] {bloqueo}")
    if reporte.aplicado:
        console.print("✓ [green]Contract aplicado.[/green] Desde aquí no se apaga AOS_CONNECTION_ROUTING.")
    if reporte.bloqueos or reporte.fallo:
        raise typer.Exit(1)


@app.command("ruteo")
def ruteo(
    tenant: str = typer.Option(..., "--tenant", help="El negocio (slug o UUID)."),
    modo: str = typer.Option(..., "--modo", help="off | shadow | active"),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Por defecto MIGRATIONS_DATABASE_URL (el rol dueño)."
    ),
) -> None:
    """Agent OS (HU-AOS-17): fija `tenants.aos_routing` del negocio. `shadow` decide y registra la
    ruta en `core_decisions.shadow_route` sin cambiar quién atiende; `active` enruta. Exige
    `aos_runtime = 'v3'`. Runbook: una semana en `shadow` y `reporte-ruteo` antes de `active`."""
    import asyncio

    from cli.tenant_ref import resolve_tenant
    from core.kernel.banderas import BanderasRechazadas
    from core.kernel.ruteo_sombra import escribir_modo

    async def _run() -> None:
        await escribir_modo(await resolve_tenant(tenant), modo, database_url)

    try:
        asyncio.run(_run())
    except (ValueError, LookupError, PermissionError, BanderasRechazadas) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None
    console.print(f"aos_routing = {modo}")


@app.command("reporte-ruteo")
def reporte_ruteo(
    tenant: str = typer.Option(..., "--tenant", help="El negocio (slug o UUID)."),
    dias: int = typer.Option(7, "--dias", min=1, max=31, help="Ventana en días."),
) -> None:
    """Agent OS (HU-AOS-17): precisión y recall por procedimiento del modo sombra frente a lo que
    de verdad se inició. Meta de R3: ≥ 0,90 / ≥ 0,85 durante 7 días."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.kernel.ruteo_sombra import Reporte, reporte_de_sombra
    from core.shared.tenant import tenant_scope

    async def _run() -> Reporte:
        tenant_id = await resolve_tenant(tenant)
        async with tenant_scope(tenant_id):
            return await reporte_de_sombra(tenant_id, dias)

    reporte = asyncio.run(_run())
    tabla = Table(title=f"Ruteo en sombra: {reporte.turnos} turnos en {dias} días")
    for columna in ("Procedimiento", "Predichos", "Reales", "Aciertos", "Precisión", "Recall"):
        tabla.add_column(columna)

    def _pct(valor: float | None) -> str:
        return "-" if valor is None else f"{valor:.2f}"

    for m in reporte.metricas:
        tabla.add_row(m.procedure_id, str(m.predichos), str(m.reales), str(m.verdaderos), _pct(m.precision), _pct(m.recall))
    console.print(tabla)


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


@app.command("presets-v2")
def presets_v2(
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    todos: bool = typer.Option(False, "--todos", help="Todos los negocios vivos con aos_runtime = v3."),
    preset: str | None = typer.Option(
        None, "--preset", help="Preset a usar en vez del registrado en el negocio (solo con --tenant)."
    ),
    aplicar: bool = typer.Option(False, "--aplicar", help="Escribe. Sin esto es dry-run: reporta lo que haría."),
) -> None:
    """Agent OS (HU-AOS-46, ADR-0139): presets v2 para negocios existentes. Crea los agentes
    especialistas del preset (Ventas, Agenda, Cobranza, Suscripciones) que falten, con su
    instancia en cada conexión (nunca por defecto), agrega las reglas del Coordinador y construye
    la release candidata de cada conexión que cambió, SIN activarla. Idempotente: no duplica
    agentes, no pisa lo que el dueño cambió y no devuelve instancias quitadas. Solo actúa con
    `AOS_CONNECTION_ROUTING` encendida y `aos_runtime = 'v3'`. Dry-run por defecto."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.plugin_manager import plugin_manager
    from core.presets import PresetError
    from core.presets.agentes import ReportePresetV2, backfill_presets_v2

    if bool(tenant) == todos:
        raise typer.BadParameter("Indica --tenant <slug> o --todos (uno de los dos).")
    if preset and todos:
        raise typer.BadParameter("--preset solo con --tenant.")

    async def _run() -> list[ReportePresetV2]:
        ids = [await resolve_tenant(tenant)] if tenant else None
        return await backfill_presets_v2(
            tenant_ids=ids, preset_id=preset, aplicar=aplicar, modules=plugin_manager.scan()
        )

    try:
        reportes = asyncio.run(_run())
    except PresetError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None
    tabla = Table(title="Presets v2" + ("" if aplicar else " (dry-run: no se escribió nada)"))
    for columna in ("Negocio", "Preset", "Omitido", "Agentes", "Reglas nuevas", "Releases candidatas"):
        tabla.add_column(columna)
    for r in reportes:
        tabla.add_row(
            str(r.tenant_id),
            f"{r.preset} v{r.version}" if r.preset else "-",
            r.omitido or "",
            ", ".join(f"{a.key}={a.estado}" for a in r.agentes),
            ", ".join(r.reglas_nuevas),
            ", ".join(f"{c[:8]}…→{n}" for c, n in sorted(r.releases.items())),
        )
    console.print(tabla)
    for r in reportes:
        for aviso in [*r.avisos, *(f"{a.key}: {x}" for a in r.agentes for x in a.avisos)]:
            console.print(f"[yellow]Aviso:[/yellow] {r.tenant_id}: {aviso}")
        for error in r.errores:
            console.print(f"[red]Error:[/red] {r.tenant_id}: {error}")
    if any(r.fallo for r in reportes):
        raise typer.Exit(1)

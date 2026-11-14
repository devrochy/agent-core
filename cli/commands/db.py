"""agent-core db: migraciones y seed."""

from __future__ import annotations

import subprocess
import sys

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def migrate(
    eval_: bool = typer.Option(
        False,
        "--eval",
        help="Migra la base `agentcore_eval` del gate de pass^k (EVAL_*_DATABASE_URL), no la del negocio.",
    ),
) -> None:
    """Corre las migraciones del core + las de todos los plugins instalados."""
    if eval_:
        _migrar_eval()
        return
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


def _migrar_eval() -> None:
    """Migra `agentcore_eval` (HU-AOS-21 parte 3b) en un proceso aparte con SUS URLs: nunca la del negocio."""
    import os
    import re

    from sqlalchemy.engine import make_url

    from core.config import get_settings

    settings = get_settings()
    runtime, dueno = settings.eval_database_url, settings.eval_migrations_database_url
    if not runtime or not dueno:
        console.print("[red]EVAL_DATABASE_URL y EVAL_MIGRATIONS_DATABASE_URL son obligatorias con --eval.[/red]")
        raise typer.Exit(1)
    negocio = (make_url(settings.database_url).database or "").lower()
    for url in (runtime, dueno):
        nombre = (make_url(url).database or "").lower()
        if not re.match(r"^agentcore_eval(_[a-z0-9]{1,20})?$", nombre) or nombre == negocio:
            console.print("[red]La base de eval debe llamarse agentcore_eval y no ser la del negocio.[/red]")
            raise typer.Exit(1)
    entorno = {**os.environ, "DATABASE_URL": runtime, "MIGRATIONS_DATABASE_URL": dueno}
    resultado = subprocess.run([sys.executable, "-m", "cli.main", "db", "migrate"], env=entorno, check=False)
    if resultado.returncode != 0:
        raise typer.Exit(resultado.returncode)


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
        ("Conversaciones de prueba ligadas a la conexión interna", reporte.conversaciones_prueba_ligadas),
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
    de agentes) y los artefactos del disco, y promovida. Idempotente. Exige `sync-packs` antes."""
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


@app.command("evaluar-release")
def evaluar_release(
    tenant: str = typer.Argument(..., help="Negocio (slug o UUID)."),
    connection: str = typer.Option(..., "--connection", help="UUID de la conexión."),
    release: str = typer.Option(..., "--release", help="UUID de la release candidata."),
    grabacion: str = typer.Option("", "--grabacion", help="`replay`: desde evals/cassettes, sin LLM. Vacío: LLM real."),
    confirmo_costo: bool = typer.Option(
        False, "--confirmo-costo", help="Obligatorio con el LLM real: la corrida cuesta (escenarios por 4 intentos)."
    ),
) -> None:
    """Gate de pass^k (HU-AOS-21 parte 3b): mide la candidata en `agentcore_eval` y deja el veredicto.

    Es la vía manual del corredor, sin worker. Con el LLM real gasta dinero de la plataforma (cuenta
    contra el tope mensual del negocio): hay que pasar `--confirmo-costo`. No promueve nada."""
    import asyncio
    import uuid as _uuid

    if grabacion not in ("", "replay"):
        raise typer.BadParameter("--grabacion solo admite `replay`.")
    if not grabacion and not confirmo_costo:
        console.print("[red]Con el LLM real hay que pasar --confirmo-costo (o usar --grabacion replay).[/red]")
        raise typer.Exit(2)
    from cli.tenant_ref import resolve_tenant
    from core.releases.corredor import ResultadoMedicion, medir
    from core.shared.tenant import tenant_scope

    async def _run() -> ResultadoMedicion:
        tid = await resolve_tenant(tenant)
        async with tenant_scope(tid):
            return await medir(
                tid, _uuid.UUID(connection), _uuid.UUID(release), actor="cli:evaluar-release", grabacion=grabacion or None
            )

    r = asyncio.run(_run())
    console.print(f"Estado: {r.estado}" + (f" — {r.detalle}" if r.detalle else ""))
    for pid, datos in sorted(r.por_procedimiento.items()):
        console.print(f"  {pid}: pass^4 {datos['pass_k']} (activa {datos['activa']}) {'ok' if datos['ok'] else 'FALLA'}")
    if r.sin_escenarios or r.no_medibles:
        console.print(f"  sin escenarios: {r.sin_escenarios}; no medibles sin ruteo: {r.no_medibles}")
    console.print(f"Costo: {r.costo_usd:.4f} USD")
    if r.estado not in ("aprobada", "rechazada", "exento", "sin_cambios", "ya_medida", "sin_escenarios"):
        raise typer.Exit(1)


@app.command("auditar-referencias")
def auditar_referencias(
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    todos: bool = typer.Option(False, "--todos", help="Todos los negocios vivos."),
) -> None:
    """Agent OS (HU-AOS-59): lista las acciones y procedimientos que el binario ya no trae y que
    todavía nombran la última versión publicada de un agente o la release activa de una conexión
    (borradores y conexiones borradas, de pruebas o de integración, aparte). Solo lectura. Exit
    code distinto de cero si hay afectados: córrelo con `--todos` tras migrar la base de cada
    despliegue (runbook `docs/runbooks/referencias-retiradas.md`)."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.plugin_manager import plugin_manager
    from core.releases.referencias_retiradas import Fila, ReporteAuditoria, auditar

    if bool(tenant) == todos:
        raise typer.BadParameter("Indica --tenant <slug> o --todos (uno de los dos).")

    async def _run() -> ReporteAuditoria:
        ids = [await resolve_tenant(tenant)] if tenant else None
        return await auditar(tenant_ids=ids, modules=plugin_manager.scan())

    reporte = asyncio.run(_run())
    resumen = Table(title="Referencias retiradas (solo lectura: no se escribió nada)")
    for columna in ("Negocio", "Agentes afectados", "Releases afectadas", "Borradores", "Aparte"):
        resumen.add_column(columna)
    for n in reporte.negocios:
        resumen.add_row(
            n.slug if not n.error else f"{n.slug} (ERROR)",
            str(n.agentes_afectados),
            str(n.releases_afectadas),
            str(len({f.agente for f in n.borradores})),
            str(len(n.aparte)),
        )
    console.print(resumen)

    def _detalle(titulo: str, filas: list[Fila]) -> None:
        tabla = Table(title=titulo)
        for columna in ("Ámbito", "Agente", "Versión", "Ruta", "Código", "Nombre", "Qué hará migrar-referencias", "Dónde corre"):
            tabla.add_column(columna)
        for f in filas:
            tabla.add_row(
                f.ambito, f.agente, str(f.version or "—"), f.ruta, f.codigo, f.nombre, f.destino, "\n".join(f.donde) or "—"
            )
        console.print(tabla)

    for n in reporte.negocios:
        if n.error:
            console.print(f"[red]Error:[/red] {n.slug}: {n.error}")
        if not tenant:
            continue  # con --todos solo el resumen: el detalle de un negocio es `--tenant <slug>`
        if n.filas:
            _detalle(f"{n.slug}: afectados", n.filas)
        if n.borradores:
            _detalle(f"{n.slug}: borradores con referencias retiradas (se avisan; no cuentan)", n.borradores)
        for a in n.aparte:
            console.print(
                f"[dim]{n.slug}: conexión {a.conexion_id} ({a.motivo}, release #{a.release}) "
                f"nombra {len(a.filas)} referencia(s) retirada(s): no cuenta como falla.[/dim]"
            )
    if reporte.fallo:
        console.print(
            "[red]Hay agentes o releases con referencias retiradas.[/red] Detalle: `auditar-referencias --tenant <slug>`; "
            "corrección: `agent-core db migrar-referencias --tenant <slug>`."
        )
        raise typer.Exit(1)
    console.print("✓ [green]Sin referencias retiradas en versiones ni releases activas.[/green]")


@app.command("migrar-referencias")
def migrar_referencias(
    tenant: str = typer.Option(..., "--tenant", help="El negocio (slug o UUID). Nunca en masa."),
    aplicar: bool = typer.Option(
        False, "--aplicar", help="Publica las versiones nuevas y reconstruye las releases. Sin esto solo informa (dry-run)."
    ),
) -> None:
    """Agent OS (HU-AOS-59): aplica el registro de retiros (`core/agents/retiros.py`) a un negocio. Cada
    agente afectado publica una versión nueva (etiqueta y nota) partiendo de su última versión
    PUBLICADA, nunca del borrador; luego se reconstruye y promueve la release de cada conexión
    afectada, igual que un build del dueño (con un canary en curso se construye pero no se
    promueve). El reemplazo de un procedimiento solo se agrega si el negocio tiene su habilidad.
    Idempotente. Cambia agentes en vivo: se revierte desde las versiones del panel (HU-AOS-27)."""
    import asyncio

    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.plugin_manager import plugin_manager
    from core.releases.referencias_retiradas import ReporteMigracion, migrar

    async def _run() -> ReporteMigracion:
        return await migrar(tenant_id=await resolve_tenant(tenant), aplicar=aplicar, modules=plugin_manager.scan())

    reporte = asyncio.run(_run())
    if not reporte.negocios:
        console.print(f"[red]No se encontró un negocio vivo para '{tenant}'.[/red]")
        raise typer.Exit(1)
    sufijo = "" if aplicar else " (dry-run: no se escribió nada; usa --aplicar)"
    for n in reporte.negocios:
        if n.sin_cambios:
            console.print(f"✓ [green]{n.slug}: sin referencias retiradas que migrar.[/green]")
        if n.agentes:
            tabla = Table(title=f"{n.slug}: agentes{sufijo}")
            for columna in ("Agente", "Parte de", "Resultado", "Versión nueva", "Cambios"):
                tabla.add_column(columna)
            for a in n.agentes:
                tabla.add_row(
                    a.agente, f"v{a.version_desde}", a.accion, str(a.version_nueva or "—"), "\n".join(a.cambios) or "—"
                )
            console.print(tabla)
        for a in n.agentes:
            for aviso in a.avisos:
                console.print(f"[yellow]Aviso:[/yellow] {n.slug}/{a.agente}: {aviso}")
            for bloqueo in a.bloqueos:
                console.print(f"[red]Bloqueado:[/red] {n.slug}/{a.agente}: {bloqueo}")
        if n.conexiones:
            tabla = Table(title=f"{n.slug}: releases{sufijo}")
            for columna in ("Conexión", "Release activa", "Resultado", "Release nueva", "Detalle"):
                tabla.add_column(columna)
            for c in n.conexiones:
                tabla.add_row(str(c.conexion_id), f"#{c.release_anterior}", c.accion, str(c.release_nueva or "—"), c.detalle or "—")
            console.print(tabla)
        for borrador in n.borradores:
            console.print(
                f"[yellow]Borrador con referencias retiradas:[/yellow] {n.slug}/{borrador.agente}: "
                f"{borrador.ruta} nombra '{borrador.nombre}' (no se toca; avisa a su dueño)."
            )
        for aviso in n.avisos:
            console.print(f"[yellow]Aviso:[/yellow] {n.slug}: {aviso}")
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


@app.command("reporte-bindings")
def reporte_bindings(
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID); por defecto, todos los v3."),
) -> None:
    """Agent OS (HU-ARCH-34): acciones que un agente de una release activa o canary debe tener concedidas
    y no tiene. Solo lectura. Debe dar 0 antes de encender el fail-closed; sale con error si hay faltantes."""
    import asyncio

    from rich.markup import escape
    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.plugin_manager import plugin_manager
    from core.releases.reporte_bindings import ReporteBindings, reporte_de_bindings

    async def _run() -> ReporteBindings:
        ids = [await resolve_tenant(tenant)] if tenant else None
        return await reporte_de_bindings(plugin_manager.scan(), ids)

    reporte = asyncio.run(_run())
    tabla = Table(title=f"Acciones sin conceder ({reporte.agentes_revisados} agentes revisados)")
    for columna in ("Negocio", "Conexión", "Release", "Agente", "Versión", "Acción", "Motivo"):
        tabla.add_column(columna)
    for f in reporte.faltantes:
        tabla.add_row(
            escape(f.negocio), str(f.conexion)[:8], str(f.release), escape(f.agente), str(f.version),
            escape(f.accion), f.motivo,
        )
    console.print(tabla)
    for (negocio, agente), cuantas in sorted(reporte.fuera_de_alcance.items()):
        console.print(f"Fuera de alcance (dejan de ofrecerse): {escape(negocio)} / {escape(agente)}: {cuantas}")
    for error in reporte.errores:
        console.print(f"[red]Error:[/red] {escape(error)}")
    console.print(f"Faltantes: {len(reporte.faltantes)}")
    if not reporte.limpio:
        raise typer.Exit(1)


@app.command("reporte-ruteo")
def reporte_ruteo(
    tenant: str = typer.Option(..., "--tenant", help="El negocio (slug o UUID)."),
    dias: int = typer.Option(7, "--dias", min=1, max=31, help="Ventana en días del tráfico real."),
    fuente: str = typer.Option(
        "ambas", "--fuente", help="golden (verdad por escenario) | trafico (lo predicho frente a lo iniciado) | ambas."
    ),
    golden: str | None = typer.Option(
        None, "--golden", help="Archivo de casos propio (JSON por línea); por defecto evals/ruteo/golden.jsonl."
    ),
) -> None:
    """Agent OS (HU-AOS-17): precisión y recall del enrutamiento por procedimiento, con dos fuentes
    y diciendo qué mide cada una. Meta de R3: ≥ 0,90 / ≥ 0,85.

    1. **Golden**: frases con el procedimiento esperado contra la release activa de cada conexión.
       No depende del tráfico ni de que el agente inicie nada; sirve con la release en shadow o active.
    2. **Tráfico real**: lo que el router predijo frente a lo que se inició (sombra o activo)."""
    import asyncio

    from rich.markup import escape
    from rich.table import Table

    from cli.tenant_ref import resolve_tenant
    from core.kernel.ruteo_sombra import (
        META_PRECISION,
        META_RECALL,
        Reporte,
        ReporteGolden,
        ReportePila,
        cargar_golden,
        cumple_meta,
        leyenda_trafico,
        reporte_de_golden,
        reporte_de_pila,
        reporte_de_sombra,
    )
    from core.shared.tenant import tenant_scope

    if fuente not in ("golden", "trafico", "ambas"):
        console.print("[red]Error:[/red] --fuente debe ser golden, trafico o ambas")
        raise typer.Exit(1)
    try:
        casos = cargar_golden(golden) if fuente != "trafico" else []
    except (OSError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None

    async def _run() -> tuple[list[ReporteGolden], Reporte | None, ReportePila | None]:
        tenant_id = await resolve_tenant(tenant)
        async with tenant_scope(tenant_id):
            por_golden = await reporte_de_golden(tenant_id, casos) if fuente != "trafico" else []
            if fuente == "golden":
                return por_golden, None, None
            return por_golden, await reporte_de_sombra(tenant_id, dias), await reporte_de_pila(tenant_id, dias)

    por_golden, reporte, pila = asyncio.run(_run())

    def _pct(valor: float | None) -> str:
        return "no medible" if valor is None else f"{valor:.2f}"

    def _tabla(titulo: str, r: Reporte, esperados: str) -> Table:
        tabla = Table(title=titulo)
        for columna in ("Procedimiento", "Predichos", esperados, "Aciertos", "Sin verdad", "Precisión", "Recall"):
            tabla.add_column(columna)
        for m in r.metricas:
            tabla.add_row(
                m.procedure_id, str(m.predichos), str(m.reales), str(m.verdaderos), str(m.sin_verdad),
                _pct(m.precision), _pct(m.recall),
            )
        return tabla

    def _veredicto(r: Reporte) -> str:
        ok = cumple_meta(r)
        meta = f"meta R3: precisión ≥ {META_PRECISION:.2f} y recall ≥ {META_RECALL:.2f}"
        return f"no medible ({meta})" if ok is None else ("CUMPLE" if ok else "NO CUMPLE") + f" ({meta})"

    console.print(f"Negocio {tenant}")
    if fuente != "trafico":
        console.print("\n[bold]1. Golden de ruteo[/bold] (verdad esperada por escenario; no depende del tráfico ni del agente)")
        if not por_golden:
            console.print("Sin conexiones conversacionales vivas con release activa: no hay contra qué medir.")
        for g in por_golden:
            console.print(
                f"Conexión {g.connection_id} ({g.canal or '-'}) · release {g.release_id}: "
                f"{g.reporte.turnos} casos medidos, {g.no_aplicables} no aplicables (su procedimiento no está en la release)"
                + (f", {g.sin_dictamen} con un procedimiento ajeno al catálogo del golden (sin verdad)" if g.sin_dictamen else "")
            )
            if g.reporte.turnos:
                console.print(_tabla("Golden contra la release activa", g.reporte, "Esperados"))
                console.print(f"Resultado: {_veredicto(g.reporte)}")
            for texto, esperado, predicho in g.fallos[:10]:
                console.print(
                    escape(f"  falló: «{texto}» esperado {esperado or 'ninguno'}, predijo {predicho or 'ninguno'}")
                )
            if g.no_evaluadas:
                console.print(
                    "  no evaluadas sin conversación (dato del cliente, etapa u horario): " + ", ".join(g.no_evaluadas)
                )
    if reporte is not None:
        origenes = ", ".join(f"{k}: {v}" for k, v in sorted(reporte.por_origen.items())) or "ninguno"
        console.print(
            f"\n[bold]2. Tráfico real[/bold] · {reporte.turnos} turnos en {dias} días (origen de la predicción → {origenes})"
        )
        for nota in leyenda_trafico(reporte):
            console.print(f"  {nota}")
        if reporte.metricas:
            console.print(_tabla("Tráfico real: lo predicho frente a lo iniciado", reporte, "Reales"))
            console.print(f"Resultado: {_veredicto(reporte)}")
    # HU-AOS-20: lo que la pila de tareas habría abierto (turnos sin foco). Meta: precisión ≥ 0,90.
    if pila is not None and pila.turnos:
        console.print(
            f"\nPila en sombra: {pila.turnos} turnos · predichos {pila.predichos} · iniciados {pila.reales} · "
            f"aciertos {pila.aciertos} · completos {pila.completos} · precisión {_pct(pila.precision)} · "
            f"recall {_pct(pila.recall)}"
        )


@app.command("presets-v2")
def presets_v2(
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    todos: bool = typer.Option(False, "--todos", help="Todos los negocios vivos."),
    preset: str | None = typer.Option(
        None, "--preset", help="Preset a usar en vez del registrado en el negocio (solo con --tenant)."
    ),
    aplicar: bool = typer.Option(False, "--aplicar", help="Escribe. Sin esto es dry-run: reporta lo que haría."),
) -> None:
    """Agent OS (HU-AOS-46, ADR-0139): presets v2 para negocios existentes. Crea los agentes
    especialistas del preset (Ventas, Agenda, Cobranza, Suscripciones) que falten, con su
    instancia en cada conexión (nunca por defecto), agrega las reglas del Coordinador y construye
    la release candidata de cada conexión que cambió, SIN activarla. Idempotente: no duplica
    agentes, no pisa lo que el dueño cambió y no devuelve instancias quitadas. Dry-run por defecto."""
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


@app.command("habilidades-por-agente")
def habilidades_por_agente(
    tenant: str | None = typer.Option(None, "--tenant", help="Solo este negocio (slug o UUID)."),
    todos: bool = typer.Option(False, "--todos", help="Todos los negocios vivos."),
    aplicar: bool = typer.Option(False, "--aplicar", help="Escribe. Sin esto es dry-run: reporta lo que haría."),
) -> None:
    """ADR-0190 (HU-AOS-65): instala en cada agente especialista existente las habilidades de su plantilla (y las
    opcionales que el negocio ya tiene), sin borrar nada. Idempotente; dry-run por defecto. Pagos queda pendiente de
    elegir la cuenta Wompi."""
    import asyncio

    from cli.tenant_ref import resolve_tenant
    from core.agents.instalaciones_agente import backfill_habilidades_por_agente

    if bool(tenant) == todos:
        raise typer.BadParameter("Indica --tenant <slug> o --todos (uno de los dos).")

    async def _run() -> list[dict[str, object]]:
        ids = [await resolve_tenant(tenant)] if tenant else None
        return await backfill_habilidades_por_agente(ids, aplicar=aplicar)

    filas = asyncio.run(_run())
    console.print("Habilidades por agente" + ("" if aplicar else " (dry-run: no se escribió nada)"))
    for f in filas:
        console.print(f"  {f['negocio']} · {f['agente']}: instala {f['instaladas'] or '—'} (ya tenía {f['ya'] or '—'})")
    console.print(f"{len(filas)} agente(s).")

"""agent-core plugin: instalación y gestión de plugins.

install: descarga (local o GitHub) -> pip install -e -> wizard de BD ->
         wizard de exposición de entidades -> actualiza SDD -> restart docker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cli.registry import (
    KNOWN_PLUGINS,
    clone_from_github,
    normalize,
    pip_install_editable,
    resolve_local,
)
from cli.sdd_generator import append_changelog, load_state, render_all, save_state

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("list")
def list_plugins() -> None:
    """Plugins conocidos del registry + instalados localmente."""
    table = Table(title="Plugins")
    table.add_column("Nombre")
    table.add_column("Descripción")
    table.add_column("Estado")
    for name, description in KNOWN_PLUGINS.items():
        local = resolve_local(name)
        status = "[green]instalado local[/green]" if local else "disponible (GitHub)"
        table.add_row(name, description, status)
    console.print(table)


@app.command()
def install(
    name: str = typer.Argument(..., help="Nombre del plugin, ej: mod-crm-engine"),
    no_wizard: bool = typer.Option(False, "--no-wizard", help="Instalar sin wizards interactivos"),
    no_restart: bool = typer.Option(False, "--no-restart", help="No reiniciar docker-compose"),
) -> None:
    """Instala un plugin: descarga, integra al monolito, migra y actualiza el SDD."""
    plugin_id = normalize(name)
    console.print(Panel.fit(f"📦 Instalando [bold]{name}[/bold]", border_style="cyan"))

    # ---------------- 1. Resolver e instalar el paquete
    path = resolve_local(name)
    if path is not None:
        console.print(f"✓ Encontrado localmente: {path}")
    else:
        console.print(f"Descargando desde GitHub ({name})...")
        try:
            path = clone_from_github(name)
            console.print(f"✓ Clonado en {path}")
        except subprocess.CalledProcessError as exc:
            console.print(f"[red]No se pudo clonar el plugin '{name}'.[/red]")
            console.print("Verifica el nombre con: agent-core plugin list")
            raise typer.Exit(1) from exc

    try:
        pip_install_editable(path)
        console.print("✓ [green]Paquete instalado (pip install -e, código embebido en el monolito)[/green]")
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]pip install falló:[/red] {exc.stderr[-400:] if exc.stderr else exc}")
        raise typer.Exit(1) from exc

    # ---------------- 2. Detectar el módulo
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    module = plugin_manager.get(plugin_id)
    if module is None:
        console.print(f"[red]El paquete no expone el entry_point '{plugin_id}' "
                      f"en el grupo agent_core.plugins.[/red]")
        raise typer.Exit(1)
    console.print(f"✓ Plugin detectado: {module.name} v{module.version}")

    # ---------------- 3. Registro en BD + migraciones del plugin
    import asyncio

    try:
        asyncio.run(plugin_manager.sync_registry())
        plugin_manager._run_plugin_migrations(plugin_id)
        console.print("✓ [green]Migraciones del plugin aplicadas[/green]")
    except Exception as exc:
        console.print(f"[yellow]⚠ BD no disponible ({str(exc)[:80]}). "
                      "Corre luego: agent-core db migrate[/yellow]")

    # ---------------- 4. Wizard de datos existentes
    entity_names: list[str] = []
    if not no_wizard:
        manifest = _load_plugin_manifest(path)
        entities: dict[str, list[str]] = manifest.get("entities", {})
        entity_names = list(entities.keys())
        if entities:
            mode = questionary.select(
                "¿Cómo inicializar los datos del plugin?",
                choices=[
                    questionary.Choice("Esquema base vacío (recomendado para empezar)", value="base"),
                    questionary.Choice("Importar desde una BD PostgreSQL existente", value="import"),
                ],
            ).ask()
            if mode == "import":
                from cli.wizards.db_import_wizard import run_import_wizard

                run_import_wizard(plugin_id, entities)

        # ---------------- 5. Wizard de exposición de entidades (SDD)
        exposure: dict[str, dict[str, bool]] = {}
        for entity in entity_names:
            console.print(f"\n[bold]Exposición de la entidad '{entity}':[/bold]")
            exposure[entity] = {
                "admin_panel": questionary.confirm("  ¿Visible en el panel admin?", default=True).ask(),
                "agent_tool": questionary.confirm("  ¿El agente puede usarla (tools)?", default=True).ask(),
            }
    else:
        exposure = {}

    # ---------------- 6. Actualizar SDD vivo
    state = load_state()
    installed = state.setdefault("installed_plugins", [])
    installed = [p for p in installed if p.get("id") != plugin_id]
    skills_info = []
    try:
        # instancia temporal para leer skills declaradas (sin activar)
        temp_host_skills = _peek_skills(module)
        skills_info = temp_host_skills
    except Exception:
        pass
    installed.append(
        {
            "id": plugin_id,
            "name": module.name or plugin_id,
            "version": module.version,
            "description": module.description,
            "skills": skills_info,
            "exposure": exposure,
        }
    )
    state["installed_plugins"] = installed
    for entity in entity_names:
        if entity not in [e.get("name") for e in state.setdefault("entities", [])]:
            state["entities"].append({"name": entity, "plugin": plugin_id, "exposure": exposure.get(entity, {})})
    append_changelog(state, f"Plugin instalado: {plugin_id} v{module.version}")
    save_state(state)
    for file in render_all(state):
        console.print(f"✓ SDD actualizado: [green]{file}[/green]")

    # ---------------- 7. Restart
    if not no_restart and Path("docker-compose.yml").exists():
        result = subprocess.run(
            ["docker", "compose", "ps", "-q", "core-api"], capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print("Reiniciando core-api para cargar el plugin...")
            subprocess.run(["docker", "compose", "restart", "core-api", "celery-worker", "celery-beat"])
            console.print("✓ [green]Servicios reiniciados[/green]")

    console.print(
        Panel(
            f"[bold]{module.name}[/bold] instalado.\n\n"
            "Siguiente paso: abre el panel y configúralo:\n"
            "  http://localhost:8000/admin/plugins",
            border_style="green",
            title="✓ instalación completada",
        )
    )


@app.command()
def remove(name: str = typer.Argument(...)) -> None:
    """Desinstala un plugin (pip uninstall). Las tablas del plugin NO se borran."""
    plugin_id = normalize(name)
    if not questionary.confirm(f"¿Desinstalar {plugin_id}?", default=False).ask():
        raise typer.Exit(0)
    subprocess.run(["pip", "uninstall", "-y", plugin_id.replace("_", "-")], check=False)

    state = load_state()
    state["installed_plugins"] = [
        p for p in state.get("installed_plugins", []) if p.get("id") != plugin_id
    ]
    append_changelog(state, f"Plugin desinstalado: {plugin_id}")
    save_state(state)
    render_all(state)
    console.print(f"✓ {plugin_id} desinstalado. Sus tablas quedan en la BD (borrado manual si aplica).")


@app.command()
def scaffold(name: str = typer.Argument(..., help="Nombre del nuevo plugin, ej: mod-mi-plugin")) -> None:
    """Genera la estructura base de un plugin nuevo."""
    plugin_id = normalize(name)
    base = Path("plugins") / plugin_id
    package = base / plugin_id
    if base.exists():
        console.print(f"[red]{base} ya existe.[/red]")
        raise typer.Exit(1)
    package.mkdir(parents=True)

    (base / "pyproject.toml").write_text(
        f'''[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "{plugin_id.replace("_", "-")}"
version = "0.1.0"
description = "Plugin {plugin_id} para agent-core"
requires-python = ">=3.12"
dependencies = []

[project.entry-points."agent_core.plugins"]
{plugin_id} = "{plugin_id}.module:Module"

[tool.agent_core.plugin]
# entidades que este plugin gestiona (para el wizard de importación)
# entities = {{ "mi_tabla" = ["campo1", "campo2"] }}
'''
    )
    (package / "__init__.py").write_text("")
    (package / "config.py").write_text(
        '''"""Configuración del plugin (Pydantic -> formulario auto-generado en el admin)."""

from pydantic import Field, SecretStr

from core.mcp.types import BasePluginConfig


class Config(BasePluginConfig):
    api_key: SecretStr = Field(..., description="API Key del servicio externo")
    enabled_feature: bool = Field(default=True, description="Activar la función principal")
'''
    )
    (package / "skills.py").write_text(
        f'''"""Skills del plugin: agrupaciones semánticas de tools."""

from typing import Any

from core.mcp.types import BaseSkill, MCPTool, ToolContext


class MainSkill(BaseSkill):
    name = "{plugin_id}_skill"
    description = "Describe qué hace esta skill"
    keywords = []  # vacío = siempre activa; con keywords, el router la activa por contexto

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="mi_tool",
                description="Describe qué hace esta tool para que el LLM sepa cuándo usarla",
                parameters={{
                    "type": "object",
                    "properties": {{
                        "ejemplo": {{"type": "string", "description": "Un parámetro"}}
                    }},
                    "required": ["ejemplo"],
                }},
                handler=self.mi_tool,
            )
        ]

    async def mi_tool(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # context.conversation_id / customer_id / idempotency_key inyectados por el core
        return {{"status": "ok", "echo": arguments.get("ejemplo")}}
'''
    )
    (package / "module.py").write_text(
        f'''"""Módulo principal del plugin: implementa BaseModule."""

from core.mcp.types import BaseModule
from {plugin_id}.config import Config
from {plugin_id}.skills import MainSkill


class Module(BaseModule):
    id = "{plugin_id}"
    name = "{name}"
    version = "0.1.0"
    description = "Describe el propósito del plugin"
    requires_config = True
    config_schema = Config

    def on_activate(self, host) -> None:  # type: ignore[no-untyped-def]
        host.register_skill(MainSkill())
'''
    )
    console.print(
        Panel(
            f"Plugin creado en [bold]{base}[/bold]\n\n"
            f"1. Edita {package}/skills.py con tus tools\n"
            f"2. Instala: pip install -e {base}\n"
            f"3. Actívalo en http://localhost:8000/admin/plugins",
            border_style="green",
            title="✓ scaffold",
        )
    )


@app.command()
def migrate(name: str = typer.Argument(...)) -> None:
    """Corre las migraciones pendientes de un plugin."""
    plugin_id = normalize(name)
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    plugin_manager._run_plugin_migrations(plugin_id)
    console.print(f"✓ Migraciones de {plugin_id} aplicadas")


# ------------------------------------------------------------------ helpers
def _load_plugin_manifest(path: Path) -> dict:  # type: ignore[type-arg]
    """Lee [tool.agent_core.plugin] del pyproject.toml del plugin."""
    import tomllib

    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return {}
    data = tomllib.loads(pyproject.read_text())
    result = data.get("tool", {}).get("agent_core", {}).get("plugin", {})
    return result if isinstance(result, dict) else {}


def _peek_skills(module) -> list[dict]:  # type: ignore[no-untyped-def, type-arg]
    """Lee las skills declaradas sin activar el plugin (para el SDD)."""
    skills = []
    declared = getattr(module, "declared_skills", None)
    if declared:
        for skill_class in declared:
            skill = skill_class()
            skills.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "tools": [t.name for t in skill.get_tools()],
                }
            )
    return skills

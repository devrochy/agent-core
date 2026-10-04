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
def scaffold(
    name: str = typer.Argument(..., help="Nombre del nuevo pack, ej: fidelidad o pack-fidelidad"),
    tenant: bool = typer.Option(
        False,
        "--tenant",
        help="Genera un plugin del tenant (ADR-0089): modelos, jobs, migraciones y tests con RLS.",
    ),
) -> None:
    """Genera la estructura base de un plugin/pack nuevo en packages/pack-<nombre>/.

    Antes generaba en plugins/<nombre>/ (heredado del monolito pre-ADR-0020).
    El código nuevo va en packages/pack-* (CLAUDE.md) — este comando ya
    entrega la estructura ahí directamente, sin el paso manual de mover
    carpetas que documentaba plugin-dev.md.

    Con `--tenant` (ADR-0089) añade `models.py`, `jobs.py`, un árbol Alembic
    propio y tests, listos para un negocio particular: cada tabla nace con
    `tenant_id` + RLS forzada.
    """
    raw = normalize(name)
    domain = raw
    for prefix in ("pack_", "mod_"):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    plugin_id = f"pack_{domain}"
    base = Path("packages") / f"pack-{domain.replace('_', '-')}"
    package = base / plugin_id
    if base.exists():
        console.print(f"[red]{base} ya existe.[/red]")
        raise typer.Exit(1)
    package.mkdir(parents=True)

    _write_common_files(base, package, plugin_id, domain, name)
    if tenant:
        _write_tenant_files(base, package, plugin_id, domain)

    extra = (
        f"\n\n[bold]Plugin del tenant (ADR-0089).[/bold] Incluye models.py, jobs.py,\n"
        f"migraciones (alembic/) y tests. La tabla de ejemplo ya cumple tenant_id + RLS.\n"
        f"Revisa {base / 'alembic/versions/'} antes de instalar."
        if tenant
        else ""
    )
    console.print(
        Panel(
            f"Plugin creado en [bold]{base}[/bold]\n\n"
            f"1. Edita {package}/skills.py con tus tools\n"
            f"2. Instala: pip install -e {base}\n"
            f"3. Actívalo en http://localhost:8000/admin/plugins"
            f"{extra}",
            border_style="green",
            title="✓ scaffold",
        )
    )


def _write_common_files(base: Path, package: Path, plugin_id: str, domain: str, name: str) -> None:
    """Archivos comunes a pack y plugin del tenant."""
    (base / "pyproject.toml").write_text(
        f'''[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "{base.name}"
version = "0.1.0"
description = "Pack {domain} para agent-core"
requires-python = ">=3.12"
dependencies = []

[project.entry-points."agent_core.plugins"]
{plugin_id} = "{plugin_id}.module:Module"

[tool.setuptools.packages.find]
include = ["{plugin_id}*"]

[tool.agent_core.plugin]
# entidades que este pack gestiona (para el wizard de importación)
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

from typing import Any, ClassVar

from core.mcp.types import BaseSkill, MCPTool, ToolContext


class MainSkill(BaseSkill):
    name = "{plugin_id}_skill"
    description = "Describe qué hace esta skill"
    keywords: ClassVar[list[str]] = []  # vacío = siempre activa; con keywords, el router la activa por contexto

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


def _write_tenant_files(base: Path, package: Path, plugin_id: str, domain: str) -> None:
    """Archivos de un plugin del tenant (ADR-0089): datos, tareas, migración y tests.

    Toda tabla nace con `tenant_id` + RLS habilitada y forzada, índice que
    empieza por `tenant_id` y `deleted_at` (borrado lógico). La migración de
    ejemplo pasa la guarda de invariantes del core.
    """
    class_name = "".join(part.capitalize() for part in domain.split("_")) or "Item"

    (package / "models.py").write_text(
        f'''"""Modelos de {plugin_id} — SQLAlchemy sobre su PROPIA Base.

Plugin del tenant (ADR-0089): acotado a UN negocio. Toda tabla lleva
`tenant_id` (invariante nº1) + RLS forzada + índice que empieza por `tenant_id`
y `deleted_at` (borrado lógico, invariante nº8).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.shared.tenant import get_current_tenant_id


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("tenant_id requerido: usar dentro de tenant_scope()")
    return tenant_id


class Base(DeclarativeBase):
    pass


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )


class {class_name}(TenantMixin, Base):
    """Tabla de ejemplo. Renómbrala y añade tus columnas."""

    __tablename__ = "{plugin_id}_{domain}"
    __table_args__ = (
        Index("ix_{plugin_id}_{domain}_tenant_created", "id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=text("now()")
    )
'''
    )

    (package / "jobs.py").write_text(
        f'''"""Cron jobs de {plugin_id} (plugin del tenant, ADR-0089).

`@register_cron` salta automáticamente si el plugin está inactivo para el
tenant. El filtro por tenant habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.db.session import get_session
from core.shared.logger import get_logger
from core.tasks.decorators import register_cron

log = get_logger(__name__)

PACK_ID = "{plugin_id}"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants cuyo AgentSpec incluye este plugin (ADR-0019)."""
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null")).scalars()
        ).all()
    out: list[uuid.UUID] = []
    for tenant_id in rows:
        spec = await load_active_spec(tenant_id)
        if PACK_ID in enabled_packs(spec):
            out.append(tenant_id)
    return out


@register_cron(PACK_ID, "0 3 * * *")
async def tarea_diaria() -> None:
    """Tarea diaria de ejemplo. Reemplázala por tu lógica."""
    for tenant_id in await _tenant_ids():
        log.info("tarea_diaria_tenant", plugin=PACK_ID, tenant_id=str(tenant_id))
'''
    )

    # ---------------- Alembic propio
    alembic_dir = base / "alembic"
    (alembic_dir / "versions").mkdir(parents=True)
    (base / "alembic.ini").write_text(
        f"""[alembic]
script_location = alembic
prepend_sys_path = .
sqlalchemy.url =
version_table = alembic_version_{plugin_id}

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
"""
    )
    (alembic_dir / "env.py").write_text(
        f'''"""Árbol Alembic PROPIO de {plugin_id} (plugin del tenant, ADR-0089).

version_table separada (alembic_version_{plugin_id}) para no chocar con el core
ni con otros plugins. El plugin_manager ejecuta esto al activar.
"""

from __future__ import annotations

from {plugin_id}.models import Base
from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if not config.get_main_option("sqlalchemy.url"):
    from core.config import get_settings

    _url = get_settings().migrations_database_url or get_settings().database_url
    config.set_main_option("sqlalchemy.url", _url.replace("+asyncpg", "+psycopg2"))

target_metadata = Base.metadata
VERSION_TABLE = config.get_main_option("version_table") or "alembic_version_{plugin_id}"


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {{}}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, version_table=VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
'''
    )
    (alembic_dir / "versions" / f"0001_{domain}.py").write_text(
        f'''"""{class_name}: tabla de ejemplo de {plugin_id} (ADR-0089).

Toda tabla de un plugin del tenant lleva tenant_id + RLS habilitada y forzada +
policy de aislamiento (invariante nº1) e índice que empieza por tenant_id.

Revision ID: {plugin_id}_0001
Revises:
Create Date: (autogenerado por scaffold)
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "{plugin_id}_0001"
down_revision = None
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "{plugin_id}_{domain}",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("nombre", sa.Text(), nullable=False, server_default=""),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_{plugin_id}_{domain}_tenant_created",
        "{plugin_id}_{domain}",
        ["tenant_id", "id", "created_at"],
    )
    op.execute("alter table {plugin_id}_{domain} enable row level security")
    op.execute(
        "create policy tenant_isolation on {plugin_id}_{domain} "
        f"using (tenant_id = {{RLS}}) with check (tenant_id = {{RLS}})"
    )
    op.execute("alter table {plugin_id}_{domain} force row level security")
    op.execute(
        "grant select, insert, update, delete on {plugin_id}_{domain} to app_runtime"
    )


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on {plugin_id}_{domain}")
    op.execute("alter table {plugin_id}_{domain} no force row level security")
    op.drop_index(
        "ix_{plugin_id}_{domain}_tenant_created", table_name="{plugin_id}_{domain}"
    )
    op.drop_table("{plugin_id}_{domain}")
'''
    )

    # ---------------- Test de invariantes (tenant_id + RLS)
    tests_dir = base / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / f"test_{domain}_migracion_rls.py").write_text(
        f'''"""Invariante nº1: toda tabla de {plugin_id} nace con tenant_id + RLS forzada.

Revisa la migración de ejemplo: si añades tablas, replica la misma forma. Este
test es la puerta del master (ADR-0089) antes de desplegar el plugin.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRACIONES = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _texto_migraciones() -> str:
    return "\\n".join(p.read_text(encoding="utf-8") for p in MIGRACIONES.glob("*.py")).lower()


def test_toda_tabla_declara_tenant_id_y_rls():
    texto = _texto_migraciones()
    assert "create_table" in texto
    assert "tenant_id" in texto
    assert "enable row level security" in texto
    # `no force` del downgrade no cuenta.
    assert re.search(r"(?<!no )force row level security", texto)
    assert "create policy" in texto
    assert "with check" in texto
'''
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

"""agent-core init: wizard de creación de un nuevo asistente.

Flujo:
1. Respuestas del negocio (wizard interactivo, o banderas con --non-interactive).
2. Detecta puertos publicables, evitando los que reclama cualquier contenedor.
3. Genera .env con credenciales, puertos y nombre de proyecto Docker.
4. Genera el SDD vivo (sdd/*.md desde plantillas).
5. Levanta la BD con docker-compose y aplica migraciones (core + plugins).
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.panel import Panel

from cli.preflight import (
    PortUnavailable,
    docker_available,
    docker_reserved_ports,
    docker_volume_exists,
    explain_docker_error,
    find_free_port,
    llm_defaults,
    parse_db_url,
    parse_redis_url,
    port_owner,
    slugify,
    update_url_port,
)
from cli.sdd_generator import append_changelog, render_all, save_state

console = Console()

DEFAULT_DB_URL = "postgresql+asyncpg://agent_core:agent_core@localhost:5432/agent_core"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

ENV_TEMPLATE = """# Generado por `agent-core init` — {assistant_name}
APP_NAME="{assistant_name}"
APP_ENV=development
APP_SECRET_KEY={app_secret}
PUBLIC_BASE_URL=http://localhost:{app_host_port}

# Identidad del proyecto Docker: aísla contenedores y volúmenes de otros clones
# del framework. Cambiarlo equivale a empezar con una base de datos nueva.
COMPOSE_PROJECT_NAME={project_name}

# Base de datos (para migraciones desde el host local)
DATABASE_URL={database_url}
REDIS_URL={redis_url}

# Credenciales postgres (usadas por docker-compose)
POSTGRES_USER={pg_user}
POSTGRES_PASSWORD={pg_password}
POSTGRES_DB={pg_db}
POSTGRES_HOST_PORT={pg_host_port}
REDIS_HOST_PORT={redis_host_port}
APP_HOST_PORT={app_host_port}

MASTER_ENCRYPTION_KEY={fernet_key}

LLM_PROVIDER={llm_provider}
LLM_API_KEY={llm_api_key}
LLM_MODEL={llm_model}
LLM_MODEL_REASONING={llm_model_reasoning}
LLM_BASE_URL={llm_base_url}
LLM_MAX_TOKENS=4096
DIFFICULTY_ROUTING=true

EMBEDDING_PROVIDER={embedding_provider}
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DIMENSIONS=384
OPENAI_API_KEY=

WHATSAPP_VERIFY_TOKEN={verify_token}
WHATSAPP_APP_SECRET=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=

DEBOUNCE_MODE={debounce_mode}
MAX_AGENT_ITERATIONS=6
MESSAGE_SPLIT_STYLE=pocos_cortos

LOG_LEVEL=INFO
"""


# ------------------------------------------------------------------ comando
def init_command(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "-y", help="Sin preguntas: usa las banderas y sus defaults"
    ),
    name: str = typer.Option("Mi Asistente", "--name", help="Nombre del asistente"),
    business: str = typer.Option("", "--business", help="Descripción del negocio"),
    language: str = typer.Option("es", "--language", help="Idioma principal (es/en/pt)"),
    tone: str = typer.Option("calido", "--tone", help="calido | formal | divertido"),
    llm: str = typer.Option("deepseek", "--llm", help="deepseek | openai | anthropic"),
    llm_model: str = typer.Option("", "--llm-model", help="Vacío = default del proveedor"),
    llm_api_key: str = typer.Option("", "--llm-api-key", help="API key del LLM"),
    embeddings: str = typer.Option("none", "--embeddings", help="none | local | openai"),
    debounce: str = typer.Option(
        "natural", "--debounce", help="inmediato | rapido | natural | pausado"
    ),
    database_url: str = typer.Option(DEFAULT_DB_URL, "--database-url"),
    redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url"),
    admin_email: str = typer.Option("admin@local", "--admin-email"),
    admin_password: str = typer.Option("", "--admin-password", help="Vacío = generar"),
    project_name: str = typer.Option("", "--project-name", help="Vacío = derivado del nombre"),
    force: bool = typer.Option(False, "--force", help="Sobrescribe el .env sin preguntar"),
    recreate_volume: bool = typer.Option(
        False, "--recreate-volume", help="DESTRUCTIVO: borra el volumen de datos si ya existe"
    ),
    skip_db: bool = typer.Option(False, "--skip-db", help="No tocar la BD (solo .env + sdd/)"),
    skip_docker: bool = typer.Option(False, "--skip-docker", help="No levantar docker-compose"),
) -> None:
    """Wizard interactivo: crea un nuevo asistente desde el core."""
    console.print(Panel.fit("🤖 [bold]agent-core init[/bold] — nuevo asistente", border_style="cyan"))

    if Path(".env").exists() and not force:
        if non_interactive:
            console.print(
                "[red]Ya existe un .env.[/red] Usa [bold]--force[/bold] para sobrescribirlo."
            )
            raise typer.Exit(1)
        if not questionary.confirm("Ya existe un .env. ¿Sobrescribir?", default=False).ask():
            console.print("[yellow]Cancelado.[/yellow]")
            raise typer.Exit(1)

    # ---- respuestas: wizard o banderas
    if non_interactive:
        answers = {
            "assistant_name": name,
            "business": business,
            "language": language,
            "tone": tone,
            "llm_provider": llm,
            "llm_model": llm_model or llm_defaults(llm)["model"],
            "llm_api_key": llm_api_key,
            "embedding_provider": embeddings,
            "debounce_mode": debounce,
            "database_url": database_url,
            "redis_url": redis_url,
            "admin_email": admin_email,
            "admin_password": admin_password,
        }
    else:
        answers = _ask_interactive(
            name, business, language, tone, llm, llm_model, debounce, database_url, redis_url,
            admin_email,
        )

    generated_password = not answers["admin_password"]
    if generated_password:
        answers["admin_password"] = secrets.token_urlsafe(10)

    project = slugify(project_name or answers["assistant_name"])

    # ---- puertos publicables
    console.print("\n[dim]Detectando puertos disponibles...[/dim]")
    docker_ok, docker_detail = docker_available()
    docker_ports = docker_reserved_ports() if docker_ok else {}
    if not docker_ok:
        console.print(f"[yellow]⚠ Docker no disponible: {docker_detail}[/yellow]")
        console.print("[yellow]  Los puertos se comprueban solo contra el host.[/yellow]")

    db_info = parse_db_url(answers["database_url"])
    redis_info = parse_redis_url(answers["redis_url"])

    try:
        assigned: set[int] = set()
        pg_host_port = find_free_port(
            int(db_info["port"]), "PostgreSQL", assigned, docker_ports
        )
        assigned.add(pg_host_port)
        redis_host_port = find_free_port(
            int(redis_info["port"]), "Redis", assigned, docker_ports
        )
        assigned.add(redis_host_port)
        app_host_port = find_free_port(8000, "API", assigned, docker_ports)
    except PortUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    for label, requested, chosen in (
        ("PostgreSQL", int(db_info["port"]), pg_host_port),
        ("Redis", int(redis_info["port"]), redis_host_port),
        ("API", 8000, app_host_port),
    ):
        if requested != chosen:
            owner = port_owner(requested, docker_ports) or "otro servicio"
            console.print(
                f"[yellow]  Puerto {requested} ocupado por {owner} → "
                f"{label} usará [bold]{chosen}[/bold][/yellow]"
            )

    if str(pg_host_port) != db_info["port"]:
        answers["database_url"] = update_url_port(answers["database_url"], pg_host_port)
    if str(redis_host_port) != redis_info["port"]:
        answers["redis_url"] = update_url_port(answers["redis_url"], redis_host_port)

    console.print(
        f"✓ Puertos: Postgres=[bold]{pg_host_port}[/bold] "
        f"Redis=[bold]{redis_host_port}[/bold] "
        f"API=[bold]{app_host_port}[/bold]  ·  proyecto Docker: [bold]{project}[/bold]"
    )

    # ---- .env
    from cryptography.fernet import Fernet

    db_info = parse_db_url(answers["database_url"])
    llm_conf = llm_defaults(answers["llm_provider"])

    Path(".env").write_text(
        ENV_TEMPLATE.format(
            assistant_name=answers["assistant_name"],
            app_secret=secrets.token_hex(32),
            project_name=project,
            database_url=answers["database_url"],
            redis_url=answers["redis_url"],
            pg_user=db_info["user"],
            pg_password=db_info["password"],
            pg_db=db_info["db"],
            pg_host_port=pg_host_port,
            redis_host_port=redis_host_port,
            app_host_port=app_host_port,
            fernet_key=Fernet.generate_key().decode(),
            llm_provider=answers["llm_provider"],
            llm_api_key=answers["llm_api_key"],
            llm_model=answers["llm_model"],
            llm_model_reasoning=llm_conf["reasoning_model"],
            llm_base_url=llm_conf["base_url"],
            embedding_provider=answers["embedding_provider"],
            verify_token=secrets.token_urlsafe(24),
            debounce_mode=answers["debounce_mode"],
        )
    )
    console.print("✓ [green].env generado[/green]")

    # ---- SDD vivo
    state = {
        "assistant_name": answers["assistant_name"],
        "business_description": answers["business"],
        "industry": "",
        "language": answers["language"],
        "tone": answers["tone"],
        "channel": "whatsapp",
        "channel_provider": "Meta Cloud API",
        "llm_provider": answers["llm_provider"],
        "llm_model": answers["llm_model"],
        "embedding_provider": answers["embedding_provider"],
        "debounce_mode": answers["debounce_mode"],
        "installed_plugins": [],
        "entities": [],
        "changelog": [],
    }
    append_changelog(
        state,
        f"Proyecto inicializado: {answers['assistant_name']} "
        f"({answers['llm_provider']}/{answers['llm_model']})",
    )
    save_state(state)
    for file in render_all(state):
        console.print(f"✓ [green]{file}[/green]")

    # ---- base de datos
    if not skip_db:
        _bootstrap_database(
            project=project,
            answers=answers,
            skip_docker=skip_docker,
            docker_ok=docker_ok,
            recreate_volume=recreate_volume,
            non_interactive=non_interactive,
        )

    # ---- resumen final
    lines = [
        f"[bold]{answers['assistant_name']}[/bold] listo.",
        "",
        "Siguientes pasos:",
        "  1. docker compose up -d",
        f"  2. Abre http://localhost:{app_host_port}/admin",
        f"  3. Ingresa con: [bold]{answers['admin_email']}[/bold]",
    ]
    if generated_password:
        lines.append(
            f"     Contraseña generada: [bold red]{answers['admin_password']}[/bold red]  (guárdala)"
        )
    lines += [
        "",
        "Si algo no arranca:  [bold]agent-core doctor[/bold]",
        "",
        "Para agregar capacidades:",
        "  agent-core plugin install mod-crm-engine",
    ]
    console.print(Panel("\n".join(lines), border_style="green", title="✓ init completado"))


# ------------------------------------------------------------------ wizard
def _ask_interactive(
    name: str,
    business: str,
    language: str,
    tone: str,
    llm: str,
    llm_model: str,
    debounce: str,
    database_url: str,
    redis_url: str,
    admin_email: str,
) -> dict[str, str]:
    assistant_name = questionary.text("Nombre del asistente:", default=name).ask()
    business_answer = questionary.text("Describe el negocio (1-2 frases):", default=business).ask() or ""
    language_answer = questionary.select(
        "Idioma principal:", choices=["es", "en", "pt"], default=language
    ).ask()
    tone_answer = questionary.select(
        "Tono del asistente:", choices=["calido", "formal", "divertido"], default=tone
    ).ask()

    provider = questionary.select(
        "Proveedor LLM:", choices=["deepseek", "openai", "anthropic"], default=llm
    ).ask()
    model = questionary.text(
        "Modelo:", default=llm_model or llm_defaults(provider)["model"]
    ).ask()
    api_key = questionary.password("API Key del LLM (Enter para dejar vacío):").ask() or ""

    embedding_provider = questionary.select(
        "Memoria vectorial (embeddings):",
        choices=[
            questionary.Choice("none — desactivada (puedes activarla luego)", value="none"),
            questionary.Choice("local — sentence-transformers, gratis (requiere extra pip)", value="local"),
            questionary.Choice("openai — text-embedding-3-small (requiere OPENAI_API_KEY)", value="openai"),
        ],
    ).ask()

    debounce_mode = questionary.select(
        "Velocidad de respuesta (debounce):",
        choices=["inmediato", "rapido", "natural", "pausado"],
        default=debounce,
    ).ask()

    db_url = questionary.text("URL de PostgreSQL:", default=database_url).ask()
    rd_url = questionary.text("URL de Redis:", default=redis_url).ask()

    email = questionary.text("Email del admin:", default=admin_email).ask()
    password = questionary.password("Contraseña del admin (Enter = generar):").ask() or ""

    return {
        "assistant_name": assistant_name,
        "business": business_answer,
        "language": language_answer,
        "tone": tone_answer,
        "llm_provider": provider,
        "llm_model": model,
        "llm_api_key": api_key,
        "embedding_provider": embedding_provider,
        "debounce_mode": debounce_mode,
        "database_url": db_url,
        "redis_url": rd_url,
        "admin_email": email,
        "admin_password": password,
    }


# --------------------------------------------------------------- base de datos
def _compose_env(project: str) -> dict[str, str]:
    """Entorno para docker compose con la identidad de proyecto explícita."""
    return {**os.environ, "COMPOSE_PROJECT_NAME": project}


def _handle_existing_volume(
    project: str, recreate_volume: bool, non_interactive: bool
) -> None:
    """Avisa (o resuelve) el caso de un volumen de datos heredado.

    PostgreSQL solo aplica POSTGRES_USER/PASSWORD/DB la primera vez que
    inicializa su directorio de datos. Un volumen que sobrevive a una
    instalación anterior conserva las credenciales viejas, y las migraciones
    mueren después con "role does not exist" sin mencionar el volumen.
    """
    volume = f"{project}_pgdata"
    if not docker_volume_exists(volume):
        return

    console.print(
        f"\n[yellow]⚠ El volumen de datos '{volume}' ya existe de una instalación anterior.[/yellow]"
    )
    console.print(
        "[yellow]  PostgreSQL conservará las credenciales con las que se creó; las del .env"
        " nuevo se ignoran.[/yellow]"
    )

    if recreate_volume:
        console.print("[yellow]  --recreate-volume: borrando el volumen (se pierden los datos)...[/yellow]")
        subprocess.run(
            ["docker", "compose", "down", "-v"],
            env=_compose_env(project), capture_output=True, text=True,
        )
        return

    if non_interactive:
        console.print(
            "[yellow]  Se reutilizará. Si las credenciales no coinciden, repite con"
            " --recreate-volume (DESTRUCTIVO).[/yellow]"
        )
        return

    choice = questionary.select(
        "¿Qué hacemos con el volumen existente?",
        choices=[
            questionary.Choice("Reutilizarlo (conserva los datos)", value="keep"),
            questionary.Choice("Borrarlo y empezar de cero (DESTRUYE los datos)", value="recreate"),
        ],
        default="keep",
    ).ask()
    if choice == "recreate":
        subprocess.run(
            ["docker", "compose", "down", "-v"],
            env=_compose_env(project), capture_output=True, text=True,
        )
        console.print("[yellow]  Volumen borrado.[/yellow]")


def _bootstrap_database(
    project: str,
    answers: dict[str, str],
    skip_docker: bool,
    docker_ok: bool,
    recreate_volume: bool,
    non_interactive: bool,
) -> None:
    """Levanta la BD (si toca), aplica migraciones y crea el admin."""
    if not skip_docker:
        if not docker_ok:
            console.print(
                "\n[yellow]⚠ Docker no está disponible: omito levantar la BD.[/yellow]"
                "\n[yellow]  Levanta PostgreSQL y Redis por tu cuenta y corre:"
                " agent-core db migrate[/yellow]"
            )
            return

        _handle_existing_volume(project, recreate_volume, non_interactive)

        console.print("\n[dim]Levantando PostgreSQL y Redis con docker-compose...[/dim]")
        result = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait", "postgres", "redis"],
            env=_compose_env(project), capture_output=True, text=True,
        )
        if result.returncode != 0:
            _report_failure(result.stderr or result.stdout, project)
            return
        console.print("✓ [green]PostgreSQL y Redis levantados[/green]")
    else:
        console.print("\n[dim]Aplicando migraciones sobre la BD existente...[/dim]")

    # Las settings se cachean con @lru_cache y el .env acaba de cambiar.
    try:
        from core.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass

    try:
        from core.db.migrations import upgrade_all

        results = upgrade_all()
    except Exception as exc:
        _report_failure(str(exc), project)
        return

    console.print("✓ [green]Migraciones del core aplicadas[/green]")
    for plugin_id, status in results.items():
        if status.startswith("error:"):
            console.print(f"[yellow]⚠ {plugin_id}: {status[7:][:150]}[/yellow]")
        elif status == "aplicadas":
            console.print(f"✓ [green]{plugin_id}: migraciones aplicadas[/green]")

    try:
        _create_admin_and_settings(
            answers["admin_email"], answers["admin_password"], answers["assistant_name"],
            answers["business"], answers["language"], answers["tone"], answers["debounce_mode"],
        )
    except Exception as exc:
        _report_failure(str(exc), project)
        return
    console.print("✓ [green]Usuario admin y configuración creados[/green]")


def _report_failure(raw_error: str, project: str) -> None:
    """Muestra la causa cuando se reconoce; si no, el error crudo."""
    console.print("\n[red]⚠ No se pudo preparar la base de datos.[/red]")
    explanation = explain_docker_error(raw_error, project=project)
    if explanation:
        console.print(f"[yellow]{explanation}[/yellow]")
    else:
        console.print(f"[dim]{(raw_error or '').strip()[:500]}[/dim]")
    console.print("\n[yellow]Diagnóstico completo:[/yellow] [bold]agent-core doctor[/bold]")
    console.print("[yellow]Cuando esté resuelto:[/yellow] agent-core db migrate && agent-core db seed")


def _create_admin_and_settings(
    email: str,
    password: str,
    assistant_name: str,
    business: str,
    language: str,
    tone: str,
    debounce_mode: str,
) -> None:
    import asyncio

    async def _seed() -> None:
        from sqlalchemy import select

        from core.admin.auth import hash_password
        from core.db.models import Admin, AppSettings
        from core.db.session import get_session

        async with get_session() as session:
            existing = (
                await session.execute(select(Admin).where(Admin.email == email))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Admin(email=email, password_hash=hash_password(password), role="master")
                )
            settings_row = (
                await session.execute(select(AppSettings).limit(1))
            ).scalar_one_or_none()
            if settings_row is None:
                session.add(
                    AppSettings(
                        assistant_name=assistant_name,
                        business_description=business,
                        language=language,
                        behavior_config={"tono": tone, "debounce_mode": debounce_mode},
                    )
                )
            else:
                settings_row.assistant_name = assistant_name
                settings_row.business_description = business

    asyncio.run(_seed())

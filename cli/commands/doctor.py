"""agent-core doctor: diagnóstico del entorno de un asistente.

Responde a "no arranca y no sé por qué" con la causa concreta, sin tener que
leer trazas de Docker ni de Alembic.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from cli.preflight import (
    docker_available,
    docker_reserved_ports,
    docker_volume_exists,
    parse_db_url,
    parse_redis_url,
    port_available,
    port_owner,
)

console = Console()

OK, WARN, FAIL = "[green]OK[/green]", "[yellow]AVISO[/yellow]", "[red]FALLA[/red]"

REQUIRED_ENV = [
    "DATABASE_URL",
    "REDIS_URL",
    "MASTER_ENCRYPTION_KEY",
    "APP_SECRET_KEY",
    "LLM_PROVIDER",
]


def _read_env_file(path: Path) -> dict[str, str]:
    """Lee un .env plano (sin dependencias): KEY=VALOR, ignora comentarios."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def doctor_command() -> None:
    """Diagnostica el entorno: .env, Docker, puertos, BD, migraciones y plugins."""
    rows: list[tuple[str, str, str]] = []
    problems = 0

    # ---------------------------------------------------------------- .env
    env_path = Path(".env")
    env = _read_env_file(env_path)
    if not env:
        rows.append((".env", FAIL, "No existe. Corre `agent-core init` (docker compose lo exige)."))
        _render(rows)
        console.print("\n[red]Sin .env no se puede diagnosticar más.[/red]")
        raise typer.Exit(1)

    missing = [key for key in REQUIRED_ENV if not env.get(key)]
    if missing:
        problems += 1
        rows.append((".env", FAIL, f"Faltan variables: {', '.join(missing)}"))
    else:
        rows.append((".env", OK, f"{len(env)} variables cargadas"))

    project = env.get("COMPOSE_PROJECT_NAME", "")
    if project:
        rows.append(("Proyecto Docker", OK, project))
    else:
        problems += 1
        rows.append((
            "Proyecto Docker",
            WARN,
            "Sin COMPOSE_PROJECT_NAME: Docker usará el nombre de la carpeta y "
            "otro clon con el mismo nombre compartiría el volumen de datos.",
        ))

    # Variables del shell que pisan al .env (pydantic prioriza el entorno)
    shadowed = [key for key in REQUIRED_ENV if key in os.environ and os.environ[key] != env.get(key)]
    if shadowed:
        problems += 1
        rows.append((
            "Entorno del shell", WARN,
            f"Estas variables del shell pisan al .env: {', '.join(shadowed)}",
        ))

    # -------------------------------------------------------------- docker
    docker_ok, docker_detail = docker_available()
    rows.append(("Docker", OK if docker_ok else FAIL, docker_detail))
    if not docker_ok:
        problems += 1
    docker_ports = docker_reserved_ports() if docker_ok else {}

    # ------------------------------------------------------------- puertos
    for label, key, default in (
        ("Puerto Postgres", "POSTGRES_HOST_PORT", "5432"),
        ("Puerto Redis", "REDIS_HOST_PORT", "6379"),
        ("Puerto API", "APP_HOST_PORT", "8000"),
    ):
        raw = env.get(key, default)
        if not raw.isdigit():
            problems += 1
            rows.append((label, FAIL, f"{key}={raw!r} no es un número"))
            continue
        port = int(raw)
        owner = docker_ports.get(port, "")
        if owner and project and owner.startswith(project):
            rows.append((label, OK, f"{port} (lo publica este proyecto: {owner})"))
        elif owner:
            problems += 1
            rows.append((label, FAIL, f"{port} lo reclama otro contenedor: {owner}"))
        elif port_available(port):
            rows.append((label, OK, f"{port} libre"))
        else:
            problems += 1
            rows.append((label, FAIL, f"{port} ocupado por {port_owner(port, docker_ports)}"))

    # -------------------------------------------------------------- volumen
    if docker_ok and project:
        volume = f"{project}_pgdata"
        if docker_volume_exists(volume):
            rows.append((
                "Volumen de datos", OK,
                f"{volume} existe (las credenciales son las de su creación, no las del .env)",
            ))
        else:
            rows.append(("Volumen de datos", OK, f"{volume} se creará al levantar"))

    # ------------------------------------------------------- conectividad BD
    db_ok = _check_postgres(env, rows)
    if not db_ok:
        problems += 1
    if not _check_redis(env, rows):
        problems += 1

    # ---------------------------------------------------------- migraciones
    if db_ok:
        if not _check_migrations(rows):
            problems += 1

    # -------------------------------------------------------------- plugins
    _check_plugins(rows)

    _render(rows)

    if problems:
        console.print(
            f"\n[red]{problems} problema(s) detectado(s).[/red] "
            "Revisa la columna Detalle: cada fila dice qué hacer."
        )
        raise typer.Exit(1)
    console.print("\n[green]Todo en orden.[/green]")


def _check_postgres(env: dict[str, str], rows: list[tuple[str, str, str]]) -> bool:
    url = env.get("DATABASE_URL", "")
    info = parse_db_url(url)
    try:
        import psycopg2
    except ImportError:
        rows.append(("PostgreSQL", WARN, "psycopg2 no instalado: no puedo probar la conexión"))
        return False
    try:
        conn = psycopg2.connect(
            host=info["host"], port=int(info["port"]), user=info["user"],
            password=info["password"], dbname=info["db"], connect_timeout=5,
        )
        conn.close()
    except Exception as exc:
        text = str(exc).strip().splitlines()[0] if str(exc).strip() else str(exc)
        hint = ""
        low = text.lower()
        if "does not exist" in low or "authentication failed" in low:
            hint = (
                " · Causa habitual: el volumen de datos viene de otra instalación y conserva "
                "sus credenciales. Ajusta el .env o recrea el volumen con `docker compose down -v`."
            )
        rows.append(("PostgreSQL", FAIL, f"{text}{hint}"))
        return False
    rows.append(("PostgreSQL", OK, f"{info['host']}:{info['port']}/{info['db']} responde"))
    return True


def _check_redis(env: dict[str, str], rows: list[tuple[str, str, str]]) -> bool:
    info = parse_redis_url(env.get("REDIS_URL", ""))
    try:
        import redis as redis_lib
    except ImportError:
        rows.append(("Redis", WARN, "redis no instalado: no puedo probar la conexión"))
        return False
    try:
        client = redis_lib.Redis(
            host=info["host"], port=int(info["port"]), socket_connect_timeout=5
        )
        client.ping()
    except Exception as exc:
        rows.append(("Redis", FAIL, str(exc).strip()[:160]))
        return False
    rows.append(("Redis", OK, f"{info['host']}:{info['port']} responde"))
    return True


def _check_migrations(rows: list[tuple[str, str, str]]) -> bool:
    """Compara la revisión aplicada con head, en el core y en cada plugin."""
    try:
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine, text

        from core.db.migrations import _core_config, plugin_dir, project_root, sync_database_url
    except Exception as exc:
        rows.append(("Migraciones", WARN, f"No se pudo inspeccionar: {exc}"))
        return False

    engine = create_engine(sync_database_url().replace("%%", "%"))

    def applied(table: str) -> str:
        try:
            with engine.connect() as conn:
                result = conn.execute(text(f"select version_num from {table}")).scalar()
                return str(result or "")
        except Exception:
            return ""

    healthy = True
    head = ScriptDirectory.from_config(_core_config()).get_current_head() or ""
    current = applied("alembic_version")
    if not current:
        healthy = False
        rows.append(("Migraciones core", FAIL, "Ninguna aplicada. Corre `agent-core db migrate`."))
    elif current != head:
        healthy = False
        rows.append((
            "Migraciones core", FAIL,
            f"Aplicada {current[:12]}, head es {head[:12]}. Corre `agent-core db migrate`.",
        ))
    else:
        rows.append(("Migraciones core", OK, f"al día ({head[:12]})"))

    plugins_root = project_root() / "plugins"
    if plugins_root.exists():
        for directory in sorted(plugins_root.iterdir()):
            if not (directory / "alembic.ini").exists():
                continue
            plugin_id = directory.name
            state = applied(f"alembic_version_{plugin_id}")
            label = f"Migraciones {plugin_id}"
            if not state:
                rows.append((label, WARN, "sin aplicar (se aplican al activar el plugin)"))
            else:
                rows.append((label, OK, f"aplicada {state[:12]}"))
            _ = plugin_dir  # la ruta se resuelve desde la raíz, no desde el CWD
    return healthy


def _check_plugins(rows: list[tuple[str, str, str]]) -> None:
    """Contrasta lo detectado por entry_points con lo presente en plugins/."""
    try:
        from core.db.migrations import project_root
        from core.plugin_manager import plugin_manager

        plugin_manager.scan()
        detected = {module.id for module in plugin_manager.list_modules()}
    except Exception as exc:
        rows.append(("Plugins", WARN, f"No se pudieron escanear: {str(exc)[:120]}"))
        return

    plugins_root = project_root() / "plugins"
    on_disk = {
        directory.name
        for directory in (plugins_root.iterdir() if plugins_root.exists() else [])
        if directory.is_dir() and (directory / "pyproject.toml").exists()
    }

    not_installed = on_disk - detected
    if not_installed:
        rows.append((
            "Plugins", WARN,
            f"Presentes en plugins/ pero no instalados: {', '.join(sorted(not_installed))}"
            " · corre `make install-plugins`",
        ))
    if detected:
        rows.append(("Plugins detectados", OK, ", ".join(sorted(detected))))
    elif not on_disk:
        rows.append(("Plugins", OK, "ninguno instalado"))


def _render(rows: list[tuple[str, str, str]]) -> None:
    table = Table(title="agent-core doctor", show_lines=False)
    table.add_column("Chequeo", style="bold")
    table.add_column("Estado", justify="center")
    table.add_column("Detalle", overflow="fold")
    for name, status, detail in rows:
        table.add_row(name, status, detail)
    console.print(table)

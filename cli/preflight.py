"""Chequeos previos al arranque: puertos, Docker y plantilla del `.env`.

Compartido por `agent-core init` y `agent-core doctor`. Las funciones son puras o
consultan el entorno sin modificarlo, para poder testearlas sin levantar nada.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import unicodedata
from urllib.parse import urlparse, urlunparse

PORT_SCAN_RANGE = 50

# Base URL y modelos por proveedor. Ojo: `LLM_BASE_URL` solo lo consume el
# proveedor `openai_compatible` (ver core/llm/__init__.py); para el resto se
# escribe por coherencia del .env. `LLM_MODEL_REASONING` sí es funcional en
# todos: el difficulty routing lo usa tal cual contra el proveedor activo.
LLM_DEFAULTS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "reasoning_model": "deepseek-reasoner",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "model": "gpt-4o-mini",
        "reasoning_model": "gpt-4o",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-4-5",
        "reasoning_model": "claude-sonnet-4-5",
    },
}


class PortUnavailable(RuntimeError):
    """No hay ningún puerto libre en el rango explorado."""


# --------------------------------------------------------------------- puertos
def port_available(port: int) -> bool:
    """¿Puede Docker publicar este puerto?

    Docker publica en 0.0.0.0, así que el puerto debe estar libre en TODAS las
    interfaces: un bind a 127.0.0.1 devolvería "libre" con un servicio
    escuchando en una IP concreta, y el `docker up` fallaría después.
    Sin SO_REUSEADDR a propósito: con esa opción el bind tiene éxito sobre
    sockets en TIME_WAIT y reaparecen los falsos positivos.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_free_port(
    start: int,
    label: str,
    reserved: set[int] | None = None,
    docker_ports: dict[int, str] | None = None,
) -> int:
    """Primer puerto publicable >= start.

    `reserved` son los puertos ya asignados en esta misma ejecución (sin esto,
    dos búsquedas seguidas devuelven el mismo número). `docker_ports` evita los
    que reclamará un contenedor existente aunque ahora esté parado.
    """
    taken = set(reserved or ())
    claimed = docker_ports if docker_ports is not None else docker_reserved_ports()
    for port in range(start, start + PORT_SCAN_RANGE):
        if port in taken or port in claimed:
            continue
        if port_available(port):
            return port
    raise PortUnavailable(
        f"No hay puerto libre para {label} entre {start} y {start + PORT_SCAN_RANGE - 1}"
    )


def port_owner(port: int, docker_ports: dict[int, str] | None = None) -> str:
    """Quién ocupa un puerto, en texto legible. '' si está libre."""
    claimed = docker_ports if docker_ports is not None else docker_reserved_ports()
    if port in claimed:
        return f"contenedor Docker '{claimed[port]}'"
    if not port_available(port):
        return "otro proceso del host"
    return ""


# ---------------------------------------------------------------------- docker
def _docker(args: list[str], timeout: int = 15) -> str:
    """Corre un comando docker; devuelve stdout, o '' si Docker no responde."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def docker_available() -> tuple[bool, str]:
    """(disponible, detalle). Distingue 'no instalado' de 'daemon caído'."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        return False, "docker no está instalado o no está en el PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"no se pudo consultar docker: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[0] if detail else "el daemon de Docker no responde"
    return True, f"Docker {result.stdout.strip()}"


def docker_reserved_ports() -> dict[int, str]:
    """Puertos publicados por contenedores existentes, incluidos los parados.

    Un contenedor parado no ocupa el puerto ahora pero lo reclamará al arrancar,
    así que cuenta como ocupado. `docker ps --format {{.Ports}}` no los lista;
    hay que mirar la configuración con inspect.
    """
    ids = _docker(["ps", "-aq"]).split()
    if not ids:
        return {}
    raw = _docker(["inspect", "--format", "{{.Name}}\t{{json .HostConfig.PortBindings}}", *ids])
    reserved: dict[int, str] = {}
    for line in raw.splitlines():
        name, _, payload = line.partition("\t")
        try:
            bindings = json.loads(payload) or {}
        except json.JSONDecodeError:
            continue
        for specs in bindings.values():
            for spec in specs or []:
                host_port = (spec or {}).get("HostPort")
                if host_port and host_port.isdigit():
                    reserved.setdefault(int(host_port), name.lstrip("/"))
    return reserved


def docker_volume_exists(name: str) -> bool:
    return bool(_docker(["volume", "inspect", name]))


# ------------------------------------------------------------------------ urls
def parse_db_url(url: str) -> dict[str, str]:
    """Extrae user, password, host, port y dbname del DATABASE_URL."""
    parsed = urlparse(url)
    return {
        "user": parsed.username or "agent_core",
        "password": parsed.password or "agent_core",
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "db": parsed.path.lstrip("/") or "agent_core",
    }


def parse_redis_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {"host": parsed.hostname or "localhost", "port": str(parsed.port or 6379)}


def update_url_port(url: str, new_port: int) -> str:
    """Reemplaza el puerto de una URL preservando credenciales y ruta.

    Se reconstruye desde urlparse en vez de sustituir por regex: una contraseña
    que termine en dígitos, o una URL sin puerto explícito, rompen el patrón
    ingenuo `(://[^/]+):(\\d+)`.
    """
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    if ":" in host:  # IPv6
        host = f"[{host}]"
    netloc = f"{host}:{new_port}"
    if parsed.username:
        # username/password vienen ya codificados en la URL: no re-escapar.
        credentials = parsed.username
        if parsed.password:
            credentials = f"{credentials}:{parsed.password}"
        netloc = f"{credentials}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def slugify(value: str, fallback: str = "agent-core") -> str:
    """Nombre apto para COMPOSE_PROJECT_NAME (minúsculas, guiones, sin acentos)."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or fallback


def llm_defaults(provider: str) -> dict[str, str]:
    """Base URL y modelos del proveedor elegido (deepseek si es desconocido)."""
    return LLM_DEFAULTS.get(provider, LLM_DEFAULTS["deepseek"])


# ------------------------------------------------------------ errores de docker
def explain_docker_error(stderr: str, project: str = "", ports: dict[str, int] | None = None) -> str:
    """Traduce un fallo de docker/alembic a una causa accionable.

    Devuelve '' si no se reconoce el error, para no inventar diagnósticos.
    """
    text = (stderr or "").lower()

    if "port is already allocated" in text or "address already in use" in text:
        match = re.search(r"0\.0\.0\.0:(\d+)", stderr or "")
        detail = ""
        if match:
            port = int(match.group(1))
            owner = port_owner(port)
            detail = f" El puerto {port} lo ocupa {owner}." if owner else f" El puerto {port} está ocupado."
        return (
            f"Un puerto ya está en uso por otro servicio.{detail}\n"
            "  Corrige POSTGRES_HOST_PORT / REDIS_HOST_PORT / APP_HOST_PORT en el .env, "
            "o para el servicio que lo ocupa."
        )

    if "password authentication failed" in text or ("role" in text and "does not exist" in text):
        volume = f"{project}_pgdata" if project else "<proyecto>_pgdata"
        return (
            "PostgreSQL rechazó las credenciales del .env.\n"
            f"  Causa habitual: el volumen '{volume}' ya existía de una instalación anterior.\n"
            "  PostgreSQL solo aplica POSTGRES_USER/PASSWORD/DB la PRIMERA vez que inicializa\n"
            "  el directorio de datos; después conserva las credenciales antiguas.\n"
            "  Salidas: usa las credenciales antiguas en el .env, o borra el volumen\n"
            "  (DESTRUYE los datos):  docker compose down -v"
        )

    if "does not exist" in text and "database" in text:
        return (
            "La base de datos indicada en DATABASE_URL no existe en el servidor.\n"
            "  Si el volumen viene de otra instalación, su nombre de BD es distinto al del .env."
        )

    if "cannot connect to the docker daemon" in text or "is the docker daemon running" in text:
        return "El daemon de Docker no está corriendo. Arráncalo y repite el comando."

    if "no such file or directory" in text and "docker" in text:
        return "No se encontró el ejecutable de Docker. Instálalo o añádelo al PATH."

    return ""

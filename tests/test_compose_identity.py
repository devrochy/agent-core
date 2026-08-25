"""Identidad del proyecto Docker.

Regresión: con un valor por defecto en `name:`, cualquier .env que no declarase
COMPOSE_PROJECT_NAME caía en el mismo proyecto que los demás clones del
framework y compartía el volumen pgdata sin avisar.
"""

from __future__ import annotations

from pathlib import Path

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_el_compose_declara_un_nombre_de_proyecto() -> None:
    assert "name: ${COMPOSE_PROJECT_NAME" in COMPOSE.read_text()


def test_compose_project_name_es_obligatorio_sin_valor_por_defecto() -> None:
    """`:?` hace fallar el arranque con un mensaje; `:-` lo haría compartir volumen."""
    linea = next(
        line for line in COMPOSE.read_text().splitlines() if line.startswith("name:")
    )
    assert "${COMPOSE_PROJECT_NAME:?" in linea, linea
    assert ":-" not in linea, "un valor por defecto haría colisionar los clones"


def test_el_mensaje_de_error_dice_como_arreglarlo() -> None:
    linea = next(
        line for line in COMPOSE.read_text().splitlines() if line.startswith("name:")
    )
    assert "agent-core init" in linea


def test_init_escribe_compose_project_name() -> None:
    from cli.commands.init import ENV_TEMPLATE

    assert "COMPOSE_PROJECT_NAME={project_name}" in ENV_TEMPLATE


def test_cada_servicio_declara_su_rol() -> None:
    """AGENT_CORE_ROLE decide quién instala plugins y quién aplica migraciones."""
    contenido = COMPOSE.read_text()
    for rol in ("AGENT_CORE_ROLE: api", "AGENT_CORE_ROLE: worker", "AGENT_CORE_ROLE: beat"):
        assert rol in contenido

"""Generador del SDD vivo.

El estado del asistente vive en sdd/.state.json (creado por `init`, actualizado
por `plugin install`). Los documentos sdd/*.md se REGENERAN desde las plantillas
Jinja2 de sdd/templates/ con ese estado. Nunca edites los .md a mano:
edita el estado o las plantillas y corre `agent-core sdd update`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

SDD_DIR = Path("sdd")
STATE_FILE = SDD_DIR / ".state.json"
TEMPLATES_DIR = SDD_DIR / "templates"


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        result = json.loads(STATE_FILE.read_text())
        if isinstance(result, dict):
            return result
    return {}


def save_state(state: dict[str, Any]) -> None:
    SDD_DIR.mkdir(exist_ok=True)
    state["updated_at"] = datetime.now(UTC).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def append_changelog(state: dict[str, Any], entry: str) -> None:
    changelog = state.setdefault("changelog", [])
    changelog.append({"date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"), "entry": entry})


def render_all(state: dict[str, Any]) -> list[str]:
    """Renderiza cada plantilla *.md.j2 -> sdd/*.md. Devuelve los archivos generados."""
    if not TEMPLATES_DIR.exists():
        return []
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), keep_trailing_newline=True)
    generated: list[str] = []
    for template_path in sorted(TEMPLATES_DIR.glob("*.md.j2")):
        template = env.get_template(template_path.name)
        output_name = template_path.name.removesuffix(".j2")
        output_path = SDD_DIR / output_name
        output_path.write_text(template.render(**state))
        generated.append(str(output_path))
    return generated

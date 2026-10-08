"""Presets de agente (ADR-0110, HU-FWK-03).

Un preset es un **manifiesto corto** (`core/presets/<id>.json`) que referencia packs;
cada pack aporta su **fragmento** por vertical (`packages/pack-x/presets/<vertical>.json`:
config sin secretos + datos de ejemplo para `seed_data`). `apply_preset` compone las dos
cosas sobre un negocio, sin copiar nada de los packs y sin prompts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agentcore_contracts.contracts import Tone
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

PRESETS_DIR = Path(__file__).resolve().parent
REPO_ROOT = PRESETS_DIR.parent.parent
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class PresetError(ValueError):
    """El preset o un fragmento no es válido, o no se puede aplicar al negocio."""


class PresetPersona(BaseModel):
    """Campos estructurados del `AgentSpec.persona`: nada de texto libre (invariante 3)."""

    model_config = ConfigDict(extra="forbid")
    tone: Tone | None = None
    warmth: int | None = Field(default=None, ge=0, le=5)
    emojis: Literal["none", "sparse", "free"] | None = None
    maxLength: int | None = Field(default=None, ge=80, le=2000)


class PresetAppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    split_style: str | None = None
    language: str | None = Field(default=None, max_length=8)
    currency: str | None = Field(default=None, max_length=3)
    timezone: str | None = Field(default=None, max_length=64)


class PresetManifest(BaseModel):
    """Manifiesto de un preset (ADR-0110 § 1). `extra="forbid"`: un campo desconocido
    (por ejemplo, un prompt colado) es un error de carga, no un dato."""

    model_config = ConfigDict(extra="forbid")
    id: str
    version: int = Field(ge=1)
    nombre: str = Field(max_length=80)
    descripcion: str = Field(default="", max_length=300)
    plan_minimo: str
    packs: list[str] = Field(min_length=1)
    playbooks: list[str] = Field(default_factory=list)
    persona: PresetPersona = Field(default_factory=PresetPersona)
    app_settings: PresetAppSettings = Field(default_factory=PresetAppSettings)
    conexiones_requeridas: list[str] = Field(default_factory=list)
    pasos_de_activacion: list[str] = Field(default_factory=list)
    golden_set: str | None = None


@dataclass(frozen=True)
class PackFragment:
    pack_id: str
    vertical: str
    config: dict[str, Any] = field(default_factory=dict)
    seed: dict[str, Any] = field(default_factory=dict)


def _check_id(value: str, que: str) -> None:
    if not _ID_RE.match(value):
        raise PresetError(f"{que} inválido: {value!r}")


def load_preset(preset_id: str) -> PresetManifest:
    _check_id(preset_id, "Id de preset")
    path = PRESETS_DIR / f"{preset_id}.json"
    if not path.exists():
        raise PresetError(f"No existe el preset {preset_id!r}")
    try:
        manifest = PresetManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise PresetError(f"Manifiesto inválido {preset_id!r}: {exc}") from exc
    if manifest.id != preset_id:
        raise PresetError(f"El manifiesto {path.name} declara id {manifest.id!r}")
    return manifest


def list_presets() -> list[PresetManifest]:
    return [load_preset(p.stem) for p in sorted(PRESETS_DIR.glob("*.json"))]


def _pack_dir(pack_id: str) -> Path | None:
    for pattern in ("packages/pack-*", "plugins/*"):
        for folder in REPO_ROOT.glob(pattern):
            if folder.name.replace("-", "_") == pack_id:
                return folder
    return None


def _secret_fields(config_schema: Any) -> set[str]:
    if config_schema is None:
        return set()
    return {
        name
        for name, info in config_schema.model_fields.items()
        if info.annotation is SecretStr or "SecretStr" in str(info.annotation)
    }


def load_fragment(pack_id: str, vertical: str, config_schema: Any = None) -> PackFragment:
    """Fragmento del pack para la vertical. Sin archivo → fragmento vacío (defaults).

    Rechaza claves de config que el `config_schema` del pack no declara y cualquier
    campo secreto (`SecretStr`): los secretos los carga el negocio, nunca un preset.
    """
    _check_id(vertical, "Vertical")
    folder = _pack_dir(pack_id)
    path = folder / "presets" / f"{vertical}.json" if folder else None
    if path is None or not path.exists():
        return PackFragment(pack_id=pack_id, vertical=vertical)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PresetError(f"Fragmento inválido {pack_id}/{vertical}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {"config", "seed"}:
        raise PresetError(f"Fragmento {pack_id}/{vertical}: solo admite 'config' y 'seed'")
    config = dict(raw.get("config") or {})
    if config_schema is not None:
        desconocidas = set(config) - set(config_schema.model_fields)
        if desconocidas:
            raise PresetError(f"Fragmento {pack_id}/{vertical}: claves desconocidas {sorted(desconocidas)}")
        secretas = set(config) & _secret_fields(config_schema)
        if secretas:
            raise PresetError(f"Fragmento {pack_id}/{vertical}: no puede traer secretos {sorted(secretas)}")
    return PackFragment(pack_id=pack_id, vertical=vertical, config=config, seed=dict(raw.get("seed") or {}))


__all__ = [
    "PackFragment",
    "PresetError",
    "PresetManifest",
    "list_presets",
    "load_fragment",
    "load_preset",
]

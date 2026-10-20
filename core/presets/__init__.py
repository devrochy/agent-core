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
from typing import Annotated, Any, Literal

from agentcore_contracts.contracts import Tone
from agentcore_contracts.kernelspec import RoutingWhen
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

PRESETS_DIR = Path(__file__).resolve().parent
PLANTILLAS_DIR = PRESETS_DIR / "plantillas"
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
    # Cómo lo llama un cliente ("tienda", "pizzería"): el resolver del tipo de negocio de un plan
    # (pack-commerce, adenda 2026-10-13 de ADR-0113) lo lleva al id del preset. Dato del preset.
    sinonimos: list[Annotated[str, Field(min_length=2, max_length=40)]] = Field(default_factory=list, max_length=40)
    plan_minimo: str
    packs: list[str] = Field(min_length=1)
    playbooks: list[str] = Field(default_factory=list)
    persona: PresetPersona = Field(default_factory=PresetPersona)
    app_settings: PresetAppSettings = Field(default_factory=PresetAppSettings)
    conexiones_requeridas: list[str] = Field(default_factory=list)
    pasos_de_activacion: list[str] = Field(default_factory=list)
    golden_set: str | None = None
    # HU-SH-01: proveedor de suscripciones que fija el preset (HU-SUB-09); debe ser
    # uno de sus packs. Solo se guarda si queda habilitado.
    proveedor_suscripciones: str | None = None
    # Presets v2 (ADR-0139, HU-AOS-46): keys de las plantillas de agente especialista
    # (`core/presets/plantillas/<key>.json`) que el preset instala en un negocio con el Agent OS
    # (`aos_runtime = 'v3'`). Sin `agentes` el manifiesto es v1: el negocio sigue con el General.
    agentes: list[Annotated[str, Field(pattern=r"^[a-z0-9_]{2,40}$")]] = Field(default_factory=list, max_length=10)

    @property
    def es_v2(self) -> bool:
        return bool(self.agentes)


class ReglaDePlantilla(BaseModel):
    """Regla del Coordinador que aporta una plantilla: `when` del contrato `kernelspec/1`
    (DSL sin regex); el destino es siempre el agente de la plantilla."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^preset_[a-z0-9_]{1,33}$")
    when: RoutingWhen


class PlantillaAgente(BaseModel):
    """Plantilla de un agente especialista «para un fin» (ADR-0139). Es configuración del core,
    como el manifiesto: solo datos (nombres, ids y frases de enrutamiento), nunca prompts. El
    contenido `agentspec/3` del agente se DERIVA al aplicarla (`core/presets/agentes.py`)."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    nombre: str = Field(min_length=1, max_length=80)
    # Metadato del agente (`agents.description` y `description` del agentspec/3): no entra al prompt.
    descripcion: str = Field(default="", max_length=300)
    # Todas tienen que estar habilitadas en el negocio; si no, la plantilla no se instala.
    habilidades: list[str] = Field(min_length=1, max_length=10)
    # Se suman si están habilitadas (p. ej. pagos para Ventas, que suele quedar pendiente de claves).
    opcionales: list[str] = Field(default_factory=list, max_length=10)
    # Acota una habilidad opcional a algunas de sus acciones (HU-AOS-22): p. ej. Agenda toma solo
    # `crear_lead` de pack_commerce, no la caja de ventas. Una opcional sin entrada entra completa.
    acciones_opcionales: dict[
        str, list[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]]
    ] = Field(default_factory=dict, max_length=10)
    # Ids `procedure/1` publicados por esas habilidades: el agente los declara con `^M.0`.
    procedimientos: list[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,60}\.[a-z0-9_]{1,40}$")]] = Field(
        min_length=1, max_length=20
    )
    playbooks: list[Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]] = Field(default_factory=list, max_length=20)
    reglas: list[ReglaDePlantilla] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _coherente(self) -> PlantillaAgente:
        from core.agents.tolerante import KEYS_RESERVADAS

        if self.key in KEYS_RESERVADAS:
            raise ValueError(f"la key {self.key!r} está reservada para los agentes del sistema")
        fuera = sorted(set(self.acciones_opcionales) - (set(self.opcionales) - set(self.habilidades)))
        if fuera:
            raise ValueError(f"acciones_opcionales de habilidades que no son opcionales (o que son obligatorias): {fuera}")
        if any(not acciones for acciones in self.acciones_opcionales.values()):
            raise ValueError("acciones_opcionales: cada habilidad lista al menos una acción")
        propias = {*self.habilidades, *self.opcionales}
        ajenos = sorted(p for p in self.procedimientos if p.split(".", 1)[0] not in propias)
        if ajenos:
            raise ValueError(f"procedimientos de habilidades que la plantilla no declara: {ajenos}")
        return self


def load_plantilla(key: str) -> PlantillaAgente:
    if not re.fullmatch(r"[a-z0-9_]{2,40}", key):
        raise PresetError(f"Key de plantilla inválida: {key!r}")
    path = PLANTILLAS_DIR / f"{key}.json"
    if not path.exists():
        raise PresetError(f"No existe la plantilla de agente {key!r}")
    try:
        plantilla = PlantillaAgente.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise PresetError(f"Plantilla inválida {key!r}: {exc}") from exc
    if plantilla.key != key:
        raise PresetError(f"La plantilla {path.name} declara key {plantilla.key!r}")
    return plantilla


def plantillas_del_preset(manifest: PresetManifest) -> list[PlantillaAgente]:
    """Las plantillas del manifiesto, validadas; una que no existe es error de carga."""
    plantillas = [load_plantilla(k) for k in manifest.agentes]
    reglas = [r.id for p in plantillas for r in p.reglas]
    if len(reglas) != len(set(reglas)):
        raise PresetError(f"El preset {manifest.id} repite ids de reglas del Coordinador")
    return plantillas


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
    for plantilla in plantillas_del_preset(manifest):
        faltan = sorted(set(plantilla.habilidades) - set(manifest.packs))
        if faltan:
            raise PresetError(f"El preset {preset_id}: la plantilla {plantilla.key!r} usa {faltan}, que no están en sus packs")
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
    "PlantillaAgente",
    "PresetError",
    "PresetManifest",
    "ReglaDePlantilla",
    "list_presets",
    "load_fragment",
    "load_plantilla",
    "load_preset",
    "plantillas_del_preset",
]

"""Manifiesto de una release por conexión, forma `release/2` (ADR-0117 §2, HU-AOS-14; HU-AOS-16). Puro.

Una release congela **entradas**, nunca contenido del core (que va con el binario y lo cubre
el CI de la release del motor, ADR-0117 §2 y ADR-0120):

- `components`: la versión exacta de cada instancia de la conexión (`release_components` es
  la fuente relacional; aquí va la copia legible con su `content_hash`). El KernelSpec del
  Coordinador trae las políticas. Desde `release/2` (HU-AOS-16) cada componente lleva además
  `is_default` (el agente por defecto de la conexión, congelado: "aplica con la próxima
  release") y `procedures`: los `ProcedureRef` de su versión RESUELTOS a una versión exacta
  (`[{id, version}]`; `0.0.0` es un procedimiento abierto). Los dos entran al hash.
- `skills`: la versión exacta de cada habilidad de la plataforma efectiva en el scope de la
  conexión (las instalaciones de conexión reemplazan enteras a las del negocio, ADR-0118 §2),
  `(pack_id, version, manifest_hash)` registrada en `pack_versions`; `tenant_plugins`: los
  plugins del negocio (ADR-0089) solo por nombre.
- `playbook_overrides`: los overrides del negocio (ADR-0026) de los nombres de la allowlist,
  con su texto y hash. En runtime (con `aos_runtime = 'v3'`) se leen de aquí, no de `TENANTS_ROOT`.
- `flow_tenant`: las secciones crudas del `flow.json` del negocio (ADR-0109) con su hash, o
  `null`. Los procedimientos abiertos se derivan al cargar la release (flujo del core y de los
  packs del binario + estas secciones), no se congelan.
- `app_settings`: los campos que entran al prompt (`brand_voice`, `assistant_name`,
  `business_description`, `language`).
- `built_with` (FUERA del hash): `git_sha` y versión de los contratos, para auditar. Un deploy
  no produce releases nuevas por sí solo.

`manifest_hash` = sha256 del JSON canónico del manifiesto sin `built_with`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.releases.packs import sha256_texto, texto_canonico

SCHEMA = "release/2"
# `release/1` (HU-AOS-14) no trae `is_default` ni `procedures` por componente: el runtime la
# lee con la regla transitoria de HU-AOS-05 (procedimientos del binario de los packs habilitados).
SCHEMA_SIN_PROCEDIMIENTOS = "release/1"
FUERA_DEL_HASH = ("built_with",)
_ORDEN_ROL = {"core": 0, "specialist": 1, "backoffice": 2}


@dataclass(frozen=True)
class Componente:
    instance_id: str
    agent_id: str
    agent_key: str
    role: str
    agent_version_id: str
    version: int
    content_hash: str
    contract: str
    is_default: bool = False
    # `(id, versión exacta)` resueltos en el build; vacío en el KernelSpec.
    procedures: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Habilidad:
    pack_id: str
    version: str
    manifest_hash: str


def _componente(c: Componente) -> dict[str, Any]:
    return {
        "instance_id": c.instance_id,
        "agent_id": c.agent_id,
        "agent_key": c.agent_key,
        "role": c.role,
        "agent_version_id": c.agent_version_id,
        "version": c.version,
        "content_hash": c.content_hash,
        "contract": c.contract,
        "is_default": c.is_default,
        "procedures": [{"id": pid, "version": v} for pid, v in sorted(c.procedures)],
    }


def armar(
    *,
    componentes: Iterable[Componente],
    habilidades: Iterable[Habilidad],
    plugins_del_negocio: Iterable[str],
    overrides: Mapping[str, str],
    flujo_del_negocio: Any,
    app_settings: Mapping[str, str | None],
    built_with: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """El manifiesto en su orden canónico (mismas entradas → mismo hash)."""
    comps = sorted(
        componentes, key=lambda c: (_ORDEN_ROL.get(c.role, 9), c.agent_key, c.instance_id)
    )
    flujo = None
    if flujo_del_negocio is not None:
        flujo = {
            "sha256": sha256_texto(texto_canonico(flujo_del_negocio)),
            "sections": flujo_del_negocio,
        }
    return {
        "schema": SCHEMA,
        "components": [_componente(c) for c in comps],
        "skills": [
            {"pack_id": h.pack_id, "version": h.version, "manifest_hash": h.manifest_hash}
            for h in sorted(habilidades, key=lambda h: h.pack_id)
        ],
        "tenant_plugins": sorted(set(plugins_del_negocio)),
        "playbook_overrides": [
            {
                "name": nombre,
                "sha256": sha256_texto(texto),
                "bytes": len(texto.encode("utf-8")),
                "text": texto,
            }
            for nombre, texto in sorted(overrides.items())
        ],
        "flow_tenant": flujo,
        "app_settings": {k: app_settings[k] for k in sorted(app_settings)},
        "built_with": dict(built_with or {}),
    }


def hash_de(manifest: Mapping[str, Any]) -> str:
    contenido = {k: v for k, v in manifest.items() if k not in FUERA_DEL_HASH}
    return hashlib.sha256(texto_canonico(contenido).encode("utf-8")).hexdigest()


def _cuantas(secciones: Any) -> int:
    if isinstance(secciones, Mapping):
        secciones = secciones.get("sections")
    return len(secciones) if isinstance(secciones, list) else 0


def resumen(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """El manifiesto sin textos (para la API): hashes y tamaños."""
    flujo = manifest.get("flow_tenant")
    return {
        "schema": manifest.get("schema"),
        "components": manifest.get("components", []),
        "skills": manifest.get("skills", []),
        "tenant_plugins": manifest.get("tenant_plugins", []),
        "playbook_overrides": [
            {"name": p.get("name"), "sha256": p.get("sha256"), "bytes": p.get("bytes")}
            for p in manifest.get("playbook_overrides", [])
        ],
        "flow_tenant": None
        if not flujo
        else {"sha256": flujo.get("sha256"), "sections": _cuantas(flujo.get("sections"))},
        "app_settings": {
            k: {"sha256": sha256_texto(str(v))}
            for k, v in (manifest.get("app_settings") or {}).items()
        },
        "built_with": manifest.get("built_with", {}),
    }


__all__ = [
    "FUERA_DEL_HASH",
    "SCHEMA",
    "SCHEMA_SIN_PROCEDIMIENTOS",
    "Componente",
    "Habilidad",
    "armar",
    "hash_de",
    "resumen",
]

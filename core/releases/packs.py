"""Versiones de las habilidades de la plataforma: manifiesto, hash y sync a `pack_versions`
(ADR-0117 §1, ADR-0118 §1, HU-AOS-14).

- `manifiesto_de_pack(module)`: el contenido de una versión de una habilidad del binario
  (`packages/pack-*`): sus acciones (nombre y scopes; el runtime usa el código del binario),
  sus playbooks (nombre, hash y texto), las secciones de su `flow.json` y sus procedimientos
  publicados (`procedures/*.json`, HU-AOS-07, con el mismo lector que los registra); por ahora sin
  consultas ni plantillas (HU-AOS-16). El texto es el JSON
  canónico (claves ordenadas, sin espacios, UTF-8) y `manifest_hash` su sha256.
- `sync_packs(modules)`: registra cada versión con `register_pack_version()` (SECURITY DEFINER,
  migración 0079), que recalcula el sha256 en la base sobre el MISMO texto y rechaza la fila si
  no coincide; con la misma `(pack_id, version)` y otro hash no la registra y alerta. Corre en el
  arranque del contenedor `api`, después de migrar, con el rol dueño (`MIGRATIONS_DATABASE_URL`):
  `app_runtime` no tiene `EXECUTE`. Nunca bloquea el arranque.
- El CI (`tests/test_pack_versions_lock.py`) compara los hashes con el lock commiteado
  (`packages/pack-versions.lock.json`): cambiar el contenido de un pack exige subir su versión.

Solo habilidades de la plataforma: la función rechaza un plugin con `owner_tenant_id`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

CONTRATO_PACK = "pack/2"
# El lock vive junto a los packs: `scripts/pack_versions_lock.py` lo regenera.
LOCK_PATH = Path(__file__).resolve().parent.parent.parent / "packages" / "pack-versions.lock.json"
# Tope de un playbook (ADR-0014), igual que `core.agents.playbooks.MAX_SIZE_BYTES`.
_MAX_PLAYBOOK_BYTES = 8 * 1024
RESULTADOS = (
    "inserted",
    "exists",
    "yanked",
    "hash_mismatch",
    "hash_invalid",
    "invalid_manifest",
    "invalid_input",
    "not_platform",
    "unknown_pack",
)
# Resultados que el arranque reporta como fallo (alerta en el log; no frenan el arranque).
ALERTAN = frozenset({"hash_mismatch", "hash_invalid", "invalid_manifest", "invalid_input"})


def texto_canonico(valor: Any) -> str:
    """JSON canónico: claves ordenadas, sin espacios, UTF-8 (el mismo de `versiones.content_hash`)."""
    return json.dumps(valor, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManifiestoDePack:
    pack_id: str
    version: str
    manifest: Mapping[str, Any]
    texto: str
    manifest_hash: str
    contract: str = CONTRATO_PACK


def _carpeta(module: Any) -> Path | None:
    try:
        return Path(inspect.getfile(type(module))).resolve().parent.parent
    except (TypeError, OSError):
        return None


def _tools(module: Any) -> list[dict[str, Any]]:
    tools: dict[str, dict[str, Any]] = {}
    for skill_cls in getattr(module, "declared_skills", None) or []:
        for tool in skill_cls().get_tools():
            tools.setdefault(tool.name, {"name": tool.name, "scopes": sorted(tool.scopes or ())})
    return [tools[n] for n in sorted(tools)]


def _playbooks(carpeta: Path | None) -> list[dict[str, Any]]:
    if carpeta is None or not (carpeta / "playbooks").is_dir():
        return []
    salida: list[dict[str, Any]] = []
    for archivo in sorted((carpeta / "playbooks").glob("*.md")):
        texto = archivo.read_text(encoding="utf-8")
        if len(texto.encode("utf-8")) > _MAX_PLAYBOOK_BYTES:
            raise ValueError(f"playbook demasiado grande en el pack: {archivo.name}")
        salida.append({"name": archivo.stem, "sha256": sha256_texto(texto), "text": texto})
    return salida


def _flujo(carpeta: Path | None) -> Any:
    if carpeta is None or not (carpeta / "flow.json").is_file():
        return []
    return json.loads((carpeta / "flow.json").read_text(encoding="utf-8"))


def _procedimientos(carpeta: Path | None) -> list[Any]:
    from core.kernel.procedures.archivos import CARPETA, leer_crudos

    return leer_crudos(carpeta / CARPETA if carpeta is not None else None)


def manifiesto_de_pack(module: Any) -> ManifiestoDePack:
    """Manifiesto de la versión del binario de una habilidad. Determinista: el mismo código
    da el mismo texto y el mismo hash en cualquier proceso."""
    carpeta = _carpeta(module)
    manifest = {
        "provides": {
            "tools": _tools(module),
            "playbooks": _playbooks(carpeta),
            "flow": _flujo(carpeta),
            "procedures": _procedimientos(carpeta),
            "consultas": [],
            "agent_templates": [],
        },
        "requires": {},
    }
    texto = texto_canonico(manifest)
    return ManifiestoDePack(
        pack_id=str(module.id),
        version=str(module.version),
        manifest=manifest,
        texto=texto,
        manifest_hash=sha256_texto(texto),
    )


_PACKAGES = Path(__file__).resolve().parent.parent.parent / "packages"


def es_de_plataforma(module: Any) -> bool:
    """¿El módulo es una habilidad de la plataforma (`packages/pack-*`)? Los plugins del negocio
    (ADR-0089) usan el mismo grupo de entry points y nunca entran al catálogo global."""
    carpeta = _carpeta(module)
    return carpeta is not None and carpeta.parent == _PACKAGES and carpeta.name.startswith("pack-")


def manifiestos(modules: Iterable[Any]) -> dict[str, ManifiestoDePack]:
    return {m.pack_id: m for m in (manifiesto_de_pack(mod) for mod in modules)}


# ------------------------------------------------------------------ lock del CI
def leer_lock(path: Path = LOCK_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def lock_actualizado(
    actual: Mapping[str, Mapping[str, str]], vigentes: Mapping[str, ManifiestoDePack]
) -> dict[str, dict[str, str]]:
    """El lock con las versiones vigentes agregadas (las viejas se conservan: son historia)."""
    nuevo = {pid: dict(versiones) for pid, versiones in actual.items()}
    for pid, man in vigentes.items():
        nuevo.setdefault(pid, {})[man.version] = man.manifest_hash
    return {pid: dict(sorted(v.items())) for pid, v in sorted(nuevo.items())}


def problemas_del_lock(
    lock: Mapping[str, Mapping[str, str]], vigentes: Mapping[str, ManifiestoDePack]
) -> list[str]:
    """Qué no coincide entre el binario y el lock (vacío si todo está bien)."""
    problemas: list[str] = []
    for pid, man in sorted(vigentes.items()):
        registrado = lock.get(pid, {}).get(man.version)
        if registrado is None:
            problemas.append(
                f"{pid} {man.version}: versión sin registrar en el lock "
                "(corre `python scripts/pack_versions_lock.py`)"
            )
        elif registrado != man.manifest_hash:
            problemas.append(
                f"{pid} {man.version}: cambió el contenido (playbooks, flow.json o acciones) sin subir "
                "la versión del pack; súbela y regenera el lock"
            )
    return problemas


# ------------------------------------------------------------------ sync a pack_versions
@dataclass
class ReporteSync:
    resultados: Counter[str]
    errores: list[str]

    @property
    def fallo(self) -> bool:
        return bool(self.errores) or any(self.resultados.get(r) for r in ALERTAN)


async def registrar(session: Any, man: ManifiestoDePack) -> str:
    """Registra una versión con la función SECURITY DEFINER. Devuelve su resultado."""
    from sqlalchemy import text

    resultado = (
        await session.execute(
            text("select register_pack_version(:pack_id, :version, :contract, :texto, :hash)"),
            {
                "pack_id": man.pack_id,
                "version": man.version,
                "contract": man.contract,
                "texto": man.texto,
                "hash": man.manifest_hash,
            },
        )
    ).scalar_one()
    return str(resultado)


async def sync_packs(modules: Iterable[Any], *, database_url: str | None = None) -> ReporteSync:
    """Registra la versión de cada habilidad del binario en `pack_versions` (idempotente).

    `database_url`: por defecto `MIGRATIONS_DATABASE_URL` (el dueño; ver el docstring del
    módulo). Antes registra los plugins en `plugins` (FK de `pack_versions`): en el arranque el
    sync corre antes que `plugin_manager.sync_registry()` de la app."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    url = database_url or settings.migrations_database_url
    reporte = ReporteSync(resultados=Counter(), errores=[])
    if not url:
        # `app_runtime` no tiene EXECUTE: sin el rol dueño el sync no puede registrar nada.
        reporte.errores.append("falta MIGRATIONS_DATABASE_URL (el sync corre con el rol dueño)")
        log.error("pack_versions_sync_failed", error="missing_migrations_database_url")
        return reporte
    lista = [m for m in modules if es_de_plataforma(m)]
    try:
        vigentes = manifiestos(lista)
    except Exception as exc:  # un pack roto no tumba el arranque: se reporta
        reporte.errores.append(f"manifiesto: {type(exc).__name__}: {str(exc)[:200]}")
        log.error("pack_versions_sync_failed", error=type(exc).__name__)
        return reporte
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            from sqlalchemy import text

            # Dos arranques a la vez (deploy rolling) se ponen en fila: sin deadlocks entre ellos.
            await conn.execute(
                text("select pg_advisory_xact_lock(hashtext('agentcore_sync_packs'))")
            )
            for mod in sorted(lista, key=lambda m: str(m.id)):
                await conn.execute(
                    text(
                        "insert into plugins (id, name, version, status) values (:id, :name, :version, :status) "
                        "on conflict (id) do nothing"
                    ),
                    {
                        "id": mod.id,
                        "name": getattr(mod, "name", None) or mod.id,
                        "version": mod.version,
                        "status": "detected"
                        if getattr(mod, "requires_config", False)
                        else "configured",
                    },
                )
            for man in sorted(vigentes.values(), key=lambda m: m.pack_id):
                resultado = await registrar(conn, man)
                reporte.resultados[resultado] += 1
                if resultado in ALERTAN:
                    # Alerta: la misma versión con otro contenido, o un manifiesto inválido. No se registra.
                    log.error(
                        "pack_version_not_registered_alert",
                        pack_id=man.pack_id,
                        version=man.version,
                        result=resultado,
                    )
                elif resultado == "yanked":
                    log.error(
                        "pack_version_yanked_in_binary", pack_id=man.pack_id, version=man.version
                    )
                elif resultado in ("not_platform", "unknown_pack"):
                    log.warning(
                        "pack_version_not_registered", pack_id=man.pack_id, result=resultado
                    )
    except Exception as exc:
        # Sin `str(exc)` de SQLAlchemy: imprimiría el manifiesto entero.
        reporte.errores.append(f"base de datos: {type(exc).__name__}")
        log.error("pack_versions_sync_failed", error=type(exc).__name__)
    finally:
        await engine.dispose()
    log.info(
        "pack_versions_sync",
        **{k: v for k, v in reporte.resultados.items()},
        errors=len(reporte.errores),
    )
    return reporte


__all__ = [
    "ALERTAN",
    "CONTRATO_PACK",
    "LOCK_PATH",
    "ManifiestoDePack",
    "ReporteSync",
    "es_de_plataforma",
    "leer_lock",
    "lock_actualizado",
    "manifiesto_de_pack",
    "manifiestos",
    "problemas_del_lock",
    "registrar",
    "sha256_texto",
    "sync_packs",
    "texto_canonico",
]

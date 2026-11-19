"""Foto de una release candidata para medirla en otra base (HU-AOS-21 parte 3b, adenda de ADR-0117).

El corredor del gate mide la candidata en `agentcore_eval`, no en la base del negocio. Allí no hay
nada de la release, así que se copia lo que la release CONGELÓ y nada más:

- **`exportar`** (base del negocio, dentro de su `tenant_scope`): `connection_releases`,
  `release_components`, las `agent_versions` y `agents` de sus componentes, las `agent_instances`
  que los atan a la conexión y las `pack_versions` de sus habilidades. Todo es inmutable, así que
  la foto no cambia aunque el negocio siga editando. Nunca lee datos del cliente (conversaciones,
  clientes, mensajes, configuración de packs).
- **`importar`** (`agentcore_eval`, dentro del `tenant_scope` del negocio efímero): inserta las
  filas conservando los UUID (las FKs compuestas y el manifiesto los referencian) y reescribiendo
  `tenant_id`. Se anulan las referencias que no existen allí (`created_by`, `legacy_agent_spec_id`,
  `graph_version_id`): el grafo A2A viaja dentro del manifiesto y es lo que lee el runtime.
- **`importar_pack_versions`** (rol dueño de la base de eval): `pack_versions` es una tabla de
  sistema de solo lectura para `app_runtime`.

Conservar los UUID implica que dos corridas con la misma release no pueden convivir en la misma base
de eval: el corredor las serializa con un candado y limpia los restos de una corrida caída.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from core.shared.tenant import get_current_tenant_id

VERSION = 1


class FotoInvalida(ValueError):
    """La foto no es coherente (faltan filas o no cuadran los ids)."""


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("snapshot: fuera del tenant_scope del negocio")


def _jsonb(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


async def exportar(session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID) -> dict[str, Any]:
    """La foto de la release `release_id` del negocio. `FotoInvalida` si no existe o está incompleta."""
    _exigir_scope(tenant_id)
    fila = (
        (
            await session.execute(
                text(
                    "SELECT id, connection_id, number, origin, manifest::text AS manifest, manifest_hash "
                    "FROM connection_releases WHERE tenant_id = :t AND id = :r"
                ),
                {"t": tenant_id, "r": release_id},
            )
        )
        .mappings()
        .first()
    )
    if fila is None:
        raise FotoInvalida(f"la release {release_id} no existe en el negocio")
    comps = [
        dict(c)
        for c in (
            await session.execute(
                text(
                    "SELECT agent_instance_id, agent_id, agent_version_id FROM release_components "
                    "WHERE tenant_id = :t AND release_id = :r ORDER BY agent_instance_id"
                ),
                {"t": tenant_id, "r": release_id},
            )
        )
        .mappings()
        .all()
    ]
    if not comps:
        raise FotoInvalida("la release no tiene componentes")
    versiones = [
        dict(v)
        for v in (
            await session.execute(
                text(
                    "SELECT id, agent_id, version, label, contract, content::text AS content, content_hash, notes "
                    "FROM agent_versions WHERE tenant_id = :t AND id = ANY(cast(:ids AS uuid[])) ORDER BY id"
                ),
                {"t": tenant_id, "ids": [str(c["agent_version_id"]) for c in comps]},
            )
        )
        .mappings()
        .all()
    ]
    agentes = [
        dict(a)
        for a in (
            await session.execute(
                text(
                    "SELECT id, key, kind, name, description, source_template, status FROM agents "
                    "WHERE tenant_id = :t AND id = ANY(cast(:ids AS uuid[])) ORDER BY id"
                ),
                {"t": tenant_id, "ids": [str(c["agent_id"]) for c in comps]},
            )
        )
        .mappings()
        .all()
    ]
    instancias = [
        dict(i)
        for i in (
            await session.execute(
                text(
                    "SELECT id, agent_id, role, is_default, priority, enabled, overrides::text AS overrides "
                    "FROM agent_instances WHERE tenant_id = :t AND connection_id = :c "
                    "AND id = ANY(cast(:ids AS uuid[])) ORDER BY id"
                ),
                {"t": tenant_id, "c": fila["connection_id"], "ids": [str(c["agent_instance_id"]) for c in comps]},
            )
        )
        .mappings()
        .all()
    ]
    manifest = json.loads(fila["manifest"])
    packs: list[dict[str, Any]] = []
    for skill in manifest.get("skills") or []:
        pv = (
            (
                await session.execute(
                    text(
                        "SELECT pack_id, version, contract, manifest::text AS manifest, manifest_hash "
                        "FROM pack_versions WHERE pack_id = :p AND version = :v"
                    ),
                    {"p": str(skill.get("pack_id")), "v": str(skill.get("version"))},
                )
            )
            .mappings()
            .first()
        )
        if pv is None:
            raise FotoInvalida(f"falta la versión {skill.get('pack_id')}@{skill.get('version')} de la habilidad")
        packs.append(dict(pv))
    if len(versiones) != len(comps) or len(agentes) != len({c["agent_id"] for c in comps}) or len(instancias) != len(comps):
        raise FotoInvalida("la release no cuadra con sus versiones, agentes o instancias")
    foto = {
        "version": VERSION,
        "release": {
            "id": fila["id"],
            "connection_id": fila["connection_id"],
            "number": fila["number"],
            "origin": fila["origin"],
            "manifest": fila["manifest"],
            "manifest_hash": fila["manifest_hash"],
        },
        "componentes": comps,
        "versiones": versiones,
        "agentes": agentes,
        "instancias": instancias,
        "packs": packs,
    }
    return json.loads(json.dumps(foto, default=str))


def _uuid(valor: Any) -> uuid.UUID:
    return valor if isinstance(valor, uuid.UUID) else uuid.UUID(str(valor))


async def importar(session: Any, tenant_id: uuid.UUID, foto: dict[str, Any], *, activar: bool = True) -> uuid.UUID:
    """Inserta la foto bajo el negocio `tenant_id` (efímero, en la base de eval) y, con `activar`,
    deja la release como la activa de su conexión. Devuelve la conexión.

    Exige el `tenant_scope` de `tenant_id`. Todo va en la transacción de `session`."""
    _exigir_scope(tenant_id)
    if foto.get("version") != VERSION:
        raise FotoInvalida(f"versión de foto desconocida: {foto.get('version')!r}")
    rel = foto["release"]
    conexion = _uuid(rel["connection_id"])
    await session.execute(
        text(
            "INSERT INTO channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
            "VALUES (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')"
        ),
        {"id": conexion, "t": tenant_id, "ext": f"gate-eval-{conexion.hex[:12]}"},
    )
    for a in foto["agentes"]:
        await session.execute(
            text(
                "INSERT INTO agents (id, tenant_id, key, kind, name, description, source_template, status) "
                "VALUES (:id, :t, :key, :kind, :name, :d, :src, :st)"
            ),
            {
                "id": _uuid(a["id"]),
                "t": tenant_id,
                "key": a["key"],
                "kind": a["kind"],
                "name": a["name"],
                "d": a["description"],
                "src": a.get("source_template"),
                "st": a["status"],
            },
        )
    for v in foto["versiones"]:
        await session.execute(
            text(
                "INSERT INTO agent_versions (id, tenant_id, agent_id, version, label, contract, content, "
                "content_hash, notes) VALUES (:id, :t, :a, :n, :l, :c, cast(:content AS jsonb), :h, :notes)"
            ),
            {
                "id": _uuid(v["id"]),
                "t": tenant_id,
                "a": _uuid(v["agent_id"]),
                "n": int(v["version"]),
                "l": v["label"],
                "c": v["contract"],
                "content": v["content"],
                "h": v["content_hash"],
                "notes": v.get("notes") or "",
            },
        )
    for i in foto["instancias"]:
        await session.execute(
            text(
                "INSERT INTO agent_instances (id, tenant_id, connection_id, agent_id, role, is_default, priority, "
                "enabled, overrides) VALUES (:id, :t, :c, :a, :role, :d, :p, :e, cast(:o AS jsonb))"
            ),
            {
                "id": _uuid(i["id"]),
                "t": tenant_id,
                "c": conexion,
                "a": _uuid(i["agent_id"]),
                "role": i["role"],
                "d": bool(i["is_default"]),
                "p": int(i["priority"]),
                "e": bool(i["enabled"]),
                "o": i.get("overrides") or "{}",
            },
        )
    release_id = _uuid(rel["id"])
    await session.execute(
        text(
            "INSERT INTO connection_releases (id, tenant_id, connection_id, number, origin, manifest, manifest_hash) "
            "VALUES (:id, :t, :c, :n, :o, cast(:m AS jsonb), :h)"
        ),
        {
            "id": release_id,
            "t": tenant_id,
            "c": conexion,
            "n": int(rel["number"]),
            "o": rel["origin"],
            "m": rel["manifest"],
            "h": rel["manifest_hash"],
        },
    )
    for c in foto["componentes"]:
        await session.execute(
            text(
                "INSERT INTO release_components (tenant_id, connection_id, release_id, agent_instance_id, "
                "agent_id, agent_version_id) VALUES (:t, :c, :r, :i, :a, :v)"
            ),
            {
                "t": tenant_id,
                "c": conexion,
                "r": release_id,
                "i": _uuid(c["agent_instance_id"]),
                "a": _uuid(c["agent_id"]),
                "v": _uuid(c["agent_version_id"]),
            },
        )
    if activar:
        await session.execute(
            text("UPDATE channel_connections SET active_release_id = :r WHERE tenant_id = :t AND id = :c"),
            {"r": release_id, "t": tenant_id, "c": conexion},
        )
    return conexion


async def importar_pack_versions(conn: Any, foto: dict[str, Any]) -> list[str]:
    """Registra en la base de eval las versiones de habilidad de la release por la misma función
    SECURITY DEFINER que `sync-packs` (`register_pack_version`, que verifica el hash). `conn`: conexión
    del rol dueño de ESA base. Devuelve los resultados (`inserted`, `exists`, `hash_mismatch`...);
    `FotoInvalida` si alguno es un rechazo."""
    from core.releases.packs import sha256_texto, texto_canonico

    resultados: list[str] = []
    for p in foto["packs"]:
        texto = texto_canonico(json.loads(p["manifest"]))
        hash_ = sha256_texto(texto)
        if hash_ != p["manifest_hash"]:
            raise FotoInvalida(f"el manifiesto de {p['pack_id']}@{p['version']} no cuadra con su hash")
        r = (
            await conn.execute(
                text("select register_pack_version(:p, :v, :c, :t, :h)"),
                {"p": p["pack_id"], "v": p["version"], "c": p["contract"], "t": texto, "h": hash_},
            )
        ).scalar_one()
        if str(r) not in ("inserted", "exists"):
            raise FotoInvalida(f"{p['pack_id']}@{p['version']}: {r}")
        resultados.append(str(r))
    return resultados


__all__ = ["VERSION", "FotoInvalida", "exportar", "importar", "importar_pack_versions"]

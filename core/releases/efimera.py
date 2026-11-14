"""Release efímera del borrador de un agente (HU-AOS-72, adenda HU-AOS-72 de ADR-0191).

Para probar un agente ANTES de publicarlo (Agentes › Nuevo agente › «Pruébalo»), el turno de la
prueba del panel corre con una release armada en memoria desde el **borrador**:

- un solo especialista, el borrador (`agent_drafts.content`), por defecto y sin ruteo;
- el Coordinador con su última versión publicada (existe desde el alta, `asegurar_coordinador`);
  si no la hay, los defaults del `KernelSpec`, también en memoria;
- habilidades, procedimientos, overrides, flujo y `AppSettings` con los MISMOS pasos del build
  (`build.armar_con_instancias`), sobre la conexión interna de pruebas del negocio.

Nada se persiste: ni release, ni componentes, ni versión, ni instancia, y nunca entra a la caché
de `core.releases.cargar` (ADR-0117 §7: lo de la caché es inmutable; un borrador no lo es). Los
ids de la instancia y la versión son `uuid4` de cada turno y los artefactos van marcados
`efimera=True`: ninguna escritura del turno los usa como FK (`release_vigente.efimera()`).

Solo se usa desde el canal `test` con el marcador del panel (sandbox de HU-ARCH-14).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.releases import manifiesto as man
from core.releases.build import ErrorDelBuild, _Instancia, _modulos, armar_con_instancias
from core.shared.logger import get_logger
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Clave del `state` de la conversación de prueba atada a un borrador.
CLAVE_BORRADOR = "draft_agent_id"


class BorradorNoEncontrado(LookupError):
    """El agente no existe en el negocio, no es especialista activo o no tiene borrador."""


async def _instancias(tenant_id: uuid.UUID, agent_id: uuid.UUID, catalogo: Any) -> list[_Instancia]:
    from agentcore_contracts import KernelSpec

    from core.agents.contrato_v3 import revisar_spec, validar_spec

    async with get_session() as session:
        agente = (
            await session.execute(
                text(
                    "SELECT a.key, d.content FROM agents a JOIN agent_drafts d "
                    "ON d.tenant_id = a.tenant_id AND d.agent_id = a.id AND d.deleted_at IS NULL "
                    "WHERE a.tenant_id = :t AND a.id = :a AND a.kind = 'specialist' "
                    "AND a.status = 'active' AND a.deleted_at IS NULL"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).first()
        if agente is None:
            raise BorradorNoEncontrado(str(agent_id))
        coordinador = (
            await session.execute(
                text(
                    "SELECT a.id, a.key, v.id, v.version, v.content_hash, v.content FROM agents a "
                    "LEFT JOIN LATERAL (SELECT id, version, content_hash, content FROM agent_versions "
                    "  WHERE tenant_id = a.tenant_id AND agent_id = a.id ORDER BY version DESC LIMIT 1) v ON true "
                    "WHERE a.tenant_id = :t AND a.kind = 'core' AND a.deleted_at IS NULL ORDER BY a.created_at LIMIT 1"
                ),
                {"t": tenant_id},
            )
        ).first()
        packs = [
            str(f[0])
            for f in (
                await session.execute(
                    text(
                        "SELECT pack_id FROM pack_installations WHERE tenant_id = :t AND scope = 'agent' "
                        "AND status = 'active' AND deleted_at IS NULL AND agent_id = :a ORDER BY pack_id"
                    ),
                    {"t": tenant_id, "a": agent_id},
                )
            ).all()
        ]
    key, crudo = agente
    problemas = revisar_spec(crudo, catalogo, "agentspec/3")
    if problemas:
        raise ErrorDelBuild(
            "borrador_invalido",
            "El borrador no pasa el contrato con este binario.",
            [f"{key}: {i.path} ({i.code})" for i in problemas][:10],
        )
    spec, _ = validar_spec(crudo, "agentspec/3")
    assert spec is not None
    contenido = spec.model_dump(mode="json", by_alias=True)
    if coordinador is not None and coordinador[2] is not None:
        core_id, core_key, core_vid, core_num, core_hash, core_contenido = coordinador
    else:
        # Sin Coordinador publicado (no pasa tras el alta): los defaults del contrato, en memoria.
        core_contenido = KernelSpec().model_dump(mode="json", by_alias=True)
        core_id = coordinador[0] if coordinador is not None else uuid.uuid4()
        core_key = coordinador[1] if coordinador is not None else "coordinador"
        core_vid, core_num, core_hash = uuid.uuid4(), 0, ""
    return [
        _Instancia(
            man.Componente(
                instance_id=str(uuid.uuid4()),
                agent_id=str(core_id),
                agent_key=str(core_key),
                role="core",
                agent_version_id=str(core_vid),
                version=int(core_num),
                content_hash=str(core_hash),
                contract="kernelspec/1",
            ),
            dict(core_contenido),
        ),
        _Instancia(
            man.Componente(
                instance_id=str(uuid.uuid4()),
                agent_id=str(agent_id),
                agent_key=str(key),
                role="specialist",
                # Id de cada turno: el borrador es mutable, nada se compila ni se cachea por él.
                agent_version_id=str(uuid.uuid4()),
                version=0,
                content_hash="",
                contract="agentspec/3",
                is_default=True,
                skills=tuple(packs) if packs else None,
            ),
            contenido,
        ),
    ]


async def release_de_borrador(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    modules: Any = None,
) -> tuple[Any, AgenteCongelado]:
    """`(ReleaseCongelada en memoria, foco)` del borrador del agente. `BorradorNoEncontrado` si el
    agente no es un especialista activo del negocio con borrador; `ErrorDelBuild` si no arma."""
    from core.kernel.episodio import DEFAULT_IDLE_MINUTES
    from core.kernel.procedures import catalogo_de_acciones
    from core.releases.cargar import ReleaseCongelada, _congelados

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("releases: fuera del tenant_scope del negocio")
    modulos = _modulos(modules)
    instancias = await _instancias(tenant_id, agent_id, catalogo_de_acciones(modulos))
    manifest = await armar_con_instancias(tenant_id, connection_id, instancias, modulos)
    contenidos = {i.componente.instance_id: i.contenido for i in instancias}
    componentes: list[AgenteCongelado] = []
    for c in manifest["components"]:
        componentes.append(
            AgenteCongelado(
                instance_id=uuid.UUID(c["instance_id"]),
                agent_id=uuid.UUID(c["agent_id"]),
                key=str(c["agent_key"]),
                role=str(c["role"]),
                version_id=uuid.UUID(c["agent_version_id"]),
                version=int(c.get("version") or 0),
                contract=str(c.get("contract") or ""),
                content=contenidos[c["instance_id"]],
                is_default=bool(c.get("is_default", False)),
                procedimientos=tuple(
                    (str(p["id"]), str(p["version"])) for p in c.get("procedures") or [] if p.get("id")
                ),
                packs=frozenset(str(s) for s in c["skills"]) if isinstance(c.get("skills"), list) else None,
            )
        )
    async with get_session() as session:
        congelados, _bytes = await _congelados(session, manifest)
    kernel = next((a for a in componentes if a.role == "core"), None)
    episodio = kernel.content.get("episode") if kernel is not None else None
    idle = episodio.get("idleMinutes") if isinstance(episodio, dict) else None
    texto = json.dumps(manifest, ensure_ascii=False)
    release = ReleaseCongelada(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        connection_id=connection_id,
        number=0,
        manifest_hash=man.hash_de(manifest),
        manifest=manifest,
        idle_minutes=int(idle) if isinstance(idle, int) and idle > 0 else DEFAULT_IDLE_MINUTES,
        bytes=len(texto.encode("utf-8")),
        componentes=tuple(componentes),
        congelados=congelados,
    )
    foco = next(a for a in componentes if a.role == "specialist")
    return release, foco


def artefactos_efimeros(release: Any, foco: AgenteCongelado) -> ArtefactosDeRelease:
    """Los artefactos del turno, marcados `efimera` (ninguna escritura usa sus ids como FK)."""
    import dataclasses

    # Sin grafo A2A: la prueba va directo al borrador, sin handoff ni consultas a otros agentes.
    return dataclasses.replace(release.artefactos("stable", foco=foco), efimera=True, a2a_graph=None)


__all__ = ["CLAVE_BORRADOR", "BorradorNoEncontrado", "artefactos_efimeros", "release_de_borrador"]

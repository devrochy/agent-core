"""Build de una release candidata por conexión (ADR-0117 §2, HU-AOS-14).

`construir(tenant_id, connection_id, ...)` congela en una release inmutable (forma `release/1`,
`core/releases/manifiesto.py`) lo que la conexión corre:

1. **Versiones de los agentes.** Por cada instancia viva y habilitada de la conexión, una versión
   exacta: la última publicada de su agente. Exige el Coordinador. Cada versión pasa otra vez el
   validador único del contrato (`revisar_spec`, ADR-0113 §1: el binario pudo cambiar desde que
   se publicó). Con `aos_runtime = 'legacy'` el Coordinador y el General son el AgentSpec v2
   activo (lector tolerante): el build primero los **refresca** (corre el backfill B2 del
   negocio, idempotente, y publica la conversión actual si su hash cambió) y congela esa
   versión, no "la última". Así la release 1 sale del v2 activo aunque se haya editado en el
   panel legacy después del backfill o el binario cambie la conversión (adenda de HU-AOS-13 en
   ADR-0113: "refrescar la versión es decisión del build").
2. **Habilidades.** Las habilitadas del negocio (`enabled_business_packs` del v2 activo, la única
   lectura de la habilitación hasta HU-AOS-16): las de la plataforma con su versión exacta,
   registrada en `pack_versions` con el mismo `manifest_hash` y no retirada; los plugins del
   negocio solo por nombre.
3. **Procedimientos.** Cada `ProcedureRef` de las versiones debe resolver: los abiertos
   (`=0.0.0`) contra los que se derivan del flujo (core y packs del binario + las secciones del
   negocio congeladas). Un rango necesita la versión de la habilidad que lo provea, y hoy ningún
   pack trae procedimientos: se rechaza.
4. **Textos del negocio.** Overrides de playbooks de la allowlist (`TENANTS_ROOT/<slug>/playbooks`)
   y las secciones crudas de su `flow.json`. Fail-closed: un override que no se puede leer o
   pasa 8 KiB, o un `flow.json` roto, hacen fallar el build (la release es inmutable; el loop
   legacy, en cambio, cae al default en silencio).
5. **AppSettings que entran al prompt** (`assistant_name`, `business_description`, `language`,
   `brand_voice`), con los mismos respaldos que el loop si el negocio no tiene fila.

Idempotente: el mismo `manifest_hash` en la conexión devuelve la release existente. El
manifiesto se arma FUERA del lock; la fila de la conexión se bloquea solo para comparar el hash,
numerar (max + 1) e insertar (así un rollback, que bloquea la misma fila, no espera a un build
que lee disco). La release no se activa sola (`promocion.promover`).

Corre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.releases import manifiesto as man
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

_PLAYBOOK_SEGURO = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_VERSIONES_ABIERTAS = ("=0.0.0", "0.0.0")


class ErrorDelBuild(RuntimeError):
    """La release no se puede construir. El mensaje lleva rutas, nombres y conteos, nunca
    valores de secretos ni textos del negocio."""

    def __init__(self, codigo: str, mensaje: str, detalle: list[str] | None = None) -> None:
        super().__init__(mensaje)
        self.codigo = codigo
        self.detalle = detalle or []


class ConexionNoEncontrada(LookupError):
    pass


@dataclass(frozen=True)
class ResultadoBuild:
    release_id: uuid.UUID
    number: int
    manifest_hash: str
    creada: bool
    avisos: list[str] = field(default_factory=list)


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("releases: fuera del tenant_scope del negocio")


def _modulos(modules: Iterable[Any] | None) -> list[Any]:
    from core.plugin_manager import plugin_manager

    return list(modules if modules is not None else plugin_manager.list_modules())


async def _conexion_conversacional(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT ct.produces_conversations FROM channel_connections cc "
                    "LEFT JOIN connection_types ct ON ct.id = cc.type_id "
                    "WHERE cc.tenant_id = :t AND cc.id = :c AND cc.deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).first()
    if fila is None:
        raise ConexionNoEncontrada(str(connection_id))
    if not fila[0]:
        raise ErrorDelBuild("conexion_no_conversacional", "Las integraciones no tienen release.")


# ------------------------------------------------------------------ 1. agentes
async def refrescar_desde_v2(
    tenant_id: uuid.UUID, modules: list[Any]
) -> dict[uuid.UUID, uuid.UUID]:
    """`{agent_id: version_id}` del Coordinador y del General que corresponden al AgentSpec v2
    activo con el binario de hoy (ver el docstring del módulo). Publica una versión nueva solo si
    la conversión cambió; nunca escribe en `agent_specs` salvo la siembra del default que hace
    el backfill (la misma que haría el loop en su primer turno)."""
    from core.agents import versiones
    from core.agents.backfill_agentes import backfill_agentes
    from core.agents.instancias import agentes_base
    from core.agents.spec import leer_spec_activo
    from core.agents.tolerante import (
        KEY_COORDINADOR,
        KEY_GENERAL,
        ConversionFallida,
        convertir_negocio,
    )
    from core.kernel.procedures import catalogo_de_acciones

    reporte = await backfill_agentes(dry_run=False, tenant_id=tenant_id, modules=modules)
    problemas = [p.mensaje for p in (*reporte.bloqueados, *reporte.errores)]
    if problemas:
        raise ErrorDelBuild(
            "backfill_pendiente", "El negocio no pasa el backfill de agentes.", problemas[:5]
        )
    leido = await leer_spec_activo(tenant_id)
    if leido is None:
        raise ErrorDelBuild("sin_spec", "El negocio no tiene AgentSpec activo.")
    spec_id, spec = leido
    try:
        conversion = await convertir_negocio(spec, modules=modules)
    except ConversionFallida as exc:
        raise ErrorDelBuild("conversion_fallida", str(exc)[:300]) from None
    finally:
        del spec  # el v2 lleva los literales: fuera de los locales del frame
    contenidos = {
        KEY_COORDINADOR: conversion.kernel.model_dump(mode="json", by_alias=True),
        KEY_GENERAL: conversion.general.model_dump(mode="json", by_alias=True),
    }
    del conversion
    async with get_session() as session:
        ids = await agentes_base(session, tenant_id)
        con_legacy = {
            aid
            for (aid,) in (
                await session.execute(
                    text(
                        "SELECT agent_id FROM agent_versions WHERE tenant_id = :t AND legacy_agent_spec_id = :s"
                    ),
                    {"t": tenant_id, "s": spec_id},
                )
            ).all()
        }
    catalogo = catalogo_de_acciones(modules)
    salida: dict[uuid.UUID, uuid.UUID] = {}
    for key, contenido in contenidos.items():
        aid = ids.get(key)
        if aid is None:
            raise ErrorDelBuild("sin_agente", f"Falta el agente reservado '{key}'.")
        try:
            version, creada = await versiones.publicar(
                tenant_id,
                aid,
                catalogo=catalogo,
                contenido=contenido,
                # El índice único por (agente, fila v2) ya la tiene: el refresco va sin ella.
                legacy_agent_spec_id=None if aid in con_legacy else spec_id,
                label="v2 (refresco del build)",
                notes="Refresco del build de releases (HU-AOS-14): conversión actual del AgentSpec v2 activo.",
            )
        except versiones.ErroresDeContrato as exc:
            raise ErrorDelBuild(
                "contrato_invalido",
                f"El agente '{key}' no pasa el contrato.",
                [f"{i.path} ({i.code})" for i in exc.issues[:10]],
            ) from None
        if creada:
            log.info("release_build_agent_refreshed", agent_key=key, version=version.version)
        salida[aid] = version.id
    return salida


async def _componentes(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    fijadas: dict[uuid.UUID, uuid.UUID],
    catalogo: Any,
) -> tuple[list[man.Componente], list[dict[str, Any]]]:
    from core.agents.contrato_v3 import revisar_spec

    async with get_session() as session:
        instancias = (
            await session.execute(
                text(
                    "SELECT ai.id, ai.agent_id, a.key, ai.role FROM agent_instances ai "
                    "JOIN agents a ON a.tenant_id = ai.tenant_id AND a.id = ai.agent_id "
                    "WHERE ai.tenant_id = :t AND ai.connection_id = :c AND ai.deleted_at IS NULL "
                    "AND ai.enabled AND a.deleted_at IS NULL ORDER BY ai.id"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).all()
        if not any(rol == "core" for *_, rol in instancias):
            raise ErrorDelBuild("sin_coordinador", "La conexión no tiene Coordinador.")
        componentes: list[man.Componente] = []
        contenidos: list[dict[str, Any]] = []
        problemas: list[str] = []
        for iid, aid, key, rol in instancias:
            version = (
                await session.execute(
                    text(
                        "SELECT id, version, content_hash, contract, content FROM agent_versions "
                        "WHERE tenant_id = :t AND agent_id = :a "
                        "AND (cast(:v AS uuid) IS NULL OR id = cast(:v AS uuid)) "
                        "ORDER BY version DESC LIMIT 1"
                    ),
                    {"t": tenant_id, "a": aid, "v": fijadas.get(aid)},
                )
            ).first()
            if version is None:
                raise ErrorDelBuild(
                    "agente_sin_version", f"El agente '{key}' no tiene versión publicada."
                )
            vid, numero, huella, contrato, contenido = version
            for issue in revisar_spec(contenido, catalogo, contrato):
                problemas.append(f"{key}: {issue.path} ({issue.code})")
            componentes.append(
                man.Componente(
                    instance_id=str(iid),
                    agent_id=str(aid),
                    agent_key=key,
                    role=rol,
                    agent_version_id=str(vid),
                    version=int(numero),
                    content_hash=huella,
                    contract=contrato,
                )
            )
            contenidos.append(contenido)
    if problemas:
        raise ErrorDelBuild(
            "contrato_invalido", "Una versión no pasa el contrato con este binario.", problemas[:10]
        )
    return componentes, contenidos


# ------------------------------------------------------------------ 2. habilidades
async def _habilidades(
    tenant_id: uuid.UUID, packs: set[str], modules: list[Any]
) -> tuple[list[man.Habilidad], list[str]]:
    from core.releases.packs import manifiesto_de_pack

    por_id = {str(m.id): m for m in modules}
    async with get_session() as session:
        duenos: dict[str, uuid.UUID | None] = {
            str(f[0]): f[1]
            for f in (
                await session.execute(
                    text("SELECT id, owner_tenant_id FROM plugins WHERE id = ANY(:ids)"),
                    {"ids": sorted(packs)},
                )
            ).all()
        }
        habilidades: list[man.Habilidad] = []
        del_negocio: list[str] = []
        problemas: list[str] = []
        for pid in sorted(packs):
            if pid not in duenos:
                problemas.append(f"{pid}: no está registrado")
                continue
            dueno = duenos[pid]
            if dueno is not None:
                if dueno == tenant_id:
                    del_negocio.append(pid)
                else:
                    # El mismo mensaje que un plugin inexistente: no revela plugins de otro negocio.
                    log.warning("release_build_foreign_plugin", pack_id=pid)
                    problemas.append(f"{pid}: no está registrado")
                continue
            modulo = por_id.get(pid)
            if modulo is None:
                problemas.append(f"{pid}: el binario no la trae")
                continue
            pack = manifiesto_de_pack(modulo)
            registrada = (
                await session.execute(
                    text(
                        "SELECT manifest_hash, yanked FROM pack_versions WHERE pack_id = :p AND version = :v"
                    ),
                    {"p": pid, "v": pack.version},
                )
            ).first()
            if registrada is None:
                problemas.append(
                    f"{pid} {pack.version}: sin registrar en pack_versions (agent-core db sync-packs)"
                )
            elif registrada[0] != pack.manifest_hash:
                problemas.append(
                    f"{pid} {pack.version}: el contenido del binario no es el registrado"
                )
            elif registrada[1]:
                problemas.append(f"{pid} {pack.version}: versión retirada (yanked)")
            else:
                habilidades.append(man.Habilidad(pid, pack.version, pack.manifest_hash))
    if problemas:
        raise ErrorDelBuild(
            "habilidad_no_disponible", "Una habilidad no tiene versión utilizable.", problemas
        )
    return habilidades, del_negocio


def _refs_sin_resolver(contenidos: list[dict[str, Any]], derivados: set[str]) -> list[str]:
    faltan: list[str] = []
    for contenido in contenidos:
        refs = contenido.get("procedures")
        if not isinstance(refs, list):  # el KernelSpec tiene `procedures` de otra forma
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            pid, version = ref.get("id"), ref.get("version")
            if version in _VERSIONES_ABIERTAS:
                if pid not in derivados:
                    faltan.append(f"{pid} {version}: el flujo ya no tiene esa sección")
            else:
                faltan.append(f"{pid} {version}: ninguna habilidad registrada lo provee")
    return sorted(set(faltan))


# ------------------------------------------------------------------ 4-5. textos y AppSettings
def _overrides(slug: str | None, nombres: Iterable[str]) -> dict[str, str]:
    from core.agents.playbooks import MAX_SIZE_BYTES
    from core.onboarding import tenant_dir

    if not slug:
        return {}
    from core.onboarding import _validate_slug

    _validate_slug(slug)
    carpeta = tenant_dir(slug) / "playbooks"
    salida: dict[str, str] = {}
    problemas: list[str] = []
    for nombre in sorted(set(nombres)):
        if not _PLAYBOOK_SEGURO.fullmatch(nombre):
            continue
        archivo = carpeta / f"{nombre}.md"
        if not archivo.exists():
            continue
        try:
            if not archivo.resolve().is_relative_to(carpeta.resolve()):
                problemas.append(f"{nombre}: apunta fuera de la carpeta del negocio")
                continue
            if archivo.stat().st_size > MAX_SIZE_BYTES:
                problemas.append(f"{nombre}: pasa {MAX_SIZE_BYTES // 1024} KiB")
                continue
            texto = archivo.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            problemas.append(f"{nombre}: no se puede leer")
            continue
        if len(texto.encode("utf-8")) > MAX_SIZE_BYTES:
            problemas.append(f"{nombre}: pasa {MAX_SIZE_BYTES // 1024} KiB")
            continue
        salida[nombre] = texto
    if problemas:
        raise ErrorDelBuild(
            "override_invalido",
            "Un override de playbook del negocio no se puede congelar.",
            problemas,
        )
    return salida


async def _app_settings(tenant_id: uuid.UUID) -> dict[str, str | None]:
    from core.config import get_settings

    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT assistant_name, business_description, language, brand_voice FROM app_settings "
                    "WHERE tenant_id = :t AND deleted_at IS NULL"
                ),
                {"t": tenant_id},
            )
        ).first()
    if fila is None:  # los mismos respaldos que el loop
        return {
            "assistant_name": get_settings().app_name,
            "business_description": "",
            "language": "es",
            "brand_voice": "",
        }
    return {
        "assistant_name": fila[0],
        "business_description": fila[1],
        "language": fila[2],
        "brand_voice": fila[3],
    }


def _built_with() -> dict[str, str]:
    import agentcore_contracts

    import core

    return {
        "git_sha": (os.environ.get("SOURCE_COMMIT") or "")[:40],
        "contracts": str(agentcore_contracts.__version__),
        "core": str(core.__version__),
    }


# ------------------------------------------------------------------ build
async def armar_manifiesto(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    modules: Iterable[Any] | None = None,
    refrescar: bool | None = None,
    fijadas: dict[uuid.UUID, uuid.UUID] | None = None,
) -> dict[str, Any]:
    """El manifiesto que construiría `construir`, sin escribir la release (sí puede refrescar
    las versiones del v2: ver el docstring del módulo). `refrescar=None`: solo en legacy.
    `fijadas`: `{agent_id: version_id}` ya refrescadas (el backfill las calcula una vez por
    negocio); con ellas no se refresca otra vez."""
    from core.agents.flow import read_tenant_flow_raw
    from core.agents.spec import enabled_business_packs, leer_spec_activo
    from core.db.session import get_session as _sesion
    from core.kernel.banderas import banderas_del_negocio
    from core.kernel.procedures import catalogo_de_acciones
    from core.releases.cargar import derivar_procedimientos
    from core.shared.tenant import tenant_slug

    _exigir_scope(tenant_id)
    await _conexion_conversacional(tenant_id, connection_id)
    modulos = _modulos(modules)
    catalogo = catalogo_de_acciones(modulos)
    if fijadas is None:
        if refrescar is None:
            async with _sesion() as session:
                refrescar = (await banderas_del_negocio(session, tenant_id)).runtime == "legacy"
        fijadas = await refrescar_desde_v2(tenant_id, modulos) if refrescar else {}
    componentes, contenidos = await _componentes(tenant_id, connection_id, fijadas, catalogo)

    leido = await leer_spec_activo(tenant_id)
    packs = set(enabled_business_packs(leido[1])) if leido else set()
    allowlist: set[str] = {p.name for p in leido[1].playbooks} if leido else set()
    del leido
    for contenido in contenidos:
        for ref in contenido.get("playbooks") or []:
            if isinstance(ref, dict) and isinstance(ref.get("name"), str):
                allowlist.add(ref["name"])
    habilidades, del_negocio = await _habilidades(tenant_id, packs, modulos)

    slug = await tenant_slug(tenant_id)
    try:
        flujo = read_tenant_flow_raw(slug) if slug else None
    except ValueError as exc:
        raise ErrorDelBuild("flujo_invalido", str(exc)) from None
    derivados = set(derivar_procedimientos([h.pack_id for h in habilidades], flujo))
    faltan = _refs_sin_resolver(contenidos, derivados)
    if faltan:
        raise ErrorDelBuild(
            "procedimiento_sin_resolver", "Un procedimiento de un agente no resuelve.", faltan[:10]
        )
    return man.armar(
        componentes=componentes,
        habilidades=habilidades,
        plugins_del_negocio=del_negocio,
        overrides=_overrides(slug, allowlist),
        flujo_del_negocio=flujo,
        app_settings=await _app_settings(tenant_id),
        built_with=_built_with(),
    )


async def construir(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    actor: str,
    created_by: uuid.UUID | None = None,
    origin: str = "owner",
    notas: str = "",
    modules: Iterable[Any] | None = None,
    refrescar: bool | None = None,
    fijadas: dict[uuid.UUID, uuid.UUID] | None = None,
) -> ResultadoBuild:
    """Construye (o devuelve, si ya existe con el mismo hash) la release candidata."""
    if origin not in ("owner", "platform"):
        raise ValueError(f"origin inválido: {origin}")
    manifest = await armar_manifiesto(
        tenant_id, connection_id, modules=modules, refrescar=refrescar, fijadas=fijadas
    )
    huella = man.hash_de(manifest)
    async with get_session() as session:
        bloqueada = (
            await session.execute(
                text(
                    "SELECT id FROM channel_connections WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL "
                    "FOR UPDATE"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
        if bloqueada is None:
            raise ConexionNoEncontrada(str(connection_id))
        existente = (
            await session.execute(
                text(
                    "SELECT id, number FROM connection_releases "
                    "WHERE tenant_id = :t AND connection_id = :c AND manifest_hash = :h"
                ),
                {"t": tenant_id, "c": connection_id, "h": huella},
            )
        ).first()
        if existente is not None:
            return ResultadoBuild(existente[0], existente[1], huella, creada=False)
        numero = (
            await session.execute(
                text(
                    "SELECT coalesce(max(number), 0) + 1 FROM connection_releases WHERE tenant_id = :t AND connection_id = :c"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one()
        release_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO connection_releases (id, tenant_id, connection_id, number, origin, manifest, "
                "manifest_hash, created_by) VALUES (:id, :t, :c, :n, :o, cast(:m as jsonb), :h, :by)"
            ),
            {
                "id": release_id,
                "t": tenant_id,
                "c": connection_id,
                "n": numero,
                "o": origin,
                "m": json.dumps(manifest, ensure_ascii=False),
                "h": huella,
                "by": created_by,
            },
        )
        for comp in manifest["components"]:
            await session.execute(
                text(
                    "INSERT INTO release_components (tenant_id, connection_id, release_id, agent_instance_id, "
                    "agent_id, agent_version_id) VALUES (:t, :c, :r, :i, :a, :v)"
                ),
                {
                    "t": tenant_id,
                    "c": connection_id,
                    "r": release_id,
                    "i": uuid.UUID(comp["instance_id"]),
                    "a": uuid.UUID(comp["agent_id"]),
                    "v": uuid.UUID(comp["agent_version_id"]),
                },
            )
        await registrar_evento(
            session,
            tenant_id,
            connection_id,
            release_id,
            "built",
            actor,
            {"origin": origin, "manifest_hash": huella, "number": numero, "notes": notas[:500]},
        )
    log.info("release_built", connection_id=str(connection_id), number=numero, origin=origin)
    return ResultadoBuild(release_id, numero, huella, creada=True)


async def registrar_evento(
    session: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    tipo: str,
    actor: str,
    data: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO release_events (tenant_id, connection_id, release_id, type, data, actor) "
            "VALUES (:t, :c, :r, :tipo, cast(:d as jsonb), :actor)"
        ),
        {
            "t": tenant_id,
            "c": connection_id,
            "r": release_id,
            "tipo": tipo,
            "d": json.dumps(data or {}, ensure_ascii=False, default=str),
            "actor": actor[:120],
        },
    )


__all__ = [
    "ConexionNoEncontrada",
    "ErrorDelBuild",
    "ResultadoBuild",
    "armar_manifiesto",
    "construir",
    "refrescar_desde_v2",
    "registrar_evento",
]

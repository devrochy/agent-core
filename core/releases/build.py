"""Build de una release candidata por conexión (ADR-0117 §2, HU-AOS-14).

`construir(tenant_id, connection_id, ...)` congela en una release inmutable (forma `release/2`,
`core/releases/manifiesto.py`) lo que la conexión corre:

1. **Versiones de los agentes.** Por cada instancia viva y habilitada de la conexión, una versión
   exacta: la última publicada de su agente. Exige el Coordinador. Cada versión pasa otra vez el
   validador único del contrato (`revisar_spec`, ADR-0113 §1: el binario pudo cambiar desde que
   se publicó). Con `aos_runtime = 'legacy'` el Coordinador y el General son el AgentSpec v2
   activo (lector tolerante): el build primero los **refresca** (corre el backfill B2 del
   negocio, idempotente, y publica la conversión actual si su hash cambió) y congela esa
   versión, no "la última". Así la release 1 sale del v2 activo aunque se haya editado en el
   panel legacy después del backfill o el binario cambie la conversión (adenda de HU-AOS-13 en
   ADR-0113: "refrescar la versión es decisión del build"). Con `v3` se refresca solo el
   General y solo si su última versión la produjo el lector tolerante (`legacy_agent_spec_id`
   no nulo): así un General que todavía no declara los procedimientos publicados (HU-AOS-16)
   los declara en su siguiente build, y uno que el dueño ya editó no se pisa.
2. **Habilidades.** Las efectivas en el scope de la conexión (ADR-0118 §2, HU-AOS-16): si la
   conexión tiene instalaciones propias vivas (`pack_installations`, scope `connection`),
   SOLO esas, sin merge; si no, las habilitadas del negocio (`enabled_business_packs` del v2
   activo). Una instalación de conexión de un pack que el negocio no tiene habilitado hace
   fallar el build (`instalacion_sin_habilitar`: en la parte 1 de la HU la activación del
   código, la config y las tools siguen colgando de `spec.packs`). Las de la plataforma con su
   versión exacta, registrada en `pack_versions` con el mismo `manifest_hash` y no retirada; los
   plugins del negocio solo por nombre.
3. **Procedimientos.** Cada `ProcedureRef` de las versiones `agentspec/3` resuelve a una
   versión exacta (`core/releases/procedimientos.py`): los abiertos (`=0.0.0`) contra los que
   se derivan del flujo (core y packs del binario + las secciones del negocio congeladas) y los
   rangos contra los procedimientos que publica la versión congelada de cada habilidad efectiva
   (el manifiesto del binario, cuyo hash ya se comprobó contra `pack_versions`). Lo resuelto
   queda en `components[].procedures`. Un ref que no resuelve, o cuya habilidad (o una que
   requiere) no está en el scope, hace fallar el build (fail-closed).
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

import dataclasses
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
async def _general_sigue_siendo_del_lector(tenant_id: uuid.UUID) -> bool:
    """¿La última versión del General la produjo el lector tolerante? (`legacy_agent_spec_id`
    no nulo.) Si el dueño publicó una versión propia, no se refresca."""
    from core.agents.instancias import agentes_base
    from core.agents.tolerante import KEY_GENERAL

    async with get_session() as session:
        aid = (await agentes_base(session, tenant_id)).get(KEY_GENERAL)
        if aid is None:
            return False
        fila = (
            await session.execute(
                text(
                    "SELECT legacy_agent_spec_id FROM agent_versions WHERE tenant_id = :t AND agent_id = :a "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"t": tenant_id, "a": aid},
            )
        ).first()
    return fila is not None and fila[0] is not None


async def refrescar_desde_v2(
    tenant_id: uuid.UUID, modules: list[Any], *, solo_general: bool = False
) -> dict[uuid.UUID, uuid.UUID]:
    """`{agent_id: version_id}` del Coordinador y del General que corresponden al AgentSpec v2
    activo con el binario de hoy (ver el docstring del módulo). Publica una versión nueva solo si
    la conversión cambió; nunca escribe en `agent_specs` salvo la siembra del default que hace
    el backfill (la misma que haría el loop en su primer turno).

    `solo_general` (negocios v3, HU-AOS-16): no corre el backfill y publica solo el General; el
    Coordinador queda en su última versión."""
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

    if not solo_general:
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
    if solo_general:
        contenidos.pop(KEY_COORDINADOR)
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


@dataclass(frozen=True)
class _Instancia:
    componente: man.Componente
    contenido: dict[str, Any]


async def _componentes(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    fijadas: dict[uuid.UUID, uuid.UUID],
    catalogo: Any,
) -> list[_Instancia]:
    from core.agents.contrato_v3 import revisar_spec

    async with get_session() as session:
        instancias = (
            await session.execute(
                text(
                    "SELECT ai.id, ai.agent_id, a.key, ai.role, ai.is_default FROM agent_instances ai "
                    "JOIN agents a ON a.tenant_id = ai.tenant_id AND a.id = ai.agent_id "
                    "WHERE ai.tenant_id = :t AND ai.connection_id = :c AND ai.deleted_at IS NULL "
                    "AND ai.enabled AND a.deleted_at IS NULL ORDER BY ai.id"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).all()
        if not any(rol == "core" for _i, _a, _k, rol, _d in instancias):
            raise ErrorDelBuild("sin_coordinador", "La conexión no tiene Coordinador.")
        salida: list[_Instancia] = []
        problemas: list[str] = []
        for iid, aid, key, rol, por_defecto in instancias:
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
            salida.append(
                _Instancia(
                    man.Componente(
                        instance_id=str(iid),
                        agent_id=str(aid),
                        agent_key=key,
                        role=rol,
                        agent_version_id=str(vid),
                        version=int(numero),
                        content_hash=huella,
                        contract=contrato,
                        is_default=bool(por_defecto),
                    ),
                    contenido,
                )
            )
    if problemas:
        raise ErrorDelBuild(
            "contrato_invalido", "Una versión no pasa el contrato con este binario.", problemas[:10]
        )
    return salida


# ------------------------------------------------------------------ 2. habilidades
async def _efectivas(tenant_id: uuid.UUID, connection_id: uuid.UUID, del_negocio: set[str]) -> set[str]:
    """Packs efectivos en el scope de la conexión (ADR-0118 §2): sus instalaciones propias si las
    tiene (reemplazo entero), si no las del negocio. Fail-closed si una instalación de conexión
    no está habilitada en el negocio (parte 1 de HU-AOS-16)."""
    from core.agents.instalaciones import instaladas_en_conexion

    propias = await instaladas_en_conexion(tenant_id, connection_id)
    if not propias:
        return set(del_negocio)
    sin_habilitar = sorted(p for p in propias if p not in del_negocio)
    if sin_habilitar:
        raise ErrorDelBuild(
            "instalacion_sin_habilitar",
            "Una habilidad instalada en la conexión no está habilitada en el negocio.",
            [f"{p}: habilítala en el negocio o quita la instalación de la conexión" for p in sin_habilitar],
        )
    return set(propias)


def _publicados(pack: Any) -> list[Any]:
    """Los `procedure/1` del manifiesto de la versión del pack (los mismos bytes que registra
    `pack_versions`, ya comprobado el hash), validados; uno inválido se salta con log."""
    from core.kernel.procedures import validar

    salida: list[Any] = []
    for crudo in pack.manifest.get("provides", {}).get("procedures", []):
        proc, _problemas = validar(crudo)
        if proc is None:
            log.warning("release_build_procedimiento_invalido", pack_id=pack.pack_id, version=pack.version)
            continue
        salida.append(proc)
    return salida


async def _habilidades(
    tenant_id: uuid.UUID, packs: set[str], modules: list[Any]
) -> tuple[list[man.Habilidad], list[str], list[Any]]:
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
        publicados: list[Any] = []
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
                publicados.extend(_publicados(pack))
    if problemas:
        raise ErrorDelBuild(
            "habilidad_no_disponible", "Una habilidad no tiene versión utilizable.", problemas
        )
    return habilidades, del_negocio, publicados


def _resolver_componentes(
    instancias: list[_Instancia], *, derivados: set[str], publicados: list[Any], habilidades: set[str]
) -> list[man.Componente]:
    """Los componentes con sus `ProcedureRef` resueltos a versión exacta (fail-closed)."""
    from core.releases.procedimientos import publicados_por_id, resolver_refs

    por_id = publicados_por_id(publicados)
    salida: list[man.Componente] = []
    faltan: list[str] = []
    for inst in instancias:
        refs = inst.contenido.get("procedures")
        if inst.componente.contract != "agentspec/3" or not isinstance(refs, list):
            salida.append(inst.componente)  # el KernelSpec tiene `procedures` de otra forma
            continue
        resueltos, problemas = resolver_refs(refs, abiertos=derivados, publicados=por_id, habilidades=habilidades)
        faltan.extend(f"{inst.componente.agent_key}: {p}" for p in problemas)
        salida.append(
            dataclasses.replace(inst.componente, procedures=tuple((r.id, r.version) for r in resueltos))
        )
    if faltan:
        raise ErrorDelBuild(
            "procedimiento_sin_resolver", "Un procedimiento de un agente no resuelve.", sorted(set(faltan))[:10]
        )
    return salida


def revisar_ruteo(instancias: list[_Instancia], componentes: list[man.Componente]) -> list[str]:
    """Referencias del `routing` del KernelSpec contra la release (HU-AOS-17, ADR-0138): cada
    `rules[].agent` es la key de un especialista de la conexión y cada `rules[].start` lo declara
    algún especialista. `defaultAgent` no se exige (el lector tolerante lo fija en `general` y el
    dueño puede quitar al General; `is_default` de la instancia gana, adenda HU-AOS-16 de
    ADR-0117). Un `routing` que no valida lo rechaza antes el contrato."""
    especialistas = {c.agent_key: c for c in componentes if c.role == "specialist"}
    declarados = {pid for c in especialistas.values() for pid, _v in c.procedures}
    problemas: list[str] = []
    for inst in instancias:
        if inst.componente.role != "core":
            continue
        routing = inst.contenido.get("routing")
        reglas = routing.get("rules") if isinstance(routing, dict) else None
        for regla in reglas if isinstance(reglas, list) else []:
            if not isinstance(regla, dict):
                continue
            rid = str(regla.get("id"))[:40]
            agente, start = regla.get("agent"), regla.get("start")
            if isinstance(agente, str) and agente not in especialistas:
                problemas.append(f"{rid}: el agente '{agente[:40]}' no es especialista de la conexión")
            if isinstance(start, str) and start not in declarados:
                problemas.append(f"{rid}: ningún especialista declara '{start[:80]}'")
    return problemas


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
                legacy = (await banderas_del_negocio(session, tenant_id)).runtime == "legacy"
            if legacy:
                fijadas = await refrescar_desde_v2(tenant_id, modulos)
            elif await _general_sigue_siendo_del_lector(tenant_id):
                fijadas = await refrescar_desde_v2(tenant_id, modulos, solo_general=True)
            else:
                fijadas = {}
        else:
            fijadas = await refrescar_desde_v2(tenant_id, modulos) if refrescar else {}
    instancias = await _componentes(tenant_id, connection_id, fijadas, catalogo)

    leido = await leer_spec_activo(tenant_id)
    del_negocio = set(enabled_business_packs(leido[1])) if leido else set()
    allowlist: set[str] = {p.name for p in leido[1].playbooks} if leido else set()
    del leido
    for inst in instancias:
        for ref in inst.contenido.get("playbooks") or []:
            if isinstance(ref, dict) and isinstance(ref.get("name"), str):
                allowlist.add(ref["name"])
    packs = await _efectivas(tenant_id, connection_id, del_negocio)
    habilidades, plugins_del_negocio, publicados = await _habilidades(tenant_id, packs, modulos)

    slug = await tenant_slug(tenant_id)
    try:
        flujo = read_tenant_flow_raw(slug) if slug else None
    except ValueError as exc:
        raise ErrorDelBuild("flujo_invalido", str(exc)) from None
    derivados = set(derivar_procedimientos([h.pack_id for h in habilidades], flujo))
    componentes = _resolver_componentes(
        instancias,
        derivados=derivados,
        publicados=publicados,
        habilidades={h.pack_id for h in habilidades} | set(plugins_del_negocio),
    )
    ruteo = revisar_ruteo(instancias, componentes)
    if ruteo:
        raise ErrorDelBuild("ruteo_invalido", "Una regla de enrutamiento no apunta a la release.", ruteo[:10])
    return man.armar(
        componentes=componentes,
        habilidades=habilidades,
        plugins_del_negocio=plugins_del_negocio,
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

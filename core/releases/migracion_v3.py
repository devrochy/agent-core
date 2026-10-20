"""Migración de un negocio al runtime por releases (HU-AOS-15, ADR-0112 y adenda de ADR-0117).

`agent-core db migrar-a-v3 --tenant <slug> | --todos [--dry-run] [--volver] [--construir]`
enciende `tenants.aos_runtime = 'v3'` (nunca `aos_procedures`) solo si el negocio está listo, y
lo devuelve a `legacy` con `--volver`. Fail-closed: cualquier precondición que falle bloquea
SOLO a ese negocio, con el motivo y el comando que lo arregla.

Precondiciones por negocio (bajo su `tenant_scope`):
1. `AOS_CONNECTION_ROUTING` encendida (orden de banderas de ADR-0112; no aplica a `--volver`).
2. El backfill de conexiones (HU-AOS-11) sin trabajo pendiente.
3. El backfill de agentes (HU-AOS-13) sin trabajo pendiente ni errores.
4. Toda conexión conversacional viva con release activa (`backfill-releases`).
5. La release activa de cada conexión carga, tiene Coordinador y especialista en foco, sus
   versiones pasan el contrato, el General concede `perfil` en `writeNamespaces` (lo que v2 ya
   escribía) y ninguna declarativa lleva referencias a secretos en la URL o el body (límite de la
   parte 1: el runtime v3 solo resuelve referencias en headers).
6. La release activa está al día: el hash del manifiesto que se construiría hoy (sin refrescar
   versiones) es su `manifest_hash`, y en `legacy` la conversión actual del AgentSpec v2 coincide con las versiones
   congeladas del Coordinador y del General (si el v2 se editó después del backfill, la release
   ejecutaría un asistente desactualizado). `--construir` construye y promueve la release nueva.

Avisos (no bloquean): conversaciones abiertas sin conexión (seguirán en legacy hasta que el
consumer las adopte, ADR-0115 §6) y `aos_procedures` ya encendida.

`--volver`: `aos_runtime = 'legacy'`. Si `aos_procedures` está encendida se rechaza salvo
`--apagar-procedimientos` (orden inverso de ADR-0112, CHECK `ck_tenants_aos_order`), y se
reportan las tareas gobernadas abiertas, que dejan de gobernarse.

Las banderas se escriben solo con `aos_fijar_banderas` (migración 0097, `core.kernel.banderas.
fijar_banderas`): desde el CLI con el rol dueño (`MIGRATIONS_DATABASE_URL`, como `sync-packs`),
desde el panel del master con su `admin_id` (`escritor`, HU-AOS-15). Fuera del `tenant_scope`:
`tenants` es tabla de sistema y solo el master las cambia (ADR-0112). Sin LLM.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings
from core.db.session import get_session
from core.kernel.banderas import BanderasRechazadas
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

COMANDO_CONEXIONES = "agent-core db backfill-conexiones"
COMANDO_AGENTES = "agent-core db backfill-agentes"
COMANDO_RELEASES = "agent-core db backfill-releases"
NAMESPACE_PERFIL = "perfil"


@dataclass
class ResultadoNegocio:
    tenant_id: uuid.UUID
    slug: str
    runtime_antes: str
    procedimientos: bool
    # encendido | ya_estaba | bloqueado | apagado | ya_legacy | listo (dry-run) | construida
    # | por_preparar (dry-run con `ejecutar_backfills`: faltan backfills que se corren al aplicar)
    accion: str = "bloqueado"
    bloqueos: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    # Lo que se hará al aplicar (dry-run con `ejecutar_backfills`).
    pasos: list[str] = field(default_factory=list)
    conexiones: int = 0


@dataclass
class ReporteMigracion:
    dry_run: bool
    volver: bool
    negocios: list[ResultadoNegocio] = field(default_factory=list)

    @property
    def fallo(self) -> bool:
        return any(n.accion == "bloqueado" for n in self.negocios)


def _mensaje(exc: Exception) -> str:
    """Motivo para la consola y el log, sin valores: de un error de validación solo las rutas
    (su texto incluye `input_value`, que puede ser contenido del negocio)."""
    if isinstance(exc, DBAPIError):
        return f"error de base de datos ({type(exc.orig).__name__})"
    if isinstance(exc, ValidationError):
        rutas = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return f"contenido que no pasa el contrato en: {', '.join(rutas)[:300]}"
    return f"{type(exc).__name__}: {str(exc)[:300]}"


# ------------------------------------------------------------------ precondiciones
async def _pendiente_de_conexiones(tenant_id: uuid.UUID) -> str | None:
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    previo = await backfill_conexiones(dry_run=True, tenant_id=tenant_id)
    pendientes: list[str] = []
    if previo.tipos_puestos:
        pendientes.append(f"{previo.tipos_puestos} conexión(es) sin tipo de catálogo")
    if previo.numeros.get("creada"):
        pendientes.append(f"{previo.numeros['creada']} número(s) vivo(s) sin conexión canónica")
    if previo.numeros.get("religada"):
        pendientes.append(f"{previo.numeros['religada']} conexión(es) canónica(s) por religar")
    if previo.chat_web.get("creada"):
        pendientes.append("el chat web sin su conexión")
    if not pendientes:
        return None
    return f"{' y '.join(pendientes)}: corre `{COMANDO_CONEXIONES}`"


async def _bloqueo_de_agentes(tenant_id: uuid.UUID, modulos: list[Any]) -> tuple[str | None, str | None]:
    """`(bloqueo, pendiente)`: un bloqueo no lo arregla correr el backfill; un pendiente sí."""
    from core.agents.backfill_agentes import backfill_agentes

    previo = await backfill_agentes(dry_run=True, tenant_id=tenant_id, modules=modulos)
    if previo.bloqueados or previo.errores:
        motivo = (*previo.bloqueados, *previo.errores)[0].mensaje
        return f"el backfill de agentes no pasa: {motivo[:200]}", None
    # Versiones por publicar (el v2 se editó después del backfill) no bloquean aquí: la release
    # queda "desactualizada" (punto 6) y `--construir` la refresca desde el v2.
    pendientes = [
        f"{v} {k}" for k, c in (("agente(s)", previo.agentes), ("instancia(s)", previo.instancias))
        if (v := c.get("creada"))
    ]
    if pendientes:
        return None, f"faltan {', '.join(pendientes)}: corre `{COMANDO_AGENTES}`"
    return None, None


async def ejecutar_backfills(tenant_id: uuid.UUID, modulos: list[Any]) -> str | None:
    """Backfills de conexiones, agentes y releases del negocio (`agent-core db backfill-*`
    acotados a él). Idempotentes y neutros en legacy (el runtime no lee esas filas). Devuelve el
    motivo si alguno falla. Los usan `migrar_a_v3(ejecutar_backfills=True)` y «Preparar mis datos»."""
    from core.agents.backfill_agentes import backfill_agentes
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.releases.release_uno import backfill_releases

    await backfill_conexiones(dry_run=False, tenant_id=tenant_id)
    agentes = await backfill_agentes(dry_run=False, tenant_id=tenant_id, modules=modulos)
    if agentes.fallo:
        problema = (*agentes.bloqueados, *agentes.errores)[0]
        return f"backfill de agentes: {problema.mensaje[:300]}"
    releases = await backfill_releases(dry_run=False, tenant_id=tenant_id, modules=modulos)
    if releases.fallo:
        return f"backfill de releases: {releases.errores[0].mensaje[:300]}"
    return None


async def _conexiones(tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT cc.id, cc.active_release_id FROM channel_connections cc "
                    "JOIN connection_types ct ON ct.id = cc.type_id "
                    "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND ct.produces_conversations ORDER BY cc.id"
                ),
                {"t": tenant_id},
            )
        ).all()
    return [(f[0], f[1]) for f in filas]


async def _hashes_del_v2(tenant_id: uuid.UUID, modulos: list[Any]) -> dict[str, str] | None:
    """`{key: content_hash}` de la conversión ACTUAL del AgentSpec v2 activo (en memoria, sin
    escribir): lo que el build congelaría hoy para el Coordinador y el General."""
    from core.agents.spec import leer_spec_activo
    from core.agents.tolerante import (
        KEY_COORDINADOR,
        KEY_GENERAL,
        ConversionFallida,
        convertir_negocio,
    )
    from core.agents.versiones import content_hash

    leido = await leer_spec_activo(tenant_id)
    if leido is None:
        return None
    try:
        conversion = await convertir_negocio(leido[1], modules=modulos)
    except ConversionFallida as exc:
        raise RuntimeError(f"conversión del v2 fallida: {str(exc)[:200]}") from None
    return {
        KEY_COORDINADOR: content_hash(conversion.kernel.model_dump(mode="json", by_alias=True)),
        KEY_GENERAL: content_hash(conversion.general.model_dump(mode="json", by_alias=True)),
    }


async def _revisar_release(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    modulos: list[Any],
    hashes_v2: dict[str, str] | None,
) -> list[str]:
    """Bloqueos de la release activa de una conexión (ver el docstring del módulo, puntos 5 y 6)."""
    from core.agents.spec_de_release import (
        VistaInvalida,
        declarativas_omitidas,
        validar_componentes,
    )
    from core.releases.build import ErrorDelBuild, armar_manifiesto
    from core.releases.cargar import ErrorDeRelease, cargar_release
    from core.releases.manifiesto import hash_de

    bloqueos: list[str] = []
    async with get_session() as session:
        release = await cargar_release(session, tenant_id, release_id)
    if release is None:
        return [f"conexión {connection_id}: la release activa no carga"]
    prefijo = f"conexión {connection_id} (release {release.number})"
    try:
        release.foco()  # hay un especialista por defecto (o único)
    except ErrorDeRelease as exc:
        return [f"{prefijo}: {exc}"]
    # Cualquier especialista puede quedar de dueño de una conversación: se revisan todos.
    for agente in (c for c in release.componentes if c.role == "specialist"):
        try:
            v3, _ = validar_componentes(agente, release.kernel)
        except VistaInvalida as exc:
            bloqueos.append(f"{prefijo}: agente '{agente.key}': {exc}")
            continue
        if NAMESPACE_PERFIL not in v3.memory.writeNamespaces:
            bloqueos.append(
                f"{prefijo}: el agente '{agente.key}' no concede '{NAMESPACE_PERFIL}' en memory.writeNamespaces "
                "(el v2 escribía el perfil del cliente): publícalo con ese namespace y construye la release"
            )
        omitidas = declarativas_omitidas(v3)
        if omitidas:
            bloqueos.append(
                f"{prefijo}: agente '{agente.key}': tools declarativas con referencias a secretos en la URL o el "
                f"body, que el runtime v3 todavía no resuelve: {', '.join(omitidas)} (muévelas a headers o espera "
                "la parte 2)"
            )
    if bloqueos:
        return bloqueos
    # Al día: el manifiesto de hoy (sin refrescar versiones) y, en legacy, la conversión del v2.
    try:
        hoy = await armar_manifiesto(tenant_id, connection_id, modules=modulos, refrescar=False)
    except ErrorDelBuild as exc:
        bloqueos.append(f"{prefijo}: la release de hoy no se puede construir ({exc.codigo}: {exc})")
        return bloqueos
    # Mismo hash que calcula el build (`built_with` fuera): versiones publicadas, habilidades,
    # overrides, flujo y AppSettings de hoy contra los congelados en la release.
    desactualizada = hash_de(hoy) != release.manifest_hash
    if hashes_v2 is not None:
        congelados = {c.key: c.content_hash for c in _componentes_del_manifiesto(release.manifest)}
        for key, huella in hashes_v2.items():
            if congelados.get(key) not in (None, huella):
                desactualizada = True
    if desactualizada:
        bloqueos.append(f"{prefijo}: desactualizada frente al AgentSpec v2 o los artefactos de hoy: construye y promueve (o `--construir`)")
    return bloqueos


@dataclass(frozen=True)
class _Comp:
    key: str
    content_hash: str


def _componentes_del_manifiesto(manifest: Any) -> list[_Comp]:
    return [
        _Comp(str(c.get("agent_key")), str(c.get("content_hash")))
        for c in (manifest.get("components") or [])
        if isinstance(c, dict)
    ]


async def _construir_y_promover(tenant_id: uuid.UUID, connection_id: uuid.UUID, modulos: list[Any]) -> uuid.UUID:
    from core.releases.build import construir
    from core.releases.promocion import promover

    resultado = await construir(
        tenant_id, connection_id, actor="system:migrar-a-v3", modules=modulos, notas="migrar-a-v3 --construir"
    )
    await promover(tenant_id, connection_id, resultado.release_id, actor="system:migrar-a-v3", motivo="migrar_a_v3")
    return resultado.release_id


async def _abiertas_sin_conexion(tenant_id: uuid.UUID) -> int:
    async with get_session() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM conversations WHERE tenant_id = :t AND deleted_at IS NULL "
                        "AND status IN ('open','escalated') AND connection_id IS NULL"
                    ),
                    {"t": tenant_id},
                )
            ).scalar_one()
        )


async def _tareas_gobernadas_abiertas(tenant_id: uuid.UUID) -> int:
    async with get_session() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM conversation_tasks WHERE tenant_id = :t "
                        "AND status IN ('active','suspended')"
                    ),
                    {"t": tenant_id},
                )
            ).scalar_one()
        )


# ------------------------------------------------------------------ banderas (rol dueño)
# Escribe las banderas: `(tenant_id, runtime, procedimientos | None)`. Por defecto, el rol dueño
# (CLI); la API del master inyecta uno que llama a la función con su `admin_id` (HU-AOS-15).
Escritor = Callable[[uuid.UUID, str, bool | None], Awaitable[None]]


async def _escribir_bandera(database_url: str | None, tenant_id: uuid.UUID, *, runtime: str, procedimientos: bool | None) -> None:
    from core.kernel.banderas import BanderasAOS, fijar_banderas

    settings = get_settings()
    url = database_url or settings.migrations_database_url
    if not url:
        # Sin admin master, `aos_fijar_banderas` solo acepta la sesión del rol dueño (0097).
        raise PermissionError("falta MIGRATIONS_DATABASE_URL (rol dueño) para escribir las banderas")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            actual = (
                await conn.execute(
                    text("SELECT aos_procedures, aos_routing FROM tenants WHERE id = :t AND deleted_at IS NULL FOR UPDATE"),
                    {"t": tenant_id},
                )
            ).first()
            if actual is None:
                raise LookupError("negocio no encontrado")
            # Volver a legacy apaga también el enrutamiento (HU-AOS-17): el CHECK de 0094 lo exige
            # (orden inverso de ADR-0112) y sin v3 no hay release que enrutar.
            objetivo = BanderasAOS(
                runtime=runtime,
                procedimientos=bool(actual[0]) if procedimientos is None else procedimientos,
                ruteo="off" if runtime == "legacy" else str(actual[1]),
            )
            await fijar_banderas(conn, tenant_id, objetivo, actor="system:migrar-a-v3")
    finally:
        await engine.dispose()


async def _negocios(tenant_ids: Iterable[uuid.UUID] | None) -> list[tuple[uuid.UUID, str, str, bool]]:
    async with get_session() as session:
        if tenant_ids is None:
            filas = await session.execute(
                text("SELECT id, slug, aos_runtime, aos_procedures FROM tenants WHERE deleted_at IS NULL ORDER BY slug")
            )
        else:
            filas = await session.execute(
                text(
                    "SELECT id, slug, aos_runtime, aos_procedures FROM tenants "
                    "WHERE id = ANY(cast(:ids AS uuid[])) AND deleted_at IS NULL ORDER BY slug"
                ),
                {"ids": list(tenant_ids)},
            )
        return [(f[0], str(f[1]), str(f[2]), bool(f[3])) for f in filas.all()]


# ------------------------------------------------------------------ comando
async def _encender(
    negocio: ResultadoNegocio,
    *,
    dry_run: bool,
    construir: bool,
    backfills: bool,
    modulos: list[Any],
    escribir: Escritor,
) -> None:
    tid = negocio.tenant_id
    if not get_settings().aos_connection_routing:
        negocio.bloqueos.append("AOS_CONNECTION_ROUTING está apagada: enciéndela antes (orden de banderas, ADR-0112)")
        return
    if negocio.runtime_antes == "legacy" and backfills and not dry_run:
        async with tenant_scope(tid):
            motivo = await ejecutar_backfills(tid, modulos)
        if motivo:
            negocio.bloqueos.append(motivo)
            return
    # En dry-run con `backfills`, lo que el backfill arregla no bloquea: es un paso de aplicar.
    previos = backfills and dry_run
    async with tenant_scope(tid):
        motivo = await _pendiente_de_conexiones(tid)
        if motivo:
            (negocio.pasos if previos else negocio.bloqueos).append(motivo)
        bloqueo, pendiente = await _bloqueo_de_agentes(tid, modulos)
        if bloqueo:
            negocio.bloqueos.append(bloqueo)
            return
        if pendiente:
            (negocio.pasos if previos else negocio.bloqueos).append(pendiente)
        if negocio.bloqueos:
            return
        if negocio.pasos:
            # Las releases dependen de las instancias: el resto se revisa después de los backfills.
            negocio.pasos.append(f"releases de las conexiones que no la tengan (`{COMANDO_RELEASES}`)")
            negocio.accion = "por_preparar"
            return
        conexiones = await _conexiones(tid)
        negocio.conexiones = len(conexiones)
        if not conexiones:
            negocio.avisos.append("sin conexiones conversacionales: con v3 ningún turno leería una release")
        sin_release = [cid for cid, rid in conexiones if rid is None]
        if sin_release:
            motivo = f"{len(sin_release)} conexión(es) sin release activa: corre `{COMANDO_RELEASES}`"
            if previos:
                negocio.pasos.append(motivo)
                negocio.accion = "por_preparar"
            else:
                negocio.bloqueos.append(motivo)
            return
        try:
            hashes_v2 = await _hashes_del_v2(tid, modulos) if negocio.runtime_antes == "legacy" else None
        except RuntimeError as exc:
            negocio.bloqueos.append(str(exc))
            return
        for cid, rid in conexiones:
            assert rid is not None
            bloqueos = await _revisar_release(tid, cid, rid, modulos=modulos, hashes_v2=hashes_v2)
            if bloqueos and construir and dry_run and all("desactualizada" in b for b in bloqueos):
                negocio.pasos.append(f"conexión {cid}: construir y promover una release nueva")
                continue
            if bloqueos and construir and not dry_run and all("desactualizada" in b for b in bloqueos):
                nueva = await _construir_y_promover(tid, cid, modulos)
                negocio.avisos.append(f"conexión {cid}: release nueva construida y promovida")
                # Se revisa la release recién promovida (no la anterior), ya sin el v2 de por medio.
                bloqueos = await _revisar_release(tid, cid, nueva, modulos=modulos, hashes_v2=None)
            negocio.bloqueos.extend(bloqueos)
        if negocio.bloqueos:
            return
        abiertas = await _abiertas_sin_conexion(tid)
        if abiertas:
            negocio.avisos.append(f"{abiertas} conversación(es) abierta(s) sin conexión: siguen en legacy hasta que el consumer las adopte")
    if negocio.procedimientos:
        negocio.avisos.append("aos_procedures ya está encendida: no se toca")
    if negocio.runtime_antes == "v3":
        negocio.accion = "ya_estaba"
        return
    if dry_run:
        negocio.accion = "listo"
        return
    await escribir(tid, "v3", None)
    negocio.accion = "encendido"


async def _apagar(negocio: ResultadoNegocio, *, dry_run: bool, apagar_procedimientos: bool, escribir: Escritor) -> None:
    tid = negocio.tenant_id
    if negocio.runtime_antes == "legacy":
        negocio.accion = "ya_legacy"
        return
    if negocio.procedimientos and not apagar_procedimientos:
        negocio.bloqueos.append("aos_procedures está encendida: apágala primero (o `--apagar-procedimientos`)")
        return
    async with tenant_scope(tid):
        abiertas = await _tareas_gobernadas_abiertas(tid)
    if abiertas:
        negocio.avisos.append(f"{abiertas} tarea(s) abierta(s) del motor dejan de gobernarse al volver a legacy")
    if dry_run:
        negocio.accion = "listo"
        return
    await escribir(tid, "legacy", False if negocio.procedimientos else None)
    negocio.accion = "apagado"


async def migrar_a_v3(
    *,
    tenant_ids: Iterable[uuid.UUID] | None,
    dry_run: bool,
    volver: bool = False,
    construir: bool = False,
    apagar_procedimientos: bool = False,
    modules: Iterable[Any] | None = None,
    database_url: str | None = None,
    backfills: bool = False,
    escritor: Escritor | None = None,
) -> ReporteMigracion:
    """Enciende (o apaga, con `volver`) `aos_runtime = 'v3'` en los negocios dados (todos con
    `tenant_ids=None`). Ver el docstring del módulo. `backfills` (solo con un negocio) corre
    antes los backfills del negocio; en dry-run, lo que ellos arreglan sale en `pasos`, no en
    `bloqueos`. `escritor` reemplaza la escritura con el rol dueño (la API del master)."""
    from core.plugin_manager import plugin_manager

    if construir and tenant_ids is None:
        # Construir y promover cambia la release activa: nunca en masa, negocio por negocio.
        raise ValueError("--construir exige --tenant (no se permite con --todos)")
    if backfills and tenant_ids is None:
        raise ValueError("los backfills se corren negocio por negocio (no con --todos)")

    async def _dueno(tid: uuid.UUID, runtime: str, procedimientos: bool | None) -> None:
        await _escribir_bandera(database_url, tid, runtime=runtime, procedimientos=procedimientos)

    escribir = escritor or _dueno
    modulos = list(modules if modules is not None else plugin_manager.list_modules())
    reporte = ReporteMigracion(dry_run=dry_run, volver=volver)
    for tid, slug, runtime, procedimientos in await _negocios(tenant_ids):
        negocio = ResultadoNegocio(tid, slug, runtime, procedimientos)
        reporte.negocios.append(negocio)
        try:
            if volver:
                await _apagar(negocio, dry_run=dry_run, apagar_procedimientos=apagar_procedimientos, escribir=escribir)
            else:
                await _encender(
                    negocio, dry_run=dry_run, construir=construir, backfills=backfills, modulos=modulos, escribir=escribir
                )
        except (DBAPIError, LookupError, RuntimeError, ValueError, PermissionError, BanderasRechazadas) as exc:
            negocio.accion = "bloqueado"
            negocio.bloqueos.append(_mensaje(exc))
            log.warning("migrar_a_v3_error", tenant_id=str(tid), error=_mensaje(exc)[:300])
        if negocio.bloqueos:
            negocio.accion = "bloqueado"
        log.info("migrar_a_v3", tenant_id=str(tid), accion=negocio.accion, dry_run=dry_run, volver=volver)
    return reporte


__all__ = ["Escritor", "ReporteMigracion", "ResultadoNegocio", "ejecutar_backfills", "migrar_a_v3"]

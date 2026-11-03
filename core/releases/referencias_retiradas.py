"""Auditoría y migración de referencias retiradas en agentes y releases (HU-AOS-59, ADR-0184).

`agent-core db auditar-referencias [--tenant <slug> | --todos]` (solo lectura) y
`agent-core db migrar-referencias --tenant <slug> [--aplicar]` (dry-run por defecto). El detector
y el reescritor son puros (`core.agents.referencias_retiradas`); aquí se leen las filas, se
comparan con el binario de hoy y, con `aplicar`, se publica y se reconstruye.

**Qué se mira** (por negocio, bajo su `tenant_scope`):

- la última versión PUBLICADA de cada agente activo (`version`);
- el borrador, si difiere de esa versión (`borrador`): se avisa, no cuenta como falla ni se migra
  (decisión del dueño: el equipo no edita lo que el dueño está redactando);
- el manifiesto de la release ACTIVA de cada conexión conversacional viva (`release`): las versiones
  que congela (`components[].agent_version_id`) y los procedimientos que resolvió
  (`components[].procedures`). Una release cuenta aunque la versión más nueva del agente ya esté
  limpia: es lo que corre. Se mira el manifiesto, no el estado del negocio (`migrar-a-v3
  --dry-run` dice `ya_estaba` aunque la release sea vieja).
- las conexiones borradas y las que no producen conversaciones (`playground`, integraciones como
  Google Calendar) se reportan aparte: no tienen release que corra ni cuentan como falla.

Afectado = una versión o una release de una conexión viva conversacional con una referencia
retirada. Es lo que pone el exit code distinto de cero.

**Cómo se migra** (`migrar_referencias`): cada agente afectado parte de su última versión
publicada (nunca del borrador), recibe el registro de retiros (`core.agents.retiros`), se valida
con `revisar_spec` (la misma regla que al publicar) y se publica con etiqueta y nota. El reemplazo
de un procedimiento solo se agrega si todas las conexiones del agente tienen la habilidad que lo
trae. Luego, por cada conexión viva cuya release activa estaba afectada, se construye la release
(`build.construir`, sin refrescar desde la ficha v2) y se promueve con la exención
`migrar_referencias`, la misma ruta de `migrar-a-v3 --construir`. Con un canary en curso la
release se construye pero NO se promueve: el canary no se detiene a ciegas. Idempotente: sin
afectados no hace nada. Sin LLM.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text

from core.agents import referencias_retiradas as ref
from core.agents.referencias_retiradas import Binario, Hallazgo
from core.agents.retiros import Reemplazo
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACTOR = "system:migrar-referencias"
MOTIVO = "migrar_referencias"
ETIQUETA = "sin referencias retiradas"
MARCA = "referencias_retiradas"

Ambito = Literal["version", "borrador", "release"]


# ------------------------------------------------------------------ auditoría
@dataclass(frozen=True)
class Fila:
    """Una referencia retirada encontrada."""

    ambito: Ambito
    agente: str
    version: int | None
    ruta: str
    codigo: str
    nombre: str
    destino: str
    # Dónde corre: «conexión <id> release #n» (releases activas que contienen esa versión).
    donde: tuple[str, ...] = ()

    @staticmethod
    def de(ambito: Ambito, agente: str, version: int | None, h: Hallazgo, donde: Iterable[str] = ()) -> Fila:
        return Fila(ambito, agente, version, h.ruta, h.codigo, h.nombre, h.destino, tuple(donde))


@dataclass(frozen=True)
class ConexionAparte:
    conexion_id: uuid.UUID
    motivo: str  # borrada | playground | integracion
    release: int | None
    filas: tuple[Fila, ...]


@dataclass
class NegocioAuditado:
    tenant_id: uuid.UUID
    slug: str
    filas: list[Fila] = field(default_factory=list)  # versiones y releases: lo que cuenta
    borradores: list[Fila] = field(default_factory=list)
    aparte: list[ConexionAparte] = field(default_factory=list)
    # Conexiones vivas conversacionales cuya release activa está afectada (lo que reconstruye migrar).
    conexiones_afectadas: dict[uuid.UUID, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def agentes_afectados(self) -> int:
        return len({f.agente for f in self.filas if f.ambito == "version"})

    @property
    def releases_afectadas(self) -> int:
        return len(self.conexiones_afectadas)

    @property
    def afectado(self) -> bool:
        return bool(self.filas)


@dataclass
class ReporteAuditoria:
    negocios: list[NegocioAuditado] = field(default_factory=list)

    @property
    def fallo(self) -> bool:
        return any(n.afectado or n.error for n in self.negocios)


def _json(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


async def _negocios(tenant_ids: Iterable[uuid.UUID] | None) -> list[tuple[uuid.UUID, str, str]]:
    async with get_session() as session:
        if tenant_ids is None:
            filas = await session.execute(
                text("SELECT id, slug, aos_runtime FROM tenants WHERE deleted_at IS NULL ORDER BY slug")
            )
        else:
            filas = await session.execute(
                text(
                    "SELECT id, slug, aos_runtime FROM tenants "
                    "WHERE id = ANY(cast(:ids AS uuid[])) AND deleted_at IS NULL ORDER BY slug"
                ),
                {"ids": list(tenant_ids)},
            )
        return [(f[0], str(f[1]), str(f[2])) for f in filas.all()]


@dataclass(frozen=True)
class _Version:
    id: uuid.UUID
    agent_id: uuid.UUID
    numero: int
    contrato: str
    contenido: dict[str, Any]


@dataclass(frozen=True)
class _Conexion:
    id: uuid.UUID
    borrada: bool
    tipo: str
    conversacional: bool
    release_id: uuid.UUID
    canary: bool

    @property
    def aparte(self) -> str | None:
        if self.borrada:
            return "borrada"
        if self.tipo == "playground":
            return "playground"
        return None if self.conversacional else "integracion"


async def _leer_agentes(tenant_id: uuid.UUID) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, _Version], dict[uuid.UUID, dict[str, Any]]]:
    """`{agent_id: key}` de los agentes activos, su última versión publicada y su borrador."""
    async with get_session() as session:
        agentes = {
            f[0]: str(f[1])
            for f in (
                await session.execute(
                    text(
                        "SELECT id, key FROM agents WHERE tenant_id = :t AND deleted_at IS NULL "
                        "AND status = 'active' ORDER BY key"
                    ),
                    {"t": tenant_id},
                )
            ).all()
        }
        ultimas = {
            f[1]: _Version(f[0], f[1], int(f[2]), str(f[3]), _json(f[4]))
            for f in (
                await session.execute(
                    text(
                        "SELECT DISTINCT ON (agent_id) id, agent_id, version, contract, content::text "
                        "FROM agent_versions WHERE tenant_id = :t ORDER BY agent_id, version DESC"
                    ),
                    {"t": tenant_id},
                )
            ).all()
            if f[1] in agentes
        }
        borradores = {
            f[0]: _json(f[1])
            for f in (
                await session.execute(
                    text("SELECT agent_id, content::text FROM agent_drafts WHERE tenant_id = :t AND deleted_at IS NULL"),
                    {"t": tenant_id},
                )
            ).all()
            if f[0] in agentes
        }
    return agentes, ultimas, borradores


async def _leer_conexiones(tenant_id: uuid.UUID) -> list[_Conexion]:
    """Conexiones con release activa, vivas o no. Una borrada puede conservar el puntero."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT cc.id, cc.deleted_at IS NOT NULL, cc.type_id, coalesce(ct.produces_conversations, false), "
                    "cc.active_release_id, cc.canary_release_id IS NOT NULL "
                    "FROM channel_connections cc LEFT JOIN connection_types ct ON ct.id = cc.type_id "
                    "WHERE cc.tenant_id = :t AND cc.active_release_id IS NOT NULL ORDER BY cc.id"
                ),
                {"t": tenant_id},
            )
        ).all()
    return [_Conexion(f[0], bool(f[1]), str(f[2]), bool(f[3]), f[4], bool(f[5])) for f in filas]


async def _leer_releases(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, dict[str, Any]]]:
    if not ids:
        return {}
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT id, number, manifest::text FROM connection_releases "
                    "WHERE tenant_id = :t AND id = ANY(cast(:ids AS uuid[]))"
                ),
                {"t": tenant_id, "ids": ids},
            )
        ).all()
    return {f[0]: (int(f[1]), _json(f[2])) for f in filas}


async def _leer_versiones(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, _Version]:
    if not ids:
        return {}
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT id, agent_id, version, contract, content::text FROM agent_versions "
                    "WHERE tenant_id = :t AND id = ANY(cast(:ids AS uuid[]))"
                ),
                {"t": tenant_id, "ids": ids},
            )
        ).all()
    return {f[0]: _Version(f[0], f[1], int(f[2]), str(f[3]), _json(f[4])) for f in filas}


def _uuid_o_none(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        return None


def _hallazgos_de_release(manifest: dict[str, Any], versiones: dict[uuid.UUID, _Version], binario: Binario) -> list[tuple[str, int, Hallazgo]]:
    """`(agent_key, versión, hallazgo)` de los componentes del manifiesto: lo que dicen las
    versiones que congela y lo que resolvió (`components[].procedures`)."""
    salida: list[tuple[str, int, Hallazgo]] = []
    for i, comp in enumerate(manifest.get("components") or []):
        if not isinstance(comp, dict):
            continue
        key = str(comp.get("agent_key"))
        numero = comp["version"] if isinstance(comp.get("version"), int) else 0
        vistos: set[tuple[str, str]] = set()
        version = versiones.get(_uuid_o_none(comp.get("agent_version_id")) or uuid.UUID(int=0))
        if version is not None:
            for h in ref.hallazgos_de_contenido(version.contenido, binario):
                salida.append((key, numero, h))
                vistos.add((h.tipo, h.nombre))
        for j, p in enumerate(comp.get("procedures") or []):
            if not isinstance(p, dict) or not isinstance(p.get("id"), str):
                continue
            pid, v = p["id"], p.get("version")
            if v in ("0.0.0", "=0.0.0") or binario.tiene_procedimiento(pid) or ("procedimiento", pid) in vistos:
                continue
            salida.append((key, numero, ref._procedimiento(f"components[{i}].procedures[{j}].id", pid)))
    return salida


async def _auditar_negocio(tenant_id: uuid.UUID, slug: str, binario: Binario) -> NegocioAuditado:
    """Debe correr dentro del `tenant_scope` del negocio (sin él, RLS devolvería 0 filas: un falso limpio)."""
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("referencias retiradas: fuera del tenant_scope del negocio")
    negocio = NegocioAuditado(tenant_id, slug)
    agentes, ultimas, borradores = await _leer_agentes(tenant_id)
    conexiones = await _leer_conexiones(tenant_id)
    releases = await _leer_releases(tenant_id, [c.release_id for c in conexiones])
    usadas: list[uuid.UUID] = []
    for _numero, manifest in releases.values():
        for comp in manifest.get("components") or []:
            vid = _uuid_o_none(comp.get("agent_version_id")) if isinstance(comp, dict) else None
            if vid is not None:
                usadas.append(vid)
    versiones = await _leer_versiones(tenant_id, usadas)

    # Releases activas de conexiones vivas conversacionales, por versión que congelan.
    donde: dict[uuid.UUID, list[str]] = {}
    for conexion in conexiones:
        if conexion.release_id not in releases:
            continue
        numero, manifest = releases[conexion.release_id]
        encontrados = _hallazgos_de_release(manifest, versiones, binario)
        filas = tuple(Fila.de("release", key, v, h) for key, v, h in encontrados)
        motivo = conexion.aparte
        if motivo is not None:
            if filas:
                negocio.aparte.append(ConexionAparte(conexion.id, motivo, numero, filas))
            continue
        for comp in manifest.get("components") or []:
            vid = _uuid_o_none(comp.get("agent_version_id")) if isinstance(comp, dict) else None
            if vid is not None:
                donde.setdefault(vid, []).append(f"conexión {conexion.id} release #{numero}")
        if filas:
            negocio.conexiones_afectadas[conexion.id] = numero
            negocio.filas.extend(
                Fila(f.ambito, f.agente, f.version, f.ruta, f.codigo, f.nombre, f.destino, (f"conexión {conexion.id} release #{numero}",))
                for f in filas
            )

    for aid, key in agentes.items():
        ultima = ultimas.get(aid)
        if ultima is not None:
            for h in ref.hallazgos_de_contenido(ultima.contenido, binario):
                negocio.filas.append(Fila.de("version", key, ultima.numero, h, donde.get(ultima.id, ())))
        borrador = borradores.get(aid)
        if borrador is not None and (ultima is None or borrador != ultima.contenido):
            for h in ref.hallazgos_de_contenido(borrador, binario):
                negocio.borradores.append(Fila.de("borrador", key, None, h))
    return negocio


async def auditar(
    *, tenant_ids: Iterable[uuid.UUID] | None = None, modules: Iterable[Any] | None = None
) -> ReporteAuditoria:
    """Solo lectura. `tenant_ids=None`: todos los negocios vivos."""
    from core.plugin_manager import plugin_manager

    binario = ref.binario_de(modules if modules is not None else plugin_manager.list_modules())
    reporte = ReporteAuditoria()
    for tid, slug, _runtime in await _negocios(tenant_ids):
        try:
            async with tenant_scope(tid):
                reporte.negocios.append(await _auditar_negocio(tid, slug, binario))
        except Exception as exc:  # un negocio que falla no tapa a los demás; el exit code lo marca
            log.warning("auditar_referencias_error", tenant_id=str(tid), error=type(exc).__name__)
            reporte.negocios.append(NegocioAuditado(tid, slug, error=f"{type(exc).__name__}: {str(exc)[:200]}"))
    return reporte


# ------------------------------------------------------------------ migración
@dataclass
class AgenteMigrado:
    agente: str
    version_desde: int
    # por_migrar (dry-run) | migrada | bloqueado
    accion: str
    version_nueva: int | None = None
    cambios: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    bloqueos: list[str] = field(default_factory=list)


@dataclass
class ConexionReconstruida:
    conexion_id: uuid.UUID
    release_anterior: int
    # por_reconstruir (dry-run) | promovida | construida_sin_promover | ya_estaba | bloqueada
    accion: str
    release_nueva: int | None = None
    detalle: str = ""


@dataclass
class NegocioMigrado:
    tenant_id: uuid.UUID
    slug: str
    runtime: str
    agentes: list[AgenteMigrado] = field(default_factory=list)
    conexiones: list[ConexionReconstruida] = field(default_factory=list)
    borradores: list[Fila] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def sin_cambios(self) -> bool:
        return not self.agentes and not self.conexiones

    @property
    def fallo(self) -> bool:
        """Quedó algo sin resolver: un agente bloqueado o una conexión que no se pudo reconstruir."""
        return any(a.accion == "bloqueado" for a in self.agentes) or any(
            c.accion == "bloqueada" for c in self.conexiones
        )


@dataclass
class ReporteMigracion:
    dry_run: bool
    negocios: list[NegocioMigrado] = field(default_factory=list)

    @property
    def fallo(self) -> bool:
        return any(n.fallo for n in self.negocios)


async def _conexiones_del_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[uuid.UUID]:
    """Conexiones vivas y conversacionales donde el agente tiene una instancia habilitada."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT DISTINCT ai.connection_id FROM agent_instances ai "
                    "JOIN channel_connections cc ON cc.tenant_id = ai.tenant_id AND cc.id = ai.connection_id "
                    "JOIN connection_types ct ON ct.id = cc.type_id "
                    "WHERE ai.tenant_id = :t AND ai.agent_id = :a AND ai.deleted_at IS NULL AND ai.enabled "
                    "AND cc.deleted_at IS NULL AND ct.produces_conversations ORDER BY ai.connection_id"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).all()
    return [f[0] for f in filas]


async def _ofrecible_para(tenant_id: uuid.UUID, agent_id: uuid.UUID, binario: Binario) -> ref.Ofrecible:
    """Quita el reemplazo de un procedimiento salvo que TODAS las conexiones del agente tengan la
    habilidad que lo trae (la versión es del agente, no de una conexión: un build donde falte
    fallaría con `procedimiento_sin_resolver`)."""
    from core.releases.build import ErrorDelBuild, habilidades_efectivas
    from core.releases.procedimientos import publicados_por_id, resolver_refs

    conexiones = await _conexiones_del_agente(tenant_id, agent_id)
    conjuntos: list[tuple[str, set[str] | str]] = []
    destinos: list[uuid.UUID | None] = [*conexiones] or [None]
    for cid in destinos:
        etiqueta = f"la conexión {cid}" if cid else "el negocio"
        try:
            conjuntos.append((etiqueta, await habilidades_efectivas(tenant_id, cid)))
        except ErrorDelBuild as exc:
            conjuntos.append((etiqueta, f"{exc.codigo}: {exc}"))
    por_id = publicados_por_id(p for ps in binario.procedimientos.values() for p in ps)

    def ofrecible(reemplazo: Reemplazo) -> str | None:
        for etiqueta, habilidades in conjuntos:
            if isinstance(habilidades, str):
                return f"{etiqueta} no se puede revisar ({habilidades})"
            _resueltos, problemas = resolver_refs(
                [{"id": reemplazo.id, "version": reemplazo.version}],
                abiertos=(),
                publicados=por_id,
                habilidades=habilidades,
            )
            if problemas:
                return f"en {etiqueta}: {problemas[0]}"
        return None

    return ofrecible


async def _migrar_agente(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    clave: str,
    ultima: _Version,
    binario: Binario,
    catalogo: Any,
    *,
    aplicar: bool,
) -> AgenteMigrado | None:
    from core.agents import versiones
    from core.agents.contrato_v3 import revisar_spec

    if not ref.hallazgos_de_contenido(ultima.contenido, binario):
        return None
    ofrecible = await _ofrecible_para(tenant_id, agent_id, binario)
    migracion = ref.migrar_contenido(ultima.contenido, binario, ofrecible)
    fila = AgenteMigrado(clave, ultima.numero, "por_migrar", cambios=migracion.cambios, avisos=migracion.avisos)
    for h in migracion.pendientes:
        fila.bloqueos.append(
            f"{h.ruta}: '{h.nombre}' ya no existe y no tiene entrada en el registro de retiros "
            "(agrégala en core/agents/retiros.py)"
        )
    if not fila.bloqueos:
        # La misma regla que al publicar: lo que el reescritor deja tiene que pasar el contrato.
        problemas = revisar_spec(migracion.contenido, catalogo, ultima.contrato)  # type: ignore[arg-type]
        fila.bloqueos.extend(f"{i.path} ({i.code})" for i in problemas)
    if fila.bloqueos:
        fila.accion = "bloqueado"
        return fila
    if not aplicar:
        return fila
    nota = (
        "Referencias retiradas del código (HU-AOS-59, registro de retiros): "
        + "; ".join(migracion.cambios)
        + (f". Avisos: {'; '.join(migracion.avisos)}" if migracion.avisos else "")
        + f". Parte de la versión {ultima.numero}; el borrador no se tocó."
    )
    version, creada = await versiones.publicar(
        tenant_id, agent_id, catalogo=catalogo, contenido=migracion.contenido, label=ETIQUETA, notes=nota
    )
    if not creada and version.version != ultima.numero:
        # El contenido corregido ya existía como una versión anterior (el hash lo repite): publicar
        # devuelve esa, y el build toma siempre la ÚLTIMA, que seguiría con lo retirado. Una marca en
        # `extensions` (reservado y sin semántica, ADR-0050) da el hash nuevo y, con él, la versión nueva.
        marcado = copy.deepcopy(migracion.contenido)
        marcado["extensions"] = {**(marcado.get("extensions") or {}), MARCA: f"corregida desde la versión {ultima.numero}"}
        version, creada = await versiones.publicar(
            tenant_id, agent_id, catalogo=catalogo, contenido=marcado, label=ETIQUETA, notes=nota
        )
        if not creada and version.version != ultima.numero:
            fila.accion = "bloqueado"
            fila.bloqueos.append(
                f"el contenido corregido ya existe como la versión {version.version} y no se pudo publicar uno nuevo: "
                "corrige el agente desde el panel"
            )
            return fila
        fila.avisos.append(
            f"el contenido corregido era idéntico a una versión anterior; se publicó con la marca `extensions.{MARCA}`"
        )
    fila.accion = "migrada"
    fila.version_nueva = version.version
    await _auditar(
        tenant_id,
        "referencias_retiradas_migradas",
        {
            "agente": clave,
            "version_desde": ultima.numero,
            "version_nueva": version.version,
            "creada": creada,
            "cambios": migracion.cambios,
            "avisos": migracion.avisos,
        },
    )
    log.info("referencias_retiradas_migradas", tenant_id=str(tenant_id), agente=clave, version=version.version)
    return fila


async def _auditar(tenant_id: uuid.UUID, accion: str, datos: dict[str, Any]) -> None:
    from core.db.models import AuditLog

    async with get_session() as session:
        session.add(AuditLog(tenant_id=tenant_id, actor=ACTOR, action=accion, input=datos))


async def _reconstruir(
    tenant_id: uuid.UUID,
    conexion: _Conexion,
    numero: int,
    modulos: list[Any],
    *,
    aplicar: bool,
) -> ConexionReconstruida:
    from core.releases.build import ErrorDelBuild, construir
    from core.releases.promocion import ConflictoDeRelease, promover

    fila = ConexionReconstruida(conexion.id, numero, "por_reconstruir")
    if not aplicar:
        if conexion.canary:
            fila.detalle = "hay una versión en prueba (canary): se construirá la release pero no se promoverá"
        return fila
    try:
        resultado = await construir(
            tenant_id, conexion.id, actor=ACTOR, origin="platform", modules=modulos,
            notas="migrar-referencias --aplicar", refrescar=False,
        )
    except ErrorDelBuild as exc:
        fila.accion = "bloqueada"
        fila.detalle = f"{exc.codigo}: {exc}" + (f" ({'; '.join(map(str, exc.detalle))})" if exc.detalle else "")
        return fila
    fila.release_nueva = resultado.number
    if conexion.canary:
        fila.accion = "construida_sin_promover"
        fila.detalle = (
            "hay una versión en prueba (canary) en la conexión: la release nueva quedó construida; "
            "publícala por el flujo del canary"
        )
        return fila
    try:
        await promover(
            tenant_id, conexion.id, resultado.release_id, actor=ACTOR, motivo=MOTIVO, exencion=MOTIVO
        )
    except ConflictoDeRelease as exc:
        if exc.codigo != "ya_activa":
            fila.accion = "bloqueada"
            fila.detalle = f"{exc.codigo}: {exc}"
            return fila
        fila.accion = "ya_estaba"
        return fila
    fila.accion = "promovida"
    await _auditar(
        tenant_id,
        "referencias_retiradas_release_promovida",
        {"conexion_id": str(conexion.id), "release_anterior": numero, "release_nueva": resultado.number},
    )
    return fila


async def migrar(
    *, tenant_id: uuid.UUID, aplicar: bool = False, modules: Iterable[Any] | None = None
) -> ReporteMigracion:
    """Migra las referencias retiradas de UN negocio (nunca en masa: cambia agentes y releases en
    vivo). Sin `aplicar` solo informa."""
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    modulos = list(modules if modules is not None else plugin_manager.list_modules())
    binario = ref.binario_de(modulos)
    catalogo = catalogo_de_acciones(modulos)
    reporte = ReporteMigracion(dry_run=not aplicar)
    for tid, slug, runtime in await _negocios([tenant_id]):
        negocio = NegocioMigrado(tid, slug, runtime)
        reporte.negocios.append(negocio)
        async with tenant_scope(tid):
            previo = await _auditar_negocio(tid, slug, binario)
            negocio.borradores = previo.borradores
            agentes, ultimas, _borradores = await _leer_agentes(tid)
            for aid, clave in agentes.items():
                if aid not in ultimas:
                    continue
                fila = await _migrar_agente(tid, aid, clave, ultimas[aid], binario, catalogo, aplicar=aplicar)
                if fila is not None:
                    negocio.agentes.append(fila)
            conexiones = {c.id: c for c in await _leer_conexiones(tid)}
            for cid, numero in previo.conexiones_afectadas.items():
                negocio.conexiones.append(await _reconstruir(tid, conexiones[cid], numero, modulos, aplicar=aplicar))
        if runtime == "legacy" and not negocio.sin_cambios:
            negocio.avisos.append(
                "el negocio está en `legacy`: el General y el Coordinador se regeneran desde la ficha v2 en cada build "
                "normal; si la ficha nombra algo retirado, reaparecerá (la ficha v2 queda fuera de HU-AOS-59)"
            )
        log.info(
            "migrar_referencias", tenant_id=str(tid), aplicar=aplicar,
            agentes=len(negocio.agentes), conexiones=len(negocio.conexiones),
        )
    return reporte


__all__ = [
    "ACTOR",
    "AgenteMigrado",
    "ConexionAparte",
    "ConexionReconstruida",
    "Fila",
    "NegocioAuditado",
    "NegocioMigrado",
    "ReporteAuditoria",
    "ReporteMigracion",
    "auditar",
    "migrar",
]

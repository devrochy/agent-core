"""Backfill de agentes del Agent OS (paso B2 de SPEC-26 §4.4, HU-AOS-13).

Por negocio, en su `tenant_scope` (ADR-0112 §1, ADR-0113 §5):

1. **Precondición (HU-AOS-11).** `agent_instance_checks()` rechaza una instancia en una conexión
   sin tipo conversacional, así que el backfill de conexiones va antes. Si en el negocio todavía
   haría trabajo que cambia las conexiones conversacionales (tipos por poner, conexiones
   canónicas por crear o por religar), el negocio queda **bloqueado** con el comando a correr y
   no se escribe nada en él. Un choque con una conexión manual del mismo número es solo un
   aviso: la conexión manual recibe sus instancias. El chat web todavía no es una conexión
   (tipo `webchat`, segunda parte de HU-AOS-11): un negocio con el widget activo se informa.
2. **El AgentSpec v2 activo, como lo ve el loop,** sin escribir en `agent_specs`
   (`leer_spec_activo`), salvo la siembra que haría el loop: un negocio sin spec activo recibe
   el default, igual que en su primer turno (`ensure_default_spec`).
3. **Conversión** con el lector tolerante (`convertir_negocio`): General (`agentspec/3`, con sus
   procedimientos abiertos por referencia `=0.0.0`; la release de HU-AOS-14 congela las
   secciones del negocio y el cuerpo se deriva al cargarla, con el flujo del binario) y Coordinador (`kernelspec/1` derivado de la `persona` v2). Se valida con el
   validador único antes de escribir nada.
4. **Secretos literales** de las `declarativeTools` v2: se COPIAN a la instalación `core`
   (`migrar_secretos`) ANTES de crear la primera versión, que es inmutable; ninguna versión
   lleva literales y `agent_specs` los conserva para el loop legacy. Un conflicto (mismo
   nombre, otro valor) o una referencia a un secreto que no existe es un error del negocio,
   detectado antes de escribir. (El lector ya poda el header sensible de una tool sin
   `allowedDomains`: hoy esa tool no llama a nadie.)
5. **Agentes reservados** `coordinador` (core) y `general` (specialist), con su borrador.
   El borrador se escribe solo al crear el agente: una re-corrida no lo pisa (`publicar` mueve
   su `base_version_id`).
6. **Versiones,** idempotentes por `legacy_agent_spec_id` (la fila de `agent_specs` de la que
   salen): con el mismo v2 activo no se publica nada; si el v2 activo cambió, se publica la
   versión siguiente. Pasan por `versiones.publicar` (mismo validador y hash que la API; con
   el dedupe por hash, el legacy id es "la primera fila v2 que produjo este contenido"). Una
   re-corrida con un binario nuevo NO refresca la versión de la misma fila v2: eso lo decide
   el build de releases (HU-AOS-14).
7. **Instancias** en cada conexión conversacional viva (también las pausadas: la pausa es
   otra cosa, ADR-0122): la `core` del Coordinador y la del General, por defecto. Las
   integraciones no reciben nada. Solo se inicializan las conexiones sin Coordinador vivo:
   una que ya lo tiene está configurada y no se toca (lo que el dueño quitó, como el General,
   no vuelve: `configurada` en el reporte). Si tiene otro Coordinador, se informa; si tiene
   otro agente por defecto, el General entra sin serlo (`instancias.asegurar_instancias_base`,
   la misma función de las altas posteriores y de la promoción de ADR-0127 §3).

Sin cambio de conducta: con `tenants.aos_runtime = 'legacy'` nada en runtime lee estas tablas
y `agent_specs` no cambia salvo la siembra del default (los literales v2 siguen ahí y el loop
los usa igual). La equivalencia del golden con el runtime v3 se mide en HU-AOS-14/15. Con `dry_run`
no se escribe nada y el reporte dice lo que se haría. Cada paso es su propia transacción; dos
corridas a la vez no duplican nada (índices únicos de key, legacy, hash, Coordinador y agente
por conexión). Re-correrlo es seguro y cubre las conexiones que nacieron después.

Nunca loguea ni reporta valores de secretos: solo nombres, rutas y conteos.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agentcore_contracts import AgentSpec
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.agents import versiones
from core.agents.contrato_v3 import Contrato, revisar_spec
from core.agents.credenciales import referencias_de_tool
from core.agents.instancias import ConexionNoConversacional
from core.agents.tolerante import (
    KEY_COORDINADOR,
    KEY_GENERAL,
    ConversionFallida,
    ConversionV3,
    convertir_negocio,
    migrar_secretos,
)
from core.db.models import (
    AgentRecord,
    AgentVersionRecord,
    AppSettings,
    ChannelConnection,
    ConnectionTypeRecord,
    Tenant,
)
from core.db.session import get_session
from core.kernel.procedures import Catalogo, catalogo_de_acciones
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

LOTE_POR_DEFECTO = 500
COMANDO_CONEXIONES = "agent-core db backfill-conexiones"

# key → (kind, nombre, descripción, contrato)
AGENTES: dict[str, tuple[str, str, str, Contrato]] = {
    KEY_COORDINADOR: (
        "core",
        "Coordinador",
        "Coordinador de la conexión (KernelSpec derivado de la persona v2).",
        "kernelspec/1",
    ),
    KEY_GENERAL: (
        "specialist",
        "General",
        "Agente General: el AgentSpec v2 del negocio (lector tolerante).",
        "agentspec/3",
    ),
}


class ErrorDelNegocio(RuntimeError):
    """El negocio no se puede migrar. El mensaje lleva rutas, nombres y conteos, nunca valores."""


@dataclass
class Problema:
    tenant_id: uuid.UUID
    mensaje: str


@dataclass
class ReporteAgentes:
    dry_run: bool
    negocios: int = 0
    migrados: int = 0
    bloqueados: list[Problema] = field(default_factory=list)
    errores: list[Problema] = field(default_factory=list)
    specs_sembrados: int = 0
    agentes: Counter[str] = field(default_factory=Counter)
    versiones: Counter[str] = field(default_factory=Counter)
    instancias: Counter[str] = field(default_factory=Counter)
    secretos: Counter[str] = field(default_factory=Counter)
    conexiones_conversacionales: int = 0
    # Integraciones (calendario, correo) y conexiones sin tipo de catálogo: no reciben agentes.
    conexiones_sin_agentes: int = 0
    # Choques con una conexión manual del mismo número (la manual recibe sus instancias).
    choques_manuales: int = 0
    # Negocios con el chat web activo: todavía no es una conexión (tipo `webchat`, HU-AOS-11).
    chat_web_sin_conexion: int = 0
    avisos: int = 0

    @property
    def fallo(self) -> bool:
        return bool(self.bloqueados or self.errores)


# ------------------------------------------------------------------ pasos
async def _pendiente_de_conexiones(tenant_id: uuid.UUID, reporte: ReporteAgentes, lote: int) -> str | None:
    """¿El backfill de conexiones (HU-AOS-11) todavía cambiaría algo que afecta a las
    instancias? Se corre en dry-run: es la única fuente de lo que le falta, sin escribir."""
    from core.gateway.conexion_canonica_backfill import backfill_conexiones

    previo = await backfill_conexiones(dry_run=True, tenant_id=tenant_id, lote=lote)
    reporte.choques_manuales += previo.numeros.get("choque_manual", 0)
    pendientes: list[str] = []
    if previo.tipos_puestos:
        pendientes.append(f"{previo.tipos_puestos} conexión(es) sin tipo de catálogo")
    if previo.numeros.get("creada"):
        pendientes.append(f"{previo.numeros['creada']} número(s) vivo(s) sin conexión canónica")
    if previo.numeros.get("religada"):
        pendientes.append(f"{previo.numeros['religada']} conexión(es) canónica(s) por religar a su cuenta")
    if not pendientes:
        return None
    return f"{' y '.join(pendientes)}: corre primero `{COMANDO_CONEXIONES}`"


async def _spec_activo(tenant_id: uuid.UUID, reporte: ReporteAgentes) -> tuple[uuid.UUID | None, AgentSpec]:
    from core.agents.spec import DEFAULT_SPEC, ensure_default_spec, leer_spec_activo

    leido = await leer_spec_activo(tenant_id)
    if leido is not None:
        return leido
    reporte.specs_sembrados += 1
    if reporte.dry_run:
        # Lo que sembraría el loop en su primer turno; en dry-run no tiene fila todavía.
        return None, AgentSpec.model_validate(DEFAULT_SPEC | {"tenantId": str(tenant_id)})
    await ensure_default_spec(tenant_id)
    leido = await leer_spec_activo(tenant_id)
    if leido is None:
        raise ErrorDelNegocio("no se pudo sembrar el AgentSpec por defecto")
    return leido


async def _chat_web_activo(tenant_id: uuid.UUID) -> bool:
    async with get_session() as session:
        config = (
            await session.execute(
                select(AppSettings.web_chat_config).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    return bool(isinstance(config, dict) and config.get("enabled"))


def _dominios_por_secreto(conversion: ConversionV3) -> dict[str, set[str]]:
    dominios: dict[str, set[str]] = {}
    for tool in conversion.general.declarativeTools:
        for ref in referencias_de_tool(tool):
            dominios.setdefault(ref, set()).update(tool.allowedDomains)
    return dominios


async def _revisar_secretos(tenant_id: uuid.UUID, conversion: ConversionV3) -> Counter[str]:
    """Antes de escribir: conflictos con secretos existentes y referencias sin secreto.
    Devuelve los conteos de lo que se migraría (sin valores)."""
    from core.agents.secretos import comparar_secreto, secretos_configurados

    dominios = _dominios_por_secreto(conversion)
    conteo: Counter[str] = Counter()
    conflictos: list[str] = []
    sin_dominios: list[str] = []
    for nombre, valor in conversion.secretos.items():
        igual, actuales = await comparar_secreto(tenant_id, nombre, valor)
        if igual is False:
            conflictos.append(nombre)
        elif igual is None:
            conteo["migrado"] += 1
        elif dominios.get(nombre, set()) <= set(actuales):
            conteo["ya_estaba"] += 1
        else:
            # Un secreto no se ata solo a dominios nuevos: hay que reescribirlo en el editor.
            sin_dominios.append(f"{nombre} ({', '.join(sorted(dominios[nombre] - set(actuales)))})")
    if conflictos:
        raise ErrorDelNegocio(
            f"secreto(s) {', '.join(sorted(conflictos))} ya existen en la instalación core con otro valor: "
            "revísalos en el editor de tools declarativas"
        )
    if sin_dominios:
        raise ErrorDelNegocio(
            f"secreto(s) ya migrado(s) que no cubren dominios nuevos de su tool: {'; '.join(sorted(sin_dominios))}. "
            "Reescríbelos en el editor de tools declarativas"
        )
    configurados = await secretos_configurados(tenant_id)
    faltan = sorted(set(dominios) - set(conversion.secretos) - set(configurados))
    if faltan:
        raise ErrorDelNegocio(f"las tools declarativas referencian secreto(s) no configurado(s): {', '.join(faltan)}")
    return conteo


def _revisar_contrato(contenido: dict[str, Any], contrato: Contrato, catalogo: Catalogo, quien: str) -> None:
    issues = revisar_spec(contenido, catalogo, contrato)
    if issues:
        detalle = "; ".join(f"{i.path} ({i.code})" for i in issues[:10])
        raise ErrorDelNegocio(f"el {quien} convertido no pasa el validador: {detalle}")


async def _asegurar_agente(
    tenant_id: uuid.UUID, key: str, borrador: dict[str, Any], reporte: ReporteAgentes
) -> uuid.UUID | None:
    """El agente reservado `key`, creado con su borrador si falta (aunque el negocio no tenga
    conexiones: las altas posteriores lo dan por existente). None solo en dry-run cuando
    todavía no existe."""
    kind, nombre, descripcion, _ = AGENTES[key]
    async with get_session() as session:
        existente = (
            await session.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == tenant_id, AgentRecord.key == key, AgentRecord.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            if existente.kind != kind:
                raise ErrorDelNegocio(f"el agente '{key}' existe con kind {existente.kind}, se esperaba {kind}")
            if existente.status != "active":
                raise ErrorDelNegocio(f"el agente '{key}' está archivado: reactívalo antes del backfill")
            reporte.agentes["ya_estaba"] += 1
            return existente.id
        reporte.agentes["creado"] += 1
        if reporte.dry_run:
            return None
        # Sin `versiones.crear_agente`: esa rechaza a propósito las keys reservadas (son de este
        # backfill). ON CONFLICT: otra corrida concurrente lo creó primero.
        agent_id = uuid.uuid4()
        creado = (
            await session.execute(
                text(
                    "insert into agents (id, tenant_id, key, kind, name, description) "
                    "values (:id, :t, :key, :kind, :name, :desc) "
                    "on conflict (tenant_id, key) where deleted_at is null do nothing returning id"
                ),
                {"id": agent_id, "t": tenant_id, "key": key, "kind": kind, "name": nombre, "desc": descripcion},
            )
        ).scalar_one_or_none()
        if creado is None:
            raise ErrorDelNegocio(f"el agente '{key}' se creó en paralelo: vuelve a correr el backfill")
        await session.execute(
            text("insert into agent_drafts (tenant_id, agent_id, content) values (:t, :a, cast(:c as jsonb))"),
            {"t": tenant_id, "a": agent_id, "c": json.dumps(borrador, ensure_ascii=False)},
        )
    return agent_id


async def _asegurar_version(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    contenido: dict[str, Any],
    spec_id: uuid.UUID | None,
    version_v2: str,
    catalogo: Catalogo,
    reporte: ReporteAgentes,
) -> None:
    if agent_id is not None and spec_id is not None:
        async with get_session() as session:
            existente = (
                await session.execute(
                    select(AgentVersionRecord.id).where(
                        AgentVersionRecord.tenant_id == tenant_id,
                        AgentVersionRecord.agent_id == agent_id,
                        AgentVersionRecord.legacy_agent_spec_id == spec_id,
                    )
                )
            ).scalar_one_or_none()
        if existente is not None:
            reporte.versiones["ya_estaba"] += 1
            return
    if reporte.dry_run or agent_id is None or spec_id is None:
        reporte.versiones["creada"] += 1
        return
    try:
        _, creada = await versiones.publicar(
            tenant_id,
            agent_id,
            catalogo=catalogo,
            contenido=contenido,
            legacy_agent_spec_id=spec_id,
            label=f"v2 {version_v2}"[:60],
            notes=f"Backfill HU-AOS-13 desde el AgentSpec v2 {version_v2} (lector tolerante, ADR-0113 §5).",
        )
    except IntegrityError as exc:
        # Dos corridas a la vez: la otra publicó la versión de esta fila v2 primero.
        if "uq_agent_versions_legacy" not in str(exc.orig) and "uq_agent_versions_hash" not in str(exc.orig):
            raise
        creada = False
    # `creada = False`: el mismo contenido ya estaba publicado (otra fila v2 idéntica).
    reporte.versiones["creada" if creada else "ya_estaba"] += 1


async def _instancias(
    tenant_id: uuid.UUID,
    agentes: dict[str, uuid.UUID | None],
    reporte: ReporteAgentes,
    avisos: list[str],
    lote: int,
) -> None:
    """Coordinador + General en cada conexión conversacional viva, por lotes de conexiones."""
    from core.agents.instancias import asegurar_instancias_base

    conocidos = {k: v for k, v in agentes.items() if v is not None}
    async with get_session() as session:
        reporte.conexiones_sin_agentes += int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChannelConnection)
                    .outerjoin(ConnectionTypeRecord, ConnectionTypeRecord.id == ChannelConnection.type_id)
                    .where(
                        ChannelConnection.tenant_id == tenant_id,
                        ChannelConnection.deleted_at.is_(None),
                        ConnectionTypeRecord.produces_conversations.is_not(True),
                    )
                )
            ).scalar_one()
        )
    ultimo: uuid.UUID | None = None
    while True:
        async with get_session() as session:
            query = (
                select(ChannelConnection.id)
                .join(ConnectionTypeRecord, ConnectionTypeRecord.id == ChannelConnection.type_id)
                .where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.deleted_at.is_(None),
                    ConnectionTypeRecord.produces_conversations.is_(True),
                )
                .order_by(ChannelConnection.id)
                .limit(lote)
            )
            if ultimo is not None:
                query = query.where(ChannelConnection.id > ultimo)
            conexiones = list((await session.execute(query)).scalars().all())
            reporte.conexiones_conversacionales += len(conexiones)
            for cid in conexiones:
                resultado = await asegurar_instancias_base(
                    session, tenant_id, cid, agentes=conocidos, aplicar=not reporte.dry_run
                )
                reporte.instancias[resultado.coordinador] += 1
                reporte.instancias[resultado.general] += 1
                if resultado.coordinador == "otro_coordinador":
                    avisos.append(f"la conexión {cid} ya tiene otro Coordinador: no se pisa")
                if resultado.general_sin_defecto:
                    avisos.append(f"la conexión {cid} ya tiene otro agente por defecto: el General entra sin serlo")
        if len(conexiones) < lote:
            return
        ultimo = conexiones[-1]


# ------------------------------------------------------------------ negocio
async def _migrar_negocio(
    tenant_id: uuid.UUID, reporte: ReporteAgentes, *, modules: list[Any], catalogo: Catalogo, lote: int
) -> list[str] | None:
    """Avisos del negocio, o None si quedó bloqueado por la precondición."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("backfill_agentes: fuera del tenant_scope del negocio")
    pendiente = await _pendiente_de_conexiones(tenant_id, reporte, lote)
    if pendiente is not None:
        reporte.bloqueados.append(Problema(tenant_id, pendiente))
        return None
    avisos: list[str] = []
    if await _chat_web_activo(tenant_id):
        reporte.chat_web_sin_conexion += 1
        avisos.append("el chat web está activo pero todavía no es una conexión: queda sin Coordinador")
    spec_id, spec = await _spec_activo(tenant_id, reporte)
    version_v2 = spec.version[:40]
    try:
        conversion = await convertir_negocio(spec, modules=modules)
    except ConversionFallida as exc:
        raise ErrorDelNegocio(str(exc)) from None
    finally:
        # El v2 lleva los literales: fuera de los locales del frame (Sentry los captura).
        del spec
    avisos.extend(conversion.avisos)
    general = conversion.general.model_dump(mode="json", by_alias=True)
    kernel = conversion.kernel.model_dump(mode="json", by_alias=True)
    _revisar_contrato(general, "agentspec/3", catalogo, "General")
    _revisar_contrato(kernel, "kernelspec/1", catalogo, "Coordinador")
    conteo_secretos = await _revisar_secretos(tenant_id, conversion)

    if not reporte.dry_run and conteo_secretos.get("migrado"):
        # Antes de la primera versión, que es inmutable (ADR-0113 §2).
        try:
            avisos.extend(await migrar_secretos(tenant_id, conversion))
        except ValueError as exc:  # SecretoInvalido o conflicto concurrente: sin valores
            raise ErrorDelNegocio(str(exc)[:300]) from None
    reporte.secretos.update(conteo_secretos)

    contenidos = {KEY_COORDINADOR: kernel, KEY_GENERAL: general}
    ids: dict[str, uuid.UUID | None] = {}
    for key in (KEY_COORDINADOR, KEY_GENERAL):
        ids[key] = await _asegurar_agente(tenant_id, key, contenidos[key], reporte)
    for key in (KEY_COORDINADOR, KEY_GENERAL):
        await _asegurar_version(tenant_id, ids[key], contenidos[key], spec_id, version_v2, catalogo, reporte)
    await _instancias(tenant_id, ids, reporte, avisos, lote)
    return avisos


async def backfill_agentes(
    *,
    dry_run: bool,
    tenant_id: uuid.UUID | None = None,
    lote: int = LOTE_POR_DEFECTO,
    modules: Iterable[Any] | None = None,
) -> ReporteAgentes:
    """Corre el backfill B2 en todos los negocios vivos (o en uno). Ver el docstring del módulo.

    `modules`: los módulos del binario para el catálogo de acciones; por defecto, los ya
    cargados (`plugin_manager.list_modules()`; el CLI hace `scan()` antes)."""
    from core.plugin_manager import plugin_manager

    if lote < 1:
        raise ValueError("El lote tiene que ser al menos 1")
    modulos = list(modules if modules is not None else plugin_manager.list_modules())
    catalogo = catalogo_de_acciones(modulos)
    reporte = ReporteAgentes(dry_run=dry_run)
    if tenant_id is not None:
        negocios = [tenant_id]
    else:
        async with get_session() as session:
            negocios = list(
                (await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)).order_by(Tenant.id)))
                .scalars()
                .all()
            )
    for negocio in negocios:
        reporte.negocios += 1
        try:
            async with tenant_scope(negocio):
                avisos = await _migrar_negocio(negocio, reporte, modules=modulos, catalogo=catalogo, lote=lote)
        except (
            ErrorDelNegocio,
            versiones.ErroresDeContrato,
            versiones.Conflicto,
            ConexionNoConversacional,
            DBAPIError,
        ) as exc:
            if isinstance(exc, versiones.ErroresDeContrato):
                mensaje = "; ".join(f"{i.path} ({i.code})" for i in exc.issues[:10])
            elif isinstance(exc, DBAPIError):
                # Sin `str(exc)`: SQLAlchemy imprime los parámetros (el contenido de la versión).
                mensaje = f"error de base de datos ({type(exc.orig).__name__}): vuelve a correr el backfill"
            else:
                mensaje = str(exc)
            reporte.errores.append(Problema(negocio, mensaje[:500]))
            log.warning("backfill_agentes_error", tenant_id=str(negocio), error=mensaje[:500])
            continue
        if avisos is None:
            log.warning("backfill_agentes_bloqueado", tenant_id=str(negocio), motivo=reporte.bloqueados[-1].mensaje)
            continue
        reporte.migrados += 1
        reporte.avisos += len(avisos)
        for aviso in avisos:
            log.info("backfill_agentes_aviso", tenant_id=str(negocio), aviso=aviso[:300])
    log.info(
        "backfill_agentes",
        dry_run=dry_run,
        negocios=reporte.negocios,
        migrados=reporte.migrados,
        bloqueados=len(reporte.bloqueados),
        errores=len(reporte.errores),
        agentes=dict(reporte.agentes),
        versiones=dict(reporte.versiones),
        instancias=dict(reporte.instancias),
        secretos=dict(reporte.secretos),
        conexiones_conversacionales=reporte.conexiones_conversacionales,
        conexiones_sin_agentes=reporte.conexiones_sin_agentes,
        choques_manuales=reporte.choques_manuales,
        chat_web_sin_conexion=reporte.chat_web_sin_conexion,
    )
    return reporte


__all__ = ["AGENTES", "LOTE_POR_DEFECTO", "ErrorDelNegocio", "Problema", "ReporteAgentes", "backfill_agentes"]

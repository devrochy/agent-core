"""Instancias de agentes por conexión (ADR-0112 §1, HU-AOS-13).

Cada conexión conversacional tiene exactamente un Coordinador (instancia `core`, índice único
parcial `uq_agent_instances_one_core`) y N especialistas, uno por defecto. Las integraciones
(calendario, correo saliente) no tienen agentes: `agent_instance_checks()` (0069) lo rechaza.

**Inicializar** una conexión es darle su Coordinador y su General. Solo se inicializa una
conexión **sin Coordinador vivo**: una que ya lo tiene está configurada y no se toca, así lo
que el dueño quitó o cambió (el General, el agente por defecto) no vuelve solo en un religar,
en otra corrida del backfill ni en el build de releases.

- `asegurar_instancias_base(session, tid, connection_id)`: inicializa la conexión con el
  Coordinador y el General del negocio (keys reservadas `coordinador` y `general`). La usan el
  backfill B2, las altas posteriores (`instancias_al_alta`) y la usará el build de releases
  (HU-AOS-14).
- `clonar_instancias(session, tid, desde, hacia)`: copia las instancias de una conexión a otra
  del negocio (promoción de ADR-0127 §3). Devuelve el mapa instancia de origen → de destino,
  solo de pares con el mismo agente: es el contrato con HU-AOS-14, que clona la release
  (`release_components`) con ese mapa.
- `instancias_al_alta(session, tid, phone_number_id, cuenta)`: el enganche de los cinco caminos
  de alta de un número (ADR-0122 §6, `meta_asset_claims.numero_para_conexion`), en la misma
  transacción, dentro de un SAVEPOINT: un error se registra y el alta sigue.
- `instancias_de_conexion_nueva(session, tid, connection_id)`: el mismo enganche para las
  conexiones sin cuenta de Meta (manual, `waweb`, chat web; HU-AOS-11 parte 2).
- Escrituras de la API: `agregar_instancia`, `actualizar_instancia`, `quitar_instancia`.
  Serializadas por conexión (bloqueo de su fila). El Coordinador lo gestiona el sistema: la API
  no lo agrega, no lo deshabilita ni lo quita. El agente por defecto está siempre habilitado y
  se cambia eligiendo otro.
- `listar_instancias(tid, connection_id)`: lectura para la API.

Todo corre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`. Con
`tenants.aos_runtime = 'legacy'` nada en runtime lee estas filas (ADR-0112): no cambia la
conducta. Con v3, qué de una instancia congela la release (ADR-0117 §2) lo decide HU-AOS-14:
la API lo documenta como "aplica con la próxima release".
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from core.agents.tolerante import KEY_COORDINADOR, KEY_GENERAL
from core.db.models import (
    AgentInstanceRecord,
    AgentRecord,
    AgentVersionRecord,
    ChannelConnection,
    ConnectionTypeRecord,
    utcnow,
)
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# `configurada`: la conexión ya tiene Coordinador y el General no está (lo quitó el dueño o
# nunca lo tuvo): no se recrea.
Resultado = Literal["creada", "ya_estaba", "otro_coordinador", "sin_agente", "configurada"]

PRIORIDAD_POR_DEFECTO = 100
# Tope de instancias vivas por conexión (lo que listan la API y el build de releases).
MAX_INSTANCIAS_POR_CONEXION = 50
_SQLSTATE_AISLAMIENTO = frozenset({"42501", "23503"})


class ConexionNoEncontrada(LookupError):
    pass


class ConexionNoConversacional(ValueError):
    """La conexión es una integración (o no tiene tipo de catálogo): no lleva agentes."""


class InstanciaNoEncontrada(LookupError):
    pass


class AgenteNoInstanciable(LookupError):
    """El agente no existe en el negocio (o está borrado)."""


class ConflictoInstancia(RuntimeError):
    """Regla de las instancias que el pedido rompe. `code` es estable para la API."""

    def __init__(self, code: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.code = code


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("instancias: fuera del tenant_scope del negocio")


async def _conexion_conversacional(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, *, bloquear: bool = False
) -> None:
    """La conexión viva y conversacional del negocio. Con `bloquear`, toma su fila
    (`FOR NO KEY UPDATE`) para serializar las escrituras de instancias de esa conexión: la regla
    "el agente por defecto está habilitado" no la garantiza ningún constraint."""
    query = (
        select(ConnectionTypeRecord.produces_conversations)
        .select_from(ChannelConnection)
        .outerjoin(ConnectionTypeRecord, ConnectionTypeRecord.id == ChannelConnection.type_id)
        .where(
            ChannelConnection.tenant_id == tenant_id,
            ChannelConnection.id == connection_id,
            ChannelConnection.deleted_at.is_(None),
        )
    )
    if bloquear:
        query = query.with_for_update(key_share=True, of=ChannelConnection)  # FOR NO KEY UPDATE
    fila = (await session.execute(query)).one_or_none()
    if fila is None:
        raise ConexionNoEncontrada(str(connection_id))
    if not fila[0]:
        raise ConexionNoConversacional(f"la conexión {connection_id} no es conversacional")


async def agentes_base(session: Any, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Ids del Coordinador y del General vivos y activos del negocio (los que existan)."""
    filas = await session.execute(
        select(AgentRecord.key, AgentRecord.id).where(
            AgentRecord.tenant_id == tenant_id,
            AgentRecord.key.in_((KEY_COORDINADOR, KEY_GENERAL)),
            AgentRecord.status == "active",
            AgentRecord.deleted_at.is_(None),
        )
    )
    return {key: aid for key, aid in filas}


async def _vivas(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> list[AgentInstanceRecord]:
    return list(
        (
            await session.execute(
                select(AgentInstanceRecord)
                .where(
                    AgentInstanceRecord.tenant_id == tenant_id,
                    AgentInstanceRecord.connection_id == connection_id,
                    AgentInstanceRecord.deleted_at.is_(None),
                )
                .order_by((AgentInstanceRecord.role != "core"), AgentInstanceRecord.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _tiene_core(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
    return (
        await session.execute(
            select(AgentInstanceRecord.id).where(
                AgentInstanceRecord.tenant_id == tenant_id,
                AgentInstanceRecord.connection_id == connection_id,
                AgentInstanceRecord.role == "core",
                AgentInstanceRecord.deleted_at.is_(None),
            )
        )
    ).first() is not None


async def _insertar(
    session: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    agent_id: uuid.UUID,
    rol: str,
    *,
    por_defecto: bool,
    prioridad: int = PRIORIDAD_POR_DEFECTO,
    habilitada: bool = True,
    overrides: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """INSERT idempotente: None si chocó con un índice único (escritura concurrente)."""
    return (
        await session.execute(
            text(
                "insert into agent_instances "
                "(tenant_id, connection_id, agent_id, role, is_default, priority, enabled, overrides) "
                "values (:t, :c, :a, :r, :d, :p, :e, cast(:o as jsonb)) on conflict do nothing returning id"
            ),
            {
                "t": tenant_id,
                "c": connection_id,
                "a": agent_id,
                "r": rol,
                "d": por_defecto,
                "p": prioridad,
                "e": habilitada,
                "o": _json(overrides or {}),
            },
        )
    ).scalar_one_or_none()


def _json(valor: dict[str, Any]) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class ResultadoInstancias:
    coordinador: Resultado
    general: Resultado
    # El General entró sin ser el agente por defecto (la conexión ya tenía otro).
    general_sin_defecto: bool = False


async def asegurar_instancias_base(
    session: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    agentes: dict[str, uuid.UUID] | None = None,
    aplicar: bool = True,
) -> ResultadoInstancias:
    """Inicializa la conexión con el Coordinador y el General (por defecto). Idempotente.

    - Solo actúa si la conexión **no tiene Coordinador vivo**. Si lo tiene, no escribe nada:
      `ya_estaba` (el del negocio) u `otro_coordinador` para el Coordinador, y `ya_estaba` o
      `configurada` (no está y no se recrea) para el General.
    - `agentes`: `{key: agent_id}` ya resueltos (el lote del backfill los pasa); por defecto
      se leen con `agentes_base`. Un agente que falta da `sin_agente` (en dry-run, todavía no
      existe: cuenta como `creada`).
    - `aplicar = False`: no escribe, dice lo que haría (dry-run). Con `aplicar` bloquea la fila
      de la conexión, como las escrituras de la API.
    - Otro agente por defecto (un especialista agregado antes que el Coordinador) → el General
      entra sin serlo.
    - Lanza `ConexionNoEncontrada` / `ConexionNoConversacional`; un error del trigger
      `agent_instance_checks()` se propaga (es un error, no un conflicto).
    - `session` tiene que haberse abierto dentro del `tenant_scope` de `tenant_id` (la RLS la
      fija al abrirla); aquí se comprueba el scope activo y además se filtra por `tenant_id`."""
    _exigir_scope(tenant_id)
    await _conexion_conversacional(session, tenant_id, connection_id, bloquear=aplicar)
    if agentes is None:
        agentes = await agentes_base(session, tenant_id)
    actuales = [(i.agent_id, i.role, i.is_default) for i in await _vivas(session, tenant_id, connection_id)]
    presentes = {aid for aid, _, _ in actuales}
    id_core = agentes.get(KEY_COORDINADOR)
    id_general = agentes.get(KEY_GENERAL)

    core_vivo = next((aid for aid, rol, _ in actuales if rol == "core"), None)
    if core_vivo is not None:
        return ResultadoInstancias(
            "ya_estaba" if core_vivo == id_core else "otro_coordinador",
            "ya_estaba" if id_general is not None and id_general in presentes else "configurada",
        )

    async def _una(aid: uuid.UUID | None, rol: str, por_defecto: bool) -> Resultado:
        if aid is not None and aid in presentes:
            return "ya_estaba"
        if aid is None:
            return "sin_agente" if aplicar else "creada"
        if not aplicar:
            return "creada"
        creada = await _insertar(session, tenant_id, connection_id, aid, rol, por_defecto=por_defecto)
        if creada is not None:
            return "creada"
        # Conflicto con una escritura concurrente: se relee para clasificarlo.
        otro = (
            await session.execute(
                select(AgentInstanceRecord.agent_id).where(
                    AgentInstanceRecord.tenant_id == tenant_id,
                    AgentInstanceRecord.connection_id == connection_id,
                    AgentInstanceRecord.role == "core",
                    AgentInstanceRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if rol == "core" and otro is not None and otro != aid:
            return "otro_coordinador"
        return "ya_estaba"

    coordinador = await _una(id_core, "core", False)
    otro_defecto = any(d for aid, _, d in actuales if aid != id_general)
    general = await _una(id_general, "specialist", not otro_defecto)
    return ResultadoInstancias(coordinador, general, general_sin_defecto=otro_defecto and general == "creada")


# ------------------------------------------------------------------ clon (promoción)
@dataclass(frozen=True)
class ResultadoClon:
    """`mapa`: instancia de origen → instancia de destino, solo pares con el mismo agente
    (creada ahora o que ya estaba). `omitidas`: (instancia de origen, motivo) de las que no
    tienen par: `destino_configurado` (el destino ya tenía Coordinador y no se le agrega nada),
    u `otro_coordinador`/`conflicto` si una escritura concurrente ganó."""

    mapa: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    omitidas: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    creadas: int = 0


async def clonar_instancias(
    session: Any, tenant_id: uuid.UUID, desde: uuid.UUID, hacia: uuid.UUID
) -> ResultadoClon:
    """Copia las instancias vivas de la conexión `desde` a `hacia` (las dos del negocio y
    conversacionales): agente, rol, por defecto, prioridad, habilitada y overrides.

    Como `asegurar_instancias_base`, solo agrega si `hacia` no tiene Coordinador vivo; si ya lo
    tiene, solo devuelve el mapa de lo que coincide. Nunca pisa el agente por defecto del
    destino. Va en la sesión del alta (misma transacción) y bloquea la fila de `hacia`.

    Contrato con HU-AOS-14: con `mapa` se clona `release_components` (FK
    `(tenant_id, connection_id, agent_id, agent_instance_id)`): por eso solo lleva pares con el
    mismo `agent_id`."""
    _exigir_scope(tenant_id)
    if desde == hacia:
        raise ValueError("clonar_instancias: origen y destino son la misma conexión")
    await _conexion_conversacional(session, tenant_id, desde)
    await _conexion_conversacional(session, tenant_id, hacia, bloquear=True)
    origen = await _vivas(session, tenant_id, desde)
    # agent_id -> (instancia, es por defecto)
    destino = {i.agent_id: (i.id, i.is_default) for i in await _vivas(session, tenant_id, hacia)}
    configurado = await _tiene_core(session, tenant_id, hacia)
    hay_defecto = any(d for _, d in destino.values())

    mapa: dict[uuid.UUID, uuid.UUID] = {}
    omitidas: list[tuple[uuid.UUID, str]] = []
    creadas = 0
    for inst in origen:  # el Coordinador primero
        par = destino.get(inst.agent_id)
        if par is not None:
            mapa[inst.id] = par[0]
            continue
        if configurado:
            omitidas.append((inst.id, "destino_configurado"))
            continue
        por_defecto = inst.is_default and not hay_defecto
        nueva = await _insertar(
            session, tenant_id, hacia, inst.agent_id, inst.role,
            por_defecto=por_defecto, prioridad=inst.priority, habilitada=inst.enabled or por_defecto,
            overrides=inst.overrides,
        )
        if nueva is None:  # una escritura concurrente ganó: se relee el par
            nueva = (
                await session.execute(
                    select(AgentInstanceRecord.id).where(
                        AgentInstanceRecord.tenant_id == tenant_id,
                        AgentInstanceRecord.connection_id == hacia,
                        AgentInstanceRecord.agent_id == inst.agent_id,
                        AgentInstanceRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if nueva is None:
                omitidas.append((inst.id, "otro_coordinador" if inst.role == "core" else "conflicto"))
                continue
        else:
            creadas += 1
            hay_defecto = hay_defecto or por_defecto
        destino[inst.agent_id] = (nueva, por_defecto)
        mapa[inst.id] = nueva
    return ResultadoClon(mapa=mapa, omitidas=omitidas, creadas=creadas)


# ------------------------------------------------------------------ altas posteriores
async def conexion_del_numero(
    session: Any, tenant_id: uuid.UUID, phone_number_id: str, cuenta: uuid.UUID
) -> uuid.UUID | None:
    """La conexión canónica viva de `phone_number_id` ligada a la cuenta `cuenta`, o None."""
    from core.gateway.conexion_canonica import PROVEEDOR

    return (
        await session.execute(
            select(ChannelConnection.id).where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.provider == PROVEEDOR,
                ChannelConnection.external_id == phone_number_id,
                ChannelConnection.meta_connection_id == cuenta,
                ChannelConnection.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def instancias_al_alta(
    session: Any,
    tenant_id: uuid.UUID,
    phone_number_id: str,
    cuenta: uuid.UUID,
    *,
    instancias_de: uuid.UUID | None = None,
) -> None:
    """Instancias de la conexión canónica de un número recién dado de alta o reconectado.

    Corre en la transacción del alta (`numero_para_conexion`, los cinco caminos de ADR-0122
    §6), dentro de un SAVEPOINT: un error se registra (`instancias_alta_fallo`) y el alta sigue.
    Con `aos_runtime = legacy` las instancias no hacen nada y el backfill las repara; un bug
    del Agent OS no debe tumbar las altas de WhatsApp. No hace nada si:
    - el número no tiene conexión canónica ligada a `cuenta` (choque con una manual);
    - el negocio todavía no tiene su Coordinador y su General: lo hará el backfill;
    - la conexión ya tiene Coordinador (religar o revivir el número no recrea nada).

    `instancias_de` (promoción de ADR-0127 §3): la conexión (`channel_connections`) del número de
    prueba. Sus instancias se clonan a la del número real antes de lo base, así el agente por
    defecto es el de la prueba. `PermissionError` (scope ajeno) se propaga: falla cerrado.
    HU-AOS-14 engancha aquí el clon de la release, con el mapa de `clonar_instancias`. Ojo: los
    triggers diferidos (`connection_requires_core`) se evalúan al COMMIT, fuera del savepoint."""

    async def _paso() -> None:
        _exigir_scope(tenant_id)
        conexion = await conexion_del_numero(session, tenant_id, phone_number_id, cuenta)
        if conexion is not None:
            await _inicializar_al_alta(session, tenant_id, conexion, instancias_de)

    await _sin_tumbar_el_alta(session, tenant_id, _paso, meta_connection_id=str(cuenta))


async def instancias_de_conexion_nueva(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Instancias de una conexión sin cuenta de Meta recién creada o revivida: manual
    (`connections.save_connection`), `waweb` (`waweb.connections`) o chat web
    (`chat_web_conexion`, al guardar el widget). La misma frontera que `instancias_al_alta`:
    en la sesión del alta, dentro de un SAVEPOINT, un error se registra y el alta sigue; no hace
    nada si el negocio todavía no tiene su Coordinador y su General, ni si la conexión ya tiene
    Coordinador. El llamador solo la invoca para tipos conversacionales."""

    async def _paso() -> None:
        _exigir_scope(tenant_id)
        await _inicializar_al_alta(session, tenant_id, connection_id, None)

    await _sin_tumbar_el_alta(session, tenant_id, _paso, connection_id=str(connection_id))


async def _sin_tumbar_el_alta(session: Any, tenant_id: uuid.UUID, paso: Any, **contexto: str) -> None:
    try:
        async with session.begin_nested():
            await paso()
    except PermissionError:
        raise  # fuera del tenant_scope del negocio: bug de aislamiento, falla cerrado
    except Exception as exc:  # frontera deliberada: el alta no depende del Agent OS
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        # RLS (42501) o FK compuesta (23503): señal de aislamiento, a nivel error con la traza.
        aislamiento = sqlstate in _SQLSTATE_AISLAMIENTO
        (log.error if aislamiento else log.warning)(
            "instancias_alta_fallo",
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
            sqlstate=sqlstate,
            exc_info=aislamiento,
            **contexto,
        )


async def _inicializar_al_alta(
    session: Any, tenant_id: uuid.UUID, conexion: uuid.UUID, instancias_de: uuid.UUID | None
) -> None:
    agentes = await agentes_base(session, tenant_id)
    if KEY_COORDINADOR not in agentes or KEY_GENERAL not in agentes:
        return
    clonadas = 0
    if instancias_de is not None and instancias_de != conexion:
        clonadas = (await clonar_instancias(session, tenant_id, instancias_de, conexion)).creadas
    base = await asegurar_instancias_base(session, tenant_id, conexion, agentes=agentes)
    if clonadas or "creada" in (base.coordinador, base.general):
        log.info(
            "instancias_alta_creadas",
            tenant_id=str(tenant_id),
            connection_id=str(conexion),
            clonadas=clonadas,
            coordinador=base.coordinador,
            general=base.general,
        )


# ------------------------------------------------------------------ escrituras de la API
@dataclass(frozen=True)
class InstanciaVista:
    id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    name: str
    role: str
    is_default: bool
    priority: int
    enabled: bool
    latest_version: int | None


def _conflicto_por_indice(exc: IntegrityError) -> ConflictoInstancia | None:
    """Traduce el choque con un índice único por su NOMBRE (no por el texto del error)."""
    orig = getattr(exc, "orig", None)
    # Con asyncpg el error original va en `orig.__cause__`; psycopg lo da en `orig.diag`.
    nombre = (
        getattr(getattr(orig, "__cause__", None), "constraint_name", None)
        or getattr(getattr(orig, "diag", None), "constraint_name", None)
        or getattr(orig, "constraint_name", None)
    )
    if nombre == "uq_agent_instances_agent":
        return ConflictoInstancia("already_assigned", "Ese agente ya está en la conexión.")
    if nombre == "uq_agent_instances_default":
        return ConflictoInstancia("conflict", "Otro cambio del agente por defecto ganó: vuelve a intentarlo.")
    if nombre == "uq_agent_instances_one_core":
        return ConflictoInstancia("core_exists", "La conexión ya tiene Coordinador.")
    return None


async def _instancia(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, instance_id: uuid.UUID
) -> AgentInstanceRecord:
    inst = (
        await session.execute(
            select(AgentInstanceRecord).where(
                AgentInstanceRecord.tenant_id == tenant_id,
                AgentInstanceRecord.connection_id == connection_id,
                AgentInstanceRecord.id == instance_id,
                AgentInstanceRecord.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        raise InstanciaNoEncontrada(str(instance_id))
    return inst


async def _abrir_escritura(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Escrituras de la API: no esperar indefinidamente detrás de un alta lenta, y bloquear la
    fila de la conexión."""
    await session.execute(text("set local lock_timeout = '5s'"))
    await _conexion_conversacional(session, tenant_id, connection_id, bloquear=True)


async def _desmarcar_defecto(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    # Primero se desmarca el anterior: `uq_agent_instances_default` no es diferible.
    await session.execute(
        update(AgentInstanceRecord)
        .where(
            AgentInstanceRecord.tenant_id == tenant_id,
            AgentInstanceRecord.connection_id == connection_id,
            AgentInstanceRecord.is_default.is_(True),
            AgentInstanceRecord.deleted_at.is_(None),
        )
        .values(is_default=False, updated_at=utcnow())
    )


async def _ya_cuenta_en_el_plan(session: Any, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    """¿El agente no necesita un cupo nuevo de `agentes`? Mismo criterio que la métrica
    (`core.billing.metrics._count_agents`): ya tiene una instancia viva en una conexión viva, o es
    el General, que viene incorporado y nunca ocupa cupo (enmienda del ADR-0142)."""
    if (
        await session.execute(
            select(AgentRecord.id).where(
                AgentRecord.tenant_id == tenant_id, AgentRecord.id == agent_id, AgentRecord.key == KEY_GENERAL
            )
        )
    ).first() is not None:
        return True
    return (
        await session.execute(
            select(AgentInstanceRecord.id)
            .join(
                ChannelConnection,
                (ChannelConnection.id == AgentInstanceRecord.connection_id)
                & (ChannelConnection.tenant_id == AgentInstanceRecord.tenant_id),
            )
            .where(
                AgentInstanceRecord.tenant_id == tenant_id,
                AgentInstanceRecord.agent_id == agent_id,
                AgentInstanceRecord.deleted_at.is_(None),
                ChannelConnection.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None


async def agente_cuenta_en_el_plan(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    """Lectura para los pre-chequeos (editor): ¿el agente ya ocupa un cupo de `agentes` o no
    necesita uno (el General)?"""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        return await _ya_cuenta_en_el_plan(session, tenant_id, agent_id)


async def agregar_instancia(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    priority: int = PRIORIDAD_POR_DEFECTO,
    enabled: bool = True,
    is_default: bool = False,
    actor: uuid.UUID | None = None,
) -> uuid.UUID:
    """Agrega un especialista a la conexión. Devuelve el id de la instancia.

    - El agente es del negocio, vivo, `active`, `kind = specialist` y con al menos una versión
      publicada (si no, el build de la release no tendría qué fijar).
    - La conexión ya tiene su Coordinador (lo pone el sistema: backfill o alta).
    - `is_default`: pasa a ser el agente por defecto (el anterior deja de serlo) y tiene que
      estar habilitado.
    - Si es la primera instancia del agente, aplica el tope de agentes publicados del plan:
      `core.billing.enforcement.QuotaExceeded` (D-35, ADR-0142)."""
    _exigir_scope(tenant_id)
    if is_default and not enabled:
        raise ConflictoInstancia("default_disabled", "El agente por defecto tiene que estar habilitado.")
    try:
        async with get_session() as session:
            await _abrir_escritura(session, tenant_id, connection_id)
            agente = (
                await session.execute(
                    select(AgentRecord).where(
                        AgentRecord.tenant_id == tenant_id,
                        AgentRecord.id == agent_id,
                        AgentRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if agente is None:
                raise AgenteNoInstanciable(str(agent_id))
            if agente.kind != "specialist":
                raise ConflictoInstancia(
                    "not_specialist",
                    "Solo se agregan especialistas: el Coordinador lo pone el sistema y el back-office "
                    "no atiende en la conversación.",
                )
            if agente.status != "active":
                raise ConflictoInstancia("agent_archived", "El agente está archivado.")
            publicada = (
                await session.execute(
                    select(func.count())
                    .select_from(AgentVersionRecord)
                    .where(AgentVersionRecord.tenant_id == tenant_id, AgentVersionRecord.agent_id == agent_id)
                )
            ).scalar_one()
            if not publicada:
                raise ConflictoInstancia("agent_unpublished", "Publica una versión del agente antes de agregarlo.")
            vivas = await _vivas(session, tenant_id, connection_id)
            if not any(i.role == "core" for i in vivas):
                raise ConflictoInstancia(
                    "core_missing",
                    "La conexión todavía no tiene Coordinador: corre el backfill de agentes del negocio.",
                )
            if any(i.agent_id == agent_id for i in vivas):
                raise ConflictoInstancia("already_assigned", "Ese agente ya está en la conexión.")
            if len(vivas) >= MAX_INSTANCIAS_POR_CONEXION:
                raise ConflictoInstancia(
                    "limit_reached", f"La conexión ya tiene {MAX_INSTANCIAS_POR_CONEXION} agentes: quita alguno antes."
                )
            # Tope de agentes publicados del plan (D-35, ADR-0142): la primera instancia de un
            # agente lo vuelve unidad facturable. `QuotaExceeded` sin escribir nada; cada entrada
            # lo traduce (409 `plan_limit_agentes`). Un agente que ya está en otra conexión no
            # crece la cuenta y no se mide. El lock va antes de esa lectura: dos altas del mismo
            # agente nuevo en dos conexiones no se cuentan dos veces.
            from core.billing.enforcement import enforce_growth, lock_growth

            await lock_growth(tenant_id, "agentes", session=session)
            if not await _ya_cuenta_en_el_plan(session, tenant_id, agent_id):
                await enforce_growth(tenant_id, "agentes", session=session)
            if is_default:
                await _desmarcar_defecto(session, tenant_id, connection_id)
            nueva = AgentInstanceRecord(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                connection_id=connection_id,
                agent_id=agent_id,
                role="specialist",
                is_default=is_default,
                priority=priority,
                enabled=enabled,
                overrides={},
            )
            session.add(nueva)
            await session.flush()
            nuevo_id = nueva.id
    except IntegrityError as exc:
        conflicto = _conflicto_por_indice(exc)
        if conflicto is None:
            raise
        raise conflicto from None
    log.info(
        "instancia_agregada",
        tenant_id=str(tenant_id), connection_id=str(connection_id), instance_id=str(nuevo_id),
        agent_id=str(agent_id), is_default=is_default, actor=str(actor) if actor else None,
    )
    return nuevo_id


async def actualizar_instancia(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    enabled: bool | None = None,
    priority: int | None = None,
    is_default: Literal[True] | None = None,
    actor: uuid.UUID | None = None,
) -> None:
    """Habilita o deshabilita, cambia la prioridad o lo vuelve el agente por defecto.

    - El Coordinador no se deshabilita (ADR-0112 §1: exactamente uno por conexión): para que
      el asistente no responda está la pausa de la conexión (ADR-0122).
    - El agente por defecto no se deshabilita: primero se elige otro. Volverse por defecto
      exige estar habilitado (o habilitarse en el mismo pedido) y ser especialista.
    - Quitar el "por defecto" sin elegir otro no existe: siempre queda uno."""
    _exigir_scope(tenant_id)
    try:
        async with get_session() as session:
            await _abrir_escritura(session, tenant_id, connection_id)
            inst = await _instancia(session, tenant_id, connection_id, instance_id)
            if enabled is False and inst.role == "core":
                raise ConflictoInstancia(
                    "core_required",
                    "El Coordinador no se deshabilita: para que el asistente no responda, pausa la conexión.",
                )
            habilitada = inst.enabled if enabled is None else enabled
            if is_default and inst.role != "specialist":
                raise ConflictoInstancia("not_specialist", "Solo un especialista puede ser el agente por defecto.")
            if (is_default or inst.is_default) and not habilitada:
                raise ConflictoInstancia(
                    "default_disabled",
                    "El agente por defecto tiene que estar habilitado: elige otro por defecto antes.",
                )
            if is_default and not inst.is_default:
                await _desmarcar_defecto(session, tenant_id, connection_id)
                inst.is_default = True
            if enabled is not None:
                inst.enabled = enabled
            if priority is not None:
                inst.priority = priority
            inst.updated_at = utcnow()
            await session.flush()
    except IntegrityError as exc:
        conflicto = _conflicto_por_indice(exc)
        if conflicto is None:
            raise
        raise conflicto from None
    log.info(
        "instancia_actualizada",
        tenant_id=str(tenant_id), connection_id=str(connection_id), instance_id=str(instance_id),
        enabled=enabled, priority=priority, is_default=is_default, actor=str(actor) if actor else None,
    )


async def quitar_instancia(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, instance_id: uuid.UUID, *, actor: uuid.UUID | None = None
) -> None:
    """Borrado lógico (invariante 8). El Coordinador y el agente por defecto no se quitan.
    Las `release_components` que apunten a la instancia siguen válidas (la fila no se borra)."""
    _exigir_scope(tenant_id)
    async with get_session() as session:
        await _abrir_escritura(session, tenant_id, connection_id)
        inst = await _instancia(session, tenant_id, connection_id, instance_id)
        if inst.role == "core":
            raise ConflictoInstancia("core_required", "El Coordinador de la conexión no se quita.")
        if inst.is_default:
            raise ConflictoInstancia(
                "default_required", "Es el agente por defecto: elige otro por defecto antes de quitarlo."
            )
        ahora = utcnow()
        inst.deleted_at = ahora
        inst.updated_at = ahora
        await session.flush()
    log.info(
        "instancia_quitada",
        tenant_id=str(tenant_id), connection_id=str(connection_id), instance_id=str(instance_id),
        actor=str(actor) if actor else None,
    )


# ------------------------------------------------------------------ lectura
async def listar_instancias(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> list[InstanciaVista]:
    """Instancias vivas de la conexión del negocio, el Coordinador primero.
    `ConexionNoEncontrada` si la conexión no es de este negocio (o está borrada)."""
    _exigir_scope(tenant_id)
    ultima = (
        select(AgentVersionRecord.agent_id, func.max(AgentVersionRecord.version).label("v"))
        .where(AgentVersionRecord.tenant_id == tenant_id)
        .group_by(AgentVersionRecord.agent_id)
        .subquery()
    )
    async with get_session() as session:
        existe = (
            await session.execute(
                select(ChannelConnection.id).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.id == connection_id,
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise ConexionNoEncontrada(str(connection_id))
        filas = (
            await session.execute(
                select(AgentInstanceRecord, AgentRecord.key, AgentRecord.name, ultima.c.v)
                .join(
                    AgentRecord,
                    (AgentRecord.tenant_id == AgentInstanceRecord.tenant_id)
                    & (AgentRecord.id == AgentInstanceRecord.agent_id),
                )
                .outerjoin(ultima, ultima.c.agent_id == AgentInstanceRecord.agent_id)
                .where(
                    AgentInstanceRecord.tenant_id == tenant_id,
                    AgentInstanceRecord.connection_id == connection_id,
                    AgentInstanceRecord.deleted_at.is_(None),
                )
                .order_by(
                    (AgentInstanceRecord.role != "core"),
                    AgentInstanceRecord.priority,
                    AgentInstanceRecord.created_at,
                )
                .limit(200)
            )
        ).all()
    return [
        InstanciaVista(
            id=i.id,
            agent_id=i.agent_id,
            key=key,
            name=name,
            role=i.role,
            is_default=i.is_default,
            priority=i.priority,
            enabled=i.enabled,
            latest_version=v,
        )
        for i, key, name, v in filas
    ]


async def obtener_instancia(tenant_id: uuid.UUID, connection_id: uuid.UUID, instance_id: uuid.UUID) -> InstanciaVista:
    """Una instancia viva, con el formato de `listar_instancias` (respuesta de las escrituras)."""
    for vista in await listar_instancias(tenant_id, connection_id):
        if vista.id == instance_id:
            return vista
    raise InstanciaNoEncontrada(str(instance_id))


__all__ = [
    "AgenteNoInstanciable",
    "ConexionNoConversacional",
    "ConexionNoEncontrada",
    "ConflictoInstancia",
    "InstanciaNoEncontrada",
    "InstanciaVista",
    "ResultadoClon",
    "ResultadoInstancias",
    "actualizar_instancia",
    "agentes_base",
    "agregar_instancia",
    "asegurar_instancias_base",
    "clonar_instancias",
    "conexion_del_numero",
    "instancias_al_alta",
    "listar_instancias",
    "obtener_instancia",
    "quitar_instancia",
]

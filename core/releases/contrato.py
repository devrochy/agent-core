"""Contract del Agent OS (C1, HU-AOS-15, ADR-0115 §6 y ADR-0117): el último paso de la migración.

`agent-core db contract-aos [--aplicar --confirmo-sin-vuelta-atras]` cierra el expand/contract de
la conversación por conexión. **Es un punto sin retorno**: después, el camino anterior (reescribir
`connection_id` con `AOS_CONNECTION_ROUTING` apagada) choca con el índice único y con el NOT NULL.
Por eso NO es una migración de Alembic (correría sola en el siguiente despliegue): es un comando
manual, en seco por defecto, que solo aplica si TODAS las precondiciones se cumplen.

Precondiciones (global: el contract lo es; cualquiera que falle bloquea y se informa con su motivo):
1. `AOS_CONNECTION_ROUTING` encendida en este entorno.
2. Ningún negocio vivo en `aos_runtime = 'legacy'`: `core_decisions` exige release y conexión. Los
   negocios dados de baja (borrado lógico) no cuentan aquí, pero sus conversaciones sí entran en las
   revisiones 3 y en la deduplicación: el índice único y el NOT NULL las alcanzan.
3. Ninguna conversación sin `connection_id` (incluye cerradas y borradas; las cubre
   `backfill-conexiones`): el NOT NULL las exigiría todas.
4. Cada conexión conversacional viva tiene release activa y **2 releases estables sin rollback**:
   al menos dos `promoted` (sin contar las de un rollback) y ningún `rolled_back` posterior a la
   penúltima.
5. Con `--aplicar`, la confirmación explícita `--confirmo-sin-vuelta-atras`.

Pasos al aplicar (cada uno idempotente; se puede reintentar si se corta):
1. **Deduplicar sin cerrar**: en cada (negocio, conexión, cliente) con más de una conversación
   abierta (`status IN ('open','escalated')`) queda la de `started_at` más reciente (el mismo orden
   que el consumer); a las demás se les pasan los mensajes y se marcan con `deleted_at` (sin
   borrado físico). Sus tareas y `task_events` no se mueven (append-only). Con el rol de la app y
   `tenant_scope` de cada negocio.
2. Con el rol dueño, fuera de transacción donde hace falta:
   - índice único parcial `uq_conversations_open_per_connection` (`CONCURRENTLY`; si un intento
     anterior dejó el índice inválido, se descarta y se rehace);
   - `conversations.connection_id` NOT NULL (CHECK `NOT VALID` → `VALIDATE` → `SET NOT NULL`, sin
     escaneo bajo bloqueo exclusivo);
   - `core_decisions`: CHECK `ck_core_decisions_contract` (conexión obligatoria y, en un turno
     `routed`, release obligatoria) solo para filas posteriores al contract, para que el histórico
     (legacy, sin release) no impida validarlo;
   - `VALIDATE CONSTRAINT` de toda FK y CHECK que el expand dejó `NOT VALID` (bloqueo
     `SHARE UPDATE EXCLUSIVE`: no frena las escrituras);
   - se quita la FK simple `conversations_connection_id_fkey`, que duplica `fk_conv_connection`.

Fuera de este contract (cambios de código con su propia revisión, ver el runbook): quitar
`meta_asset_claims.mover_conversaciones` cuando el sender y las guardas dejen de leer
`conversations.meta_connection_id`, `web_chat_resolve`, el secreto en claro de `app_settings` y
`agent_specs` de solo lectura.

Sin LLM.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from core.config import get_settings
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

AVISO_CAMINOS = (
    "antes de aplicar, comprueba que el despliegue incluya que toda conversación nace con conexión (conexión interna "
    "`playground` del Centro de pruebas y conexión del número en el consumer, ADR-0115 adenda 2026-10-24): sin eso un "
    "`INSERT` con `connection_id = NULL` fallaría con el NOT NULL (runbook § 5.1)"
)
INDICE = "uq_conversations_open_per_connection"
CHECK_CONEXION = "ck_conv_connection_nn"
CHECK_DECISIONES = "ck_core_decisions_contract"
FK_SIMPLE = "conversations_connection_id_fkey"
FK_COMPUESTA = "fk_conv_connection"
COMANDO_CONEXIONES = "agent-core db backfill-conexiones"
COMANDO_MIGRAR = "agent-core db migrar-a-v3"
_LOCK_TIMEOUT = "5s"
_LOCK_VALIDAR = "60s"


@dataclass
class ReporteContrato:
    dry_run: bool
    aplicado: bool = False
    # Lo que impide aplicar (cada uno con el comando o la acción que lo arregla).
    bloqueos: list[str] = field(default_factory=list)
    # Lo que se hará (en seco) o se hizo (al aplicar).
    pasos: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    negocios: int = 0
    duplicadas: int = 0
    sin_conexion: int = 0
    validadas: list[str] = field(default_factory=list)
    fallo: bool = False


# ------------------------------------------------------------------ precondiciones
async def _negocios(tenant_ids: Sequence[uuid.UUID] | None = None) -> list[tuple[uuid.UUID, str, str, bool]]:
    """`(id, slug, aos_runtime, vivo)` de TODOS los negocios, también los dados de baja (borrado
    lógico): sus conversaciones siguen en la tabla y el índice único y el NOT NULL las alcanzan."""
    async with get_session() as session:
        if tenant_ids is None:
            filas = await session.execute(
                text("SELECT id, slug, aos_runtime, deleted_at IS NULL FROM tenants ORDER BY slug")
            )
        else:
            filas = await session.execute(
                text(
                    "SELECT id, slug, aos_runtime, deleted_at IS NULL FROM tenants "
                    "WHERE id = ANY(cast(:ids AS uuid[])) ORDER BY slug"
                ),
                {"ids": list(tenant_ids)},
            )
        return [(f[0], str(f[1]), str(f[2]), bool(f[3])) for f in filas.all()]


async def _sin_conexion(tenant_id: uuid.UUID) -> int:
    """Conversaciones del negocio sin conexión, de cualquier estado (el NOT NULL las exige todas)."""
    async with get_session() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM conversations WHERE tenant_id = :t AND connection_id IS NULL"),
                    {"t": tenant_id},
                )
            ).scalar_one()
        )


async def _problemas_de_releases(tenant_id: uuid.UUID) -> list[str]:
    """Conexiones conversacionales vivas sin release activa o sin 2 releases estables sin rollback."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    """
                    SELECT cc.id, cc.active_release_id,
                      (SELECT count(*) FROM release_events e
                        WHERE e.tenant_id = cc.tenant_id AND e.connection_id = cc.id AND e.type = 'promoted'
                          AND coalesce(e.data->>'reason', '') <> 'rollback') AS promociones,
                      (SELECT count(*) FROM release_events r
                        WHERE r.tenant_id = cc.tenant_id AND r.connection_id = cc.id AND r.type = 'rolled_back'
                          AND r.created_at > coalesce((
                            SELECT p.created_at FROM release_events p
                             WHERE p.tenant_id = cc.tenant_id AND p.connection_id = cc.id AND p.type = 'promoted'
                               AND coalesce(p.data->>'reason', '') <> 'rollback'
                             ORDER BY p.created_at DESC OFFSET 1 LIMIT 1), 'infinity'::timestamptz)) AS rollbacks
                    FROM channel_connections cc
                    JOIN connection_types ct ON ct.id = cc.type_id
                    WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND ct.produces_conversations
                    ORDER BY cc.id
                    """
                ),
                {"t": tenant_id},
            )
        ).all()
    problemas: list[str] = []
    for conexion, activa, promociones, rollbacks in filas:
        if activa is None:
            problemas.append(f"la conexión {conexion} no tiene release activa")
        elif promociones < 2:
            problemas.append(f"la conexión {conexion} tiene {promociones} release(s) promovida(s): se piden 2 estables")
        elif rollbacks:
            problemas.append(f"la conexión {conexion} tuvo un rollback después de su penúltima promoción")
    return problemas


async def _duplicadas(tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """`(sobrante, la_que_queda, cliente)` de cada conversación abierta repetida por (conexión,
    cliente). Queda primero la escalada o con el bot pausado (una persona la atiende: perderla
    devolvería el cliente al bot) y, entre iguales, la de `started_at` más reciente (el orden del
    consumer)."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    """
                    SELECT id, keep, customer_id FROM (
                      SELECT id, customer_id,
                        first_value(id) OVER w AS keep,
                        row_number() OVER w AS rn
                      FROM conversations
                      WHERE tenant_id = :t AND status IN ('open','escalated') AND deleted_at IS NULL
                        AND connection_id IS NOT NULL
                      WINDOW w AS (PARTITION BY connection_id, customer_id
                                   ORDER BY (status = 'escalated' OR bot_paused) DESC, started_at DESC, id DESC)
                    ) d WHERE rn > 1 ORDER BY id
                    """
                ),
                {"t": tenant_id},
            )
        ).all()
    return [(f[0], f[1], f[2]) for f in filas]


async def _deduplicar(tenant_id: uuid.UUID, trios: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]) -> int:
    """Pasa los mensajes del sobrante a la que queda y lo marca borrado (nunca lo cierra ni lo
    borra). Una transacción por cliente, con el MISMO candado que el consumer: un mensaje que
    entra a mitad no se pierde, y si algo cambió desde el cálculo (alguna ya no está abierta) se
    salta ese par; el siguiente intento lo recalcula. Devuelve cuántas deduplicó."""
    hechas = 0
    for sobrante, queda, cliente in trios:
        async with get_session() as session:
            await session.execute(
                text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"conversation:{tenant_id}:{cliente}"}
            )
            vigentes = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM conversations WHERE tenant_id = :t AND id IN (:s, :k) "
                        "AND customer_id = :c AND status IN ('open','escalated') AND deleted_at IS NULL"
                    ),
                    {"t": tenant_id, "s": sobrante, "k": queda, "c": cliente},
                )
            ).scalar_one()
            if vigentes != 2:
                log.warning("contrato_aos_par_cambio", sobrante=str(sobrante))
                continue
            await session.execute(
                text(
                    "UPDATE messages SET conversation_id = :k WHERE tenant_id = :t AND conversation_id = :s "
                    "AND EXISTS (SELECT 1 FROM conversations WHERE tenant_id = :t AND id = :k)"
                ),
                {"t": tenant_id, "k": queda, "s": sobrante},
            )
            # La ventana de servicio de WhatsApp más larga de las dos sigue valiendo.
            await session.execute(
                text(
                    "UPDATE conversations SET service_window_expires_at = greatest(service_window_expires_at, "
                    "(SELECT service_window_expires_at FROM conversations WHERE tenant_id = :t AND id = :s)) "
                    "WHERE tenant_id = :t AND id = :k"
                ),
                {"t": tenant_id, "k": queda, "s": sobrante},
            )
            await session.execute(
                text("UPDATE conversations SET deleted_at = now() WHERE tenant_id = :t AND id = :s AND deleted_at IS NULL"),
                {"t": tenant_id, "s": sobrante},
            )
            hechas += 1
    return hechas


async def _pendientes_de_validar() -> list[str]:
    """Constraints `NOT VALID` de `public` (lo que el contract va a validar). Catálogo: sin RLS."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT c.conrelid::regclass::text || '.' || c.conname FROM pg_constraint c "
                    "WHERE NOT c.convalidated AND c.contype IN ('c', 'f') "
                    "AND c.connamespace = 'public'::regnamespace ORDER BY 1"
                )
            )
        ).all()
    return [str(f[0]) for f in filas]


# ------------------------------------------------------------------ DDL (rol dueño)
# Cada paso recibe el esquema (siempre `public`; los tests usan uno descartable).
async def _constraint_existe(conn: AsyncConnection, esquema: str, tabla: str, nombre: str) -> tuple[bool, bool]:
    """`(existe, validada)` de un constraint de `<esquema>.<tabla>`."""
    fila = (
        await conn.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = :n AND conrelid = cast(:r AS regclass)"),
            {"n": nombre, "r": f"{esquema}.{tabla}"},
        )
    ).first()
    return (fila is not None, bool(fila[0]) if fila is not None else False)


async def _crear_indice(conn: AsyncConnection, esquema: str = "public") -> str:
    estado = (
        await conn.execute(
            text(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :n AND c.relnamespace = cast(:s AS regnamespace)"
            ),
            {"n": INDICE, "s": esquema},
        )
    ).first()
    if estado is not None and bool(estado[0]):
        return "índice único ya existía"
    if estado is not None:
        await conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {esquema}.{INDICE}"))
    await conn.execute(
        text(
            f"CREATE UNIQUE INDEX CONCURRENTLY {INDICE} ON {esquema}.conversations (tenant_id, connection_id, customer_id) "
            "WHERE status IN ('open','escalated') AND deleted_at IS NULL"
        )
    )
    return "índice único creado"


async def _connection_id_obligatorio(conn: AsyncConnection, esquema: str = "public") -> str:
    anulable = (
        await conn.execute(
            text(
                "SELECT is_nullable = 'YES' FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'conversations' AND column_name = 'connection_id'"
            ),
            {"s": esquema},
        )
    ).scalar_one()
    if not anulable:
        return "conversations.connection_id ya era NOT NULL"
    existe, validada = await _constraint_existe(conn, esquema, "conversations", CHECK_CONEXION)
    if not existe:
        await conn.execute(
            text(f"ALTER TABLE {esquema}.conversations ADD CONSTRAINT {CHECK_CONEXION} CHECK (connection_id IS NOT NULL) NOT VALID")
        )
    if not validada:
        await conn.execute(text(f"ALTER TABLE {esquema}.conversations VALIDATE CONSTRAINT {CHECK_CONEXION}"))
    # PG12+: con el CHECK validado, SET NOT NULL no escanea la tabla.
    await conn.execute(text(f"ALTER TABLE {esquema}.conversations ALTER COLUMN connection_id SET NOT NULL"))
    return "conversations.connection_id pasó a NOT NULL"


async def _check_de_decisiones(conn: AsyncConnection, esquema: str = "public") -> str:
    existe, validada = await _constraint_existe(conn, esquema, "core_decisions", CHECK_DECISIONES)
    if existe and validada:
        return "CHECK de core_decisions ya existía"
    if not existe:
        # El corte deja afuera el histórico (legacy, sin release): solo vale para filas nuevas.
        corte = (await conn.execute(text("SELECT now()"))).scalar_one().isoformat()
        await conn.exec_driver_sql(
            f"ALTER TABLE {esquema}.core_decisions ADD CONSTRAINT {CHECK_DECISIONES} CHECK "
            f"(created_at < TIMESTAMPTZ '{corte}' OR (connection_id IS NOT NULL "
            "AND (outcome <> 'routed' OR release_id IS NOT NULL))) NOT VALID"
        )
    await conn.execute(text(f"ALTER TABLE {esquema}.core_decisions VALIDATE CONSTRAINT {CHECK_DECISIONES}"))
    return "CHECK de core_decisions creado y validado"


async def _validar_pendientes(conn: AsyncConnection, esquema: str = "public") -> list[str]:
    """`VALIDATE CONSTRAINT` de todo lo que el expand dejó `NOT VALID` en el esquema."""
    filas = (
        await conn.execute(
            text(
                "SELECT c.conrelid::regclass::text, c.conname FROM pg_constraint c "
                "WHERE NOT c.convalidated AND c.contype IN ('c', 'f') "
                "AND c.connamespace = cast(:s AS regnamespace) ORDER BY 1, 2"
            ),
            {"s": esquema},
        )
    ).all()
    hechas: list[str] = []
    for tabla, nombre in filas:
        await conn.execute(text(f'ALTER TABLE {tabla} VALIDATE CONSTRAINT "{nombre}"'))
        hechas.append(f"{tabla}.{nombre}")
    return hechas


async def _quitar_fk_simple(conn: AsyncConnection, esquema: str = "public") -> str:
    existe_simple, _ = await _constraint_existe(conn, esquema, "conversations", FK_SIMPLE)
    if not existe_simple:
        return "la FK simple de conversations ya no existía"
    existe, validada = await _constraint_existe(conn, esquema, "conversations", FK_COMPUESTA)
    if not (existe and validada):
        raise RuntimeError(f"{FK_COMPUESTA} no existe o no está validada: no se quita la FK simple")
    await conn.execute(text(f"ALTER TABLE {esquema}.conversations DROP CONSTRAINT {FK_SIMPLE}"))
    return "FK simple de conversations quitada (la cubre la compuesta)"


async def _aplicar_ddl(reporte: ReporteContrato, database_url: str | None) -> None:
    url = database_url or get_settings().migrations_database_url
    if not url:
        raise PermissionError("falta MIGRATIONS_DATABASE_URL (rol dueño) para aplicar el contract")
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
            reporte.pasos.append(await _crear_indice(conn))
            reporte.pasos.append(await _connection_id_obligatorio(conn))
            reporte.pasos.append(await _check_de_decisiones(conn))
            # Validar no frena las escrituras, pero puede tardar y esperar a otro DDL o a un vacuum.
            await conn.execute(text(f"SET lock_timeout = '{_LOCK_VALIDAR}'"))
            reporte.validadas = await _validar_pendientes(conn)
            reporte.pasos.append(f"{len(reporte.validadas)} constraint(s) NOT VALID validadas")
            await conn.execute(text(f"SET lock_timeout = '{_LOCK_TIMEOUT}'"))
            reporte.pasos.append(await _quitar_fk_simple(conn))
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ comando
async def contraer(
    *,
    dry_run: bool = True,
    confirmado: bool = False,
    database_url: str | None = None,
    tenant_ids: Sequence[uuid.UUID] | None = None,
) -> ReporteContrato:
    """Revisa las precondiciones y, si todas se cumplen y `dry_run` es falso con `confirmado`,
    aplica el contract. Nunca aplica a medias por una precondición: bloquea antes de escribir.

    `tenant_ids` acota SOLO la revisión en seco (para saber si un negocio está listo): el contract
    es global y aplicarlo sobre un subconjunto no tiene sentido."""
    if tenant_ids is not None and not dry_run:
        raise ValueError("el contract es global: `tenant_ids` solo vale en seco")
    reporte = ReporteContrato(dry_run=dry_run)
    if not dry_run and not confirmado:
        reporte.bloqueos.append("aplicar es un punto sin retorno: confirma con --confirmo-sin-vuelta-atras")
    if not get_settings().aos_connection_routing:
        reporte.bloqueos.append("AOS_CONNECTION_ROUTING está apagada en este entorno (orden de banderas, ADR-0112)")

    negocios = await _negocios(tenant_ids)
    reporte.negocios = len(negocios)
    pares: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]] = {}
    for tenant_id, slug, runtime, vivo in negocios:
        if vivo and runtime != "v3":
            reporte.bloqueos.append(f"{slug}: sigue en aos_runtime = {runtime} (corre `{COMANDO_MIGRAR} --tenant {slug}`)")
        async with tenant_scope(tenant_id):
            huerfanas = await _sin_conexion(tenant_id)
            if huerfanas:
                reporte.sin_conexion += huerfanas
                estado = "" if vivo else " (negocio dado de baja)"
                reporte.bloqueos.append(
                    f"{slug}{estado}: {huerfanas} conversación(es) sin conexión (corre `{COMANDO_CONEXIONES}` o purga el negocio)"
                )
            # Un negocio dado de baja ya no atiende: no necesita releases ni runtime v3.
            for problema in await _problemas_de_releases(tenant_id) if vivo else []:
                reporte.bloqueos.append(f"{slug}: {problema}")
            repetidas = await _duplicadas(tenant_id)
        if repetidas:
            pares[tenant_id] = repetidas
            reporte.duplicadas += len(repetidas)

    pendientes = await _pendientes_de_validar()
    reporte.avisos.append(
        f"se validarán {len(pendientes)} constraint(s) NOT VALID de `public` (no solo las del Agent OS): " + ", ".join(pendientes)
        if pendientes
        else "no hay constraints NOT VALID por validar"
    )
    reporte.avisos.append(AVISO_CAMINOS)
    if reporte.duplicadas:
        reporte.pasos.append(f"deduplicar {reporte.duplicadas} conversación(es) abierta(s) repetida(s), sin cerrarlas")
    reporte.pasos.append(f"índice único {INDICE} y conversations.connection_id NOT NULL")
    reporte.pasos.append(f"CHECK {CHECK_DECISIONES} (conexión y release obligatorias desde hoy) en core_decisions")
    reporte.pasos.append("VALIDATE de las FK y CHECK que el expand dejó NOT VALID; se quita la FK simple de conversations")

    if reporte.bloqueos or dry_run:
        return reporte

    hechas = 0
    for tenant_id, repetidas in pares.items():
        async with tenant_scope(tenant_id):
            hechas += await _deduplicar(tenant_id, repetidas)
    # Lo planeado ya no es futuro: de aquí en adelante se informa lo que pasó.
    reporte.pasos = [f"{hechas} de {reporte.duplicadas} conversación(es) repetida(s) deduplicada(s) sin cerrarlas"]
    try:
        await _aplicar_ddl(reporte, database_url)
    except Exception as exc:  # fail-closed: se informa dónde quedó; cada paso es re-ejecutable
        log.error("contrato_aos_fallo", error=type(exc).__name__)
        reporte.fallo = True
        reporte.bloqueos.append(f"se cortó al aplicar ({type(exc).__name__}: {exc}); vuelve a correr el comando")
        return reporte
    reporte.aplicado = True
    return reporte


__all__ = ["ReporteContrato", "contraer"]

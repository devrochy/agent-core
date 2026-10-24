"""Backfill de la conexión canónica (ADR-0115 §6, HU-AOS-11): idempotente, por lotes y por negocio.

Cubre lo que existía antes del despliegue; desde ahí cada alta crea su conexión
(`meta_asset_claims.numero_para_conexion`). Pasos, en `tenant_scope` de cada negocio:

1. **Tipo de las conexiones viejas.** Las filas sin `type_id` reciben el del catálogo cuando
   `(channel, provider)` corresponde a un solo tipo (WhatsApp por Meta → `whatsapp_meta`,
   `waweb` → `whatsapp_waweb`). Va antes del backfill de agentes de HU-AOS-13:
   `agent_instance_checks()` rechaza una instancia en una conexión sin tipo conversacional.
   Las que no se pueden resolver se informan y no se tocan.
2. **Una conexión por número vivo de WhatsApp** (BYOK, Embedded Signup o manual de Meta):
   `asegurar_conexion_del_numero`, con el mismo candado por número que `claim_assets` (un alta
   concurrente del mismo número se serializa). Una conexión manual viva del mismo
   `external_id` es un **choque**: se informa y no se crea una segunda fila.
3. **`conversations.connection_id`** de las conversaciones sin conexión que salen por una
   cuenta de Meta (`meta_connection_id`), cuando esa cuenta tiene exactamente una conexión
   canónica viva. Con varias, la conversación no guarda por qué número escribió el cliente:
   se informa como ambigua y queda sin conexión (sigue pausándose y enviando por la cuenta,
   como antes). Incluye conversaciones cerradas y borradas: el *contract* exige
   `connection_id` en todas.

5. **Centro de pruebas**: las conversaciones de prueba (`channel == 'test'`, de cualquier estado) sin
   conexión pasan a la conexión interna `playground` del negocio, que se crea si hace falta.

4. **Chat web** (segunda parte): la configuración del widget en `app_settings.web_chat_config`
   queda como conexión `webchat` (`chat_web_conexion.sincronizar`: la crea o la pone al día,
   porque hasta el *contract* `app_settings` es la fuente de verdad), y las conversaciones del
   canal `web` sin conexión reciben la del widget cuando el negocio tiene exactamente una viva.
   Las abiertas que quedan sin conexión se suman a `abiertas_sin_ligar`.

Messenger e Instagram quedan fuera hasta que haya un cliente. Cada lote es su propia
transacción. Con `dry_run` no se escribe nada y el reporte dice lo que se haría.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select, text, update

from core.db.models import AppSettings, ChannelConnection, Conversation, PhoneNumber, Tenant
from core.db.session import get_session
from core.gateway import chat_web_conexion
from core.gateway.conexion_canonica import PROVEEDOR, asegurar_conexion_del_numero
from core.gateway.conexion_pruebas import asegurar_conexion_de_pruebas
from core.gateway.connection_types import CONNECTION_TYPES, WEBCHAT
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

LOTE_POR_DEFECTO = 500


@dataclass
class Choque:
    tenant_id: uuid.UUID
    phone_number_id: str


@dataclass
class ReporteBackfill:
    dry_run: bool
    negocios: int = 0
    tipos_puestos: int = 0
    sin_tipo_resoluble: int = 0
    numeros: Counter[str] = field(default_factory=Counter)
    choques: list[Choque] = field(default_factory=list)
    conversaciones_ligadas: int = 0
    conversaciones_ambiguas: int = 0
    conversaciones_sin_conexion: int = 0
    # Abiertas que quedan sin conexión (ambiguas o sin conexión): con `AOS_CONNECTION_ROUTING`
    # encendida, el consumer las adopta cuando el cliente vuelve a escribir a un número de su
    # cuenta. Revisar antes de encender la bandera.
    abiertas_sin_ligar: int = 0
    # Chat web: `creada` | `sincronizada` | `ya_estaba` por negocio con el widget guardado.
    chat_web: Counter[str] = field(default_factory=Counter)
    conversaciones_web_ligadas: int = 0
    conversaciones_web_sin_conexion: int = 0
    # Centro de pruebas: las conversaciones de prueba sin conexión pasan a la conexión interna.
    conversaciones_prueba_ligadas: int = 0


def _tipo_por_canal_y_proveedor() -> dict[tuple[str, str], tuple[str, str]]:
    """`(channel, provider)` → `(type_id, category)`, solo si corresponde a un único tipo."""
    candidatos: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for tipo in CONNECTION_TYPES:
        if tipo.id == WEBCHAT:
            # Una fila vieja `web/web` no es un widget: su `external_id` no es una clave aleatoria
            # y quedaría resoluble en la superficie pública. Las `webchat` las crea el paso 4.
            continue
        candidatos.setdefault((tipo.channel, tipo.provider), []).append((tipo.id, tipo.category))
    return {clave: tipos[0] for clave, tipos in candidatos.items() if len(tipos) == 1}


async def _poner_tipos(tenant_id: uuid.UUID, reporte: ReporteBackfill, lote: int) -> None:
    tipos = _tipo_por_canal_y_proveedor()
    ultimo: uuid.UUID | None = None
    while True:
        async with get_session() as session:
            query = (
                select(ChannelConnection)
                .where(ChannelConnection.tenant_id == tenant_id, ChannelConnection.type_id.is_(None))
                .order_by(ChannelConnection.id)
                .limit(lote)
            )
            if ultimo is not None:
                query = query.where(ChannelConnection.id > ultimo)
            filas = list((await session.execute(query)).scalars().all())
            for fila in filas:
                tipo = tipos.get((fila.channel, fila.provider))
                if tipo is None:
                    reporte.sin_tipo_resoluble += 1
                    log.warning("backfill_conexion_sin_tipo_resoluble", tenant_id=str(tenant_id), connection_id=str(fila.id))
                    continue
                reporte.tipos_puestos += 1
                if not reporte.dry_run:
                    fila.type_id, fila.category = tipo
            if not reporte.dry_run:
                await session.flush()
        if len(filas) < lote:
            return
        ultimo = filas[-1].id


async def _conexiones_por_numero(
    tenant_id: uuid.UUID, reporte: ReporteBackfill, lote: int
) -> dict[str, uuid.UUID]:
    """Paso 2. Devuelve `phone_number_id → cuenta` de los números que tienen (o tendrían, en
    dry-run) su conexión canónica: el paso 3 lo usa para saber qué cuentas tienen una sola."""
    planeadas: dict[str, uuid.UUID] = {}
    ultimo: uuid.UUID | None = None
    while True:
        async with get_session() as session:
            query = (
                select(PhoneNumber.id, PhoneNumber.phone_number_id, PhoneNumber.connection_id)
                .where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.deleted_at.is_(None))
                .order_by(PhoneNumber.id)
                .limit(lote)
            )
            if ultimo is not None:
                query = query.where(PhoneNumber.id > ultimo)
            numeros = list((await session.execute(query)).all())
            for _id, phone_number_id, cuenta in numeros:
                # El mismo candado que `claim_assets`: un alta concurrente del número espera.
                await session.execute(
                    text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"meta_asset:phone:{phone_number_id}"}
                )
                resultado = await asegurar_conexion_del_numero(
                    session, tenant_id, phone_number_id, cuenta, aplicar=not reporte.dry_run
                )
                reporte.numeros[resultado] += 1
                if resultado == "choque_manual":
                    reporte.choques.append(Choque(tenant_id, phone_number_id))
                else:
                    planeadas[phone_number_id] = cuenta
        if len(numeros) < lote:
            return planeadas
        ultimo = numeros[-1][0]


async def _ligar_conversaciones(
    tenant_id: uuid.UUID, planeadas: dict[str, uuid.UUID], reporte: ReporteBackfill, lote: int
) -> None:
    # Conexiones canónicas vivas que ya existen (también las que el número dejó al darse de
    # baja), más las que el paso 2 crearía en dry-run.
    async with get_session() as session:
        existentes = (
            await session.execute(
                select(ChannelConnection.external_id, ChannelConnection.meta_connection_id).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.provider == PROVEEDOR,
                    ChannelConnection.meta_connection_id.is_not(None),
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).all()
        por_cuenta: dict[uuid.UUID, set[str]] = {}
        for external_id, cuenta in [*existentes, *planeadas.items()]:
            por_cuenta.setdefault(cuenta, set()).add(external_id)
        pendientes = (
            await session.execute(
                select(
                    Conversation.meta_connection_id,
                    func.count(),
                    func.count().filter(Conversation.status.in_(("open", "escalated"))),
                )
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.connection_id.is_(None),
                    Conversation.meta_connection_id.is_not(None),
                )
                .group_by(Conversation.meta_connection_id)
            )
        ).all()

    for cuenta, cuantas, abiertas in pendientes:
        numeros = por_cuenta.get(cuenta, set())
        if len(numeros) != 1:
            reporte.abiertas_sin_ligar += abiertas
            if numeros:
                reporte.conversaciones_ambiguas += cuantas
            else:
                reporte.conversaciones_sin_conexion += cuantas
            continue
        if reporte.dry_run:
            reporte.conversaciones_ligadas += cuantas
            continue
        (external_id,) = numeros
        while True:
            async with get_session() as session:
                conexion = (
                    await session.execute(
                        select(ChannelConnection.id).where(
                            ChannelConnection.tenant_id == tenant_id,
                            ChannelConnection.provider == PROVEEDOR,
                            ChannelConnection.external_id == external_id,
                            ChannelConnection.meta_connection_id == cuenta,
                            ChannelConnection.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
                ids = select(Conversation.id).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.connection_id.is_(None),
                    Conversation.meta_connection_id == cuenta,
                ).limit(lote)
                resultado = await session.execute(
                    update(Conversation)
                    .where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.id.in_(ids.scalar_subquery()),
                        # Se repite el filtro: si el consumer la movió mientras tanto, no se pisa.
                        Conversation.connection_id.is_(None),
                        Conversation.meta_connection_id == cuenta,
                    )
                    .values(connection_id=conexion)
                    .execution_options(synchronize_session=False)
                )
                ligadas = int(getattr(resultado, "rowcount", 0) or 0)
            reporte.conversaciones_ligadas += ligadas
            if ligadas < lote:
                break


async def _chat_web(tenant_id: uuid.UUID, reporte: ReporteBackfill, lote: int) -> None:
    """Paso 4: la conexión `webchat` desde `app_settings` y las conversaciones web sin conexión."""
    async with get_session() as session:
        config = (
            await session.execute(
                select(AppSettings.web_chat_config).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        resultado, _ = await chat_web_conexion.sincronizar(
            session, tenant_id, chat_web_conexion.desde_app_settings(config), aplicar=not reporte.dry_run
        )
        if resultado != "sin_widget":
            reporte.chat_web[resultado] += 1
        vivas = list(
            (
                await session.execute(
                    select(ChannelConnection.id).where(
                        ChannelConnection.tenant_id == tenant_id,
                        ChannelConnection.type_id == WEBCHAT,
                        ChannelConnection.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        cuantas, abiertas = (
            await session.execute(
                select(func.count(), func.count().filter(Conversation.status.in_(("open", "escalated")))).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.channel == "web",
                    Conversation.connection_id.is_(None),
                )
            )
        ).one()
    if not cuantas:
        return
    # En dry-run la conexión que se crearía cuenta como la única.
    destinos = len(vivas) + (1 if reporte.dry_run and resultado == "creada" else 0)
    if destinos != 1:
        reporte.conversaciones_web_sin_conexion += cuantas
        reporte.abiertas_sin_ligar += abiertas
        return
    if reporte.dry_run:
        reporte.conversaciones_web_ligadas += cuantas
        return
    (conexion,) = vivas
    while True:
        async with get_session() as session:
            ids = (
                select(Conversation.id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.channel == "web",
                    Conversation.connection_id.is_(None),
                )
                .limit(lote)
            )
            resultado_update = await session.execute(
                update(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id.in_(ids.scalar_subquery()),
                    # Se repite el filtro: si el consumer la ligó mientras tanto, no se pisa.
                    Conversation.channel == "web",
                    Conversation.connection_id.is_(None),
                )
                .values(connection_id=conexion)
                .execution_options(synchronize_session=False)
            )
            ligadas = int(getattr(resultado_update, "rowcount", 0) or 0)
        reporte.conversaciones_web_ligadas += ligadas
        if ligadas < lote:
            return


async def _pruebas(tenant_id: uuid.UUID, reporte: ReporteBackfill, lote: int) -> None:
    """Paso 5: las conversaciones del Centro de pruebas (`channel == 'test'`) sin conexión pasan a la
    conexión interna del negocio, que se crea si hace falta (adenda 2026-10-24 de ADR-0115): el
    *contract* exige `connection_id` en todas, también en las de prueba y en las cerradas."""
    async with get_session() as session:
        cuantas = int(
            (
                await session.execute(
                    select(func.count()).where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.channel == TEST_CHANNEL,
                        Conversation.connection_id.is_(None),
                    )
                )
            ).scalar_one()
        )
        if not cuantas:
            return
        if reporte.dry_run:
            reporte.conversaciones_prueba_ligadas += cuantas
            return
        conexion = await asegurar_conexion_de_pruebas(session, tenant_id)
    while True:
        async with get_session() as session:
            ids = (
                select(Conversation.id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.channel == TEST_CHANNEL,
                    Conversation.connection_id.is_(None),
                )
                .limit(lote)
            )
            resultado = await session.execute(
                update(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id.in_(ids.scalar_subquery()),
                    Conversation.channel == TEST_CHANNEL,
                    Conversation.connection_id.is_(None),
                )
                .values(connection_id=conexion)
                .execution_options(synchronize_session=False)
            )
            ligadas = int(getattr(resultado, "rowcount", 0) or 0)
        reporte.conversaciones_prueba_ligadas += ligadas
        if ligadas < lote:
            return


async def backfill_conexiones(
    *, dry_run: bool, tenant_id: uuid.UUID | None = None, lote: int = LOTE_POR_DEFECTO
) -> ReporteBackfill:
    """Corre el backfill en todos los negocios (o en uno). Ver el docstring del módulo."""
    if lote < 1:
        raise ValueError("El lote tiene que ser al menos 1")
    reporte = ReporteBackfill(dry_run=dry_run)
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
        async with tenant_scope(negocio):
            await _poner_tipos(negocio, reporte, lote)
            planeadas = await _conexiones_por_numero(negocio, reporte, lote)
            await _ligar_conversaciones(negocio, planeadas, reporte, lote)
            await _chat_web(negocio, reporte, lote)
            await _pruebas(negocio, reporte, lote)
        reporte.negocios += 1
    log.info(
        "backfill_conexiones",
        dry_run=dry_run,
        negocios=reporte.negocios,
        tipos_puestos=reporte.tipos_puestos,
        numeros=dict(reporte.numeros),
        choques=len(reporte.choques),
        conversaciones_ligadas=reporte.conversaciones_ligadas,
        conversaciones_ambiguas=reporte.conversaciones_ambiguas,
        abiertas_sin_ligar=reporte.abiertas_sin_ligar,
        chat_web=dict(reporte.chat_web),
        conversaciones_web_ligadas=reporte.conversaciones_web_ligadas,
        conversaciones_web_sin_conexion=reporte.conversaciones_web_sin_conexion,
        conversaciones_prueba_ligadas=reporte.conversaciones_prueba_ligadas,
    )
    return reporte

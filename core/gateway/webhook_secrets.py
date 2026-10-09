"""Con qué secretos se verifica un grupo del webhook de Meta (HU-CONN-05, ADR-0126).

Un negocio puede tener varias conexiones a la vez: una BYOK con su propia app y otra de
Embedded Signup con la app de la plataforma, o dos apps propias. Cada app firma sus
entregas con SU App Secret, así que la firma de un grupo `(entry, número)` se verifica con
el secreto de la conexión **dueña de los activos del grupo** (el número, la WABA, la página
o la cuenta de Instagram del payload), no con "el" secreto del negocio.

Quién elige el secreto: la base de datos, nunca el que manda el POST. El cuerpo solo dice
qué activos trae; el dueño de cada activo sale de `route_event` (resolvedores `SECURITY
DEFINER`, ADR-0095) y la conexión dueña, de las filas vivas de ese negocio. Un atacante
puede poner en el payload el número de otro negocio, pero entonces el secreto que se prueba
es el de ese otro negocio, que no conoce: el grupo no se autoriza.

Cuando el grupo trae número, manda el número (es lo que se procesa como entrante):
- número de una conexión de Meta: su conexión; la de la WABA del payload solo cuenta si esa
  WABA es la del número. Así, la WABA de la conexión A con el número de la conexión B
  (un par que no existe en Meta) no hereda el secreto de A;
- número de una conexión manual: solo su secreto;
- número que no es de nadie del negocio: decide la WABA (un número nuevo de una WABA ya
  conectada, todavía sin sincronizar).
Sin número (plantillas, cuenta): la conexión de la WABA.

Lo que NO se hace (fail-closed):
- Un grupo sin dueño resoluble no tiene secreto propio: solo lo autorizaría la plataforma.
- Una conexión eliminada o sin alta (`draft`) no presta su secreto.
- El secreto de una conexión no autoriza los grupos de otra conexión, aunque sea del mismo
  negocio.

Todas las lecturas van dentro de `tenant_scope` del negocio del grupo (las filas y el
token store viven bajo RLS) y con `tenant_id` explícito (defensa en profundidad).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.config import get_settings
from core.db.models import (
    ChannelConnection,
    FbPage,
    IgAccount,
    MetaConnection,
    PhoneNumber,
    WabaAccount,
)
from core.db.session import get_session
from core.gateway.meta_byok import ESTADOS_CON_CREDENCIAL
from core.shared.tenant import tenant_scope

# Secretos ya leídos en este POST, por conexión: un POST con varios grupos de la misma
# conexión no vuelve a descifrar su secreto.
CacheDeSecretos = dict[uuid.UUID, str | None]


async def _conexiones_por_id(tenant_id: uuid.UUID, ids: set[uuid.UUID]) -> list[MetaConnection]:
    """Las conexiones vivas que prestan su secreto (`ESTADOS_CON_CREDENCIAL`) entre `ids`."""
    if not ids:
        return []
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(MetaConnection)
                    .where(
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.id.in_(ids),
                        MetaConnection.deleted_at.is_(None),
                        MetaConnection.status.in_(ESTADOS_CON_CREDENCIAL),
                    )
                    .order_by(MetaConnection.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _conexion_de_la_waba(tenant_id: uuid.UUID, waba_id: str) -> set[uuid.UUID]:
    async with get_session() as session:
        return set(
            (
                await session.execute(
                    select(WabaAccount.connection_id).where(
                        WabaAccount.tenant_id == tenant_id,
                        WabaAccount.waba_id == waba_id,
                        WabaAccount.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )


async def _manual_del_numero(tenant_id: uuid.UUID, phone_number_id: str) -> ChannelConnection | None:
    """La conexión manual (`ChannelConnection`) dueña del número, con el mismo criterio de
    dueño que `resolve_tenant_for_phone` (sin borrar, de WhatsApp por Meta)."""
    async with get_session() as session:
        return (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.external_id == phone_number_id,
                    ChannelConnection.channel == "whatsapp",
                    ChannelConnection.provider == "meta",
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def _duenas_whatsapp(
    tenant_id: uuid.UUID, entry_id: str, sub_id: str | None
) -> tuple[set[uuid.UUID], ChannelConnection | None]:
    """(ids de `MetaConnection` dueñas, conexión manual dueña) de un grupo de WhatsApp. Ver la
    regla en el docstring del módulo."""
    if not sub_id:
        return await _conexion_de_la_waba(tenant_id, entry_id), None

    async with get_session() as session:
        numero = (
            await session.execute(
                select(PhoneNumber.connection_id, WabaAccount.waba_id, WabaAccount.connection_id)
                .outerjoin(
                    WabaAccount,
                    (WabaAccount.id == PhoneNumber.waba_account_id)
                    & (WabaAccount.tenant_id == tenant_id)
                    & WabaAccount.deleted_at.is_(None),
                )
                .where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.phone_number_id == sub_id,
                    PhoneNumber.deleted_at.is_(None),
                )
            )
        ).first()
    if numero is not None:
        conexion_del_numero, waba_del_numero, conexion_de_su_waba = numero
        duenas = {conexion_del_numero}
        if waba_del_numero == entry_id and conexion_de_su_waba is not None:
            duenas.add(conexion_de_su_waba)
        return duenas, None

    manual = await _manual_del_numero(tenant_id, sub_id)
    if manual is not None:
        return set(), manual
    return await _conexion_de_la_waba(tenant_id, entry_id), None


async def _duenas(
    tenant_id: uuid.UUID, object_type: str, entry_id: str, sub_id: str | None
) -> tuple[set[uuid.UUID], ChannelConnection | None]:
    if object_type == "whatsapp_business_account":
        return await _duenas_whatsapp(tenant_id, entry_id, sub_id)
    if object_type in ("page", "instagram"):
        modelo, columna = (FbPage, FbPage.page_id) if object_type == "page" else (IgAccount, IgAccount.ig_user_id)
        async with get_session() as session:
            ids = (
                await session.execute(
                    select(modelo.connection_id).where(modelo.tenant_id == tenant_id, columna == entry_id)
                )
            ).scalars().all()
        return set(ids), None
    return set(), None


async def conexiones_del_grupo(
    tenant_id: uuid.UUID, *, object_type: str, entry_id: str, sub_id: str | None
) -> list[MetaConnection]:
    """Conexiones de Meta del negocio dueñas de los activos del grupo que todavía prestan su
    secreto (`ESTADOS_CON_CREDENCIAL`, sin `deleted_at`). Necesita el `tenant_scope`."""
    ids, _manual = await _duenas(tenant_id, object_type, entry_id, sub_id)
    return await _conexiones_por_id(tenant_id, ids)


async def secretos_del_grupo(
    tenant_id: uuid.UUID,
    *,
    object_type: str,
    entry_id: str,
    sub_id: str | None,
    cache: CacheDeSecretos | None = None,
) -> tuple[str, ...]:
    """Secretos propios con los que puede venir firmado un grupo del negocio `tenant_id`:
    el App Secret guardado de cada conexión dueña de sus activos (sea cual sea su origen:
    una BYOK tomada por Embedded Signup conserva el de su app, ADR-0101) o el de la conexión
    manual dueña del número. Sin el de la plataforma, que el llamador prueba aparte.

    Vacío si ninguna conexión del negocio es dueña de los activos del grupo. Las
    excepciones (BD, descifrado) suben: el llamador decide (nunca autorizar)."""
    from core.gateway.connections import decrypt_credentials
    from core.gateway.meta_credentials import stored_app_secret

    cache = {} if cache is None else cache
    secretos: list[str] = []
    async with tenant_scope(tenant_id):
        ids, manual = await _duenas(tenant_id, object_type, entry_id, sub_id)
        for connection in await _conexiones_por_id(tenant_id, ids):
            if connection.id not in cache:
                cache[connection.id] = await stored_app_secret(connection)
            secreto = cache[connection.id]
            if secreto:
                secretos.append(secreto)
        if manual is not None:
            secreto_manual = decrypt_credentials(manual).get("app_secret")
            if secreto_manual:
                secretos.append(secreto_manual)
    return tuple(dict.fromkeys(secretos))


async def verify_tokens_aceptados(tenant_id: uuid.UUID) -> tuple[str, ...]:
    """`verify_token` que acepta el handshake `GET /webhooks/whatsapp/<slug>`: el que registró
    cada conexión que apunta a esa URL. Necesita el `tenant_scope` del negocio.

    - Conexión manual primaria: su `verify_token`.
    - Cada conexión de Meta viva (`ESTADOS_CON_CREDENCIAL`) con `verify_token` propio: ese.
    - Cada conexión BYOK viva sin `verify_token` propio: el global (`WHATSAPP_VERIFY_TOKEN`),
      que es el que registra el wizard y "Registrar webhook" (`verify_token_de_registro`).
    - Sin ninguna de las anteriores: el global, como antes.

    Antes se aceptaba un solo token por negocio: con dos apps BYOK, la que no ganaba el
    orden fallaba el handshake. Una conexión de Embedded Signup sin token propio no aporta
    nada: su app (la de la plataforma) apunta al webhook genérico."""
    from core.gateway.connections import resolve_primary_credentials
    from core.gateway.meta_credentials import stored_verify_token

    global_token = get_settings().whatsapp_verify_token or ""
    tokens: list[str] = []
    manual = (await resolve_primary_credentials(tenant_id) or {}).get("verify_token")
    if manual:
        tokens.append(manual)
    async with get_session() as session:
        connections = (
            (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tenant_id,
                        MetaConnection.channel == "whatsapp",
                        MetaConnection.deleted_at.is_(None),
                        MetaConnection.status.in_(ESTADOS_CON_CREDENCIAL),
                    )
                )
            )
            .scalars()
            .all()
        )
    for connection in connections:
        propio = await stored_verify_token(connection)
        if propio:
            tokens.append(propio)
        elif connection.origin == "byok" and global_token:
            tokens.append(global_token)
    if not tokens and global_token:
        tokens.append(global_token)
    return tuple(dict.fromkeys(tokens))

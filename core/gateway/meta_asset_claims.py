"""Un número o una WABA de Meta pertenecen a un solo negocio (ADR-0095).

El webhook rutea cada evento por número o por WABA (`webhook_route_tenant.py`): si un
negocio pudiera guardar la WABA o el número de otro, recibiría sus mensajes. Por eso,
antes de guardar una WABA o un número, cada alta (manual, BYOK, Embedded Signup):

1. verifica contra Graph, con las credenciales de la conexión, que el token ve la WABA
   y que el número es de esa WABA (`verify_waba_and_phone`): un ID tipeado o sacado del
   popup no prueba nada;
2. dentro de la misma transacción que inserta las filas, toma un candado por activo y
   pregunta si otro negocio ya lo tiene (`claim_assets`). La función SQL devuelve un
   booleano y nunca dice cuál (mismo criterio que ADR-0039).

El orden importa: la verificación va primero, así la pregunta "¿lo tiene otro?" solo
la responde quien ya demostró ante Meta que tiene acceso al activo.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.gateway.meta_errors import GraphError
from core.gateway.meta_graph import graph_fetch
from core.gateway.meta_phone_numbers import list_phone_numbers
from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

CLAIMED_MESSAGE = (
    "Esta cuenta de WhatsApp Business o este número ya están conectados a otro negocio "
    "de la plataforma. Si son tuyos, pide soporte."
)


class MetaAssetError(AgentCoreError):
    """La WABA o el número no se pueden guardar en este negocio.

    `code`: `waba_not_accessible` (el token no ve la WABA), `phone_not_in_waba` (el número
    no es de esa WABA), `asset_claimed` (otro negocio ya lo tiene) o `connection_deleted` (la
    conexión se eliminó mientras se completaba el alta)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


async def verify_waba_and_phone(
    *,
    waba_id: str,
    phone_number_id: str | None,
    token: str,
    app_secret: str,
    version: str,
    connection_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Comprueba en Graph que el token ve la WABA y, si hay número, que es de esa WABA.

    Con número se listan los números de la WABA (paginado: una WABA puede tener muchos);
    sin número (`FINISH_ONLY_WABA`) basta con leer la WABA. Devuelve los datos del número
    según Meta (vacío si no hay número). `connection_id=None` si la conexión aún no existe."""
    try:
        if not phone_number_id:
            await graph_fetch(
                f"/{waba_id}", token=token, app_secret=app_secret,
                query={"fields": "id"}, version=version, connection_id=connection_id,
            )
            return {}
        numeros = await list_phone_numbers(waba_id, token, app_secret, version, connection_id)
    except GraphError as exc:
        raise MetaAssetError(
            f"Meta no deja ver la cuenta de WhatsApp Business {waba_id} con estas credenciales: "
            f"{exc.message}",
            code="waba_not_accessible",
        ) from exc
    for numero in numeros:
        if str(numero.get("id") or "") == phone_number_id:
            return numero
    raise MetaAssetError(
        f"El número {phone_number_id} no pertenece a la cuenta de WhatsApp Business {waba_id}.",
        code="phone_not_in_waba",
    )


async def claim_assets(
    session: Any,
    tenant_id: uuid.UUID | None,
    *,
    waba_id: str | None,
    phone_number_id: str | None,
) -> None:
    """Falla si otro negocio ya tiene la WABA o el número. Va en la MISMA sesión que el insert.

    El candado (`pg_advisory_xact_lock`, uno por activo, siempre en el mismo orden) dura
    hasta el commit: dos altas simultáneas del mismo activo se serializan y la segunda ve
    la fila de la primera."""
    if await _lock_and_check(session, tenant_id, waba_id=waba_id, phone_number_id=phone_number_id):
        raise MetaAssetError(CLAIMED_MESSAGE, code="asset_claimed")


async def claimed_elsewhere(
    session: Any,
    tenant_id: uuid.UUID,
    *,
    phone_number_id: str,
    source: str,
) -> bool:
    """Como `claim_assets`, pero devuelve el resultado en vez de lanzar: para números que
    Graph lista dentro de una WABA que este negocio ya tiene (sincronizar o agregar
    números). Si otro negocio tiene el mismo número, el llamador no lo guarda y se avisa:
    guardarlo dejaría el número con dos dueños, y un número así no rutea a nadie."""
    claimed = await _lock_and_check(session, tenant_id, waba_id=None, phone_number_id=phone_number_id)
    if claimed:
        log.warning(
            "meta_numero_de_otro_negocio_no_se_guarda",
            source=source, tenant_id=str(tenant_id), phone_number_id=phone_number_id,
        )
    return claimed


async def asset_claimed_by_other_tenant(
    tenant_id: uuid.UUID | None, *, waba_id: str | None = None, phone_number_id: str | None = None
) -> bool:
    """¿Otro negocio ya tiene la WABA o el número? Sin candado (para validar antes de un alta
    que no guarda en `phone_numbers`/`waba_accounts`, como `onboard_tenant`).

    `tenant_id=None` (tenant aún sin crear): cualquier dueño cuenta como otro."""
    async with get_session() as session:
        return await _claimed_by_other_tenant(
            session, tenant_id,
            waba_id=(waba_id or "").strip() or None,
            phone_number_id=(phone_number_id or "").strip() or None,
        )


async def _lock_and_check(
    session: Any, tenant_id: uuid.UUID | None, *, waba_id: str | None, phone_number_id: str | None
) -> bool:
    waba_id = (waba_id or "").strip() or None
    phone_number_id = (phone_number_id or "").strip() or None
    activos = [f"waba:{waba_id}"] if waba_id else []
    if phone_number_id:
        activos.append(f"phone:{phone_number_id}")
    for activo in activos:
        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"meta_asset:{activo}"}
        )
    return await _claimed_by_other_tenant(session, tenant_id, waba_id=waba_id, phone_number_id=phone_number_id)


async def _claimed_by_other_tenant(
    session: Any, tenant_id: uuid.UUID | None, *, waba_id: str | None, phone_number_id: str | None
) -> bool:
    return bool(
        (
            await session.execute(
                text("select meta_asset_claimed_by_other_tenant(:w, :p, :t)"),
                {"w": waba_id, "p": phone_number_id, "t": tenant_id},
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------- filas de activos
# Un número vive en una sola fila por negocio (`uq_phone_numbers_tenant_phone`) y una
# conexión eliminada deja sus números y WABAs con `deleted_at` (ADR-0122). Reconectar el
# mismo activo en el mismo negocio revive esas filas: así plantillas y precios siguen en
# su WABA. Todas las altas pasan por acá, dentro de la transacción de `claim_assets`.


async def waba_para_conexion(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, waba_id: str
) -> Any:
    """La fila de `waba_id` para `connection_id`: la propia si existe; si no, revive la
    eliminada más reciente del negocio (con sus plantillas y precios); si no, una nueva.

    Una WABA revivida queda `subscribed=False` hasta que el alta la suscriba (ADR-0102)."""
    from sqlalchemy import select

    from core.db.models import WabaAccount

    propia = (
        await session.execute(
            select(WabaAccount).where(
                WabaAccount.tenant_id == tenant_id,
                WabaAccount.connection_id == connection_id,
                WabaAccount.waba_id == waba_id,
            )
        )
    ).scalar_one_or_none()
    if propia is not None:
        propia.deleted_at = None
        return propia
    eliminada = (
        await session.execute(
            select(WabaAccount)
            .where(
                WabaAccount.tenant_id == tenant_id,
                WabaAccount.waba_id == waba_id,
                WabaAccount.deleted_at.is_not(None),
            )
            .order_by(WabaAccount.deleted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if eliminada is not None:
        log.info("waba_revivida", waba_id=waba_id, connection_id=str(connection_id))
        eliminada.connection_id = connection_id
        eliminada.deleted_at = None
        eliminada.subscribed = False
        await session.flush()
        return eliminada
    nueva = WabaAccount(tenant_id=tenant_id, connection_id=connection_id, waba_id=waba_id)
    session.add(nueva)
    await session.flush()
    return nueva


async def numero_para_conexion(
    session: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    phone_number_id: str,
    *,
    waba_account_id: uuid.UUID | None = None,
    instancias_de: uuid.UUID | None = None,
) -> tuple[Any, uuid.UUID | None]:
    """La fila única de `phone_number_id` en el negocio, asignada (y revivida) a
    `connection_id`. Devuelve `(fila, conexión anterior o None)`. Lanza `MetaAssetError` si
    la fila estaba eliminada y otro negocio tomó el número mientras tanto.

    Si la conexión anterior se queda sin números vivos, sus conversaciones pasan a esta: si
    no, los avisos y seguimientos de esos clientes fallarían cerrados (ADR-0122).

    En la misma transacción crea, revive o religa la conexión canónica del número
    (`channel_connections`, ADR-0115 §6): así toda alta o reconexión, por cualquiera de los
    cinco caminos, deja el número con su conexión ligada a esta cuenta. Y le pone sus instancias
    de agentes si el negocio ya los tiene (`instancias.instancias_al_alta`, HU-AOS-13);
    `instancias_de` es la conexión de la que se clonan (la del número de prueba, en la promoción
    de ADR-0127)."""
    from sqlalchemy import func, select

    from core.db.models import PhoneNumber

    fila = (
        await session.execute(
            select(PhoneNumber).where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.phone_number_id == phone_number_id)
        )
    ).scalar_one_or_none()
    anterior: uuid.UUID | None = None
    if fila is None:
        fila = PhoneNumber(
            tenant_id=tenant_id, connection_id=connection_id, phone_number_id=phone_number_id,
            waba_account_id=waba_account_id,
        )
        session.add(fila)
    else:
        if fila.connection_id != connection_id:
            anterior = fila.connection_id
        if fila.deleted_at is not None:
            # Mientras estuvo eliminado, otro negocio pudo tomarlo (también por una conexión
            # manual, que el índice parcial no ve): revivirlo lo dejaría con dos dueños.
            if await claimed_elsewhere(session, tenant_id, phone_number_id=phone_number_id, source="revivir_numero"):
                raise MetaAssetError(CLAIMED_MESSAGE, code="asset_claimed")
            log.info("numero_revivido", phone_number_id=phone_number_id, connection_id=str(connection_id))
        fila.connection_id = connection_id
        fila.deleted_at = None
        if waba_account_id is not None:
            fila.waba_account_id = waba_account_id
    await session.flush()

    if anterior is not None:
        quedan = (
            await session.execute(
                select(func.count()).select_from(PhoneNumber).where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.connection_id == anterior,
                    PhoneNumber.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        if not quedan:
            await mover_conversaciones(session, tenant_id, desde=anterior, hacia=connection_id)

    from core.gateway.conexion_canonica import asegurar_conexion_del_numero

    await asegurar_conexion_del_numero(session, tenant_id, phone_number_id, connection_id)

    from core.agents.instancias import instancias_al_alta

    await instancias_al_alta(session, tenant_id, phone_number_id, connection_id, instancias_de=instancias_de)
    return fila, anterior


async def mover_conversaciones(session: Any, tenant_id: uuid.UUID, *, desde: uuid.UUID, hacia: uuid.UUID) -> None:
    """Las conversaciones de la conexión `desde` pasan a `hacia` (mismo número, otra conexión)."""
    from sqlalchemy import update

    from core.db.models import Conversation

    await session.execute(
        update(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.meta_connection_id == desde)
        .values(meta_connection_id=hacia)
    )

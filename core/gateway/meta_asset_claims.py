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
    no es de esa WABA) o `asset_claimed` (otro negocio ya lo tiene)."""

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


async def warn_if_claimed(
    session: Any,
    tenant_id: uuid.UUID,
    *,
    waba_id: str | None,
    phone_number_id: str | None,
    source: str,
) -> bool:
    """Como `claim_assets`, pero sin bloquear: para números que Graph lista dentro de una
    WABA que este negocio ya tiene (sincronizar o agregar números). Esos están
    verificados por construcción; si otro negocio tiene el mismo número es una fila
    heredada sin verificar. Se avisa y se guarda igual: el número queda ambiguo (deja de
    rutear por número) y decide la WABA, que es de este negocio."""
    claimed = await _lock_and_check(session, tenant_id, waba_id=waba_id, phone_number_id=phone_number_id)
    if claimed:
        log.warning(
            "meta_asset_tambien_de_otro_negocio",
            source=source, tenant_id=str(tenant_id), waba_id=waba_id, phone_number_id=phone_number_id,
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

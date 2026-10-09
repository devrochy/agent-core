"""Números telefónicos de una conexión Meta (fuente única SSR + API).

Envuelve el gateway de Meta (`sync_phone_numbers`, `add_phone_number`,
`request_verification_code`, `verify_code`) con la resolución de WABA/token y el
rate limit de reenvío de código. Cada función devuelve un error legible o `None`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.db.models import MetaConnection, PhoneNumber, WabaAccount
from core.db.session import get_session
from core.gateway.meta_connection_state import get_live_meta_connection


async def resolve_waba_and_credentials(connection_id: uuid.UUID, tenant_id: uuid.UUID | None):
    """(connection, waba, credenciales) del tenant; cualquiera puede ser None.

    Las credenciales son `MetaCredentials` (HU-CONN-01): token y App Secret de ESA
    conexión según su `origin`. Son `None` si la conexión no tiene credenciales
    utilizables; el llamador responde con su mensaje de "no hay conexión lista".
    """
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for

    async with get_session() as session:
        # Solo conexiones vivas y sus WABAs vivas (ADR-0122).
        connection = await get_live_meta_connection(session, connection_id, tenant_id)
        if connection is None:
            return None, None, None
        waba = (
            await session.execute(
                select(WabaAccount)
                .where(
                    WabaAccount.connection_id == connection_id,
                    WabaAccount.tenant_id == connection.tenant_id,
                    WabaAccount.deleted_at.is_(None),
                )
                .order_by(WabaAccount.created_at)
            )
        ).scalars().first()
    try:
        creds = await credentials_for(connection, waba_id=waba.waba_id if waba is not None else None)
    except CredentialsUnavailable:
        creds = None
    return connection, waba, creds


async def list_numbers(
    tenant_id: uuid.UUID, connection_id: str
) -> tuple[list[dict], str | None]:
    """Sincroniza con Meta y devuelve los números del tenant. (números, error)."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import sync_phone_numbers

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return [], "Conexión inválida"

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None:
        return [], "Conexión no encontrada"

    sync_error = None
    if waba is not None and creds is not None:
        try:
            await sync_phone_numbers(
                waba.waba_id,
                creds.access_token,
                creds.app_secret,
                creds.graph_api_version,
                connection.id,
                tenant_id,
            )
        except GraphError as exc:
            sync_error = _meta_error_text(exc)

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.connection_id == connection.id,
                        PhoneNumber.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    numbers = [
        {
            "id": n.id,
            "phone_number_id": n.phone_number_id,
            "display_phone_number": n.display_phone_number,
            "verified_name": n.verified_name,
            # `PhoneNumber` no tiene columna `status` (N-18): se deriva de los
            # campos reales que sí existen, para el API JSON del panel.
            "status": _number_status(n),
            "registered": bool(n.registered),
        }
        for n in rows
    ]
    return numbers, sync_error


def _number_status(n) -> str:  # type: ignore[no-untyped-def]
    """Estado visible del número a partir de las columnas reales.

    Prioridad: verificado > conectado > estado del nombre > 'PENDING'.
    """
    if (n.code_verification_status or "").upper() == "VERIFIED":
        return "VERIFIED"
    if n.registered:
        return "CONNECTED"
    return n.name_status or "PENDING"


_ERROR_TEXT_MAX = 300


def _meta_error_text(exc) -> str:  # type: ignore[no-untyped-def]
    """Texto del error para el panel: con un `GraphError`, el motivo que da Meta
    (`user_text`), no solo "Invalid parameter"; con otros errores, su mensaje."""
    from core.gateway.meta_errors import GraphError

    text = exc.user_text if isinstance(exc, GraphError) else exc.message
    return (text or "")[:_ERROR_TEXT_MAX]


_NOT_THIS_CONNECTION = "El número no es de esta conexión"


async def _phone_of_connection(connection: MetaConnection, phone_number_id: str) -> PhoneNumber | None:
    """El número si es de ESTA conexión y de su negocio; si no, `None` y no se llama a Meta.

    El token de una conexión puede ver números de otros negocios (un BYOK de agencia con
    WABAs de varios clientes): sin esto, un negocio podría operar un número ajeno.
    """
    async with get_session() as session:
        return (
            await session.execute(
                select(PhoneNumber).where(
                    PhoneNumber.phone_number_id == phone_number_id,
                    PhoneNumber.connection_id == connection.id,
                    PhoneNumber.tenant_id == connection.tenant_id,
                    # Uno dado de baja (p. ej. el de prueba tras "Pasar a producción") ya no es de
                    # la conexión: pudo tomarlo otro negocio (ADR-0127).
                    PhoneNumber.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def add_number(
    tenant_id: uuid.UUID,
    connection_id: str,
    *,
    cc: str,
    phone_number: str,
    verified_name: str,
) -> str | None:
    from core.gateway.meta_asset_claims import MetaAssetError
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import add_phone_number

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or waba is None or creds is None:
        return "No hay conexión/WABA lista todavía"

    try:
        await add_phone_number(
            waba.waba_id,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            tenant_id,
            cc=cc,
            phone_number=phone_number,
            verified_name=verified_name,
        )
    except (GraphError, MetaAssetError) as exc:
        return _meta_error_text(exc)
    return None


async def request_code(
    tenant_id: uuid.UUID,
    connection_id: str,
    phone_number_id: str,
    *,
    code_method: str,
    language: str = "es",
) -> str | None:
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import request_verification_code
    from core.memory.working import get_redis

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, _waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or creds is None:
        return "No hay conexión lista todavía"
    if await _phone_of_connection(connection, phone_number_id) is None:
        return _NOT_THIS_CONNECTION

    redis = get_redis()
    # Namespaced por tenant: phone_number_id es único global en Meta, pero un
    # tenant no debe interferir el rate limit de otro (auditoría 2026-09-22).
    rate_limit_key = f"verify_code_resend:{tenant_id}:{phone_number_id}"
    if await redis.get(rate_limit_key):
        return "Esperá un momento antes de reenviar el código"

    try:
        await request_verification_code(
            phone_number_id,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            code_method=code_method,
            language=language,
        )
    except GraphError as exc:
        return _meta_error_text(exc)

    await redis.set(rate_limit_key, "1", ex=60)
    return None


async def verify_number(
    tenant_id: uuid.UUID, connection_id: str, phone_number_id: str, *, code: str
) -> tuple[bool, str | None]:
    """(verificado, error)."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import verify_code

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return False, "Conexión inválida"

    connection, _waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or creds is None:
        return False, "No hay conexión lista todavía"
    if await _phone_of_connection(connection, phone_number_id) is None:
        return False, _NOT_THIS_CONNECTION

    try:
        ok = await verify_code(
            phone_number_id,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            tenant_id,
            code=code,
        )
    except GraphError as exc:
        return False, _meta_error_text(exc)
    return bool(ok), None


_PIN_LEN = 6
_REGISTER_RATE_LIMIT_S = 60


def _register_rate_limit_key(tenant_id: uuid.UUID, phone_number_id: str) -> str:
    return f"register_number:{tenant_id}:{phone_number_id}"


async def release_register_rate_limit(tenant_id: uuid.UUID, phone_number_id: str) -> None:
    """Libera el límite de un intento por minuto: después de fijar un PIN nuevo en Meta, el
    registro con ese PIN no es adivinar (el límite existe para no llegar al bloqueo 133008)."""
    from core.memory.working import get_redis

    await get_redis().delete(_register_rate_limit_key(tenant_id, phone_number_id))


async def register_number(
    tenant_id: uuid.UUID, connection_id: str, phone_number_id: str, *, pin: str | None = None
) -> str | None:
    """Registra el número en la Cloud API con un PIN de verificación en dos pasos.

    Para cualquier origen (BYOK, manual, Embedded Signup): las credenciales salen de la
    conexión (`credentials_for`). Antes solo lo hacía el wizard de Embedded Signup.

    - El número tiene que ser de ESTA conexión y de este negocio: si no, no se llama a
      Meta (nadie registra un número ajeno con el token de su conexión).
    - En coexistencia (número compartido con la app de WhatsApp Business) no se registra,
      igual que en el wizard.
    - `pin`: el de verificación en dos pasos que ya tenga el número. Sin él se usa el
      guardado (p. ej. el que fijó el wizard, que nadie vio) y, si no hay, uno nuevo. Se
      guarda cifrado ANTES de llamar a Meta, como en Embedded Signup, y nunca se loguea ni se
      devuelve. Si Meta lo rechaza vuelve el que había: un PIN errado no borra el bueno.
    - Un intento por minuto y número: cada PIN errado suma hacia el bloqueo 133008 de Meta.
    - Si la conexión solo esperaba este registro (`pending` con `MOTIVO_NUMERO_SIN_REGISTRAR`,
      un alta BYOK con número sin registrar), queda `connected` (HU-CONN-06, ADR-0127).
    """
    from core.gateway import meta_token_store
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import (
        PIN_KIND,
        PIN_MISMATCH_CODE,
        TOO_MANY_GUESSES_CODE,
        generate_pin,
        register_phone_number,
    )
    from core.memory.working import get_redis

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"
    pin = (pin or "").strip() or None  # un campo vacío del panel es "generalo"
    # `isascii`: `isdigit` acepta dígitos Unicode (p. ej. de ancho completo) que Meta no.
    if pin is not None and not (len(pin) == _PIN_LEN and pin.isascii() and pin.isdigit()):
        return "El PIN tiene que ser de 6 dígitos"

    connection, _waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None:
        return "Conexión no encontrada"
    if creds is None:
        return "No hay conexión lista todavía"
    if (connection.health or {}).get("coexistence"):
        return "Esta conexión comparte el número con la app de WhatsApp Business: no hace falta registrarlo"

    phone = await _phone_of_connection(connection, phone_number_id)
    if phone is None:
        return _NOT_THIS_CONNECTION

    previous_pin = await meta_token_store.get_token(
        connection.id, PIN_KIND, phone_number_id, tenant_id=connection.tenant_id
    )
    if pin is None and previous_pin is None and _pin_obligatorio(connection):
        # El número real del negocio: un PIN generado que nadie ve le dejaría una verificación
        # en dos pasos que el negocio no conoce (ADR-0127; también Embedded Signup, ADR-0128).
        return "Escribe el PIN de 6 dígitos del número: será su verificación en dos pasos"

    redis = get_redis()
    rate_limit_key = _register_rate_limit_key(connection.tenant_id, phone_number_id)
    if not await redis.set(rate_limit_key, "1", nx=True, ex=_REGISTER_RATE_LIMIT_S):
        return "Esperá un minuto antes de reintentar el registro"
    pin = pin or previous_pin or generate_pin()
    if pin != previous_pin:
        # Antes de llamar a Meta: si el registro sale bien y algo falla después, el PIN que
        # quedó en el número no se pierde (mismo criterio que `register_number_step`).
        await meta_token_store.put_token(connection.id, PIN_KIND, phone_number_id, pin, tenant_id=connection.tenant_id)

    try:
        await register_phone_number(
            phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id, pin=pin
        )
    except GraphError as exc:
        # Meta rechazó el pedido (4xx): el PIN nuevo no quedó en el número, vuelve el que había.
        # Sin respuesta (timeout, red) o con un 5xx del borde no se sabe si el registro se
        # aplicó: se queda el nuevo, por si sí.
        if exc.http_status is not None and 400 <= exc.http_status < 500 and pin != previous_pin:
            await _restore_pin(connection, phone_number_id, previous_pin)
        if exc.code == PIN_MISMATCH_CODE:
            return (
                "El número ya tiene verificación en dos pasos con otro PIN: escribí ese PIN, o "
                "cambialo en WhatsApp Manager"
            )
        if exc.code == TOO_MANY_GUESSES_CODE:
            return "Meta bloqueó por un rato los intentos de PIN de este número: esperá antes de reintentar"
        message = exc.message or ""
        if not ("already" in message.lower() and "regist" in message.lower()):
            return _meta_error_text(exc)
        # Ya estaba registrado: es idempotente, como en Embedded Signup. Meta no validó este
        # PIN, así que no se da por fijado.
        await _mark_registered(phone, connection, pin_set=False)
        return None

    await _mark_registered(phone, connection, pin_set=True)
    return None


def _pin_obligatorio(connection: MetaConnection) -> bool:
    """En producción el PIN lo escribe el negocio: BYOK (ADR-0127) y Embedded Signup (ADR-0128).
    La carga manual sigue como antes (el token es de la plataforma y es un respaldo)."""
    return (
        getattr(connection, "origin", None) in ("byok", "embedded_signup")
        and getattr(connection, "environment", None) == "production"
    )


async def _restore_pin(connection: MetaConnection, phone_number_id: str, previous_pin: str | None) -> None:
    from core.gateway import meta_token_store
    from core.gateway.meta_phone_numbers import PIN_KIND

    if previous_pin is not None:
        await meta_token_store.put_token(
            connection.id, PIN_KIND, phone_number_id, previous_pin, tenant_id=connection.tenant_id
        )
    else:
        await meta_token_store.revoke_token(connection.id, PIN_KIND, phone_number_id, tenant_id=connection.tenant_id)


async def _mark_registered(phone: PhoneNumber, connection: MetaConnection, *, pin_set: bool) -> None:
    async with get_session() as session:
        row = await session.get(PhoneNumber, phone.id)
        if (
            row is not None
            and row.tenant_id == connection.tenant_id
            and row.connection_id == connection.id
            and row.deleted_at is None
        ):
            row.registered = True
            if pin_set:
                row.pin_set = True
    from core.gateway.meta_connection_state import esperando_registro

    if esperando_registro(connection):
        await _conectar_si_solo_faltaba_el_registro(connection)


async def _conectar_si_solo_faltaba_el_registro(connection: MetaConnection) -> None:
    """Una conexión `pending` que solo esperaba el registro (alta BYOK con número sin
    registrar) pasa a `connected` si ya no le queda ningún número vivo sin registrar. El alta
    ya verificó token y suscripción; el registro que Meta acaba de aceptar era lo que faltaba
    (HU-CONN-06, ADR-0127)."""
    from sqlalchemy import func

    from core.gateway.meta_connection_state import esperando_registro

    async with get_session() as session:
        # FOR UPDATE y releída: una desactivación o un alta concurrente no queda pisada.
        locked = await get_live_meta_connection(session, connection.id, connection.tenant_id, for_update=True)
        if locked is None or not esperando_registro(locked):
            return
        sin_registrar = (
            await session.execute(
                select(func.count()).select_from(PhoneNumber).where(
                    PhoneNumber.tenant_id == connection.tenant_id,
                    PhoneNumber.connection_id == connection.id,
                    PhoneNumber.deleted_at.is_(None),
                    PhoneNumber.registered.is_(False),
                )
            )
        ).scalar_one()
        if not sin_registrar:
            locked.status = "connected"
            locked.status_reason = None


# Orígenes cuyo token es del propio negocio: solo con ellos se recorre el portafolio. Con
# Embedded Signup y `manual` el token es un system user de la PLATAFORMA (`credentials_for`,
# ADR-0094): su portafolio puede ser el de la plataforma, con WABAs de otros negocios, así
# que solo se lista la WABA de la conexión.
_PORTFOLIO_ORIGINS = {"byok"}


async def business_numbers(tenant_id: uuid.UUID, connection_id: str) -> tuple[dict | None, str | None]:
    """Números de TODAS las WABAs propias del portafolio de Meta dueño de la WABA de la
    conexión: el límite de números de Meta cuenta el portafolio entero. (datos, error).

    Solo lectura: no sincroniza nada a `phone_numbers` (los números de otras WABAs no se le
    asignan a este negocio). Lo que falle por partes (permiso del token sobre el portafolio,
    una WABA) queda en `warnings` y el resto se muestra igual.
    """
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import (
        list_owned_wabas,
        list_phone_numbers,
        waba_owner_business,
    )

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return None, "Conexión inválida"

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None:
        return None, "Conexión no encontrada"
    if waba is None or creds is None:
        return None, "No hay conexión/WABA lista todavía"

    graph_args = (creds.access_token, creds.app_secret, creds.graph_api_version, connection.id)
    warnings: list[str] = []
    business: dict | None = None
    wabas: list[dict] = [{"id": waba.waba_id, "name": None}]

    if connection.origin in _PORTFOLIO_ORIGINS:
        try:
            business = await waba_owner_business(waba.waba_id, *graph_args)
        except GraphError as exc:
            warnings.append(f"No se pudo leer el portafolio de la cuenta: {_meta_error_text(exc)}")
        if business is not None:
            try:
                owned = await list_owned_wabas(str(business["id"]), *graph_args)
            except GraphError as exc:
                warnings.append(
                    "Solo se muestra la cuenta de esta conexión: el token no puede leer las demás "
                    f"cuentas del portafolio (permiso business_management). {_meta_error_text(exc)}"
                )
            else:
                names = {str(w.get("id")): w.get("name") for w in owned if w.get("id")}
                wabas[0]["name"] = names.pop(waba.waba_id, None)
                wabas.extend({"id": wid, "name": name} for wid, name in names.items())
    else:
        warnings.append(
            "Con esta conexión (token de la plataforma) solo se muestran los números de su cuenta "
            "de WhatsApp, no los del resto del portafolio."
        )

    async with get_session() as session:
        in_connection = set(
            (
                await session.execute(
                    select(PhoneNumber.phone_number_id).where(
                        PhoneNumber.connection_id == connection.id,
                        PhoneNumber.tenant_id == connection.tenant_id,
                    )
                )
            ).scalars()
        )

    numbers: list[dict] = []
    other_tenants = 0
    for w in wabas:
        try:
            items = await list_phone_numbers(w["id"], *graph_args)
        except GraphError as exc:
            warnings.append(f"Cuenta {w['name'] or w['id']}: {_meta_error_text(exc)}")
            continue
        for item in items:
            pnid = str(item.get("id") or "")
            if not pnid:
                continue
            # Un token de agencia (BYOK) ve WABAs de varios clientes: lo que ya tiene otro
            # negocio de la plataforma cuenta para el límite, pero no se muestra.
            if await asset_claimed_by_other_tenant(connection.tenant_id, waba_id=w["id"], phone_number_id=pnid):
                other_tenants += 1
                continue
            numbers.append(
                {
                    "waba_id": w["id"],
                    "waba_name": w["name"],
                    "is_connection_waba": w["id"] == waba.waba_id,
                    "phone_number_id": pnid,
                    "display_phone_number": item.get("display_phone_number"),
                    "verified_name": item.get("verified_name"),
                    "status": item.get("status"),
                    "code_verification_status": item.get("code_verification_status"),
                    "platform_type": item.get("platform_type"),
                    "quality_rating": item.get("quality_rating"),
                    "name_status": item.get("name_status"),
                    "in_this_connection": pnid in in_connection,
                }
            )

    if other_tenants:
        warnings.append(
            f"{other_tenants} número(s) del portafolio están conectados a otro negocio de la "
            "plataforma: cuentan para el límite de Meta, pero no se muestran aquí."
        )
    return (
        {
            "business_id": str(business["id"]) if business else None,
            "business_name": business.get("name") if business else None,
            "numbers": numbers,
            "other_tenants_count": other_tenants,
            "warnings": warnings,
        },
        None,
    )

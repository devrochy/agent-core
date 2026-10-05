"""Números de WhatsApp de una WABA: listar, agregar y verificar (después de
la conexión inicial hecha por `core/gateway/meta_onboarding.py`).

`fields` es opcional en `GET /{waba_id}/phone_numbers`, pero sin él la
respuesta omite `code_verification_status`/`platform_type` -- siempre se
pide explícito. Las formas exactas de `request_code`/`verify_code` NO están
verificadas contra doc primaria de 2026 (ver docstrings de cada función);
probar contra un número de staging antes de confiar ciegamente en ellas.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import PhoneNumber, WabaAccount, utcnow
from core.db.session import get_session
from core.gateway.meta_graph import graph_fetch
from core.shared.logger import get_logger

log = get_logger(__name__)

_PHONE_FIELDS = (
    "id,display_phone_number,verified_name,quality_rating,code_verification_status,"
    "name_status,platform_type,messaging_limit_tier,throughput,account_mode"
)


async def list_phone_numbers(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    """Pagina siguiendo `paging.cursors.after` hasta agotar páginas.

    `connection_id=None`: la conexión aún no existe (verificación previa a un alta)."""
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        query = {"fields": _PHONE_FIELDS}
        if after:
            query["after"] = after
        data = await graph_fetch(
            f"/{waba_id}/phone_numbers", token=token, app_secret=app_secret,
            query=query, version=version, connection_id=connection_id,
        )
        items.extend((data or {}).get("data", []))
        after = ((data or {}).get("paging", {}).get("cursors", {}) or {}).get("after")
        next_url = (data or {}).get("paging", {}).get("next")
        if not after or not next_url:
            break
    return items


async def sync_phone_numbers(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Upsert de cada número devuelto por Meta en `phone_numbers` -- idempotente
    por `phone_number_id` (+ `tenant_id` explícito, defensa en profundidad,
    mismo criterio que el resto de este módulo hermano)."""
    items = await list_phone_numbers(waba_id, token, app_secret, version, connection_id)

    async with get_session() as session:
        waba_row = (
            await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id, WabaAccount.connection_id == connection_id))
        ).scalar_one_or_none()
        waba_account_id = waba_row.id if waba_row else None

    for item in items:
        phone_number_id = item.get("id", "")
        if not phone_number_id:
            continue
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.phone_number_id == phone_number_id, PhoneNumber.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                from core.gateway.meta_asset_claims import claimed_elsewhere

                # Otro negocio ya lo tiene (fila heredada): no se guarda, o el número
                # quedaría con dos dueños (ADR-0095). Los demás números siguen.
                if await claimed_elsewhere(
                    session, tenant_id, phone_number_id=phone_number_id, source="sync_phone_numbers"
                ):
                    continue
                session.add(
                    PhoneNumber(
                        tenant_id=tenant_id, connection_id=connection_id, waba_account_id=waba_account_id,
                        phone_number_id=phone_number_id, display_phone_number=item.get("display_phone_number"),
                        verified_name=item.get("verified_name"), quality_rating=item.get("quality_rating"),
                        code_verification_status=item.get("code_verification_status"),
                        name_status=item.get("name_status"), platform_type=item.get("platform_type"),
                        messaging_limit_tier=item.get("messaging_limit_tier"), raw=item,
                    )
                )
            else:
                existing.display_phone_number = item.get("display_phone_number")
                existing.verified_name = item.get("verified_name")
                existing.quality_rating = item.get("quality_rating")
                existing.code_verification_status = item.get("code_verification_status")
                existing.name_status = item.get("name_status")
                existing.platform_type = item.get("platform_type")
                existing.messaging_limit_tier = item.get("messaging_limit_tier")
                existing.raw = item
                existing.updated_at = utcnow()
    return items


async def add_phone_number(
    waba_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID,
    *, cc: str, phone_number: str, verified_name: str,
    migrate_phone_number: bool | None = None, preverified_id: str | None = None,
    data_localization_region: str | None = None,
) -> PhoneNumber:
    """`phone_number` va SIN `+` ni prefijo -- lo separa Meta a partir de
    `cc` (country code) + `phone_number`. `data_localization_region`: ISO de
    2 letras, NO VERIFICADO -- pasado tal cual si el llamador lo manda, sin
    exponerlo todavía en la UI (ver ticket)."""
    body: dict[str, Any] = {"cc": cc, "phone_number": phone_number, "verified_name": verified_name}
    if migrate_phone_number is not None:
        body["migrate_phone_number"] = migrate_phone_number
    if preverified_id:
        body["preverified_id"] = preverified_id
    if data_localization_region:
        body["data_localization_region"] = data_localization_region

    data = await graph_fetch(
        f"/{waba_id}/phone_numbers", method="POST", token=token, app_secret=app_secret,
        body=body, version=version, connection_id=connection_id,
    )
    phone_number_id = str((data or {}).get("id") or "")

    async with get_session() as session:
        waba_row = (
            await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id, WabaAccount.connection_id == connection_id))
        ).scalar_one_or_none()
        from core.gateway.meta_asset_claims import (
            CLAIMED_MESSAGE,
            MetaAssetError,
            claimed_elsewhere,
        )

        if await claimed_elsewhere(session, tenant_id, phone_number_id=phone_number_id, source="add_phone_number"):
            raise MetaAssetError(CLAIMED_MESSAGE, code="asset_claimed")
        row = PhoneNumber(
            tenant_id=tenant_id, connection_id=connection_id, waba_account_id=waba_row.id if waba_row else None,
            phone_number_id=phone_number_id, display_phone_number=f"+{cc}{phone_number}", verified_name=verified_name,
        )
        session.add(row)
        await session.flush()
        return row


async def request_verification_code(
    phone_number_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID,
    *, code_method: str, language: str = "es",
) -> None:
    """`POST /{phone_number_id}/request_code` -- forma NO verificada contra
    doc primaria de 2026, ver docstring del módulo. `code_method`:
    'SMS' | 'VOICE'."""
    await graph_fetch(
        f"/{phone_number_id}/request_code", method="POST", token=token, app_secret=app_secret,
        body={"code_method": code_method, "language": language}, version=version, connection_id=connection_id,
    )


async def verify_code(
    phone_number_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID, tenant_id: uuid.UUID, *, code: str
) -> bool:
    """`POST /{phone_number_id}/verify_code` -- forma NO verificada contra
    doc primaria de 2026, ver docstring del módulo. El código llega al
    teléfono del cliente por SMS/voz -- lo teclea un humano en el panel."""
    data = await graph_fetch(
        f"/{phone_number_id}/verify_code", method="POST", token=token, app_secret=app_secret,
        body={"code": code}, version=version, connection_id=connection_id,
    )
    success = bool((data or {}).get("success"))
    if success:
        # tenant_id explícito -- defensa en profundidad, mismo criterio ya
        # aplicado en sync_phone_numbers/add_phone_number (hallazgo
        # auditor-multitenant, 2026-09-22, severidad media).
        async with get_session() as session:
            row = (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.phone_number_id == phone_number_id, PhoneNumber.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.code_verification_status = "VERIFIED"
    return success

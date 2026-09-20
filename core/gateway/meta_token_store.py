"""Custodia cifrada de tokens de Meta (`meta_private.meta_tokens`).

Único módulo del repo que debe importar `MetaToken` o tocar el schema
`meta_private` -- verificado por `tests/test_meta_token_store_aislado.py`
(grep estático). El AAD ata cada fila a su tenant+conexión+tipo de token:
moverla a otro tenant, o leerla con otra conexión/kind, hace fallar la
autenticación AES-GCM en vez de devolver un secreto ajeno en silencio.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import MetaToken
from core.db.session import get_session
from core.shared.crypto import seal_parts, token_fingerprint, unseal_parts
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


def _aad(tenant_id: uuid.UUID, connection_id: uuid.UUID, kind: str) -> bytes:
    return f"{tenant_id}:{connection_id}:{kind}".encode()


async def put_token(
    connection_id: uuid.UUID,
    kind: str,
    subject_id: str,
    token: str,
    *,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Cifra y guarda un token. Upsert real (`ON CONFLICT ... DO UPDATE` sobre
    el UNIQUE(connection_id, kind, subject_id)) -- nunca un INSERT duplicado
    ni un DELETE+INSERT con ventana de carrera."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    ciphertext, iv, auth_tag, key_id = seal_parts(token.encode(), aad=_aad(tid, connection_id, kind))

    async with get_session() as session:
        stmt = pg_insert(MetaToken).values(
            id=uuid.uuid4(),
            tenant_id=tid,
            connection_id=connection_id,
            kind=kind,
            subject_id=subject_id,
            ciphertext=ciphertext,
            iv=iv,
            auth_tag=auth_tag,
            key_id=key_id,
            token_hash=token_fingerprint(token),
            scopes=scopes or [],
            expires_at=expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_meta_tokens_connection_kind_subject",
            set_={
                "ciphertext": stmt.excluded.ciphertext,
                "iv": stmt.excluded.iv,
                "auth_tag": stmt.excluded.auth_tag,
                "key_id": stmt.excluded.key_id,
                "token_hash": stmt.excluded.token_hash,
                "scopes": stmt.excluded.scopes,
                "expires_at": stmt.excluded.expires_at,
                "revoked_at": None,
            },
        )
        await session.execute(stmt)


async def get_token(
    connection_id: uuid.UUID, kind: str, subject_id: str, *, tenant_id: uuid.UUID | None = None
) -> str | None:
    """Descifra y devuelve el token, o None si no existe/fue revocado."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        row = (
            await session.execute(
                select(MetaToken).where(
                    MetaToken.connection_id == connection_id,
                    MetaToken.kind == kind,
                    MetaToken.subject_id == subject_id,
                    MetaToken.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    plaintext = unseal_parts(row.ciphertext, row.iv, row.auth_tag, row.key_id, aad=_aad(tid, connection_id, kind))
    return plaintext.decode()


async def get_token_expiry(
    connection_id: uuid.UUID, kind: str, subject_id: str, *, tenant_id: uuid.UUID | None = None
) -> datetime | None:
    """Lee `expires_at` sin descifrar (barato: sin AES-GCM) -- para crons que
    solo necesitan decidir SI hace falta refrescar un token antes de pagar
    el costo de `get_token`. `None` si no existe/fue revocado, o si nunca se
    guardó `expires_at`."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        row = (
            await session.execute(
                select(MetaToken.expires_at).where(
                    MetaToken.tenant_id == tid,
                    MetaToken.connection_id == connection_id,
                    MetaToken.kind == kind,
                    MetaToken.subject_id == subject_id,
                    MetaToken.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    return row


async def revoke_token(
    connection_id: uuid.UUID, kind: str, subject_id: str, *, tenant_id: uuid.UUID | None = None
) -> None:
    """`tenant_id` explícito por consistencia con put_token/get_token (defensa en
    profundidad más allá de RLS, aunque acá no hay AAD que verificar -- revocar
    no descifra nada). Si no encuentra la fila, lo loguea: un "no se pudo
    revocar" silencioso es peor que un log de más (a diferencia de `get_token`,
    donde "no existe" es un resultado normal y esperado)."""
    from datetime import UTC

    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        row = (
            await session.execute(
                select(MetaToken).where(
                    MetaToken.tenant_id == tid,
                    MetaToken.connection_id == connection_id,
                    MetaToken.kind == kind,
                    MetaToken.subject_id == subject_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            log.warning(
                "meta_token_revoke_not_found",
                connection_id=str(connection_id),
                kind=kind,
                subject_id=subject_id,
            )
            return
        row.revoked_at = datetime.now(UTC)

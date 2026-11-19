"""Persistencia de la sesión del canal `waweb` (ADR-0074).

Neonize trabaja sobre un **directorio** (su store SQLite/archivos). Para no dejar
credenciales en claro en disco ni en backups, el connector usa un **directorio
efímero por tenant** (idealmente `tmpfs`) y la copia **durable y cifrada**
(SecretBox/AES-GCM, ADR-0027) va a `waweb_sessions.creds_encrypted` como un
`tar.gz` en base64.

El connector materializa el directorio al arrancar, y lo persiste (re-cifrado)
cuando la sesión cambia. El proceso es agnóstico del backend de socket.
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from core.shared.logger import get_logger

log = get_logger(__name__)


class SessionStore(Protocol):
    """Contrato de persistencia de la sesión (durable, cifrada)."""

    def work_dir(self, tenant_id: uuid.UUID) -> Path: ...

    async def materialize(self, tenant_id: uuid.UUID, work_dir: Path) -> bool:
        """Escribe la sesión durable en `work_dir`. `True` si había algo."""
        ...

    async def persist(self, tenant_id: uuid.UUID, work_dir: Path) -> None: ...

    async def clear(self, tenant_id: uuid.UUID) -> None: ...


def pack_dir(work_dir: Path) -> bytes:
    """Empaqueta el contenido del directorio como `tar.gz` en memoria."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(work_dir.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(work_dir)))
    return buffer.getvalue()


def unpack_dir(data: bytes, work_dir: Path) -> None:
    """Desempaqueta un `tar.gz` en `work_dir` (creando el directorio)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        # Extracción segura: nada fuera de work_dir (path traversal).
        for member in tar.getmembers():
            target = (work_dir / member.name).resolve()
            if not str(target).startswith(str(work_dir.resolve())):
                raise ValueError(f"Entrada de tar fuera del directorio: {member.name}")
        tar.extractall(work_dir)


def _base_escribible(base: Path) -> Path:
    """Devuelve una raíz donde el proceso pueda crear sesiones.

    El `tmpfs` de despliegue puede montarse `root:root 0755` y el conector corre
    como `app`: `mkdir` fallaba con `PermissionError` y no se emitía ningún QR
    (visto en test 2026-10-28). Antes de romper el vínculo, se usa una raíz
    efímera bajo `/tmp` (también escribible por `app`) y se deja traza.
    """
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / f".escritura-{os.getpid()}"
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)
        return base
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / f"waweb-sessions-{os.getpid()}"
        fallback.mkdir(parents=True, exist_ok=True)
        log.error(
            "waweb_session_dir_no_escribible",
            base=str(base),
            fallback=str(fallback),
            detail=str(exc),
        )
        return fallback


class DbEncryptedSessionStore:
    """Store durable: `tar.gz` cifrado en `waweb_sessions.creds_encrypted`.

    `base_dir` es la raíz de los directorios efímeros (p. ej. un volumen tmpfs).
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = _base_escribible(Path(base_dir))

    def work_dir(self, tenant_id: uuid.UUID) -> Path:
        return self._base_dir / str(tenant_id)

    async def materialize(self, tenant_id: uuid.UUID, work_dir: Path) -> bool:
        raw = await self._read_blob(tenant_id)
        if not raw:
            return False
        try:
            unpack_dir(base64.b64decode(raw), work_dir)
        except Exception:
            # Sesión corrupta: se descarta y se re-vincula (no tumbar el connector).
            return False
        return True

    async def persist(self, tenant_id: uuid.UUID, work_dir: Path) -> None:
        if not work_dir.exists():
            return
        data = pack_dir(work_dir)
        if not data:
            return
        await self._write_blob(tenant_id, base64.b64encode(data).decode())

    async def clear(self, tenant_id: uuid.UUID) -> None:
        await self._write_blob(tenant_id, None)

    # --- acceso a BD (aislado para testear el resto sin Postgres) ---
    def _session_query(self):  # type: ignore[no-untyped-def]
        """Sesión viva de la conexión `waweb` VIVA (una sola).

        Se une con `channel_connections` para no tomar una sesión vieja de una
        conexión borrada: con dos sesiones por tenant (incidente 2026-10-04),
        `.first()` por tenant materializaba credenciales ajenas y el connector
        reautenticaba sin emitir QR.
        """
        from sqlalchemy import select

        from core.db.models import ChannelConnection, WaWebSession

        return (
            select(WaWebSession)
            .join(ChannelConnection, ChannelConnection.id == WaWebSession.connection_id)
            .where(
                WaWebSession.deleted_at.is_(None),
                ChannelConnection.deleted_at.is_(None),
            )
            .order_by(WaWebSession.created_at)
            .limit(1)
        )

    async def _read_blob(self, tenant_id: uuid.UUID) -> str | None:
        from core.db.models import WaWebSession
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        # El connector no corre en el scope de un tenant: hay que abrirlo para
        # que RLS deje ver la fila (defensa en profundidad además del filtro).
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                row = (
                    await session.execute(
                        self._session_query().where(WaWebSession.tenant_id == tenant_id)
                    )
                ).scalars().first()
                if row is None or not row.creds_encrypted:
                    return None
                from core.shared.crypto import get_secret_box

                try:
                    return get_secret_box().decrypt(row.creds_encrypted)
                except Exception:
                    return None

    async def _write_blob(self, tenant_id: uuid.UUID, value: str | None) -> None:
        from core.db.models import WaWebSession
        from core.db.session import get_session
        from core.shared.crypto import get_secret_box
        from core.shared.tenant import tenant_scope

        encrypted = get_secret_box().encrypt(value) if value is not None else None
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                row = (
                    await session.execute(
                        self._session_query().where(WaWebSession.tenant_id == tenant_id)
                    )
                ).scalars().first()
                if row is not None:
                    row.creds_encrypted = encrypted

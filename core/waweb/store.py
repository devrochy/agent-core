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
import tarfile
import uuid
from pathlib import Path
from typing import Protocol


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


class DbEncryptedSessionStore:
    """Store durable: `tar.gz` cifrado en `waweb_sessions.creds_encrypted`.

    `base_dir` es la raíz de los directorios efímeros (p. ej. un volumen tmpfs).
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

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
    async def _read_blob(self, tenant_id: uuid.UUID) -> str | None:
        from sqlalchemy import select

        from core.db.models import WaWebSession
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        # El connector no corre en el scope de un tenant: hay que abrirlo para
        # que RLS deje ver la fila (defensa en profundidad además del filtro).
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                row = (
                    await session.execute(
                        select(WaWebSession).where(
                            WaWebSession.tenant_id == tenant_id,
                            WaWebSession.deleted_at.is_(None),
                        )
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
        from sqlalchemy import select

        from core.db.models import WaWebSession
        from core.db.session import get_session
        from core.shared.crypto import get_secret_box
        from core.shared.tenant import tenant_scope

        encrypted = get_secret_box().encrypt(value) if value is not None else None
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                row = (
                    await session.execute(
                        select(WaWebSession).where(
                            WaWebSession.tenant_id == tenant_id,
                            WaWebSession.deleted_at.is_(None),
                        )
                    )
                ).scalars().first()
                if row is not None:
                    row.creds_encrypted = encrypted

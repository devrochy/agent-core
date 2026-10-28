"""Tests puros del store de sesión del canal `waweb` (ADR-0074)."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from core.waweb.store import DbEncryptedSessionStore, pack_dir, unpack_dir


def test_pack_unpack_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "creds.json").write_text('{"k":"v"}')
    (src / "sub" / "app-state").write_text("state")

    data = pack_dir(src)
    dst = tmp_path / "dst"
    unpack_dir(data, dst)

    assert (dst / "creds.json").read_text() == '{"k":"v"}'
    assert (dst / "sub" / "app-state").read_text() == "state"


class _MemoryStore(DbEncryptedSessionStore):
    def __init__(self, base: Path) -> None:
        super().__init__(base)
        self.blobs: dict[str, str] = {}

    async def _read_blob(self, tenant_id: uuid.UUID) -> str | None:
        return self.blobs.get(str(tenant_id))

    async def _write_blob(self, tenant_id: uuid.UUID, value: str | None) -> None:
        if value is None:
            self.blobs.pop(str(tenant_id), None)
        else:
            self.blobs[str(tenant_id)] = value


async def test_materialize_persist_roundtrip(tmp_path: Path) -> None:
    store = _MemoryStore(tmp_path)
    tenant = uuid.UUID(int=1)
    work = store.work_dir(tenant)

    # Sin sesión durable: materialize no trae nada.
    assert await store.materialize(tenant, work) is False

    # Persistir crea el blob cifrado; materialize lo restaura.
    work.mkdir(parents=True)
    (work / "creds.json").write_text("secreto")
    await store.persist(tenant, work)
    assert str(tenant) in store.blobs

    work2 = tmp_path / "restored" / "1"
    assert await store.materialize(tenant, work2) is True
    assert (work2 / "creds.json").read_text() == "secreto"


async def test_clear_borra_la_sesion(tmp_path: Path) -> None:
    store = _MemoryStore(tmp_path)
    tenant = uuid.UUID(int=2)
    work = store.work_dir(tenant)
    work.mkdir(parents=True)
    (work / "x").write_text("y")
    await store.persist(tenant, work)

    await store.clear(tenant)
    assert await store.materialize(tenant, work) is False


def test_unpack_rechaza_path_traversal(tmp_path: Path) -> None:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"x"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    try:
        unpack_dir(buffer.getvalue(), tmp_path / "out")
    except ValueError as exc:
        assert "fuera del directorio" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("debía rechazar la entrada")


def test_base_sin_permiso_cae_a_tmp_y_no_rompe_el_vinculo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un tmpfs root:root 0755 no debe dejar al conector sin QR."""
    base = tmp_path / "waweb-sessions"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    real_write = Path.write_bytes

    def _sin_escritura(self: Path, data: bytes) -> int:
        if self.name.startswith(".escritura-"):
            raise PermissionError(13, "permiso denegado")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", _sin_escritura)

    store = DbEncryptedSessionStore(base)
    work = store.work_dir(uuid.UUID(int=3))

    assert work.parent == tmp_path / f"waweb-sessions-{os.getpid()}"
    work.mkdir()
    assert work.is_dir()

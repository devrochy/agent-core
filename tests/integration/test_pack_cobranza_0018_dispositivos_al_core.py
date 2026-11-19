"""`pack_cobranza_0018` (HU-ACC-02, ADR-0174): dispositivos e intentos del pack → core.

Siembra los casos del criterio de aceptación en dos negocios, corre la migración (bajando y subiendo el
árbol del pack) y comprueba que migrados + omitidos suman el origen, que el estado (incluido `bloqueado`)
se conserva, que los activos sin `imei` quedan `legacy`, que la copia respeta el aislamiento por negocio
y que una segunda corrida no crea filas nuevas.

Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
RAIZ = Path(__file__).resolve().parents[2]

_ARBOL_DEL_PACK = """
import sys
from alembic import command
from alembic.config import Config
from core.db.migrations import plugin_dir, sync_database_url

d = plugin_dir("pack_cobranza")
cfg = Config(str(d / "alembic.ini"))
cfg.set_main_option("script_location", str(d / "alembic"))
cfg.set_main_option("sqlalchemy.url", sync_database_url())
cfg.set_main_option("version_table", "alembic_version_pack_cobranza")
accion, destino = sys.argv[1], sys.argv[2]
getattr(command, accion)(cfg, destino)
"""


def _pack(accion: str, destino: str) -> None:
    r = subprocess.run(
        [sys.executable, "-c", _ARBOL_DEL_PACK, accion, destino],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert r.returncode == 0, r.stderr[-2000:]


async def _negocio(slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de migración.",
        persona={"tone": "cercano"},
        admin_email=f"{slug}-owner@test.local",
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    return tid


async def _gestor(
    tenant_id: uuid.UUID, *, con_cuenta: bool, nombre: str
) -> tuple[uuid.UUID, uuid.UUID | None]:
    from pack_cobranza.models import Gestor

    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    admin_id: uuid.UUID | None = None
    if con_cuenta:
        async with get_session() as session:
            admin = Admin(
                email=f"{uniq('mig18')}@test.local",
                password_hash=hash_password(PW),
                role="colaborador",
            )
            session.add(admin)
            await session.flush()
            admin_id = admin.id
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if admin_id is not None:
                session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role="colaborador"))
            gestor = Gestor(tenant_id=tenant_id, admin_id=admin_id, nombre=nombre)
            session.add(gestor)
            await session.flush()
            return gestor.id, admin_id


async def _device(
    tenant_id: uuid.UUID,
    gestor_id: uuid.UUID | None,
    imei: str | None,
    estado: str,
    *,
    borrado: bool = False,
) -> uuid.UUID:
    from pack_cobranza.models import Device

    from core.admin.auth import hash_password
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            d = Device(
                tenant_id=tenant_id,
                codigo=str(uuid.uuid4()),
                api_key_hash=hash_password(uuid.uuid4().hex),
                gestor_id=gestor_id,
                imei=imei,
                whatsapp_number="3000000000",
                estado=estado,
                deleted_at=datetime.now(UTC) if borrado else None,
            )
            session.add(d)
            await session.flush()
            return d.id


async def _intento(
    tenant_id: uuid.UUID, gestor_id: uuid.UUID, resultado: str, hace_dias: int
) -> None:
    from pack_cobranza.models import IntentoAcceso

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(
                IntentoAcceso(
                    tenant_id=tenant_id,
                    gestor_id=gestor_id,
                    imei="I-A",
                    resultado=resultado,
                    motivo="dispositivo no autorizado" if resultado == "denegado" else "ok",
                    created_at=datetime.now(UTC) - timedelta(days=hace_dias),
                )
            )


async def _filas(tenant_id: uuid.UUID) -> list:
    from sqlalchemy import select

    from core.db.models import AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(AccountDevice).where(AccountDevice.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )


async def _eventos(tenant_id: uuid.UUID) -> list:
    from sqlalchemy import select

    from core.db.models import AccessEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(AccessEvent).where(
                            AccessEvent.tenant_id == tenant_id, AccessEvent.tipo == "legado_pack"
                        )
                    )
                )
                .scalars()
                .all()
            )


async def _sembrar() -> dict:
    """Negocio A con los siete casos y negocio B con un dispositivo propio."""
    a, b = await _negocio(uniq("m18a")), await _negocio(uniq("m18b"))
    g1, admin1 = await _gestor(a, con_cuenta=True, nombre="Con cuenta")
    g2, _ = await _gestor(a, con_cuenta=False, nombre="Sin cuenta")
    gb, admin_b = await _gestor(b, con_cuenta=True, nombre="De B")
    ids = {
        "activo_imei": await _device(a, g1, "I-A", "activo"),
        "activo_sin_imei": await _device(a, g1, None, "activo"),
        "pendiente": await _device(a, g1, "I-C", "pendiente"),
        "bloqueado": await _device(a, g1, "I-D", "bloqueado"),
        "borrado": await _device(a, g1, "I-E", "activo", borrado=True),
        "gestor_sin_admin": await _device(a, g2, "I-F", "activo"),
        "v1_sin_gestor": await _device(a, None, "I-V1", "activo"),
        "de_b": await _device(b, gb, "I-B", "activo"),
    }
    await _intento(a, g1, "denegado", 1)
    await _intento(a, g1, "ok", 5)
    await _intento(a, g1, "ok", 100)  # fuera de la ventana de 90 días
    return {"a": a, "b": b, "admin1": admin1, "admin_b": admin_b, "ids": ids}


async def test_migra_conserva_estado_y_es_idempotente() -> None:
    s = await _sembrar()
    a, b, ids = s["a"], s["b"], s["ids"]

    _pack("downgrade", "pack_cobranza_0017")
    _pack("upgrade", "head")

    filas = {f.legacy_pack_device_id: f for f in await _filas(a)}
    # Origen del negocio A con gestor: 5 (activo_imei, activo_sin_imei, pendiente, bloqueado, gestor_sin_admin).
    # Migrados 4; omitido 1 (gestor sin cuenta). El borrado y el v1 no entran en el origen.
    assert set(filas) == {
        ids["activo_imei"],
        ids["activo_sin_imei"],
        ids["pendiente"],
        ids["bloqueado"],
    }
    assert ids["gestor_sin_admin"] not in filas and ids["borrado"] not in filas
    assert ids["v1_sin_gestor"] not in filas

    activo = filas[ids["activo_imei"]]
    assert (activo.estado, activo.device_id, activo.legacy) == ("activo", "I-A", False)
    assert activo.admin_id == s["admin1"] and activo.origen == "migrado_pack"
    assert activo.rol_al_registrar == "gestor" and activo.aprobado_at is not None

    legacy = filas[ids["activo_sin_imei"]]
    assert (legacy.estado, legacy.device_id, legacy.legacy) == ("activo", None, True)

    assert filas[ids["pendiente"]].estado == "pendiente"
    bloqueado = filas[ids["bloqueado"]]
    assert (bloqueado.estado, bloqueado.device_id) == (
        "bloqueado",
        "I-D",
    )  # el bloqueo no se pierde
    assert bloqueado.bloqueado_at is not None

    # Aislamiento: el negocio B tiene solo lo suyo.
    de_b = await _filas(b)
    assert [(f.legacy_pack_device_id, f.admin_id, f.tenant_id) for f in de_b] == [
        (ids["de_b"], s["admin_b"], b)
    ]

    eventos = await _eventos(a)
    assert len(eventos) == 2  # el de hace 100 días queda fuera
    assert {e.severidad for e in eventos} == {"aviso", "info"}
    assert all(e.rol == "gestor" and e.admin_id == s["admin1"] for e in eventos)

    # Segunda corrida: se vuelve a ejecutar la migración con los datos ya copiados.
    _pack("stamp", "pack_cobranza_0017")
    _pack("upgrade", "head")

    assert len(await _filas(a)) == 4 and len(await _filas(b)) == 1
    assert len(await _eventos(a)) == 2


async def test_la_baja_de_la_migracion_borra_solo_lo_migrado() -> None:
    from core.access.devices import evaluar
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    s = await _sembrar()
    a = s["a"]
    _pack("downgrade", "pack_cobranza_0017")
    _pack("upgrade", "head")
    # Un equipo registrado después por la política abierta del core no es de la migración.
    async with tenant_scope(a):
        async with get_session() as session:
            d = await evaluar(
                session,
                tenant_id=a,
                admin_id=s["admin1"],
                rol="colaborador",
                device_id="EQUIPO-NUEVO",
            )
            assert d.permitido

    _pack("downgrade", "pack_cobranza_0017")
    try:
        restantes = await _filas(a)
        assert [f.device_id for f in restantes] == ["EQUIPO-NUEVO"]
    finally:
        _pack("upgrade", "head")

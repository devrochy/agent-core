"""Cuentas Wompi del negocio y la cuenta que usa cada agente (ADR-0190 §6, HU-AOS-62).

Una cuenta se registra una vez por negocio; cada agente con la habilidad Pagos en línea guarda en la config de SU
instalación (`pack_installations`, scope `agent`) el `cuenta_id` que eligió. Rotar llaves es editar la cuenta: los
agentes que la usan cobran con las nuevas sin publicar una versión.

Resolución de la config con la que se cobra (`config_de_cobro`):
1. `cuenta_id` explícito (el webhook por cuenta, un cargo que ya la guarda);
2. si no, la cuenta elegida por `agent_id`;
3. si no, la config del negocio en `plugin_configs` (lo de antes; se retira con la resiembra, HU-AOS-65).

Los secretos nunca salen de aquí en claro salvo hacia el proveedor (`PaymentsConfig`), ni se loguean.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import select, text

from core.db.session import get_session
from core.shared.crypto import seal, unseal
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_payments.config import PaymentsConfig
from pack_payments.models import CuentaWompi, utcnow

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"
CLAVE_CUENTA = "cuenta_id"


class CuentaNoEncontrada(LookupError):
    pass


class CuentaEnUso(RuntimeError):
    def __init__(self, agentes: list[uuid.UUID]) -> None:
        super().__init__("La cuenta la usan agentes del negocio.")
        self.agentes = agentes


@dataclass(frozen=True)
class CuentaVista:
    """Lo que se puede mostrar de una cuenta: sin llave privada ni secreto de eventos."""

    id: uuid.UUID
    nombre: str
    public_key: str
    sandbox: bool
    link_expira_horas: int
    monto_minimo: float | None


def _tenant() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("cuentas Wompi: usar dentro de tenant_scope()")
    return tenant_id


def _aad(tenant_id: uuid.UUID, cuenta_id: uuid.UUID) -> bytes:
    return f"pack_payments_accounts:{tenant_id}:{cuenta_id}".encode()


def _vista(fila: CuentaWompi) -> CuentaVista:
    return CuentaVista(
        id=fila.id,
        nombre=fila.nombre,
        public_key=fila.public_key,
        sandbox=bool(fila.sandbox),
        link_expira_horas=int(fila.link_expira_horas),
        monto_minimo=float(fila.monto_minimo) if fila.monto_minimo is not None else None,
    )


def _sellar(tenant_id: uuid.UUID, cuenta_id: uuid.UUID, private_key: str, events_secret: str) -> str:
    plano = json.dumps({"private_key": private_key, "events_secret": events_secret})
    return seal(plano, _aad(tenant_id, cuenta_id))


async def crear_cuenta(
    *,
    nombre: str,
    public_key: str,
    private_key: str,
    events_secret: str,
    sandbox: bool = True,
    link_expira_horas: int = 24,
    monto_minimo: float | None = None,
) -> CuentaVista:
    tenant_id = _tenant()
    cuenta_id = uuid.uuid4()
    fila = CuentaWompi(
        id=cuenta_id,
        tenant_id=tenant_id,
        nombre=nombre.strip(),
        public_key=public_key.strip(),
        secretos_cifrados=_sellar(tenant_id, cuenta_id, private_key, events_secret),
        sandbox=sandbox,
        link_expira_horas=link_expira_horas,
        monto_minimo=monto_minimo,
    )
    async with get_session() as session:
        session.add(fila)
        await session.flush()
        vista = _vista(fila)
    log.info("wompi_cuenta_creada", cuenta_id=str(cuenta_id), sandbox=sandbox)
    return vista


async def _fila(session, cuenta_id: uuid.UUID, *, bloquear: bool = False) -> CuentaWompi:  # type: ignore[no-untyped-def]
    query = select(CuentaWompi).where(
        CuentaWompi.tenant_id == _tenant(), CuentaWompi.id == cuenta_id, CuentaWompi.deleted_at.is_(None)
    )
    if bloquear:
        query = query.with_for_update()
    fila = (await session.execute(query)).scalar_one_or_none()
    if fila is None:
        raise CuentaNoEncontrada(str(cuenta_id))
    return fila


async def actualizar_cuenta(
    cuenta_id: uuid.UUID,
    *,
    nombre: str | None = None,
    public_key: str | None = None,
    private_key: str | None = None,
    events_secret: str | None = None,
    sandbox: bool | None = None,
    link_expira_horas: int | None = None,
    monto_minimo: float | None = None,
) -> CuentaVista:
    """Edita la cuenta. Un secreto en `None` se conserva; para rotar se mandan los dos."""
    tenant_id = _tenant()
    async with get_session() as session:
        fila = await _fila(session, cuenta_id, bloquear=True)
        if nombre is not None:
            fila.nombre = nombre.strip()
        if public_key is not None:
            fila.public_key = public_key.strip()
        if private_key is not None or events_secret is not None:
            actuales = json.loads(unseal(fila.secretos_cifrados, _aad(tenant_id, fila.id)))
            fila.secretos_cifrados = _sellar(
                tenant_id,
                fila.id,
                private_key if private_key is not None else actuales["private_key"],
                events_secret if events_secret is not None else actuales["events_secret"],
            )
        if sandbox is not None:
            fila.sandbox = sandbox
        if link_expira_horas is not None:
            fila.link_expira_horas = link_expira_horas
        if monto_minimo is not None:
            fila.monto_minimo = monto_minimo
        await session.flush()
        vista = _vista(fila)
    log.info("wompi_cuenta_actualizada", cuenta_id=str(cuenta_id))
    return vista


async def listar_cuentas() -> list[CuentaVista]:
    async with get_session() as session:
        filas = (
            await session.execute(
                select(CuentaWompi)
                .where(CuentaWompi.tenant_id == _tenant(), CuentaWompi.deleted_at.is_(None))
                .order_by(CuentaWompi.nombre)
                .limit(100)
            )
        ).scalars().all()
    return [_vista(f) for f in filas]


async def agentes_que_usan(cuenta_id: uuid.UUID) -> list[uuid.UUID]:
    """Agentes con Pagos instalado que eligieron esta cuenta."""
    from core.agents.instalaciones_agente import config_del_agente

    tenant_id = _tenant()
    async with get_session() as session:
        agentes = [
            f[0]
            for f in (
                await session.execute(
                    text(
                        "SELECT agent_id FROM pack_installations WHERE tenant_id = :t AND scope = 'agent' "
                        "AND pack_id = :p AND deleted_at IS NULL ORDER BY agent_id"
                    ),
                    {"t": tenant_id, "p": PLUGIN_ID},
                )
            ).all()
        ]
    salida = []
    for agente in agentes:
        config = await config_del_agente(tenant_id, agente, PLUGIN_ID) or {}
        if str(config.get(CLAVE_CUENTA) or "") == str(cuenta_id):
            salida.append(agente)
    return salida


async def borrar_cuenta(cuenta_id: uuid.UUID) -> None:
    """Borrado lógico. Se niega si algún agente la usa (`CuentaEnUso` con sus ids)."""
    usan = await agentes_que_usan(cuenta_id)
    if usan:
        raise CuentaEnUso(usan)
    async with get_session() as session:
        fila = await _fila(session, cuenta_id, bloquear=True)
        fila.deleted_at = utcnow()
        await session.flush()
    log.info("wompi_cuenta_borrada", cuenta_id=str(cuenta_id))


async def config_de_cuenta(cuenta_id: uuid.UUID, *, moneda: str = "COP") -> PaymentsConfig:
    """La config de cobro de una cuenta, con sus secretos (solo para el proveedor)."""
    tenant_id = _tenant()
    async with get_session() as session:
        fila = await _fila(session, cuenta_id)
    secretos = json.loads(unseal(fila.secretos_cifrados, _aad(tenant_id, fila.id)))
    return PaymentsConfig(
        public_key=fila.public_key,
        private_key=SecretStr(secretos["private_key"]),
        events_secret=SecretStr(secretos["events_secret"]),
        sandbox=bool(fila.sandbox),
        currency=moneda,
        link_expira_horas=int(fila.link_expira_horas),
    )


async def cuenta_del_agente(agent_id: uuid.UUID) -> uuid.UUID | None:
    """La cuenta que eligió el agente en su instalación de Pagos, o `None`."""
    from core.agents.instalaciones_agente import config_del_agente

    config = await config_del_agente(_tenant(), agent_id, PLUGIN_ID) or {}
    valor = config.get(CLAVE_CUENTA)
    try:
        return uuid.UUID(str(valor)) if valor else None
    except ValueError:
        return None


async def config_de_cobro(
    *,
    agent_id: uuid.UUID | None = None,
    cuenta_id: uuid.UUID | None = None,
    respaldo: Callable[[], Awaitable[PaymentsConfig]] | None = None,
) -> tuple[PaymentsConfig, uuid.UUID | None]:
    """`(config, cuenta_id)` con la que se cobra (orden en el docstring del módulo). `cuenta_id` es `None` si salió
    de la config del negocio. `PluginError` si no hay ninguna."""
    if cuenta_id is None and agent_id is not None:
        cuenta_id = await cuenta_del_agente(agent_id)
    if cuenta_id is not None:
        try:
            return await config_de_cuenta(cuenta_id), cuenta_id
        except CuentaNoEncontrada:
            raise PluginError(PLUGIN_ID, "La cuenta Wompi del agente no existe") from None
    if respaldo is None:
        from pack_payments.proveedor import tenant_config as respaldo

    return await respaldo(), None


__all__ = [
    "CLAVE_CUENTA",
    "CuentaEnUso",
    "CuentaNoEncontrada",
    "CuentaVista",
    "actualizar_cuenta",
    "agentes_que_usan",
    "borrar_cuenta",
    "config_de_cobro",
    "config_de_cuenta",
    "crear_cuenta",
    "cuenta_del_agente",
    "listar_cuentas",
]

"""Dispositivos de la cuenta: decisión de acceso y transiciones de estado (ADR-0174 §1 y §3).

`evaluar` es la regla de decisión que consumirán el login de campo del pack y la dependencia
del principal (HU-ACC-02/03); esta HU solo la deja lista y probada, sin enganchar el login.
Todo recibe la sesión del llamador (una transacción por petición) y escribe los eventos en ella.

Reglas que no se negocian:
- Un equipo `bloqueado` deniega **siempre**, en política abierta y restringida (cierra el hueco
  por el que bloquear equivalía a quitar la restricción).
- El alta es `INSERT … ON CONFLICT` sobre el único parcial por cuenta: nunca un 500 por duplicado.
- Las transiciones llevan el estado esperado (`UPDATE … WHERE estado = :esperado`): 409 si cambió.
- Una baja (`deleted_at`) no sale de `bloqueado`: si pudiera, en política abierta el equipo volvería a
  registrarse y el bloqueo se evaporaría.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.access import policy
from core.access.events import registrar_evento, sufijo
from core.db.models import AccountDevice, Admin, TenantUser
from core.shared.logger import get_logger

log = get_logger(__name__)

PENDIENTE, ACTIVO, BLOQUEADO = "pendiente", "activo", "bloqueado"
ALCANCES: tuple[str, ...] = ("cuenta", "equipo")
PLATAFORMAS: tuple[str, ...] = ("android", "ios")

_UNICO_CUENTA = {
    "index_elements": ["tenant_id", "admin_id", "device_id"],
    "index_where": text("deleted_at is null and device_id is not null"),
}


class AccesoError(Exception):
    """Error de dominio con código estable; el router lo traduce a `ApiError`."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Decision:
    permitido: bool
    estado: str  # activo | pendiente | bloqueado | none (sin fila o master de plataforma)
    politica: str  # abierto | restringido | plataforma
    code: str | None = None  # motivo estable del 403 (ADR-0174 §3)
    device_row_id: uuid.UUID | None = None


def _ahora() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ decisión
async def _fila(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, device_id: str
) -> AccountDevice | None:
    return (
        await session.execute(
            select(AccountDevice).where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.admin_id == admin_id,
                AccountDevice.device_id == device_id,
                AccountDevice.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _equipo_bloqueado(session: Any, tenant_id: uuid.UUID, device_id: str) -> bool:
    """¿Algún admin del negocio bloqueó ESTE equipo para todas las cuentas (`bloqueo_equipo`)?"""
    return (
        await session.execute(
            select(AccountDevice.id)
            .where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.device_id == device_id,
                AccountDevice.estado == BLOQUEADO,
                AccountDevice.bloqueo_equipo.is_(True),
                AccountDevice.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None


async def _otra_cuenta_lo_usa(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, device_id: str
) -> bool:
    return (
        await session.execute(
            select(AccountDevice.id)
            .where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.device_id == device_id,
                AccountDevice.admin_id != admin_id,
                AccountDevice.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None


async def _crear(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    device_id: str,
    label: str | None,
    plataforma: str,
    estado: str,
    origen: str,
    rol: str | None,
    ip: str | None,
    aprobado_por: uuid.UUID | None = None,
) -> tuple[AccountDevice, bool]:
    """Alta idempotente. `(fila, True)` si la creó esta llamada; `(fila, False)` si ya existía
    (otra petición concurrente ganó el `ON CONFLICT`)."""
    ahora = _ahora()
    nuevo_id = (
        await session.execute(
            pg_insert(AccountDevice)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                admin_id=admin_id,
                device_id=device_id,
                device_label=label,
                plataforma=plataforma,
                estado=estado,
                origen=origen,
                rol_al_registrar=rol,
                ultimo_uso_at=ahora,
                ultima_ip=ip,
                aprobado_por=aprobado_por,
                aprobado_at=ahora if estado == ACTIVO else None,
            )
            .on_conflict_do_nothing(**_UNICO_CUENTA)
            .returning(AccountDevice.id)
        )
    ).scalar_one_or_none()
    if nuevo_id is not None:
        return await session.get(AccountDevice, nuevo_id), True
    existente = await _fila(session, tenant_id, admin_id, device_id)
    if (
        existente is None
    ):  # la ganadora se dio de baja entre medio: caso extremo, que reintente el llamador
        raise AccesoError(
            409, "conflicto_de_alta", "El equipo cambió mientras se registraba; reintenta."
        )
    return existente, False


async def _reclamar_legacy(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, device_id: str, ip: str | None
) -> AccountDevice | None:
    """Completa con `device_id` la fila heredada ACTIVA de la cuenta que aún no tiene equipo.

    Solo si la cuenta no tiene otro equipo activo con id: así un equipo desconocido no entra "colgado"
    de una fila heredada cuando el gestor ya tiene un equipo vinculado (mismo criterio que ADR-0068).
    `FOR UPDATE SKIP LOCKED`: dos logins simultáneos no reclaman la misma fila."""
    ya_tiene_activo = (
        await session.execute(
            select(AccountDevice.id)
            .where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.admin_id == admin_id,
                AccountDevice.estado == ACTIVO,
                AccountDevice.device_id.is_not(None),
                AccountDevice.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if ya_tiene_activo is not None:
        return None
    fila = (
        await session.execute(
            select(AccountDevice)
            .where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.admin_id == admin_id,
                AccountDevice.legacy.is_(True),
                AccountDevice.device_id.is_(None),
                AccountDevice.estado == ACTIVO,
                AccountDevice.deleted_at.is_(None),
            )
            .order_by(AccountDevice.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if fila is None:
        return None
    fila.device_id, fila.legacy = device_id, False
    fila.ultimo_uso_at, fila.ultima_ip = _ahora(), ip
    await session.flush()
    return fila


def _denegar(code: str, estado: str, politica: str, fila: AccountDevice | None = None) -> Decision:
    return Decision(False, estado, politica, code, fila.id if fila is not None else None)


async def evaluar(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    rol: str,
    device_id: str | None,
    label: str | None = None,
    plataforma: str = "android",
    canal: str = "campo",
    ip: str | None = None,
    es_master_plataforma: bool = False,
    request_id: str | None = None,
    detalle_alta: dict[str, Any] | None = None,
) -> Decision:
    """Decide si esta cuenta puede entrar desde este equipo (ADR-0174 §3) y deja el rastro.

    `rol` es `owner`, `colaborador` o `gestor` (rol efectivo en el negocio). Registra el equipo si
    es nuevo y escribe los eventos de la decisión en `session`. `detalle_alta` viaja en el evento del
    alta (p. ej. el contacto que dio el gestor al pedir el vínculo).
    """
    if rol not in policy.ROLES:
        raise ValueError(f"rol desconocido: {rol}")
    plataforma = plataforma if plataforma in PLATAFORMAS else "android"
    ev = dict(
        tenant_id=tenant_id, admin_id=admin_id, rol=rol, canal=canal, ip=ip, request_id=request_id
    )

    # 1. Master de plataforma: nunca se registra por negocio ni se bloquea.
    if es_master_plataforma:
        registrar_evento(session, tipo="acceso_master", device_id=device_id, **ev)
        return Decision(True, "none", "plataforma")

    politica = (await policy.leer(session, tenant_id))[rol]
    device_id = (device_id or "").strip() or None

    # 2. Sin identificador de equipo.
    if device_id is None:
        registrar_evento(session, tipo="dispositivo_sin_id", **ev)
        if politica == policy.RESTRINGIDO:
            return _denegar("dispositivo_requerido", "none", politica)
        return Decision(True, "none", politica)

    for _ in range(2):  # el segundo intento solo ocurre si otra petición ganó el alta concurrente
        fila = await _fila(session, tenant_id, admin_id, device_id)

        # 3. Bloqueado (esta cuenta o todo el equipo): deniega en ambas políticas.
        if (fila is not None and fila.estado == BLOQUEADO) or await _equipo_bloqueado(
            session, tenant_id, device_id
        ):
            registrar_evento(
                session,
                tipo="dispositivo_bloqueado_usado",
                device_row_id=fila.id if fila is not None else None,
                device_id=device_id,
                **ev,
            )
            return _denegar("dispositivo_bloqueado", BLOQUEADO, politica, fila)

        if fila is not None:
            # 4. Activo: entra.
            if fila.estado == ACTIVO:
                fila.ultimo_uso_at, fila.ultima_ip = _ahora(), ip
                return Decision(True, ACTIVO, politica, None, fila.id)
            # 5. Pendiente.
            if politica == policy.RESTRINGIDO:
                return _denegar("dispositivo_pendiente", PENDIENTE, politica, fila)
            fila.estado, fila.ultimo_uso_at, fila.ultima_ip = ACTIVO, _ahora(), ip
            fila.aprobado_at = _ahora()
            registrar_evento(
                session,
                tipo="dispositivo_aprobado",
                device_row_id=fila.id,
                device_id=device_id,
                detalle={"por": "politica_abierta"},
                **ev,
            )
            return Decision(True, ACTIVO, politica, None, fila.id)

        # 6. Sin fila. Una fila heredada del pack sin equipo (vínculo manual de ADR-0068) se completa con
        # este primer equipo en vez de registrar otro: el gestor que ya estaba vinculado sigue entrando.
        reclamada = await _reclamar_legacy(session, tenant_id, admin_id, device_id, ip)
        if reclamada is not None:
            registrar_evento(
                session,
                tipo="legado_pack",
                device_row_id=reclamada.id,
                device_id=device_id,
                detalle={"por": "primer_login"},
                **ev,
            )
            return Decision(True, ACTIVO, politica, None, reclamada.id)

        restringido = politica == policy.RESTRINGIDO
        fila, creada = await _crear(
            session,
            tenant_id=tenant_id,
            admin_id=admin_id,
            device_id=device_id,
            label=label,
            plataforma=plataforma,
            estado=PENDIENTE if restringido else ACTIVO,
            origen="autoregistro" if restringido else "politica_abierta",
            rol=rol,
            ip=ip,
        )
        if not creada:
            continue  # otra petición la creó: se decide con su fila
        if await _otra_cuenta_lo_usa(session, tenant_id, admin_id, device_id):
            registrar_evento(
                session,
                tipo="dispositivo_compartido",
                device_row_id=fila.id,
                device_id=device_id,
                **ev,
            )
        registrar_evento(
            session,
            tipo="dispositivo_pendiente" if restringido else "dispositivo_nuevo",
            device_row_id=fila.id,
            device_id=device_id,
            detalle=detalle_alta,
            **ev,
        )
        if restringido:
            return _denegar("dispositivo_pendiente", PENDIENTE, politica, fila)
        return Decision(True, ACTIVO, politica, None, fila.id)

    raise AccesoError(
        409, "conflicto_de_alta", "El equipo cambió mientras se registraba; reintenta."
    )


# ------------------------------------------------------------------ autoridad
async def rol_de_cuenta(session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> str | None:
    """`master` (plataforma), `owner` o `colaborador` en este negocio; `None` si no pertenece."""
    admin_role = (
        await session.execute(select(Admin.role).where(Admin.id == admin_id))
    ).scalar_one_or_none()
    if admin_role == "master":
        return "master"
    return (
        await session.execute(
            select(TenantUser.role).where(
                TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id
            )
        )
    ).scalar_one_or_none()


async def _exigir_autoridad(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, actor_es_owner_o_master: bool
) -> None:
    """Los equipos de un owner o del master solo los administra un owner o el master."""
    if actor_es_owner_o_master:
        return
    if await rol_de_cuenta(session, tenant_id, admin_id) in ("owner", "master"):
        raise AccesoError(
            403,
            "solo_dueno",
            "Solo el dueño del negocio o un admin de plataforma puede administrar los equipos del dueño.",
        )


async def cargar(session: Any, tenant_id: uuid.UUID, device_row_id: uuid.UUID) -> AccountDevice:
    fila = await session.get(AccountDevice, device_row_id)
    if fila is None or fila.tenant_id != tenant_id or fila.deleted_at is not None:
        raise AccesoError(404, "dispositivo_no_encontrado", "Dispositivo no encontrado.")
    return fila


async def _invalidar_sesiones(session: Any, fila: AccountDevice, *, bloqueo_de_equipo: bool) -> None:
    """Saca de la caché de vigencia (core.access.sesion) los equipos cuyo estado acaba de cambiar, para que
    el corte llegue a las sesiones abiertas en menos de 60 s. Un bloqueo «de equipo» alcanza a las demás
    cuentas que usan el mismo `device_id`."""
    from core.access.sesion import invalidar

    afectadas = [(fila.admin_id, fila.id)]
    if bloqueo_de_equipo and fila.device_id:
        otras = (
            await session.execute(
                select(AccountDevice.admin_id, AccountDevice.id).where(
                    AccountDevice.tenant_id == fila.tenant_id,
                    AccountDevice.device_id == fila.device_id,
                    AccountDevice.deleted_at.is_(None),
                )
            )
        ).all()
        afectadas += [(a, i) for a, i in otras]
    for admin_id, row_id in dict.fromkeys(afectadas):
        await invalidar(fila.tenant_id, admin_id, row_id)


async def _transicion(
    session: Any,
    fila: AccountDevice,
    esperado: str,
    valores: dict[str, Any],
) -> None:
    """`UPDATE … WHERE estado = :esperado`: 409 si otra persona ya lo cambió."""
    cambiado = (
        await session.execute(
            update(AccountDevice)
            .where(
                AccountDevice.id == fila.id,
                AccountDevice.tenant_id == fila.tenant_id,
                AccountDevice.deleted_at.is_(None),
                AccountDevice.estado == esperado,
            )
            .values(updated_at=_ahora(), **valores)
            .returning(AccountDevice.id)
        )
    ).scalar_one_or_none()
    if cambiado is None:
        await session.refresh(fila)
        raise AccesoError(
            409,
            "estado_desactualizado",
            f"El dispositivo ya está {fila.estado}; actualiza la lista.",
            {"estado_actual": fila.estado},
        )
    await session.refresh(fila)
    await _invalidar_sesiones(session, fila, bloqueo_de_equipo=bool(valores.get("bloqueo_equipo")))


# ------------------------------------------------------------------ transiciones
async def aprobar(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    device_row_id: uuid.UUID,
    esperado: str,
    actor_admin_id: uuid.UUID,
    actor_es_owner_o_master: bool,
    canal: str = "campo",
) -> AccountDevice:
    """`pendiente → activo` (aprobar) o `bloqueado → activo` (reactivar)."""
    if esperado not in (PENDIENTE, BLOQUEADO):
        raise AccesoError(
            400, "transicion_invalida", "Solo se aprueba un equipo pendiente o bloqueado."
        )
    fila = await cargar(session, tenant_id, device_row_id)
    await _exigir_autoridad(session, tenant_id, fila.admin_id, actor_es_owner_o_master)
    if fila.admin_id == actor_admin_id and not actor_es_owner_o_master:
        # Sin esto, un colaborador con `devices:write` se saltaría la política aprobando su propio equipo.
        raise AccesoError(
            403, "no_te_autorices", "No puedes aprobar ni reactivar tu propio equipo."
        )
    if (
        esperado == BLOQUEADO
        and fila.bloqueado_por is not None
        and not actor_es_owner_o_master
        and await rol_de_cuenta(session, tenant_id, fila.bloqueado_por) in ("owner", "master")
    ):
        raise AccesoError(
            403,
            "solo_dueno",
            "Un bloqueo hecho por el dueño solo lo levanta el dueño o un admin de plataforma.",
        )
    await _transicion(
        session,
        fila,
        esperado,
        {
            "estado": ACTIVO,
            "aprobado_por": actor_admin_id,
            "aprobado_at": _ahora(),
            "bloqueo_equipo": False,
        },
    )
    registrar_evento(
        session,
        tenant_id=tenant_id,
        admin_id=fila.admin_id,
        device_row_id=fila.id,
        device_id=fila.device_id,
        tipo="dispositivo_aprobado",
        canal=canal,
        detalle={
            "accion": "reactivar" if esperado == BLOQUEADO else "aprobar",
            "por": str(actor_admin_id),
        },
    )
    return fila


async def bloquear(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    device_row_id: uuid.UUID,
    esperado: str,
    motivo: str | None,
    alcance: str,
    actor_admin_id: uuid.UUID,
    actor_es_owner_o_master: bool,
    llamante_device_id: str | None = None,
) -> AccountDevice:
    """`activo → bloqueado`. Con `alcance="equipo"` vale para todas las cuentas de ese equipo."""
    if alcance not in ALCANCES:
        raise AccesoError(400, "alcance_invalido", "El alcance del bloqueo es «cuenta» o «equipo».")
    if esperado != ACTIVO:
        raise AccesoError(400, "transicion_invalida", "Solo se bloquea un equipo activo.")
    if alcance == "equipo" and not actor_es_owner_o_master:
        # `bloqueo_equipo` deniega a TODAS las cuentas con ese `device_id`, incluida la del dueño: un
        # colaborador podría registrar el equipo del dueño como propio y bloquearlo con este alcance.
        raise AccesoError(
            403,
            "solo_dueno",
            "Bloquear el equipo para todas las cuentas lo hace el dueño o un admin de plataforma.",
        )
    fila = await cargar(session, tenant_id, device_row_id)
    await _exigir_autoridad(session, tenant_id, fila.admin_id, actor_es_owner_o_master)
    if (
        fila.admin_id == actor_admin_id
        and llamante_device_id
        and fila.device_id == llamante_device_id
    ):
        raise AccesoError(
            409,
            "no_puedes_bloquear_este_equipo",
            "No puedes bloquear el equipo desde el que estás conectado.",
        )
    await _transicion(
        session,
        fila,
        esperado,
        {
            "estado": BLOQUEADO,
            "bloqueado_por": actor_admin_id,
            "bloqueado_at": _ahora(),
            "bloqueo_equipo": alcance == "equipo",
            "motivo": (motivo or "").strip() or None,
        },
    )
    log.warning(
        "access_device_blocked",
        tenant_id=str(tenant_id),
        device_row_id=str(fila.id),
        device=sufijo(fila.device_id),
        alcance=alcance,
    )
    return fila


async def dar_de_baja(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    device_row_id: uuid.UUID,
    esperado: str,
    actor_es_owner_o_master: bool,
) -> None:
    """Baja lógica desde `pendiente` o `activo`. Un `bloqueado` no se da de baja (409)."""
    if esperado == BLOQUEADO:
        raise AccesoError(
            409,
            "bloqueado_no_se_da_de_baja",
            "Un equipo bloqueado no se da de baja: reactívalo primero. Si no, volvería a registrarse.",
        )
    if esperado not in (PENDIENTE, ACTIVO):
        raise AccesoError(
            400, "transicion_invalida", "Solo se da de baja un equipo pendiente o activo."
        )
    fila = await cargar(session, tenant_id, device_row_id)
    await _exigir_autoridad(session, tenant_id, fila.admin_id, actor_es_owner_o_master)
    await _transicion(session, fila, esperado, {"deleted_at": _ahora()})


async def alta_manual(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    device_id: str,
    label: str | None,
    plataforma: str,
    actor_admin_id: uuid.UUID,
    actor_es_owner_o_master: bool,
) -> AccountDevice:
    """Registra un equipo ya aprobado para una cuenta del negocio (origen `alta_manual`)."""
    device_id = device_id.strip()
    if not device_id:
        raise AccesoError(400, "device_id_requerido", "El identificador del equipo es obligatorio.")
    rol = await rol_de_cuenta(session, tenant_id, admin_id)
    if rol is None:
        raise AccesoError(404, "cuenta_no_encontrada", "Esa cuenta no pertenece al negocio.")
    if rol == "master":  # el master de plataforma nunca se registra por negocio
        raise AccesoError(
            400,
            "master_no_se_registra",
            "Los admins de plataforma no registran equipos por negocio.",
        )
    await _exigir_autoridad(session, tenant_id, admin_id, actor_es_owner_o_master)
    if admin_id == actor_admin_id and not actor_es_owner_o_master:
        raise AccesoError(
            403, "no_te_autorices", "No puedes registrarte un equipo aprobado a ti mismo."
        )
    fila, creada = await _crear(
        session,
        tenant_id=tenant_id,
        admin_id=admin_id,
        device_id=device_id,
        label=(label or "").strip() or None,
        plataforma=plataforma if plataforma in PLATAFORMAS else "android",
        estado=ACTIVO,
        origen="alta_manual",
        rol=rol,
        ip=None,
        aprobado_por=actor_admin_id,
    )
    if not creada:
        raise AccesoError(
            409, "dispositivo_ya_registrado", "Esa cuenta ya tiene registrado este equipo."
        )
    if await _otra_cuenta_lo_usa(session, tenant_id, admin_id, device_id):
        registrar_evento(
            session,
            tenant_id=tenant_id,
            admin_id=admin_id,
            device_row_id=fila.id,
            device_id=device_id,
            tipo="dispositivo_compartido",
        )
    return fila

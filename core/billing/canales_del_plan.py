"""Canales por plan y topes de conexiones y correos (D-79, ADR-0208 §8-9, HU-BILL-19).

Toda alta o activación de una conexión de canal pasa por `exigir_alta_de_conexion`, en la MISMA
transacción que inserta o activa la fila:

1. **Canal del plan** (§9): el `ChannelConnection.channel` tal cual contra `features.canales` del plan.
   WhatsApp por Meta, Twilio, Plivo o QR es `whatsapp`; el chat web es `web`. Quedan exentos el correo
   (tiene su tope en `correos`), el Centro de pruebas (`test`) y lo que no conversa (Google Calendar).
2. **Tope estructural** (§8): `correos` para `email` y `conexiones` para los canales conversacionales,
   con `enforce_growth` (advisory lock del negocio y la métrica hasta el fin de la transacción).

Un negocio sin suscripción o con un plan que el catálogo no conoce no se bloquea (grandfathered, el
mismo criterio que `enforcement.quota_decision`). Bajar de plan no apaga nada: solo impide crecer.

`verificar_alta_de_conexion` es la misma pregunta sin lock, para avisar ANTES de un OAuth (Meta) y no
cortar al dueño después de completarlo; la decisión definitiva sigue siendo la del insert.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.billing.enforcement import PlanNoIncluye, QuotaExceeded, enforce_growth, quota_decision
from core.billing.metrics import CANALES_CONVERSACIONALES, metrica_de_canal
from core.billing.plans import get_plan
from core.shared.logger import get_logger

log = get_logger(__name__)

_NOMBRES = {
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
    "messenger": "Messenger",
    "telegram": "Telegram",
    "web": "el chat web",
}
# Lo que se conecta de cada canal, para el final del mensaje.
_QUE_SE_CONECTA = {
    "instagram": "tus cuentas",
    "messenger": "tus Páginas",
    "telegram": "tu bot",
}


class CanalNoIncluido(PlanNoIncluye):
    """El plan no incluye el canal (409 `plan_channel_not_included`)."""

    code = "plan_channel_not_included"

    def __init__(self, channel: str, plan_id: str | None, plan_name: str) -> None:
        self.channel = channel
        nombre = _NOMBRES.get(channel, channel)
        que = _QUE_SE_CONECTA.get(channel, "este canal")
        super().__init__(
            f"Tu plan {plan_name} no incluye {nombre}. Mejora tu plan para conectar {que}.",
            details={"channel": channel, "plan": plan_id},
        )


def canal_exento(channel: str | None) -> bool:
    """El canal no se valida contra `features.canales`: correo, Centro de pruebas, calendario."""
    return channel not in CANALES_CONVERSACIONALES


def plan_incluye_canal(plan_id: str | None, canal: str) -> bool:
    """¿El plan del catálogo incluye el canal? (`features.canales`, ADR-0208 §9). Pura: un plan vacío o
    desconocido no incluye ningún canal conversacional (la tolerancia de los negocios sin suscripción la
    aplica `canal_permitido`). Los canales exentos siempre están incluidos."""
    if canal_exento(canal):
        return True
    plan = get_plan(plan_id) if plan_id else None
    return bool(plan and canal in (plan.features.get("canales") or []))


async def _plan_del_negocio(tenant_id: uuid.UUID, session: Any = None) -> tuple[str | None, Any]:
    from core.billing.service import get_active_subscription

    sub = await get_active_subscription(tenant_id, session=session)
    if sub is None:
        return None, None
    return sub.plan_id, get_plan(sub.plan_id)


async def canal_permitido(tenant_id: uuid.UUID, channel: str, *, session: Any = None) -> bool:
    """¿El negocio puede dar de alta una conexión de `channel`? Sin suscripción o sin plan conocido, sí."""
    if canal_exento(channel):
        return True
    plan_id, plan = await _plan_del_negocio(tenant_id, session)
    if plan is None:
        return True
    return plan_incluye_canal(plan_id, channel)


async def exigir_canal(tenant_id: uuid.UUID, channel: str, *, session: Any = None) -> None:
    """Levanta `CanalNoIncluido` si el plan del negocio no incluye `channel`. Nada si lo incluye."""
    if canal_exento(channel):
        return
    plan_id, plan = await _plan_del_negocio(tenant_id, session)
    if plan is None or plan_incluye_canal(plan_id, channel):
        return
    log.info("plan_channel_not_included", tenant_id=str(tenant_id), channel=channel, plan=plan_id)
    raise CanalNoIncluido(channel, plan_id, plan.name)


async def exigir_alta_de_conexion(session: Any, tenant_id: uuid.UUID, channel: str | None) -> None:
    """Exige el canal del plan y el tope (`conexiones` o `correos`) para una conexión NUEVA o que pasa a
    activa. `session` es la del caller, abierta en el `tenant_scope` del negocio, y es la que inserta o
    activa la fila: el lock de `enforce_growth` dura hasta su commit, así dos altas simultáneas con un solo
    cupo no entran las dos. Quien llama decide antes si la fila es una unidad nueva (volver a ligar la
    misma fila canónica, o editar una conexión que ya estaba activa, no lo es)."""
    channel = channel or ""
    await exigir_canal(tenant_id, channel, session=session)
    metrica = metrica_de_canal(channel)
    if metrica is not None:
        await enforce_growth(tenant_id, metrica, session=session)


async def verificar_alta_de_conexion(tenant_id: uuid.UUID, channel: str, *, session: Any = None) -> None:
    """La misma pregunta que `exigir_alta_de_conexion`, sin lock y sin escribir: para avisar antes de
    empezar un OAuth (Meta). Levanta `CanalNoIncluido` o `QuotaExceeded`."""
    await exigir_canal(tenant_id, channel, session=session)
    metrica = metrica_de_canal(channel)
    if metrica is None:
        return
    decision = await quota_decision(tenant_id, metrica, session=session)
    if not decision.allowed and decision.limit is not None:
        raise QuotaExceeded(metrica, decision.used, decision.limit)


def codigo_y_mensaje(exc: QuotaExceeded | PlanNoIncluye) -> tuple[str, str]:
    """`(code, message)` del 409 para las altas que informan el tope sin pasar por la API (Páginas o cuentas
    omitidas, errores de dominio de un alta). Los textos son los de `core/api/plan_limits.py`."""
    if isinstance(exc, PlanNoIncluye):
        return exc.code, exc.message
    from core.api.plan_limits import plan_limit_code, plan_limit_message

    return plan_limit_code(exc.metric), plan_limit_message(exc)


__all__ = [
    "CanalNoIncluido",
    "canal_exento",
    "canal_permitido",
    "codigo_y_mensaje",
    "exigir_alta_de_conexion",
    "exigir_canal",
    "plan_incluye_canal",
    "verificar_alta_de_conexion",
]

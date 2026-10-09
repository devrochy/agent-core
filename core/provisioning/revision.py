"""Altas en revisión: aprobar y descartar (HU-SH-07, ADR-0125 § 6).

Lo usan el master desde Plataforma (`core/api/routers/platform_altas.py`) y la CLI. Este
módulo NO corre el alta: aprobar deja la fila `en_curso` con la aprobación y emite
`provisioning.alta_aprobada` por el outbox, en la misma transacción. El consumidor la corre
en el worker (`core.provisioning.consumer.on_alta_aprobada`), con los reintentos del outbox
y, si algo falla tres veces, de vuelta en revisión. Así ninguna ruta de la API llega al
consumidor.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB

from core.db.models import ProvisioningRequest
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.events import ON_ALTA_APROBADA
from core.shared.tenant import tenant_scope


def clave_del_candado(registro_id: uuid.UUID) -> int:
    """Clave del candado consultivo de un alta: el consumidor la toma mientras la procesa."""
    return int.from_bytes(
        hashlib.sha256(f"provisioning:{registro_id}".encode()).digest()[:8], "big", signed=True
    )


async def por_referencia(vendedor: uuid.UUID, referencia: str) -> ProvisioningRequest | None:
    async with tenant_scope(vendedor):
        async with get_session() as session:
            return (
                await session.execute(
                    select(ProvisioningRequest).where(
                        ProvisioningRequest.tenant_id == vendedor,
                        ProvisioningRequest.referencia == referencia,
                        ProvisioningRequest.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()


async def aprobar_alta(vendedor: uuid.UUID, referencia: str, *, por: str) -> ProvisioningRequest:
    """Una persona revisó un alta **en revisión** y la aprueba: corre sin los controles de
    riesgo (interruptor, aceptación, un Free por persona, señales, Free repetido y recompra).
    Las validaciones de datos (plan, correo, pedido pagado, cliente, owner) siguen.

    La aprobación es de un solo uso: si el alta vuelve a revisión, o se reintenta, se borra.
    Queda en `audit_log` con quién aprobó y por qué estaba en revisión."""
    from core.events.outbox import emit_domain

    registro = await por_referencia(vendedor, referencia)
    if registro is None:
        raise ValueError(f"No hay un alta con la referencia {referencia!r} en ese vendedor.")
    if registro.estado != "revision":
        raise ValueError(f"El alta está {registro.estado}: solo se aprueba una en revisión.")
    if not (registro.detalle or {}).get("revision_por_riesgo"):
        # Frenada por un dato (correo inválido, cliente distinto, pedido sin pagar…): aprobarla
        # saltaría controles de riesgo que nunca se evaluaron. Se corrige y se reintenta.
        raise ValueError(
            "Esta alta está en revisión por un dato, no por un control de riesgo: corrígelo y "
            "reintenta (agent-core tenant reintentar-alta)."
        )
    aprobacion = {
        "id": str(uuid.uuid4()),
        "por": (por or "desconocido")[:254],
        "en": datetime.now(UTC).isoformat(),
        "motivo_revision": registro.motivo,
    }
    async with tenant_scope(vendedor):
        async with get_session() as session:
            # Condicional y atómico: si otro proceso ya la movió, no se aprueba nada, y la
            # aprobación se suma al detalle sin pisar lo que esté escribiendo otro.
            cambiadas = (
                await session.execute(
                    update(ProvisioningRequest)
                    .where(
                        ProvisioningRequest.id == registro.id,
                        ProvisioningRequest.tenant_id == vendedor,
                        ProvisioningRequest.estado == "revision",
                    )
                    .values(
                        estado="en_curso",
                        intentos=0,
                        motivo=None,
                        detalle=ProvisioningRequest.detalle.op("||")(
                            bindparam("aprobacion", {"aprobacion": aprobacion}, type_=JSONB)
                        ),
                        updated_at=func.now(),
                    )
                )
            ).rowcount
            if not cambiadas:
                raise ValueError("El alta cambió de estado mientras tanto: vuelve a cargar la cola.")
            await emit_domain(
                session,
                tenant_id=vendedor,
                topic=ON_ALTA_APROBADA,
                dedupe_key=f"alta_aprobada:{registro.id}:{aprobacion['id']}",
                payload={"referencia": referencia},
            )
        await record_audit(
            actor=aprobacion["por"],
            action="aprovisionamiento_aprobado",
            input_data={"referencia": referencia, "motivo_revision": registro.motivo},
        )
    actualizado = await por_referencia(vendedor, referencia)
    return actualizado or registro


async def descartar_alta(
    vendedor: uuid.UUID, referencia: str, motivo: str, *, por: str = "cli"
) -> ProvisioningRequest:
    """Cierra un alta en `revision` sin crear el negocio (`omitido`, queda el motivo). Un
    evento repetido del mismo pedido ya no la reabre."""
    registro = await por_referencia(vendedor, referencia)
    if registro is None:
        raise ValueError(f"No hay un alta con la referencia {referencia!r} en ese vendedor.")
    if registro.estado not in ("revision", "en_curso"):
        raise ValueError(f"El alta está {registro.estado}: solo se descarta una en revisión.")
    texto = (motivo or "").strip() or "descartada por el master"
    async with tenant_scope(vendedor):
        async with get_session() as session:
            # El mismo candado que toma el consumidor: no se descarta un alta que un worker está
            # creando (si no, el negocio se crearía igual y `completado` pisaría `omitido`).
            tomado = (
                await session.execute(
                    text("select pg_try_advisory_xact_lock(:clave)"), {"clave": clave_del_candado(registro.id)}
                )
            ).scalar()
            if not tomado:
                raise ValueError("El alta se está procesando ahora: espera un minuto y vuelve a intentarlo.")
            cambiadas = (
                await session.execute(
                    update(ProvisioningRequest)
                    .where(
                        ProvisioningRequest.id == registro.id,
                        ProvisioningRequest.tenant_id == vendedor,
                        ProvisioningRequest.estado.in_(("revision", "en_curso")),
                    )
                    .values(estado="omitido", motivo=f"descartada: {texto}"[:500], updated_at=func.now())
                )
            ).rowcount
            if not cambiadas:
                raise ValueError("El alta cambió de estado mientras tanto: vuelve a cargar la cola.")
        await record_audit(
            actor=por,
            action="aprovisionamiento_descartado",
            input_data={"referencia": referencia, "estado_anterior": registro.estado},
            output_data={"motivo": texto},
        )
    actualizado = await por_referencia(vendedor, referencia)
    return actualizado or registro


__all__ = ["aprobar_alta", "clave_del_candado", "descartar_alta", "por_referencia"]

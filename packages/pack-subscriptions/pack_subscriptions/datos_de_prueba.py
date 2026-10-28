"""Datos de prueba de Suscripciones (ajuste de HU-QA-10).

La forma `suscripciones` crea clientes ficticios y los suscribe a los planes que Comercio
sembró para el mismo lote (`ContextoDeNegocio.planes`, con el snapshot del plan). No lee el
catálogo de Comercio (invariante 5) ni usa teléfonos o correos reales: los clientes nacen con
canal `web` y un `external_id` del lote.

Deja una mezcla útil para la pantalla: una suscripción activa y pagada, una por vencer con
cargo pendiente, una vencida y una anual pagada (los casos se repiten si hay más clientes).
Retirar es borrado lógico de cargos, suscripciones y clientes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from core.db.models import Customer, utcnow
from core.db.session import get_session
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.tenant import tenant_scope
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import etiqueta_periodo, sumar_periodo

# (índice de plan, estado, días hasta el vencimiento del cargo; None = al día y pagada)
_CASOS: tuple[tuple[int, str, int | None], ...] = (
    (0, "activa", None),
    (1, "por_vencer", 2),
    (0, "vencida", -10),
    (1, "activa", None),
)


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ClienteDeSuscripcionDePrueba(_Estricto):
    nombre: str = Field(min_length=2, max_length=60)
    apellido: str = Field(default="", max_length=60)
    numero_documento: str = Field(pattern=r"^[0-9]{6,12}$", description="Documento ficticio.")


class SuscripcionesDePrueba(_Estricto):
    clientes: list[ClienteDeSuscripcionDePrueba] = Field(min_length=2, max_length=6)


def formas() -> list[FormaDeDatos]:
    respaldo = {
        "clientes": [
            {"nombre": "Ana", "apellido": "Ríos", "numero_documento": "10203040"},
            {"nombre": "Luis", "apellido": "Mora", "numero_documento": "10203041"},
            {"nombre": "Eva", "apellido": "Paz", "numero_documento": "10203042"},
            {"nombre": "Mario", "apellido": "León", "numero_documento": "10203043"},
        ]
    }
    return [
        FormaDeDatos(
            modo="suscripciones",
            titulo="Suscripciones",
            habilidad=None,
            prioridad=20,
            esquema=SuscripcionesDePrueba,
            respaldo=respaldo,
        )
    ]


async def _cliente(tenant_id: uuid.UUID, datos: ClienteDeSuscripcionDePrueba, sufijo: str, n: int) -> uuid.UUID:
    nombre = f"{datos.nombre} {datos.apellido}".strip()
    async with get_session() as session:
        cliente = Customer(
            tenant_id=tenant_id,
            channel="web",
            external_id=f"datos-prueba-{sufijo}-{n}",
            name=nombre,
        )
        session.add(cliente)
        await session.flush()
        return cliente.id


async def _cargo_pendiente(
    tenant_id: uuid.UUID, sub_id: uuid.UUID, vence: datetime, estado: str
) -> uuid.UUID:
    async with get_session() as session:
        sub = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == tenant_id,
                    Subscription.id == sub_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        cargo = SubscriptionCharge(
            subscription_id=sub.id,
            periodo=etiqueta_periodo(vence, sub.periodicidad),
            monto=Decimal(sub.valor_periodo or 0),
            estado="pendiente",
            tipo="periodo",
            fecha_vencimiento=vence,
        )
        session.add(cargo)
        await session.flush()
        sub.proximo_cargo = sumar_periodo(vence, sub.periodicidad, sub.dia_ancla)
        if estado == "vencida":
            sub.estado = "vencida"
        return cargo.id


async def sembrar(
    tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
) -> list[RegistroSembrado]:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    assert isinstance(datos, SuscripcionesDePrueba)
    if not contexto.planes:
        return []
    capacidad = SubscriptionCapabilityImpl()
    ahora = datetime.now(UTC)
    creados: list[RegistroSembrado] = []
    async with tenant_scope(tenant_id):
        for n, cliente_datos in enumerate(datos.clientes):
            customer_id = await _cliente(tenant_id, cliente_datos, contexto.sufijo, n + 1)
            creados.append(RegistroSembrado(kind="cliente", ref=customer_id))
            indice_plan, estado, dias = _CASOS[n % len(_CASOS)]
            plan = contexto.planes[indice_plan % len(contexto.planes)]
            sub = await crear_suscripcion_para(
                customer_id=customer_id,
                variante_id=plan.variant_id,
                plan=dict(plan.plan),
                reference=f"datos-prueba:{contexto.sufijo}:{n + 1}",
            )
            creados.append(RegistroSembrado(kind="suscripcion", ref=sub.id))
            if dias is None:
                inicio = sub.periodo_actual_inicio or ahora
                pago = await capacidad.registrar_pago_externo(
                    sub.id,
                    inicio=inicio,
                    fin=sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla),
                    referencia=f"QA-PAGO-{contexto.sufijo}-{n + 1}",
                    canal="transferencia",
                )
                creados.append(RegistroSembrado(kind="cargo", ref=uuid.UUID(str(pago["charge_id"]))))
            else:
                cargo_id = await _cargo_pendiente(
                    tenant_id, sub.id, ahora + timedelta(days=dias), estado
                )
                creados.append(RegistroSembrado(kind="cargo", ref=cargo_id))
    return creados


async def retirar(tenant_id: uuid.UUID, registros: list[RegistroSembrado]) -> int:
    cargos = [r.ref for r in registros if r.kind == "cargo"]
    suscripciones = [r.ref for r in registros if r.kind == "suscripcion"]
    clientes = [r.ref for r in registros if r.kind == "cliente"]
    total = 0
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if cargos:
                res = await session.execute(
                    update(SubscriptionCharge)
                    .where(
                        SubscriptionCharge.tenant_id == tenant_id,
                        SubscriptionCharge.id.in_(cargos),
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                    .values(deleted_at=utcnow())
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
            if suscripciones:
                res = await session.execute(
                    update(Subscription)
                    .where(
                        Subscription.tenant_id == tenant_id,
                        Subscription.id.in_(suscripciones),
                        Subscription.deleted_at.is_(None),
                    )
                    .values(deleted_at=utcnow())
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
            if clientes:
                res = await session.execute(
                    update(Customer)
                    .where(
                        Customer.tenant_id == tenant_id,
                        Customer.id.in_(clientes),
                        Customer.deleted_at.is_(None),
                    )
                    .values(deleted_at=utcnow())
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
    return total


__all__ = ["SuscripcionesDePrueba", "formas", "retirar", "sembrar"]

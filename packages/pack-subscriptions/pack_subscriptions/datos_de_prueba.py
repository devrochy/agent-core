"""Datos de prueba de Suscripciones (HU-QA-10, ADR-0146; planes propios desde SPEC-43).

Dos formas, las dos de este pack:
- `planes`: planes del catálogo con periodicidad y días de prueba (D-16: solo $0 tiene prueba). Sin
  habilidad propia: la usa la plantilla del agente que la nombra (`datos_de_prueba` en
  `core/presets/plantillas/`). Los códigos llevan el sufijo del lote (`PRUEBA-<sufijo>-NN`), así nunca
  chocan con los del negocio.
- `suscripciones`: clientes ficticios inscritos en los planes que sembró la forma anterior para el mismo
  lote (`ContextoDeNegocio.planes`, con el snapshot del plan). Los clientes nacen con canal `web` y un
  `external_id` del lote; no usan teléfonos ni correos reales.

Deja una mezcla útil para la pantalla: una suscripción activa y pagada, una por vencer con cargo
pendiente, una vencida y una anual pagada (los casos se repiten si hay más clientes).
Retirar es borrado lógico de cargos, suscripciones, clientes y planes (invariante 8).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update

from core.db.models import Customer, utcnow
from core.db.session import get_session
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.tenant import tenant_scope
from pack_subscriptions import planes as catalogo
from pack_subscriptions.models import Plan, Subscription, SubscriptionCharge
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


class PlanDePrueba(_Estricto):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=400, description="Qué incluye el plan.")
    price: Decimal = Field(ge=0, le=100_000_000, description="Precio por período, sin decimales si es COP.")
    periodicidad: Literal["mensual", "trimestral", "anual"]
    trial_dias: int = Field(default=0, ge=0, le=60, description="Solo un plan de precio 0 tiene días de prueba.")

    @model_validator(mode="after")
    def _prueba_solo_en_free(self) -> PlanDePrueba:
        # D-16: lo que el LLM invente de prueba en un plan de pago se ignora, no rompe el lote.
        if self.price > 0:
            self.trial_dias = 0
        return self


class PlanesDePrueba(_Estricto):
    planes: Annotated[list[PlanDePrueba], Field(min_length=2, max_length=4)]


RESPALDO_PLANES = {
    "planes": [
        {"name": "Plan Free", "description": "Plan de entrada sin costo.", "price": 0, "periodicidad": "mensual", "trial_dias": 30},
        {"name": "Plan mensual", "description": "Plan de pago con cobro mensual.", "price": 100000, "periodicidad": "mensual"},
    ]
}


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
            modo="planes",
            titulo="Planes",
            habilidad=None,
            prioridad=20,
            esquema=PlanesDePrueba,
            respaldo=RESPALDO_PLANES,
        ),
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


async def _sembrar_planes(
    tenant_id: uuid.UUID, datos: PlanesDePrueba, contexto: ContextoDeNegocio
) -> list[RegistroSembrado]:
    registros: list[RegistroSembrado] = []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for n, p in enumerate(datos.planes, start=1):
                plan = Plan(
                    tenant_id=tenant_id,
                    codigo=f"PRUEBA-{contexto.sufijo}-{n:02d}",
                    nombre=p.name,
                    descripcion=p.description,
                    precio=p.price,
                    moneda=contexto.moneda,
                    periodicidad=p.periodicidad,
                    trial_dias=p.trial_dias,
                    orden=n,
                )
                session.add(plan)
                await session.flush()
                registros.append(RegistroSembrado(kind="plan", ref=plan.id, metadata={"plan": dict(catalogo.snapshot(plan))}))
    return registros


async def sembrar(
    tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
) -> list[RegistroSembrado]:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    if modo == "planes":
        assert isinstance(datos, PlanesDePrueba)
        return await _sembrar_planes(tenant_id, datos, contexto)
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
            sub = await crear_suscripcion_para(customer_id=customer_id, plan_id=plan.plan_id)
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
    planes = [r.ref for r in registros if r.kind == "plan"]
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
            if planes:
                res = await session.execute(
                    update(Plan)
                    .where(Plan.tenant_id == tenant_id, Plan.id.in_(planes), Plan.deleted_at.is_(None))
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


__all__ = ["PlanesDePrueba", "SuscripcionesDePrueba", "formas", "retirar", "sembrar"]

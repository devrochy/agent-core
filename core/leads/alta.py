"""Alta manual e importación de leads desde el panel (Clientes › Nuevo lead / Importar).

Es una acción de una persona del equipo, no de un agente: escribe el perfil con `source="human"` o `"import"` y
crea el lead con `origen="manual"`. La etapa pasa por `core.leads.etapas.cambiar_etapa`, que deja historia.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select

from core.db.models import Customer, Lead
from core.leads.etapas import ETAPAS, cambiar_etapa, motivo_valido, nota_de_perdida
from core.leads.servicio import asegurar, vivo


class LeadYaTieneDatos(LookupError):
    """El cliente ya tiene un lead con datos; una persona puede abrirlo en vez de crear otro."""

    def __init__(self, customer_id: uuid.UUID) -> None:
        self.customer_id = customer_id
        super().__init__("Este cliente ya tiene un lead con datos.")


def _con_texto(columna: Any) -> Any:
    return func.length(func.trim(func.coalesce(columna, ""))) > 0


async def _cliente_por_contacto(
    session: Any, tenant_id: uuid.UUID, telefono: str | None, email: str | None
) -> Customer | None:
    """El cliente que ya existe con ese teléfono o correo, si lo hay (el más viejo manda)."""
    condiciones = []
    if telefono:
        condiciones.extend((Customer.contact_phone == telefono, Customer.external_id == telefono))
    if email:
        condiciones.append(Customer.external_id == email)
    if not condiciones:
        return None
    return (
        (
            await session.execute(
                select(Customer)
                .where(
                    Customer.tenant_id == tenant_id,
                    Customer.deleted_at.is_(None),
                    or_(*condiciones),
                )
                .order_by(Customer.created_at, Customer.id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def crear_desde_panel(
    session: Any,
    tenant_id: uuid.UUID,
    *,
    nombre: Any,
    telefono: str | None = None,
    email: str | None = None,
    interes: Any = "",
    estado: str | None = None,
    valor: Decimal | None = None,
    seguimiento_en: datetime | None = None,
    seguimiento_nota: str | None = None,
    motivo_perdida: str | None = None,
    nota_perdida: str | None = None,
    source: str = "human",
) -> tuple[Customer, Lead, bool, bool]:
    """Crea (o completa) el cliente y su lead desde el panel o una importación.

    Devuelve `(cliente, lead, lead_creado, cliente_creado)`. Si el cliente ya tenía un lead con datos y la fuente es
    una persona, lanza `LeadYaTieneDatos` en vez de pisarlo: para eso está su ficha. Una importación sí completa los
    campos que trae, porque es una acción deliberada sobre una lista.
    """
    nombre = " ".join(str(nombre or "").split())
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    email = str(email or "").strip().lower() or None
    telefono = str(telefono or "").strip() or None
    if not telefono and not email:
        raise ValueError("Indica un teléfono o un correo.")
    interes = " ".join(str(interes or "").split())[:500]
    if estado is not None and estado not in ETAPAS:
        raise ValueError(f"«{estado}» no es una etapa válida.")
    if valor is not None and valor < 0:
        raise ValueError("El valor estimado no puede ser negativo.")
    motivo = nota = None
    if estado == "perdido":
        motivo = motivo_valido(motivo_perdida)
        if motivo is None:
            raise ValueError("Para marcar un lead como «perdido» hay que indicar el motivo.")
        nota = nota_de_perdida(nota_perdida)
    if seguimiento_nota and len(seguimiento_nota) > 200:
        raise ValueError("La próxima acción admite hasta 200 caracteres.")

    ahora = datetime.now(UTC)
    cliente = await _cliente_por_contacto(session, tenant_id, telefono, email)
    cliente_creado = cliente is None
    if cliente is None:
        cliente = Customer(
            tenant_id=tenant_id,
            channel="manual",
            external_id=email or telefono or f"manual:{uuid.uuid4()}",
            contact_phone=telefono,
            name=nombre,
        )
        session.add(cliente)
        await session.flush()
    else:
        if nombre and not cliente.name:
            cliente.name = nombre
        if telefono and not cliente.contact_phone:
            cliente.contact_phone = telefono

    from core.memory import facts

    await facts.escribir_hechos(
        session,
        tenant_id=tenant_id,
        customer_id=cliente.id,
        namespace=facts.PERFIL,
        valores={
            "name": nombre,
            **({"phone": telefono} if telefono else {}),
            **({"email": email} if email else {}),
        },
        source=source,
    )

    lead = await vivo(session, tenant_id, cliente.id)
    lead_creado = lead is None
    if lead is None:
        lead, lead_creado = await asegurar(
            session, tenant_id, cliente.id, origen="manual", idempotency_key=f"panel:{cliente.id}"
        )
    elif source == "human" and any(
        _con_texto(columna) for columna in (lead.name, lead.phone, lead.email, lead.interest)
    ):
        raise LeadYaTieneDatos(cliente.id)

    lead.name = nombre
    if telefono:
        lead.phone = telefono
    if email:
        lead.email = email
    if interes:
        lead.interest = interes
    if valor is not None:
        lead.estimated_value = valor
    if seguimiento_en is not None:
        lead.next_action_at = seguimiento_en
    if seguimiento_nota is not None:
        lead.next_action_note = seguimiento_nota or None
    lead.last_activity_at = ahora
    await session.flush()

    # La etapa solo la escribe `core.leads.etapas.cambiar_etapa`: deja la historia y valida el motivo de la pérdida.
    if estado is not None and estado != lead.status:
        await cambiar_etapa(
            session,
            lead,
            estado,
            actor="persona",
            loss_reason=motivo,
            loss_note=nota,
        )
    return cliente, lead, lead_creado, cliente_creado

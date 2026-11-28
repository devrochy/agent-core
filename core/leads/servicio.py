"""Servicio de leads del core (ADR-0193 §4): la única puerta para leer y escribir leads.

Dos capas:
- Para el core (tools, ficha, crons, provider): funciones sobre el modelo, en la sesión del llamador.
- Para los packs: entradas por ids que devuelven `LeadVista` (nunca el ORM). Ningún pack importa `core.db.models.Lead*`
  (lo vigila `tests/test_leads_core_config.py`).

Un cliente, un lead vivo (`uq_leads_tenant_cliente`). Crear es «asegurar»: `INSERT … ON CONFLICT DO NOTHING` contra el
único parcial y volver a leer, así dos turnos a la vez o un evento repetido nunca dejan dos leads del mismo cliente.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from core.db.models import Customer, Lead
from core.leads.catalogo import ORIGENES
from core.leads.etapas import ETAPA_INICIAL, ETAPAS, motivo_valido, nota_de_perdida


class ClienteSinLead(LookupError):
    """El cliente no es del negocio (o es de prueba), o no tiene lead."""


class LeadYaTieneDatos(LookupError):
    """El cliente ya tiene un lead con datos; una persona puede abrirlo en vez de crear otro."""

    def __init__(self, customer_id: uuid.UUID) -> None:
        self.customer_id = customer_id
        super().__init__("Este cliente ya tiene un lead con datos.")


@dataclass(frozen=True)
class LeadVista:
    """Lo que un pack puede saber del lead de un cliente."""

    id: uuid.UUID
    customer_id: uuid.UUID | None
    name: str | None
    phone: str | None
    email: str | None
    interest: str
    score: int
    status: str
    origen: str | None
    estimated_value: Decimal | None
    last_activity_at: datetime | None


def vista(lead: Lead) -> LeadVista:
    return LeadVista(
        id=lead.id,
        customer_id=lead.customer_id,
        name=lead.name,
        phone=lead.phone,
        email=lead.email,
        interest=lead.interest or "",
        score=lead.score or 0,
        status=lead.status,
        origen=lead.origen,
        estimated_value=lead.estimated_value,
        last_activity_at=lead.last_activity_at,
    )


def _origen(origen: str | None) -> str | None:
    """Solo un origen del catálogo; uno desconocido queda en NULL (no se inventa)."""
    return origen if origen in ORIGENES else None


# ------------------------------------------------------------------ núcleo (sesión del llamador)
async def vivo(session: Any, tenant_id: uuid.UUID, customer_id: uuid.UUID, *, bloquear: bool = False) -> Lead | None:
    """El lead vivo del cliente, o None."""
    q = select(Lead).where(
        Lead.tenant_id == tenant_id, Lead.customer_id == customer_id, Lead.deleted_at.is_(None)
    )
    if bloquear:
        q = q.with_for_update()
    return (await session.execute(q)).scalars().first()


async def asegurar(
    session: Any,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    origen: str | None = None,
    conversation_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[Lead, bool]:
    """El lead vivo del cliente; si no tiene, lo crea en «nuevo». Devuelve `(lead, creado)`. Idempotente por cliente
    (ADR-0193 §2): una carrera con otro turno termina con el mismo lead para los dos."""
    ahora = datetime.now(UTC)
    stmt = (
        insert(Lead)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            status=ETAPA_INICIAL,
            origen=_origen(origen),
            idempotency_key=idempotency_key,
            interest="",
            score=0,
            custom={},
            last_activity_at=ahora,
            created_at=ahora,
            updated_at=ahora,
        )
        # Sin destino: cubre el único por cliente y el de la clave de idempotencia (un reintento del intake).
        .on_conflict_do_nothing()
        .returning(Lead.id)
    )
    creado = (await session.execute(stmt)).scalar_one_or_none() is not None
    if creado:
        from core.leads import config as config_leads

        await config_leads.asegurar_por_defecto(session, tenant_id)
    lead = await vivo(session, tenant_id, customer_id)
    if lead is None:
        # Solo si chocó la clave de idempotencia con el lead de OTRO cliente: es un error del llamador.
        raise RuntimeError("asegurar: la clave de idempotencia ya es de otro lead")
    return lead, creado


async def _cliente_del_negocio(session: Any, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> bool:
    """El cliente existe, es de este negocio y no está borrado. Lo bloquea en `key share`: dos ediciones a la vez no
    pisan su lead (`customer_id` es FK lógica)."""
    return (
        await session.execute(
            select(Customer.id)
            .where(Customer.tenant_id == tenant_id, Customer.id == customer_id, Customer.deleted_at.is_(None))
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none() is not None


async def vivo_o_nuevo(
    session: Any, tenant_id: uuid.UUID, customer_id: uuid.UUID, *, origen: str | None
) -> Lead | None:
    """Para editar: el lead vivo del cliente, creándolo si falta. None si el cliente no es de este negocio."""
    if not await _cliente_del_negocio(session, tenant_id, customer_id):
        return None
    lead, _ = await asegurar(session, tenant_id, customer_id, origen=origen)
    return lead


def fijar_contacto(lead: Lead, *, name: Any = None, phone: Any = None, email: Any = None, interest: Any = None) -> bool:
    """Pone en el lead los datos que llegaron (solo los no vacíos). True si cambió algo."""
    cambio = False
    for campo, valor in (("name", name), ("phone", phone), ("email", email), ("interest", interest)):
        texto = str(valor).strip() if valor is not None else ""
        if texto and getattr(lead, campo) != texto:
            setattr(lead, campo, texto)
            cambio = True
    if cambio:
        lead.last_activity_at = datetime.now(UTC)
    return cambio


def fijar_negocio(lead: Lead, negocio: str) -> bool:
    """Pone en `custom["negocio"]` el negocio que dijo el cliente (vacío no borra). True si cambió."""
    if not negocio or (lead.custom or {}).get("negocio") == negocio:
        return False
    # Diccionario nuevo: SQLAlchemy no detecta la mutación en sitio de un JSONB.
    lead.custom = {**(lead.custom or {}), "negocio": negocio}
    lead.last_activity_at = datetime.now(UTC)
    return True


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
    campos que trae, porque es una acción deliberada sobre una lista. No registra historia de etapa: la creación fija
    el estado inicial; los cambios posteriores pasan por `cambiar_etapa`.
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
    if estado is not None:
        lead.status = estado
        if estado != ETAPA_INICIAL:
            lead.stage_changed_at = ahora
        lead.loss_reason = motivo
        lead.loss_note = nota
    if valor is not None:
        lead.estimated_value = valor
    if seguimiento_en is not None:
        lead.next_action_at = seguimiento_en
    if seguimiento_nota is not None:
        lead.next_action_note = seguimiento_nota or None
    lead.last_activity_at = ahora
    await session.flush()
    return cliente, lead, lead_creado, cliente_creado


# ------------------------------------------------------------------ entradas por ids (para los packs)
async def completar_contacto(
    session: Any, tenant_id: uuid.UUID, customer_id: uuid.UUID, *, nombre: Any = None, email: Any = None,
    telefono: Any = None,
) -> bool:
    """Completa el lead EXISTENTE del cliente con lo que dio en un recorrido guiado (ahí las tools de lead no se
    ofrecen). Nunca crea otro lead ni toca la memoria del cliente; pisa con el último valor dicho. True si había lead."""
    lead = await vivo(session, tenant_id, customer_id)
    if lead is None:
        return False
    limpios = {
        campo: " ".join(str(valor or "").split())[:tope]
        for campo, valor, tope in (("name", nombre, 200), ("email", email, 254), ("phone", telefono, 40))
    }
    if limpios["email"]:
        limpios["email"] = limpios["email"].lower()
    fijar_contacto(lead, **limpios)
    lead.last_activity_at = datetime.now(UTC)
    return True



async def por_cliente(
    session: Any, tenant_id: uuid.UUID, customer_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, LeadVista]:
    """El lead vivo de cada cliente (de todos si `customer_ids` es None)."""
    q = select(Lead).where(Lead.tenant_id == tenant_id, Lead.customer_id.is_not(None), Lead.deleted_at.is_(None))
    if customer_ids is not None:
        if not customer_ids:
            return {}
        q = q.where(Lead.customer_id.in_(customer_ids))
    return {lead.customer_id: vista(lead) for lead in (await session.execute(q)).scalars()}


async def contar_vivos(session: Any, tenant_id: uuid.UUID, *, excluir_canal: str | None = None) -> int:
    """Cuántos leads vivos tiene el negocio; con `excluir_canal`, sin los de clientes de ese canal (p. ej. prueba)."""
    q = select(func.count(Lead.id)).where(Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None))
    if excluir_canal is not None:
        de_canal = select(Customer.id).where(Customer.tenant_id == tenant_id, Customer.channel == excluir_canal)
        q = q.where((Lead.customer_id.is_(None)) | (Lead.customer_id.not_in(de_canal)))
    return int((await session.execute(q)).scalar_one())


async def fijar_datos(
    session: Any, tenant_id: uuid.UUID, customer_id: uuid.UUID, *, name: Any = None, interest: Any = None
) -> LeadVista | None:
    """Edición manual del nombre o el interés (panel SSR): asegura el lead (origen `manual`). None si el cliente no es
    del negocio."""
    lead = await vivo_o_nuevo(session, tenant_id, customer_id, origen="manual")
    if lead is None:
        return None
    if name is not None:
        lead.name = str(name) or None
    if interest is not None:
        lead.interest = str(interest or "")
    lead.last_activity_at = datetime.now(UTC)
    return vista(lead)


async def registrar_desde_intake(
    session: Any,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    name: Any = None,
    phone: Any = None,
    email: Any = None,
    interest: Any = None,
    negocio: Any = None,
    idempotency_key: str | None = None,
) -> tuple[LeadVista, bool]:
    """Lead del formulario de la página (origen `landing`): si el cliente ya tiene uno vivo, se le ponen los datos que
    trae el formulario en vez de crear otro. Devuelve `(lead, creado)`. `ClienteSinLead` si el cliente no es del
    negocio."""
    if not await _cliente_del_negocio(session, tenant_id, customer_id):
        raise ClienteSinLead("Cliente no encontrado.")
    lead, creado = await asegurar(session, tenant_id, customer_id, origen="landing", idempotency_key=idempotency_key)
    fijar_contacto(lead, name=name, phone=phone, email=email, interest=interest)
    # El negocio del formulario también va a su campo (HU-AOS-70), como el que guarda `actualizar_lead`.
    fijar_negocio(lead, " ".join(str(negocio or "").split())[:200])
    return vista(lead), creado


async def sembrar_para_qa(
    session: Any,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    name: str | None,
    interest: str,
    score: int,
    status: str,
    idempotency_key: str,
) -> LeadVista:
    """Datos de prueba (escenarios de QA, HU-QA-13): un lead con etapa y puntaje fijos. Siembra directa a propósito:
    no deja historia de etapa (no es un cambio, es el estado inicial del escenario). `ClienteSinLead` si el cliente no
    es del negocio."""
    if not await _cliente_del_negocio(session, tenant_id, customer_id):
        raise ClienteSinLead("Cliente no encontrado.")
    lead, _ = await asegurar(session, tenant_id, customer_id, origen="manual", idempotency_key=idempotency_key)
    lead.name = name
    lead.interest = interest
    lead.score = score
    lead.status = status  # escritura directa permitida: siembra de QA (ver `tests/test_leads_etapa_unica.py`)
    return vista(lead)


async def marcar_seguimiento_hecho(
    session: Any,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    siguiente_en: str | None = None,
    siguiente_nota: str | None = None,
    excluir_canal: str | None = None,
) -> None:
    """Cierra el seguimiento del lead del cliente como «hecho» y, si se pide, programa el siguiente en el mismo gesto
    (HU-CRM-21). `siguiente_en` es ISO 8601: sin zona es hora del negocio. `ValueError` si la fecha o la nota no
    valen; `ClienteSinLead` si no hay a quién."""
    from core.leads import seguimiento

    q = select(Customer.id).where(
        Customer.tenant_id == tenant_id, Customer.id == customer_id, Customer.deleted_at.is_(None)
    )
    if excluir_canal is not None:
        q = q.where(Customer.channel != excluir_canal)
    if (await session.execute(q)).scalar_one_or_none() is None:
        raise ClienteSinLead("Cliente no encontrado.")
    lead = await vivo(session, tenant_id, customer_id, bloquear=True)
    if lead is None:
        raise ClienteSinLead("Este cliente no tiene un lead.")
    zona = await seguimiento.zona_del_negocio(session, tenant_id)
    siguiente = seguimiento.parsear_fecha(siguiente_en, zona) if siguiente_en else None
    seguimiento.completar(session, lead, siguiente_fecha=siguiente, siguiente_nota=siguiente_nota)


def clientes_con_datos(tenant_id: uuid.UUID, ids: list[uuid.UUID] | None = None) -> Any:
    """`SELECT customer_id` de los clientes cuyo lead vivo tiene contenido (nombre, teléfono, correo o interés). Para
    que un pack sume a sus «clientes con datos» los que solo tienen lead sin importar el modelo (ADR-0193 §4): así
    `?agente=ventas` y el botón de plantilla de la APK instalada siguen viendo al cliente que solo dejó sus datos."""
    def _con_texto(columna: Any) -> Any:
        return func.length(func.trim(func.coalesce(columna, ""))) > 0

    q = select(Lead.customer_id.label("customer_id")).where(
        Lead.tenant_id == tenant_id,
        Lead.customer_id.is_not(None),
        Lead.deleted_at.is_(None),
        _con_texto(Lead.name) | _con_texto(Lead.phone) | _con_texto(Lead.email) | _con_texto(Lead.interest),
    )
    if ids is not None:
        q = q.where(Lead.customer_id.in_(ids))
    return q

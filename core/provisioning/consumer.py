"""Consumidor de `subscription.activated` que da de alta negocios (HU-SH-03, ADR-0121 § 3).

Corre en el worker, dentro del despachador del outbox (ADR-0107), bajo el `tenant_scope`
del negocio VENDEDOR. Pasos:

1. Solo vendedores (allowlist `PROVISIONING_SELLER_TENANTS`); el resto, nada.
2. Registro idempotente en `provisioning_requests` (único por referencia y por suscripción).
3. Candado consultivo por alta: dos workers nunca trabajan la misma.
4. Datos del pedido por la fuente del vendedor (`pack_commerce`), no del texto del LLM, y
   cotejo del cliente del pedido con el de la suscripción.
5. Controles de HU-SH-07 (ADR-0125): interruptor del master, aceptación de términos
   registrada, un Free por persona y señales de riesgo; si algo falla, revisión. Un alta que
   aprobó una persona (`aprobar_alta`) los salta. Un Free solo una vez por cliente y por
   correo (ADR-0121).
6. Negocio con id y slug deterministas (`onboard_tenant`), período pagado (`core/billing`)
   y preset del vertical.
7. Owner: cuenta, invitación y correo con el enlace (solo si la cuenta la creó esta alta);
   por WhatsApp, un aviso neutro sin token.
8. Cierre: fila `completado` y `audit_log` en los dos negocios con actor `provisioning`.

Cada paso es idempotente: un reintento (o una caída a mitad) retoma la misma alta. Lo que no
va a llegar solo (falta el correo, plan desconocido…) deja el alta en `revision`; el master
la reintenta o la descarta (`agent-core tenant reintentar-alta | descartar-alta`).
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.billing.periodos import Periodo
from core.db.models import ProvisioningRequest, Tenant
from core.db.session import get_owned, get_session
from core.provisioning import (
    ACTOR,
    AltaEnRevision,
    AltaEnRevisionPorRiesgo,
    AltaOcupada,
    AltaTransitoria,
)
from core.provisioning.antiabuso import aceptacion_del_pedido, motivos_de_revision
from core.provisioning.fuentes import DatosDeAlta, fuentes_de_alta
from core.provisioning.revision import (
    aprobar_alta,
    clave_del_candado,
    descartar_alta,
    por_referencia,
)
from core.provisioning.vendedores import es_vendedor
from core.shared.audit import record_audit
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from core.shared.texto_seguro import contiene_url

log = get_logger(__name__)

# Intentos del alta antes de dejarla en revisión ante algo transitorio (el pedido aún no es
# visible, el correo no salió). Menor que los 5 del outbox: el alta siempre termina en un
# estado que alguien puede ver, en vez de quedar en la Bandeja de fallos a medias.
MAX_INTENTOS = 3
# Espacio de nombres del UUIDv5 de los negocios aprovisionados. NO cambiarlo nunca: es lo
# que hace que un reintento encuentre el negocio que ya creó.
_NAMESPACE = uuid.UUID("b8f1c0de-5a3e-4c2b-9d61-0f7e2a4c9b13")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ESPACIOS = re.compile(r"\s+")
_NO_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_NOMBRE = 80
_CORREO_RESUELTO = frozenset({"enviado", "sin_smtp"})


# ------------------------------------------------------------------ piezas puras
def referencia_del_alta(referencia: str | None, subscription_id: uuid.UUID) -> tuple[str, bool]:
    """(referencia del alta, ¿sin pedido?). El emisor manda `referencia` nula cuando la
    suscripción no viene de un pedido; entonces la suscripción es la referencia."""
    valor = (referencia or "").strip()
    if not valor:
        return f"sub:{subscription_id}", True
    return valor[:120], False


def id_del_negocio(vendedor: uuid.UUID, referencia: str) -> uuid.UUID:
    """Id determinista del negocio: un reintento retoma el mismo, nunca crea otro."""
    return uuid.uuid5(_NAMESPACE, f"{vendedor}:{referencia}")


def slug_del_negocio(nombre: str, vendedor: uuid.UUID, referencia: str) -> str:
    """Slug legible y determinista: nombre en ASCII + sufijo derivado de vendedor y pedido."""
    ascii_ = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    base = _NO_SLUG.sub("-", ascii_).strip("-")[:48].strip("-") or "negocio"
    sufijo = hashlib.sha256(f"{vendedor}:{referencia}".encode()).hexdigest()[:6]
    return f"{base}-{sufijo}"


def _limpio(valor: Any) -> str:
    texto = _ESPACIOS.sub(" ", str(valor or "")).strip()
    return "".join(c for c in texto if c.isprintable())


def nombre_del_negocio(datos: Mapping[str, Any], referencia: str) -> str:
    """Nombre del negocio del pedido; sin él, el del comprador; sin ninguno, la referencia.
    Un nombre con un enlace o un dominio se descarta: va en correos y avisos (phishing)."""
    for clave in ("negocio", "nombre"):
        valor = _limpio(datos.get(clave))
        if valor and not contiene_url(valor):
            return valor[:_MAX_NOMBRE].rstrip()
    return f"Negocio {referencia}"[:_MAX_NOMBRE]


def correo_valido(valor: Any) -> str | None:
    texto = str(valor or "").strip().lower()
    if not texto or len(texto) > 254 or not _EMAIL.match(texto):
        return None
    return texto


def huella_del_correo(email: str) -> str:
    """Huella del correo para reconocer altas repetidas sin guardar el correo en la fila."""
    return hashlib.sha256(email.encode()).hexdigest()


def _uuid(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ entrada
async def on_subscription_activated(**payload: Any) -> None:
    """Handler de `subscription.activated` en el despachador del outbox (ADR-0107).

    El tenant actual es el del evento (lo fijó la RLS al insertarlo): un negocio no puede
    emitir eventos a nombre de otro. Si no es vendedor, no lee ni escribe nada."""
    vendedor = get_current_tenant_id()
    if vendedor is None or not await es_vendedor(vendedor):
        return
    await aprovisionar(vendedor, payload)


async def aprovisionar(vendedor: uuid.UUID, payload: Mapping[str, Any]) -> ProvisioningRequest | None:
    """Da de alta (o retoma) el negocio de una suscripción activada del vendedor."""
    subscription_id = _uuid(payload.get("subscription_id"))
    if subscription_id is None:
        # Evento mal formado: reintentarlo no lo arregla.
        log.warning("provisioning_evento_invalido", tenant_id=str(vendedor))
        return None
    referencia, _sin_pedido = referencia_del_alta(payload.get("referencia"), subscription_id)
    variant_id = str(payload.get("variant_id") or "").strip() or None
    registro = await _registro(
        vendedor,
        subscription_id=subscription_id,
        referencia=referencia,
        customer_id=_uuid(payload.get("customer_id")),
        variant_id=variant_id,
        periodo=_periodo_del_evento(payload),
    )
    if registro.subscription_id != subscription_id:
        # Segunda línea de un pedido con dos planes: un pedido, un negocio.
        log.info(
            "provisioning_pedido_ya_registrado",
            tenant_id=str(vendedor),
            referencia=referencia,
            subscription_id=str(subscription_id),
        )
        return registro
    if registro.referencia != referencia:
        # Otro pedido (o un cargo de cambio) sobre una suscripción ya registrada: un cambio
        # de plan. El plan del negocio cambia y se aplica el preset pendiente (HU-SH-04). Si
        # el alta todavía corre, primero se termina; si no se puede, el outbox reintenta.
        if registro.estado == "en_curso":
            registro = await _procesar(vendedor, registro)
        await sincronizar_cambio_de_plan(vendedor, registro, payload, referencia, variant_id)
        return registro
    if registro.estado != "en_curso":
        log.info(
            "provisioning_ya_resuelto",
            tenant_id=str(vendedor),
            referencia=registro.referencia,
            estado=registro.estado,
        )
        return registro
    return await _procesar(vendedor, registro)


# ------------------------------------------------------------------ registro y candado
async def _buscar(
    vendedor: uuid.UUID, subscription_id: uuid.UUID, referencia: str
) -> ProvisioningRequest | None:
    async with get_session() as session:
        filas = (
            await session.execute(
                select(ProvisioningRequest).where(
                    ProvisioningRequest.tenant_id == vendedor,
                    ProvisioningRequest.deleted_at.is_(None),
                    or_(
                        ProvisioningRequest.subscription_id == subscription_id,
                        ProvisioningRequest.referencia == referencia,
                    ),
                )
            )
        ).scalars().all()
    por_suscripcion = [f for f in filas if f.subscription_id == subscription_id]
    return (por_suscripcion or list(filas) or [None])[0]


def _periodo_del_evento(payload: Mapping[str, Any]) -> dict[str, Any]:
    """El período en curso de la suscripción del cliente en el vendedor (HU-SH-04): el plan
    del negocio corta sus períodos el mismo día que el asistente."""
    periodo = {
        clave: payload.get(clave)
        for clave in ("periodo_inicio", "proximo_cargo", "dia_ancla")
        if payload.get(clave) is not None
    }
    return periodo


def _fecha(valor: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(valor)) if valor else None
    except ValueError:
        return None


async def _registro(
    vendedor: uuid.UUID,
    *,
    subscription_id: uuid.UUID,
    referencia: str,
    customer_id: uuid.UUID | None,
    variant_id: str | None,
    periodo: dict[str, Any] | None = None,
) -> ProvisioningRequest:
    """La fila del alta (la crea si no existe). INSERT … ON CONFLICT DO NOTHING: dos
    entregas a la vez terminan leyendo la misma fila."""
    existente = await _buscar(vendedor, subscription_id, referencia)
    if existente is not None:
        return existente
    detalle: dict[str, Any] = {"variant_id": variant_id} if variant_id else {}
    if periodo:
        detalle["periodo"] = periodo
    async with get_session() as session:
        await session.execute(
            pg_insert(ProvisioningRequest)
            .values(
                id=uuid.uuid4(),
                tenant_id=vendedor,
                referencia=referencia,
                subscription_id=subscription_id,
                customer_id=customer_id,
                estado="en_curso",
                detalle=detalle,
                intentos=0,
            )
            .on_conflict_do_nothing()
        )
    existente = await _buscar(vendedor, subscription_id, referencia)
    if existente is None:  # pragma: no cover - el INSERT o la otra entrega la dejaron
        raise AltaTransitoria(f"no se pudo registrar el alta {referencia}")
    return existente


@asynccontextmanager
async def _candado(registro_id: uuid.UUID) -> AsyncIterator[bool]:
    """Candado consultivo de transacción: se suelta solo al terminar (o si el proceso muere)."""
    async with get_session() as session:
        tomado = (
            await session.execute(
                text("select pg_try_advisory_xact_lock(:clave)"),
                {"clave": clave_del_candado(registro_id)},
            )
        ).scalar()
        yield bool(tomado)


# Las filas se leen y se escriben filtrando también por el vendedor, no solo por la RLS.
async def _fila(
    session: Any, vendedor: uuid.UUID, registro_id: uuid.UUID, *, para_escribir: bool = False
) -> ProvisioningRequest:
    if para_escribir:
        fila = (
            await session.execute(
                select(ProvisioningRequest)
                .where(ProvisioningRequest.id == registro_id, ProvisioningRequest.tenant_id == vendedor)
                .with_for_update()
            )
        ).scalar_one_or_none()
    else:
        fila = await get_owned(session, ProvisioningRequest, registro_id, vendedor)
    if fila is None:
        raise AltaTransitoria(f"la fila del alta {registro_id} no es visible")
    return fila


async def _leer(vendedor: uuid.UUID, registro_id: uuid.UUID) -> ProvisioningRequest | None:
    async with get_session() as session:
        return await get_owned(session, ProvisioningRequest, registro_id, vendedor)


async def _sumar_intento(vendedor: uuid.UUID, registro_id: uuid.UUID) -> int:
    async with get_session() as session:
        return int(
            (
                await session.execute(
                    update(ProvisioningRequest)
                    .where(
                        ProvisioningRequest.id == registro_id,
                        ProvisioningRequest.tenant_id == vendedor,
                    )
                    .values(intentos=ProvisioningRequest.intentos + 1, updated_at=func.now())
                    .returning(ProvisioningRequest.intentos)
                )
            ).scalar_one()
        )


async def _guardar_detalle(vendedor: uuid.UUID, registro_id: uuid.UUID, **campos: Any) -> None:
    async with get_session() as session:
        # FOR UPDATE: no pisa una escritura concurrente del detalle (p. ej. reintentar, que
        # borra la aprobación).
        fila = await _fila(session, vendedor, registro_id, para_escribir=True)
        fila.detalle = {**(fila.detalle or {}), **campos}


async def _guardar_negocio(
    vendedor: uuid.UUID,
    registro_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    plan_id: str,
    vertical: str | None,
) -> None:
    async with get_session() as session:
        fila = await _fila(session, vendedor, registro_id)
        fila.negocio_tenant_id = tenant_id
        fila.negocio_slug = slug
        fila.plan_id = plan_id
        fila.vertical = vertical


# ------------------------------------------------------------------ proceso
async def _procesar(vendedor: uuid.UUID, registro: ProvisioningRequest) -> ProvisioningRequest:
    async with _candado(registro.id) as tomado:
        if not tomado:
            raise AltaOcupada(f"el alta {registro.referencia} está en curso en otro worker")
        actual = await _leer(vendedor, registro.id)
        if actual is None or actual.estado != "en_curso":
            return actual or registro
        intentos = await _sumar_intento(vendedor, actual.id)
        inicio = time.monotonic()
        # Lo que se sabe del comprador para el aviso por WhatsApp, también si va a revisión.
        aviso: dict[str, Any] = {}
        try:
            return await _dar_de_alta(vendedor, actual, inicio, aviso)
        except AltaEnRevision as exc:
            fila = await _cerrar_sin_alta(
                vendedor, actual, "revision", exc.motivo, por_riesgo=isinstance(exc, AltaEnRevisionPorRiesgo)
            )
            await _avisar_al_comprador(vendedor, actual, aviso)
            return fila
        except AltaTransitoria as exc:
            if intentos >= MAX_INTENTOS:
                fila = await _cerrar_sin_alta(vendedor, actual, "revision", str(exc))
                await _avisar_al_comprador(vendedor, actual, aviso)
                return fila
            log.warning(
                "provisioning_reintento",
                tenant_id=str(vendedor),
                referencia=actual.referencia,
                intentos=intentos,
                motivo=str(exc),
            )
            raise


async def _datos_del_pedido(vendedor: uuid.UUID, registro: ProvisioningRequest) -> DatosDeAlta:
    if registro.referencia.startswith("sub:"):
        raise AltaEnRevision("la suscripción se activó sin un pedido: no hay datos para el alta")
    fuente = await fuentes_de_alta.para_negocio(vendedor)
    if fuente is None:
        raise AltaEnRevision("el vendedor no tiene una habilidad de pedidos que dé los datos del alta")
    datos = await fuente.datos_de_alta(
        registro.referencia, variant_id=(registro.detalle or {}).get("variant_id")
    )
    if datos is None:
        raise AltaTransitoria(f"el pedido {registro.referencia} todavía no es visible")
    cliente_del_pedido = _uuid(datos.get("customer_id"))
    if registro.customer_id and cliente_del_pedido and cliente_del_pedido != registro.customer_id:
        raise AltaEnRevision("el cliente del pedido no coincide con el de la suscripción")
    if not datos.get("pagado"):
        raise AltaTransitoria(f"el pedido {registro.referencia} todavía no está pagado")
    return datos


async def _free_repetido(
    vendedor: uuid.UUID, registro: ProvisioningRequest, huella: str
) -> bool:
    """¿Ya hubo un alta de este cliente o de este correo? (Free → cancelar → Free, o el mismo
    correo desde otro número): un Free más queda en revisión (antiabuso mínimo, HU-SH-07)."""
    condiciones = [ProvisioningRequest.detalle["correo"].astext == huella]
    if registro.customer_id is not None:
        condiciones.append(ProvisioningRequest.customer_id == registro.customer_id)
    async with get_session() as session:
        otra = (
            await session.execute(
                select(ProvisioningRequest.id)
                .where(
                    ProvisioningRequest.tenant_id == vendedor,
                    ProvisioningRequest.id != registro.id,
                    ProvisioningRequest.deleted_at.is_(None),
                    ProvisioningRequest.estado != "omitido",
                    or_(*condiciones),
                )
                .limit(1)
            )
        ).first()
    return otra is not None


async def _recompra(vendedor: uuid.UUID, registro: ProvisioningRequest) -> bool:
    """¿El mismo cliente ya tiene un negocio creado por otra suscripción del vendedor?"""
    if registro.customer_id is None:
        return False
    async with get_session() as session:
        otra = (
            await session.execute(
                select(ProvisioningRequest.id)
                .where(
                    ProvisioningRequest.tenant_id == vendedor,
                    ProvisioningRequest.id != registro.id,
                    ProvisioningRequest.customer_id == registro.customer_id,
                    ProvisioningRequest.estado == "completado",
                    ProvisioningRequest.negocio_tenant_id.isnot(None),
                    ProvisioningRequest.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).first()
    return otra is not None


async def _dar_de_alta(
    vendedor: uuid.UUID, registro: ProvisioningRequest, inicio: float, aviso: dict[str, Any]
) -> ProvisioningRequest:
    from core.alta_negocio import onboard_tenant
    from core.billing.plans import get_plan
    from core.provisioning.owner import (
        cuenta_del_owner,
        preparar_invitacion,
        validar_correo_del_owner,
    )

    datos = await _datos_del_pedido(vendedor, registro)
    plan_id = str(datos.get("plan") or "").strip().lower()
    if not plan_id:
        return await _cerrar_sin_alta(
            vendedor, registro, "omitido", "el pedido no vende un plan de la plataforma"
        )
    plan = get_plan(plan_id)
    if plan is None:
        raise AltaEnRevision(f"plan de la plataforma desconocido en el pedido: {plan_id}")
    email = correo_valido(datos.get("email"))
    if email is None:
        raise AltaEnRevision("el pedido no trae un correo válido del dueño")
    negocio = nombre_del_negocio(datos, registro.referencia)
    nombre = _limpio(datos.get("nombre"))[:60] or None
    if nombre and contiene_url(nombre):
        nombre = None
    aviso.update(email=email, negocio=negocio, nombre=nombre)
    huella = huella_del_correo(email)
    await _guardar_detalle(vendedor, registro.id, correo=huella)
    # El plan queda en la fila desde ya: quien revisa la cola lo ve antes de aprobar.
    async with get_session() as session:
        (await _fila(session, vendedor, registro.id)).plan_id = plan.id
    # Una persona ya revisó y aprobó esta alta: no vuelve a revisión por lo mismo (ADR-0125).
    # Si no, se juntan TODOS los motivos, así quien la revisa los ve todos antes de aprobar.
    if not (registro.detalle or {}).get("aprobacion"):
        motivos = await motivos_de_revision(
            vendedor,
            referencia=registro.referencia,
            email=email,
            gratis=plan.is_free,
            customer_id=registro.customer_id,
        )
        if plan.is_free and await _free_repetido(vendedor, registro, huella):
            motivos.append("ya hubo un alta para este cliente o este correo: un Free más se revisa")
        if not plan.is_free and await _recompra(vendedor, registro):
            # HU-SH-04: quien canceló y vuelve a comprar abre otra suscripción; crear otro
            # negocio le dejaría dos. Una persona decide si reactivar el que tenía.
            motivos.append(
                "el cliente ya tiene un negocio de un alta anterior (recompra después de cancelar): "
                "revisar si se reactiva ese negocio"
            )
        if motivos:
            raise AltaEnRevisionPorRiesgo("; ".join(motivos))
    await validar_correo_del_owner(email)
    vertical = _limpio(datos.get("vertical")).lower() or None

    tenant_id = id_del_negocio(vendedor, registro.referencia)
    slug = await _slug_propio(negocio, vendedor, registro.referencia, tenant_id)
    creado, pasos = await onboard_tenant(name=negocio, slug=slug, tenant_id=tenant_id, language="es")
    if creado != tenant_id:  # pragma: no cover - `_slug_propio` ya lo descartó
        raise AltaEnRevision(f"el slug {slug} quedó asociado a otro negocio")
    # Desde acá la fila apunta al negocio, aunque después el alta quede en revisión.
    await _guardar_negocio(
        vendedor, registro.id, tenant_id=tenant_id, slug=slug, plan_id=plan.id, vertical=vertical
    )
    if not plan.is_free:
        await _registrar_periodo(tenant_id, plan.id, slug, (registro.detalle or {}).get("periodo") or {})
    preset = await _aplicar_preset(tenant_id, vertical)
    vendedor_slug = await _slug_de(vendedor)
    # Qué versiones de términos, privacidad y DPA aceptó el dueño y cuándo (HU-SH-07).
    aceptacion = await aceptacion_del_pedido(vendedor, registro.referencia)
    await _guardar_origen(
        tenant_id,
        {
            "origen": ACTOR,
            "vendedor": vendedor_slug,
            "referencia": registro.referencia,
            "plan": plan.id,
            "vertical": vertical,
            "preset": preset,
            "aceptacion": aceptacion,
            "aprobada_por": ((registro.detalle or {}).get("aprobacion") or {}).get("por"),
        },
    )

    cuenta = await cuenta_del_owner(
        email, vendedor=vendedor, registro_id=registro.id, previo=(registro.detalle or {}).get("owner")
    )
    invitacion = await preparar_invitacion(tenant_id, email, cuenta)
    entrega = await _entregar(vendedor, registro, invitacion, negocio=negocio, nombre=nombre)
    entrega["whatsapp"] = await _avisar_al_comprador(vendedor, registro, aviso)
    return await _completar(
        vendedor,
        registro,
        tenant_id=tenant_id,
        slug=slug,
        vendedor_slug=vendedor_slug,
        negocio=negocio,
        plan_id=plan.id,
        vertical=vertical,
        preset=preset,
        invitation_id=invitacion.invitation_id if invitacion else None,
        con_enlace=bool(invitacion and invitacion.con_enlace),
        entrega=entrega,
        pasos=pasos,
        duracion_ms=int((time.monotonic() - inicio) * 1000),
        aceptacion=aceptacion,
    )


async def _slug_propio(
    nombre: str, vendedor: uuid.UUID, referencia: str, tenant_id: uuid.UUID
) -> str:
    """Slug del negocio de esta alta. Nunca uno de OTRO negocio: `onboard_tenant` es
    idempotente por slug y, si no, le agregaría un owner a un negocio que no creamos."""
    slug = slug_del_negocio(nombre, vendedor, referencia)
    async with get_session() as session:
        propio = await session.get(Tenant, tenant_id)
        if propio is not None:
            return propio.slug  # un reintento: el negocio ya existe con su slug
        ajeno = (
            await session.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()
    if ajeno is not None:
        raise AltaEnRevision(f"el slug {slug} ya es de otro negocio")
    return slug


def periodo_del_vendedor(slug: str, periodo: Mapping[str, Any], *, now: datetime | None = None) -> Periodo:
    """El período pagado en el vendedor (`periodo_inicio` → `proximo_cargo` de la suscripción
    del cliente), para que el plan del negocio corte el mismo día (HU-SH-04). Sin esos datos
    (un evento anterior a ADR-0123), un período por calendario desde hoy."""
    from core.billing.periodos import periodo_de, referencia_del_periodo

    inicio = _fecha(periodo.get("periodo_inicio"))
    fin = _fecha(periodo.get("proximo_cargo"))
    if inicio is None or fin is None or fin <= inicio:
        return periodo_de(slug, now or datetime.now(UTC), desde_cero=True)
    return Periodo(inicio=inicio, fin=fin, referencia=referencia_del_periodo(slug, inicio), desde_cero=True)


def _ancla(periodo: Mapping[str, Any]) -> int | None:
    valor = periodo.get("dia_ancla")
    return int(valor) if isinstance(valor, int) and 1 <= valor <= 31 else None


async def _registrar_periodo(tenant_id: uuid.UUID, plan_id: str, slug: str, periodo: Mapping[str, Any]) -> None:
    """Plan pago: el período pagado por el pedido, con su pago en `subscription_payments`,
    lo escribe `core/billing` (idempotente por la referencia del período). Los bordes del
    período son los de la suscripción del cliente en el vendedor (HU-SH-04). Free se queda
    con la prueba de 30 días que dejó el alta (D-16)."""
    from core.billing.service import registrar_periodo_pagado

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await registrar_periodo_pagado(
                session,
                tenant_id,
                plan_id,
                periodo=periodo_del_vendedor(slug, periodo),
                dia_ancla=_ancla(periodo),
            )


async def _aplicar_preset(tenant_id: uuid.UUID, vertical: str | None) -> dict[str, Any]:
    """Preset del vertical declarado (ADR-0110), sin datos de ejemplo. Si no se puede
    aplicar (por ejemplo, Free: ningún preset cabe en ese plan), el negocio se crea igual y
    el preset queda pendiente con el motivo; al subir de plan lo aplica HU-SH-04."""
    from core.presets import PresetError, load_preset
    from core.presets.apply import apply_preset

    if not vertical:
        return {"id": None, "estado": "sin_vertical"}
    try:
        load_preset(vertical)
        resultado = await apply_preset(tenant_id, vertical, sembrar_ejemplos=False)
    except PresetError as exc:
        return {"id": vertical, "estado": "pendiente", "detalle": str(exc)[:300]}
    except Exception as exc:  # el alta no se cae por un pack que no activa
        log.exception("provisioning_preset_fallido", tenant_id=str(tenant_id), preset=vertical)
        return {"id": vertical, "estado": "pendiente", "detalle": f"error al aplicarlo ({type(exc).__name__})"}
    return {
        "id": vertical,
        "estado": "aplicado",
        "version": resultado.version,
        "pendientes": resultado.pendientes,
    }


async def _slug_de(tenant_id: uuid.UUID) -> str:
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    return tenant.slug if tenant else str(tenant_id)


async def _guardar_origen(tenant_id: uuid.UUID, origen: dict[str, Any]) -> None:
    """`tenants.settings["alta"]`: de dónde salió el negocio (para el onboarding, HU-SH-06)."""
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return
        settings = dict(tenant.settings or {})
        anterior = dict(settings.get("alta") or {})
        settings["alta"] = {**origen, "creado_en": anterior.get("creado_en") or datetime.now(UTC).isoformat()}
        tenant.settings = settings


async def _entregar(
    vendedor: uuid.UUID,
    registro: ProvisioningRequest,
    invitacion: Any,
    *,
    negocio: str,
    nombre: str | None,
) -> dict[str, Any]:
    """El correo (con el enlace o la invitación en la app) sale una sola vez: su estado
    queda en la fila al enviarse y un reintento no lo repite."""
    from core.provisioning.owner import entregar_por_correo

    if invitacion is None:
        return {"estado": "ya_es_miembro"}
    estado = dict((registro.detalle or {}).get("invitacion") or {})
    if estado.get("correo") not in _CORREO_RESUELTO:
        estado["correo"] = await entregar_por_correo(invitacion, negocio=negocio, nombre=nombre)
        await _guardar_detalle(vendedor, registro.id, invitacion=estado)
    return estado


async def _avisar_al_comprador(
    vendedor: uuid.UUID, registro: ProvisioningRequest, aviso: Mapping[str, Any]
) -> str | None:
    """El mismo aviso por WhatsApp, haya terminado el alta o haya quedado en revisión (sin
    enumeración). Sin correo válido no hay nada que avisar. Best-effort."""
    from core.provisioning.owner import avisar_por_whatsapp

    if not aviso.get("email"):
        return None
    try:
        return await avisar_por_whatsapp(
            registro.customer_id,
            negocio=str(aviso.get("negocio") or ""),
            nombre=aviso.get("nombre"),
            email=str(aviso["email"]),
            aviso_key=f"provisioning_aviso:{registro.id}",
        )
    except Exception:
        log.warning(
            "provisioning_aviso_fallido",
            tenant_id=str(vendedor),
            referencia=registro.referencia,
            exc_info=True,
        )
        return "no_enviado"


# ------------------------------------------------------------------ cierre
async def _completar(
    vendedor: uuid.UUID,
    registro: ProvisioningRequest,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    vendedor_slug: str,
    negocio: str,
    plan_id: str,
    vertical: str | None,
    preset: dict[str, Any],
    invitation_id: uuid.UUID | None,
    con_enlace: bool,
    entrega: dict[str, Any],
    pasos: list[str],
    duracion_ms: int,
    aceptacion: dict[str, Any] | None = None,
) -> ProvisioningRequest:
    aviso = None
    if entrega.get("estado") != "ya_es_miembro" and entrega.get("correo") != "enviado":
        aviso = "el acceso no salió por correo (sin SMTP): hay que enviárselo al dueño"
    async with get_session() as session:
        fila = await _fila(session, vendedor, registro.id)
        fila.estado = "completado"
        fila.plan_id = plan_id
        fila.vertical = vertical
        fila.negocio_tenant_id = tenant_id
        fila.negocio_slug = slug
        fila.invitation_id = invitation_id
        fila.motivo = aviso
        fila.detalle = {
            **(fila.detalle or {}),
            "negocio": negocio,
            "preset": preset,
            "invitacion": {**entrega, "con_enlace": con_enlace},
            "pasos": pasos[-25:],
            "duracion_ms": duracion_ms,
        }
    entrada = {
        "referencia": registro.referencia,
        "plan": plan_id,
        "preset": preset.get("id"),
        "vertical": vertical,
    }
    salida = {
        "negocio_tenant_id": str(tenant_id),
        "slug": slug,
        "preset_estado": preset.get("estado"),
        "invitation_id": str(invitation_id) if invitation_id else None,
        "invitacion": entrega,
    }
    await record_audit(
        actor=ACTOR,
        action="negocio_aprovisionado",
        input_data={
            **entrada,
            "subscription_id": str(registro.subscription_id) if registro.subscription_id else None,
        },
        output_data=salida,
    )
    # En el negocio nuevo, sin ids internos del vendedor (su suscripción no es de este negocio).
    async with tenant_scope(tenant_id):
        await record_audit(
            actor=ACTOR,
            action="negocio_creado",
            input_data={**entrada, "vendedor": vendedor_slug, "aceptacion": aceptacion},
            output_data={k: v for k, v in salida.items() if k != "negocio_tenant_id"},
        )
    log.info(
        "provisioning_completado",
        tenant_id=str(vendedor),
        referencia=registro.referencia,
        negocio_tenant_id=str(tenant_id),
        plan=plan_id,
        preset=preset.get("id"),
        preset_estado=preset.get("estado"),
        invitacion=entrega,
        duracion_ms=duracion_ms,
    )
    if aviso:
        await _avisar_a_los_duenos(vendedor, "Alta sin acceso enviado", f"{negocio}: {aviso}", registro)
    return fila


async def _cerrar_sin_alta(
    vendedor: uuid.UUID,
    registro: ProvisioningRequest,
    estado: str,
    motivo: str,
    *,
    por_riesgo: bool = False,
) -> ProvisioningRequest:
    async with get_session() as session:
        fila = await _fila(session, vendedor, registro.id, para_escribir=True)
        fila.estado = estado
        fila.motivo = motivo[:500]
        # La aprobación es de un solo uso: si el alta vuelve a revisión, una persona la tiene
        # que volver a mirar con el motivo nuevo. Y solo se aprueba una revisión por riesgo:
        # una por un dato se corrige y se reintenta (ADR-0125 § 6).
        detalle = {k: v for k, v in (fila.detalle or {}).items() if k not in ("aprobacion", "revision_por_riesgo")}
        if por_riesgo:
            detalle["revision_por_riesgo"] = True
        fila.detalle = detalle
    await record_audit(
        actor=ACTOR,
        action="aprovisionamiento_en_revision" if estado == "revision" else "aprovisionamiento_omitido",
        input_data={
            "referencia": registro.referencia,
            "subscription_id": str(registro.subscription_id) if registro.subscription_id else None,
        },
        output_data={"motivo": motivo},
    )
    if estado == "revision":
        log.warning("provisioning_en_revision", tenant_id=str(vendedor), referencia=registro.referencia, motivo=motivo)
        await _avisar_a_los_duenos(
            vendedor, "Alta automática en revisión", f"Pedido {registro.referencia}: {motivo}", registro
        )
    else:
        log.info("provisioning_omitido", tenant_id=str(vendedor), referencia=registro.referencia, motivo=motivo)
    return fila


async def sincronizar_cambio_de_plan(
    vendedor: uuid.UUID,
    registro: ProvisioningRequest,
    payload: Mapping[str, Any],
    referencia: str,
    variant_id: str | None,
) -> dict[str, Any]:
    """Cambio de plan en el asistente de un negocio ya aprovisionado (HU-SH-04, ADR-0123).

    La suscripción del cliente en el vendedor cambió de plan (Free → pago con un pedido nuevo
    o con `sub_cambiar_plan`, o un upgrade pagado): el plan del negocio cambia, SIN crear otro
    negocio, y se aplica el preset que quedó pendiente porque no cabía en el plan anterior
    (ADR-0110 § 3.1).

    - Desde Free (en prueba o vencida): período pagado desde el cambio, con los bordes del
      vendedor y su referencia (`registrar_periodo_pagado`, idempotente).
    - Entre planes pagos: el plan cambia en el acto y el período sigue (el vendedor ya cobró la
      diferencia). Un downgrade llega con el cargo del período siguiente.

    Queda auditado en los dos negocios. Idempotente: un reintento no mueve el período ni vuelve
    a aplicar el preset.
    """
    from core.billing.canales import plan_del_cargo, por_debajo_del_precio, registrar_monto_distinto
    from core.billing.periodos import tiene_periodo_pago
    from core.billing.plans import get_plan
    from core.billing.service import registrar_periodo_pagado
    from core.db.models import TenantSubscription

    resultado: dict[str, Any] = {"negocio_tenant_id": str(registro.negocio_tenant_id) if registro.negocio_tenant_id else None}
    negocio = registro.negocio_tenant_id
    plan_id = await plan_del_cargo(vendedor, variant_id)
    plan = get_plan(plan_id) if plan_id else None
    if registro.estado != "completado" or negocio is None:
        resultado["estado"] = f"sin_negocio ({registro.estado})"
    elif plan is None:
        resultado["estado"] = "plan_desconocido"
    elif plan.is_free or payload.get("estado") != "activa":
        resultado["estado"] = "sin_cambio"  # a Free o sin pagar todavía: nada que aplicar
    elif por_debajo_del_precio(plan.id, payload.get("valor_periodo")):
        # La suscripción del vendedor quedó con un precio menor que el del plan (p. ej. creada
        # a mano): ese plan no está pagado. No se aplica y se alerta.
        resultado.update(estado="monto_distinto", plan=plan.id, valor_periodo=payload.get("valor_periodo"))
        slug = registro.negocio_slug or await _slug_de(negocio)
        await registrar_monto_distinto(
            negocio,
            periodo_del_vendedor(slug, _periodo_del_evento(payload)),
            plan_id=plan.id,
            monto=payload.get("valor_periodo"),
            origen=f"cambio de plan {referencia}",
            vendedor=vendedor,
        )
    else:
        slug = registro.negocio_slug or await _slug_de(negocio)
        async with tenant_scope(negocio):
            async with get_session() as session:
                sub = (
                    await session.execute(
                        select(TenantSubscription)
                        .where(TenantSubscription.tenant_id == negocio, TenantSubscription.deleted_at.is_(None))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                anterior = sub.plan_id if sub is not None else None
                if sub is None or not tiene_periodo_pago(sub.plan_id, sub.status):
                    periodo = periodo_del_vendedor(slug, _periodo_del_evento(payload))
                    pago = await registrar_periodo_pagado(
                        session, negocio, plan.id, periodo=periodo, dia_ancla=_ancla(_periodo_del_evento(payload))
                    )
                    resultado.update(estado="periodo_pagado", referencia=periodo.referencia, pago=pago.resultado)
                else:
                    sub.plan_id = plan.id
                    resultado["estado"] = "plan_cambiado"
        resultado.update(plan_anterior=anterior, plan=plan.id)
        resultado["preset"] = await _aplicar_preset_pendiente(vendedor, registro, negocio)
        async with get_session() as session:
            fila = await _fila(session, vendedor, registro.id)
            fila.plan_id = plan.id
        async with tenant_scope(negocio):
            await record_audit(
                actor=ACTOR,
                action="plan_cambiado_por_el_asistente",
                input_data={"referencia": referencia, "plan": plan.id},
                output_data={k: v for k, v in resultado.items() if k != "negocio_tenant_id"},
            )
    await record_audit(
        actor=ACTOR,
        action="aprovisionamiento_cambio_de_plan",
        input_data={
            "referencia": referencia,
            "referencia_del_alta": registro.referencia,
            "subscription_id": str(registro.subscription_id) if registro.subscription_id else None,
            "variant_id": variant_id,
        },
        output_data=resultado,
    )
    log.info(
        "provisioning_cambio_de_plan",
        tenant_id=str(registro.tenant_id),
        referencia=referencia,
        referencia_del_alta=registro.referencia,
        estado=resultado.get("estado"),
        plan=resultado.get("plan"),
    )
    return resultado


async def _aplicar_preset_pendiente(
    vendedor: uuid.UUID, registro: ProvisioningRequest, negocio: uuid.UUID
) -> dict[str, Any] | None:
    """El preset que quedó pendiente en el alta (no cabía en el plan, ADR-0110 § 3.1) se
    aplica al subir de plan. Si ya estaba aplicado o no hay vertical, no hace nada."""
    async with get_session() as session:
        tenant = await session.get(Tenant, negocio)
    alta = dict(((tenant.settings or {}) if tenant else {}).get("alta") or {})
    previo = dict(alta.get("preset") or (registro.detalle or {}).get("preset") or {})
    vertical = registro.vertical or alta.get("vertical")
    if not vertical or previo.get("estado") != "pendiente":
        return previo or None
    preset = await _aplicar_preset(negocio, vertical)
    await _guardar_origen(negocio, {**alta, "preset": preset})
    await _guardar_detalle(vendedor, registro.id, preset=preset)
    log.info("provisioning_preset_al_subir_de_plan", negocio_tenant_id=str(negocio), estado=preset.get("estado"))
    return preset


async def _avisar_a_los_duenos(
    vendedor: uuid.UUID, titulo: str, cuerpo: str, registro: ProvisioningRequest
) -> None:
    """Push a los dueños del vendedor (best-effort, igual que commerce)."""
    try:
        from core.tasks.push_tasks import notify_owners_push

        notify_owners_push.delay(
            str(vendedor),
            titulo,
            cuerpo[:240],
            {"kind": "aprovisionamiento", "provisioning_id": str(registro.id)},
        )
    except Exception:
        log.warning("provisioning_push_fallido", tenant_id=str(vendedor), exc_info=True)


# ------------------------------------------------------------------ operación (master)
async def on_alta_aprobada(**payload: Any) -> None:
    """Handler de `provisioning.alta_aprobada` (ADR-0125 § 6): una persona aprobó un alta en
    revisión y el alta corre en el worker, con los reintentos del outbox. Bajo el
    `tenant_scope` del vendedor (el del evento); si ya no es vendedor, no hace nada."""
    vendedor = get_current_tenant_id()
    if vendedor is None or not await es_vendedor(vendedor):
        return
    registro = await por_referencia(vendedor, str(payload.get("referencia") or ""))
    if registro is None or registro.estado != "en_curso" or not (registro.detalle or {}).get("aprobacion"):
        return  # ya terminó, se descartó o volvió a revisión: nada que hacer
    await _procesar(vendedor, registro)


async def reintentar_alta(vendedor: uuid.UUID, referencia: str) -> ProvisioningRequest:
    """Vuelve a correr un alta en `revision` (o trabada `en_curso`) después de corregir el
    dato que faltaba (runbook `docs/runbooks/altas-en-revision.md`). Retoma el mismo negocio.
    Vuelve a aplicar TODOS los controles: borra una aprobación anterior (ADR-0125 § 6)."""
    registro = await por_referencia(vendedor, referencia)
    if registro is None:
        raise ValueError(f"No hay un alta con la referencia {referencia!r} en ese vendedor.")
    if registro.estado not in ("revision", "en_curso"):
        raise ValueError(f"El alta está {registro.estado}: solo se reintenta una en revisión.")
    async with tenant_scope(vendedor):
        async with get_session() as session:
            await session.execute(
                update(ProvisioningRequest)
                .where(
                    ProvisioningRequest.id == registro.id,
                    ProvisioningRequest.tenant_id == vendedor,
                    ProvisioningRequest.estado.in_(("revision", "en_curso")),
                )
                .values(
                    estado="en_curso",
                    intentos=0,
                    motivo=None,
                    detalle=ProvisioningRequest.detalle.op("-")("aprobacion").op("-")("revision_por_riesgo"),
                    updated_at=func.now(),
                )
            )
        await record_audit(
            actor="cli",
            action="aprovisionamiento_reintentado",
            input_data={"referencia": referencia, "estado_anterior": registro.estado},
        )
        actualizado = await por_referencia(vendedor, referencia)
        resultado = await _procesar(vendedor, actualizado or registro)
    return resultado


async def aprobar_y_procesar(vendedor: uuid.UUID, referencia: str, *, por: str) -> ProvisioningRequest:
    """CLI: aprueba (ver `core.provisioning.revision.aprobar_alta`) y corre el alta ya, sin
    esperar al despachador. El evento del outbox llega después y no hace nada (el alta ya no
    está `en_curso`). Si algo transitorio falla, el evento la reintenta."""
    registro = await aprobar_alta(vendedor, referencia, por=por)
    async with tenant_scope(vendedor):
        return await _procesar(vendedor, registro)


__all__ = [
    "MAX_INTENTOS",
    "aprobar_alta",
    "aprobar_y_procesar",
    "aprovisionar",
    "correo_valido",
    "descartar_alta",
    "huella_del_correo",
    "id_del_negocio",
    "nombre_del_negocio",
    "on_alta_aprobada",
    "on_subscription_activated",
    "referencia_del_alta",
    "reintentar_alta",
    "slug_del_negocio",
]

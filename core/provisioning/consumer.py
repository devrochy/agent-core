"""Consumidor de `subscription.activated` que da de alta negocios (HU-SH-03, ADR-0121 § 3).

Corre en el worker, dentro del despachador del outbox (ADR-0107), bajo el `tenant_scope`
del negocio VENDEDOR. Pasos:

1. Solo vendedores (allowlist `PROVISIONING_SELLER_TENANTS`); el resto, nada.
2. Registro idempotente en `provisioning_requests` (único por referencia y por suscripción).
3. Candado consultivo por alta: dos workers nunca trabajan la misma.
4. Datos del pedido por la fuente del vendedor (`pack_commerce`), no del texto del LLM, y
   cotejo del cliente del pedido con el de la suscripción.
5. Un Free solo una vez por cliente y por correo; si no, revisión.
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

from core.db.models import ProvisioningRequest, Tenant
from core.db.session import get_owned, get_session
from core.provisioning import ACTOR, AltaEnRevision, AltaOcupada, AltaTransitoria
from core.provisioning.fuentes import DatosDeAlta, fuentes_de_alta
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
        # de plan. Sincronizar el plan del negocio aprovisionado es de HU-SH-04.
        await _auditar_cambio_de_plan(registro, referencia, variant_id)
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


async def _registro(
    vendedor: uuid.UUID,
    *,
    subscription_id: uuid.UUID,
    referencia: str,
    customer_id: uuid.UUID | None,
    variant_id: str | None,
) -> ProvisioningRequest:
    """La fila del alta (la crea si no existe). INSERT … ON CONFLICT DO NOTHING: dos
    entregas a la vez terminan leyendo la misma fila."""
    existente = await _buscar(vendedor, subscription_id, referencia)
    if existente is not None:
        return existente
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
                detalle={"variant_id": variant_id} if variant_id else {},
                intentos=0,
            )
            .on_conflict_do_nothing()
        )
    existente = await _buscar(vendedor, subscription_id, referencia)
    if existente is None:  # pragma: no cover - el INSERT o la otra entrega la dejaron
        raise AltaTransitoria(f"no se pudo registrar el alta {referencia}")
    return existente


def _clave_del_candado(registro_id: uuid.UUID) -> int:
    return int.from_bytes(
        hashlib.sha256(f"provisioning:{registro_id}".encode()).digest()[:8], "big", signed=True
    )


@asynccontextmanager
async def _candado(registro_id: uuid.UUID) -> AsyncIterator[bool]:
    """Candado consultivo de transacción: se suelta solo al terminar (o si el proceso muere)."""
    async with get_session() as session:
        tomado = (
            await session.execute(
                text("select pg_try_advisory_xact_lock(:clave)"),
                {"clave": _clave_del_candado(registro_id)},
            )
        ).scalar()
        yield bool(tomado)


# Las filas se leen y se escriben filtrando también por el vendedor, no solo por la RLS.
async def _fila(session: Any, vendedor: uuid.UUID, registro_id: uuid.UUID) -> ProvisioningRequest:
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
        fila = await _fila(session, vendedor, registro_id)
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
            fila = await _cerrar_sin_alta(vendedor, actual, "revision", exc.motivo)
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
    if plan.is_free and await _free_repetido(vendedor, registro, huella):
        raise AltaEnRevision("ya hubo un alta para este cliente o este correo: un Free más se revisa")
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
        await _registrar_periodo(tenant_id, plan.id, slug)
    preset = await _aplicar_preset(tenant_id, vertical)
    vendedor_slug = await _slug_de(vendedor)
    await _guardar_origen(
        tenant_id,
        {
            "origen": ACTOR,
            "vendedor": vendedor_slug,
            "referencia": registro.referencia,
            "plan": plan.id,
            "vertical": vertical,
            "preset": preset,
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


async def _registrar_periodo(tenant_id: uuid.UUID, plan_id: str, slug: str) -> None:
    """Plan pago: el período pagado por el pedido, con su pago en `subscription_payments`,
    lo escribe `core/billing` (idempotente por la referencia del período). Free se queda con
    la prueba de 30 días que dejó el alta (D-16)."""
    from core.billing.service import registrar_periodo_pagado

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await registrar_periodo_pagado(
                session,
                tenant_id,
                plan_id,
                provider="asistente",
                provider_reference=f"plat-{slug}-{datetime.now(UTC):%Y%m}",
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
            input_data={**entrada, "vendedor": vendedor_slug},
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
    vendedor: uuid.UUID, registro: ProvisioningRequest, estado: str, motivo: str
) -> ProvisioningRequest:
    async with get_session() as session:
        fila = await _fila(session, vendedor, registro.id)
        fila.estado = estado
        fila.motivo = motivo[:500]
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


async def _auditar_cambio_de_plan(
    registro: ProvisioningRequest, referencia: str, variant_id: str | None
) -> None:
    await record_audit(
        actor=ACTOR,
        action="aprovisionamiento_cambio_de_plan",
        input_data={
            "referencia": referencia,
            "referencia_del_alta": registro.referencia,
            "subscription_id": str(registro.subscription_id) if registro.subscription_id else None,
            "variant_id": variant_id,
        },
        output_data={
            "negocio_tenant_id": str(registro.negocio_tenant_id) if registro.negocio_tenant_id else None,
            "pendiente": "sincronizar el plan del negocio y su preset pendiente (HU-SH-04)",
        },
    )
    log.info(
        "provisioning_cambio_de_plan",
        tenant_id=str(registro.tenant_id),
        referencia=referencia,
        referencia_del_alta=registro.referencia,
    )


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
async def _por_referencia(vendedor: uuid.UUID, referencia: str) -> ProvisioningRequest | None:
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


async def reintentar_alta(vendedor: uuid.UUID, referencia: str) -> ProvisioningRequest:
    """Vuelve a correr un alta en `revision` (o trabada `en_curso`) después de corregir el
    dato que faltaba (runbook `docs/runbooks/altas-en-revision.md`). Retoma el mismo negocio."""
    registro = await _por_referencia(vendedor, referencia)
    if registro is None:
        raise ValueError(f"No hay un alta con la referencia {referencia!r} en ese vendedor.")
    if registro.estado not in ("revision", "en_curso"):
        raise ValueError(f"El alta está {registro.estado}: solo se reintenta una en revisión.")
    async with tenant_scope(vendedor):
        async with get_session() as session:
            fila = await _fila(session, vendedor, registro.id)
            fila.estado = "en_curso"
            fila.intentos = 0
            fila.motivo = None
        await record_audit(
            actor="cli",
            action="aprovisionamiento_reintentado",
            input_data={"referencia": referencia, "estado_anterior": registro.estado},
        )
        resultado = await _procesar(vendedor, registro)
    return resultado


async def descartar_alta(vendedor: uuid.UUID, referencia: str, motivo: str) -> ProvisioningRequest:
    """Cierra un alta en `revision` sin crear el negocio (`omitido`, queda el motivo). Un
    evento repetido del mismo pedido ya no la reabre."""
    registro = await _por_referencia(vendedor, referencia)
    if registro is None:
        raise ValueError(f"No hay un alta con la referencia {referencia!r} en ese vendedor.")
    if registro.estado not in ("revision", "en_curso"):
        raise ValueError(f"El alta está {registro.estado}: solo se descarta una en revisión.")
    texto = (motivo or "").strip() or "descartada por el master"
    async with tenant_scope(vendedor):
        async with get_session() as session:
            fila = await _fila(session, vendedor, registro.id)
            fila.estado = "omitido"
            fila.motivo = f"descartada: {texto}"[:500]
        await record_audit(
            actor="cli",
            action="aprovisionamiento_descartado",
            input_data={"referencia": referencia, "estado_anterior": registro.estado},
            output_data={"motivo": texto},
        )
        actualizado = await _por_referencia(vendedor, referencia)
    return actualizado or registro


__all__ = [
    "MAX_INTENTOS",
    "aprovisionar",
    "correo_valido",
    "descartar_alta",
    "huella_del_correo",
    "id_del_negocio",
    "nombre_del_negocio",
    "on_subscription_activated",
    "referencia_del_alta",
    "reintentar_alta",
    "slug_del_negocio",
]

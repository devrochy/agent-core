"""Inscripción de un cliente en un plan del catálogo (SPEC-43, HU-SUB-28, ADR-0177).

Un plan de pago nace `pendiente_pago` con su primer cargo (tipo `inscripcion`) y su link; Free nace
`trialing` en el acto. El pago activa por UN solo camino (`_activar_por_pago`), que llaman el webhook
`payment.confirmed` y la confirmación manual del panel. Lo que no se paga expira a los
`dias_pago_inscripcion` días.

Concurrencia (design.md): la decisión de inscribir se toma bajo un advisory lock por (negocio,
cliente) y con `FOR UPDATE` de las suscripciones vivas del cliente, así dos inscripciones paralelas
dejan UNA suscripción y UN cargo. Orden de locks en todo el pack: advisory → suscripción → cargo.
La inscripción, su primer cargo y el registro del alta se confirman en una transacción; el link de
pago se crea DESPUÉS del commit y es reintentable (`charge_link`).

Promociones (HU-SUB-33): con un `codigo_promocion` el primer cobro sale NETO (`monto`), con `monto_bruto` y la
fotografía del descuento, y el canje se reserva en la misma transacción (`promociones.py`). `monto_alto_inscripcion`
se evalúa sobre el precio de lista.

Observabilidad: logs `subs_inscripcion_*` sin correo, teléfono, documento ni el texto de los códigos.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import select, text

from core.datos_cliente.legado import base_desde_lista, requeridos_efectivos
from core.datos_cliente.servicio import evaluar_accion
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.shared.texto_seguro import contiene_url
from core.subscriptions import SubscriptionError, SubscriptionView
from pack_subscriptions import planes, promociones
from pack_subscriptions.capability import (
    _anular_cambios_pendientes,
    _config,
    _emitir_activada,
    _link_de_cargo,
    _view,
    aplicar_plan,
    cambio_no_aplicable,
    es_free_vencida,
    evaluar_cambio,
    mismo_monto,
)
from pack_subscriptions.datos import A_LEGADO, ACCION_INSCRIBIRSE
from pack_subscriptions.models import (
    CARGO_INSCRIPCION,
    PENDIENTE_PAGO,
    Plan,
    Subscription,
    SubscriptionCharge,
)
from pack_subscriptions.periodos import sumar_periodo

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"
METODOS_DE_PAGO = ("pago_en_linea", "transferencia")
ORIGENES = ("agente", "panel")
CANAL_TRANSFERENCIA = "transferencia"
# Estados que cuentan como «el cliente ya tiene algo» al inscribir (los mismos del único parcial).
_VIVOS = (PENDIENTE_PAGO, "trialing", "activa", "pausada", "vencida")

_LIMITES_DATOS = {"nombre": 120, "email": 254, "telefono": 40, "negocio": 120, "vertical": 60}


# ----------------------------------------------------------------------------- datos del alta
@lru_cache(maxsize=1)
def _verticales() -> tuple[tuple[str, str], ...]:
    """(id, nombre) de los presets del core (ADR-0110): archivos del release, no datos del negocio."""
    from core.presets import list_presets

    return tuple((p.id, p.nombre) for p in list_presets())


def _opciones_de_vertical() -> list[dict[str, str]]:
    from core.presets import PresetError

    try:
        return [{"id": pid, "nombre": nombre} for pid, nombre in _verticales()]
    except PresetError:
        log.warning("subs_inscripcion_verticales_no_disponibles", exc_info=True)
        return []


def _texto(valor: Any, limite: int) -> str:
    return " ".join(str(valor or "").split())[:limite]


def validar_datos(
    campos_requeridos: list[str], datos_alta: dict[str, Any] | None, *, origen: str
) -> tuple[dict[str, str], str | None]:
    """Datos del alta que el servidor guarda, y el documento (solo para la huella del antiabuso).

    Desde el chat (`origen='agente'`): faltan campos → `faltan_datos`; un enlace en el nombre del
    negocio → `dato_invalido` (va en correos y avisos: sirve para phishing); un `vertical` que no es un
    preset → `dato_invalido` con las opciones. Desde el panel (`origen='panel'`) no se exige nada: el
    operador completa lo que sabe y el alta pasa por revisión humana. El documento NO se guarda."""
    crudos = dict(datos_alta or {})
    datos: dict[str, str] = {
        "nombre": _texto(crudos.get("nombre"), _LIMITES_DATOS["nombre"]),
        "email": str(crudos.get("email") or "").strip().lower()[: _LIMITES_DATOS["email"]],
        "telefono": _texto(crudos.get("telefono"), _LIMITES_DATOS["telefono"]),
        "negocio": _texto(crudos.get("negocio"), _LIMITES_DATOS["negocio"]),
        "vertical": str(crudos.get("vertical") or "").strip()[: _LIMITES_DATOS["vertical"]],
    }
    documento = _texto(crudos.get("documento"), 40) or None
    if origen == "agente":
        faltan = [c for c in campos_requeridos if not (documento if c == "documento" else datos.get(c))]
        if faltan:
            raise SubscriptionError(
                "Faltan datos del cliente.", code="faltan_datos", data={"faltan": faltan}
            )
        if datos["negocio"] and contiene_url(datos["negocio"]):
            if "negocio" in campos_requeridos:
                raise SubscriptionError(
                    "El nombre del negocio no puede llevar enlaces ni dominios.",
                    code="dato_invalido",
                    data={"campo": "negocio"},
                )
            datos["negocio"] = ""
        if datos["vertical"] and datos["vertical"] not in {o["id"] for o in _opciones_de_vertical()}:
            if "vertical" in campos_requeridos:
                raise SubscriptionError(
                    "Tipo de negocio no válido.",
                    code="dato_invalido",
                    data={"campo": "vertical", "opciones": _opciones_de_vertical()},
                )
            datos["vertical"] = ""
    return {k: v for k, v in datos.items() if v}, documento


# ----------------------------------------------------------------------------- decisión
@dataclass
class Decision:
    """Qué hace `inscribir` según lo que el cliente ya tiene (tabla de design.md)."""

    accion: str  # nueva | repetida | cambio_de_plan
    sub: Subscription | None = None  # la repetida o la Free que cambia de plan
    reemplazar: list[Subscription] = field(default_factory=list)  # pendientes de otros planes


def decidir(vivas: list[Subscription], plan: Plan, *, varias: bool) -> Decision:
    """Función pura: aplica la tabla de decisión a las suscripciones vivas del cliente.

    - Mismo plan `pendiente_pago` → la repetida (replay: mismo cargo y link).
    - Mismo plan en otro estado vivo → `ya_tiene_este_plan`.
    - Otra `pendiente_pago` de otro plan → se reemplaza (solo si el negocio no permite varias).
    - Otro plan vigente sin `varias_por_cliente` → `ya_tiene_suscripcion` (se ofrece cambiar de plan),
      salvo una Free (vigente o vencida) que contrata un plan de pago: eso es un CAMBIO DE PLAN sobre la
      misma suscripción (D-SPEC-43-3), para no crear un segundo negocio en el alta automática.
    """
    precio = Decimal(plan.precio or 0)
    mismas = [s for s in vivas if s.plan_id == plan.id]
    if mismas:
        previa = mismas[0]
        if previa.estado == PENDIENTE_PAGO:
            return Decision("repetida", previa)
        raise SubscriptionError("El cliente ya tiene este plan.", code="ya_tiene_este_plan")
    pendientes = [s for s in vivas if s.estado == PENDIENTE_PAGO]
    otras = [s for s in vivas if s.estado != PENDIENTE_PAGO]
    if varias:
        return Decision("nueva")
    if not otras:
        return Decision("nueva", reemplazar=pendientes)
    if precio > 0:
        free = [
            s
            for s in otras
            if Decimal(s.valor_periodo or 0) == 0 and (s.estado in ("trialing", "activa") or es_free_vencida(s))
        ]
        if len(free) == len(otras):
            return Decision("cambio_de_plan", free[0], reemplazar=pendientes)
    raise SubscriptionError(
        "El cliente ya tiene una suscripción: se cambia de plan, no se inscribe de nuevo.",
        code="ya_tiene_suscripcion",
        data={"suscripcion_actual": otras[0].concepto},
    )


async def _vivas_del_cliente(session, customer_id: uuid.UUID, *, bloquear: bool) -> list[Subscription]:  # type: ignore[no-untyped-def]
    consulta = (
        select(Subscription)
        .where(
            Subscription.tenant_id == get_current_tenant_id(),
            Subscription.customer_id == customer_id,
            Subscription.estado.in_(_VIVOS),
            Subscription.deleted_at.is_(None),
        )
        .order_by(Subscription.created_at.desc())
    )
    if bloquear:
        consulta = consulta.with_for_update()
    return list((await session.execute(consulta)).scalars().all())


async def _advisory_lock(session, customer_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    """Serializa las inscripciones de un cliente en un negocio (primer lock del orden)."""
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:clave, 0))"),
        {"clave": f"{PLUGIN_ID}:inscribir:{get_current_tenant_id()}:{customer_id}"},
    )


# ----------------------------------------------------------------------------- cotizar
async def cotizar(
    plan_code: str, codigo_promocion: str | None = None, *, customer_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Lo que costaría inscribirse, sin efectos. Con un código evalúa la promoción (vigencia, plan, cupo) y
    devuelve el descuento y el total del primer cargo; un código que no aplica responde `codigo_no_valido`.
    `customer_id` (opcional) acota los intentos con códigos a ese cliente; sin él, el tope es del negocio."""
    plan = await _plan_o_error(plan_code)
    config = await _config()
    precio = Decimal(plan.precio or 0)
    descuento = Decimal("0")
    total = precio
    etiqueta: str | None = None
    codigo = promociones.normalizar_codigo(codigo_promocion)
    if codigo is not None:
        async with get_session() as session:
            evaluacion = await promociones.evaluar(
                session,
                codigo=codigo,
                plan_id=plan.id,
                precio=precio,
                moneda=plan.moneda,
                customer_id=customer_id,
                huella_=None,
                ahora=datetime.now(UTC),
            )
        descuento, total, etiqueta = evaluacion.descuento, evaluacion.neto, evaluacion.etiqueta
    salida: dict[str, Any] = {
        "plan_code": plan.codigo,
        "plan": plan.nombre,
        "precio_lista": float(precio),
        "descuento": float(descuento),
        "total_primer_cargo": float(total),
        "precio_periodo_siguiente": float(precio),
        "moneda": plan.moneda,
        "periodicidad": plan.periodicidad,
        "trial_dias": int(plan.trial_dias or 0),
        "requiere_pago": precio > 0,
        "datos_requeridos": list(config.campos_de_inscripcion),
    }
    if etiqueta is not None:
        salida["promocion"] = etiqueta
    return salida


async def _evaluar_datos(
    datos_alta: dict[str, Any] | None, requeridos_del_modulo: list[str]
) -> dict[str, Any] | None:
    """Sobre unificado `faltan_datos` (aditivo: no sustituye `faltan`). Nunca rompe la tool."""
    try:
        evaluacion = await evaluar_accion(
            ACCION_INSCRIBIRSE, datos_alta, base=base_desde_lista(requeridos_del_modulo)
        )
    except Exception:
        log.warning("subs_sobre_faltan_datos_fallo", exc_info=True)
        return None
    return evaluacion.como_sobre() if evaluacion is not None else None


async def _plan_o_error(plan_code: str) -> Plan:
    plan = await planes.por_codigo(plan_code)
    if plan is None:
        raise SubscriptionError("Ese plan no existe o no está disponible.", code="plan_no_encontrado")
    return plan


# ----------------------------------------------------------------------------- inscribir
async def inscribir(
    *,
    customer_id: uuid.UUID,
    plan_code: str,
    datos_alta: dict[str, Any] | None = None,
    origen: str = "agente",
    metodo_pago: str = "pago_en_linea",
    terminos: dict[str, Any] | None = None,
    codigo_promocion: str | None = None,
    monto_esperado: Decimal | float | str | None = None,
) -> SubscriptionView:
    """Inscribe al cliente en el plan `plan_code` del catálogo (ver el módulo).

    `terminos` trae lo que el antiabuso del core necesita del turno: `{acepta_terminos, conversation_id,
    canal}` (el servidor lo arma desde `ToolContext`; el LLM solo manda el token). Solo se evalúa con
    `origen='agente'` y solo si el negocio es un vendedor de planes de la plataforma.
    `monto_esperado` es lo que el cliente confirmó: si no coincide con el primer cobro NETO (ya con la
    promoción), no se hace nada."""
    if origen not in ORIGENES:
        raise SubscriptionError("Origen de inscripción no válido.", code="origen_invalido")
    if metodo_pago not in METODOS_DE_PAGO:
        raise SubscriptionError("Método de pago no válido.", code="metodo_pago_invalido")
    codigo = promociones.normalizar_codigo(codigo_promocion)
    config = await _config()
    plan = await _plan_o_error(plan_code)
    precio = Decimal(plan.precio or 0)
    requeridos = list(config.campos_de_inscripcion)
    try:  # política de datos del cliente (HU-CRM-13): sin ajustes del dueño la lista no cambia
        requeridos = await requeridos_efectivos(ACCION_INSCRIBIRSE, requeridos, A_LEGADO)
    except Exception:
        log.warning("subs_politica_datos_no_aplicada", exc_info=True)
    try:
        datos, documento = validar_datos(requeridos, datos_alta, origen=origen)
    except SubscriptionError as error:
        if error.code == "faltan_datos":
            evaluacion = await _evaluar_datos(datos_alta, list(config.campos_de_inscripcion))
            if evaluacion is not None:
                error.data["faltan_datos"] = evaluacion
        raise
    telefono_canal = await _cliente_del_negocio(customer_id)
    if (
        origen == "agente"
        and precio > 0
        and config.monto_alto_inscripcion is not None
        and precio > Decimal(str(config.monto_alto_inscripcion))
    ):
        raise SubscriptionError(
            "El valor del plan requiere aprobación humana.", code="monto_alto", escalate=True
        )
    huella_ = (
        promociones.huella_de(documento=documento, telefono_canal=telefono_canal, telefono_datos=datos.get("telefono"))
        if codigo
        else None
    )

    # Replay y rechazos baratos ANTES del antiabuso: repetir una inscripción no gasta cupo de altas
    # ni pide los términos otra vez. La decisión autoritativa se repite bajo el lock.
    repetida = None
    async with get_session() as session:
        previas = await _vivas_del_cliente(session, customer_id, bloquear=False)
        previa = decidir(previas, plan, varias=bool(config.varias_por_cliente))
        if previa.accion == "repetida" and previa.sub is not None:
            repetida = (previa.sub, await cargo_de_inscripcion(session, previa.sub))
            await _validar_replay(session, repetida[1], plan, codigo, monto_esperado)
        else:
            try:
                evaluacion = await _evaluar_promocion(
                    session, codigo, plan, customer_id, huella_, previa, contar_intento=True
                )
            except SubscriptionError as exc:
                if exc.code != "promo_ya_usada":
                    raise
                # Una petición idéntica pudo confirmarse entre la lectura de arriba y esta evaluación: si ahora
                # hay una inscripción repetida es un replay (idempotente), no un segundo canje.
                previa = decidir(
                    await _vivas_del_cliente(session, customer_id, bloquear=False),
                    plan,
                    varias=bool(config.varias_por_cliente),
                )
                if previa.accion != "repetida" or previa.sub is None:
                    raise
                repetida = (previa.sub, await cargo_de_inscripcion(session, previa.sub))
                await _validar_replay(session, repetida[1], plan, codigo, monto_esperado)
            else:
                _comparar_monto(plan, evaluacion, monto_esperado)
    if repetida is not None:
        return await _salida_de_repetida(repetida[0], repetida[1], plan, metodo_pago, config)

    pedido = await _antiabuso(customer_id, terminos, datos, documento, precio, origen)

    resultado: dict[str, Any] = {}
    async with get_session() as session:
        await _advisory_lock(session, customer_id)
        vivas = await _vivas_del_cliente(session, customer_id, bloquear=True)
        decision = decidir(vivas, plan, varias=bool(config.varias_por_cliente))
        if decision.accion == "repetida" and decision.sub is not None:
            # Otra inscripción del mismo cliente ganó el lock: es esa, no una segunda.
            repetida = (decision.sub, await cargo_de_inscripcion(session, decision.sub))
            await _validar_replay(session, repetida[1], plan, codigo, monto_esperado)
        else:
            # Evaluación AUTORITATIVA, ya con las suscripciones del cliente bloqueadas: el neto que se cobra y
            # el que el cliente confirmó se comparan aquí, en la misma transacción que crea el cobro.
            evaluacion = await _evaluar_promocion(
                session, codigo, plan, customer_id, huella_, decision, contar_intento=False
            )
            _comparar_monto(plan, evaluacion, monto_esperado)
            if decision.accion == "cambio_de_plan" and decision.sub is not None:
                resultado = await _cambio_desde_free(session, decision, plan, evaluacion, customer_id, huella_)
            else:
                resultado = await _crear(
                    session, decision, customer_id, plan, datos, origen, config, pedido, evaluacion, huella_
                )
            vista = _view(resultado["sub"], plan_codigo=plan.codigo)
            vista["inscripcion"] = resultado["inscripcion"]
    if repetida is not None:
        return await _salida_de_repetida(repetida[0], repetida[1], plan, metodo_pago, config)
    sub, cargo = resultado["sub"], resultado["cargo"]
    if pedido is not None:
        from core.provisioning.antiabuso import cerrar_aceptacion

        await cerrar_aceptacion(pedido)

    log.info(
        "subs_inscripcion_creada",
        subscription_id=str(sub.id),
        numero_publico=sub.numero_publico,
        plan=plan.codigo,
        estado=sub.estado,
        inscripcion=resultado["inscripcion"],
        origen=origen,
        con_cargo=cargo is not None,
        con_promocion=cargo is not None and cargo.canje_id is not None,
    )
    if cargo is not None:
        vista["cargo_inscripcion"] = await _cargo_en_vista(cargo, sub, plan, metodo_pago, config)
        if resultado["inscripcion"] == "cambio_de_plan":
            vista["cambio"] = "pendiente_pago"
            vista["cargo_cambio"] = {
                "monto": vista["cargo_inscripcion"]["monto"],
                "link": vista["cargo_inscripcion"]["link"],
                "referencia": vista["cargo_inscripcion"]["referencia"],
            }
    return vista


async def _cargos_a_liberar(session, decision: Decision) -> list[uuid.UUID]:  # type: ignore[no-untyped-def]
    """Cobros pendientes que esta decisión va a anular (las inscripciones que se reemplazan y el cambio de plan
    pendiente de la Free): sus canjes se liberan, así que no cuentan como «ya canjeó» para evaluar el código."""
    subs = [*decision.reemplazar, *([decision.sub] if decision.accion == "cambio_de_plan" and decision.sub else [])]
    if not subs:
        return []
    filas = (
        await session.execute(
            select(SubscriptionCharge.id).where(
                SubscriptionCharge.tenant_id == get_current_tenant_id(),
                SubscriptionCharge.subscription_id.in_([s.id for s in subs]),
                SubscriptionCharge.tipo.in_((CARGO_INSCRIPCION, "cambio_plan")),
                SubscriptionCharge.estado == "pendiente",
                SubscriptionCharge.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return list(filas)


async def _evaluar_promocion(
    session,  # type: ignore[no-untyped-def]
    codigo: str | None,
    plan: Plan,
    customer_id: uuid.UUID,
    huella_: str | None,
    decision: Decision,
    *,
    contar_intento: bool,
) -> promociones.Evaluacion | None:
    """Evalúa el código de la inscripción (None si no mandó código). Un plan de $0 no recibe promociones."""
    if codigo is None:
        return None
    return await promociones.evaluar(
        session,
        codigo=codigo,
        plan_id=plan.id,
        precio=Decimal(plan.precio or 0),
        moneda=plan.moneda,
        customer_id=customer_id,
        huella_=huella_,
        ahora=datetime.now(UTC),
        contar_intento=contar_intento,
        excluir_cargos=await _cargos_a_liberar(session, decision),
    )


def _comparar_monto(plan: Plan, evaluacion: promociones.Evaluacion | None, monto_esperado: Any) -> None:
    """`monto_esperado` es lo que el cliente confirmó: se compara con el primer cobro NETO, nunca se usa de monto."""
    if monto_esperado is None:
        return
    neto = evaluacion.neto if evaluacion is not None else Decimal(plan.precio or 0)
    if not mismo_monto(neto, monto_esperado, plan.moneda):
        raise SubscriptionError(
            "El valor de la inscripción ya no es el que se cotizó: vuelve a cotizarlo y confírmalo de nuevo.",
            code="monto_distinto",
        )


async def _validar_replay(
    session,  # type: ignore[no-untyped-def]
    cargo: SubscriptionCharge | None,
    plan: Plan,
    codigo: str | None,
    monto_esperado: Any,
) -> None:
    """Repetir una inscripción devuelve la MISMA (mismo cobro, mismo descuento): lo confirmado se compara con
    ese cobro, y un código distinto del que ya tiene aplicado se rechaza en vez de ignorarse."""
    if monto_esperado is not None and cargo is not None and not mismo_monto(cargo.monto, monto_esperado, plan.moneda):
        raise SubscriptionError(
            "El valor de la inscripción ya no es el que se cotizó: vuelve a cotizarlo y confírmalo de nuevo.",
            code="monto_distinto",
        )
    if codigo is None or cargo is None:
        return
    # Un código inexistente y uno de otra promoción responden igual: no es un oráculo de códigos.
    pedida = await promociones.id_por_codigo(session, codigo)
    if pedida is None or promociones.promocion_del_cargo(cargo) != str(pedida):
        raise SubscriptionError(
            "Esta inscripción ya existe con otra promoción: cancélala para inscribirte con este código.",
            code="promo_distinta",
        )


async def _cliente_del_negocio(customer_id: uuid.UUID) -> str | None:
    """El cliente tiene que ser del negocio (`Subscription.customer_id` no tiene FK): quien llame a
    `inscribir` no puede inscribir un UUID de otro negocio, ni por una tool ni por la API. Devuelve el
    teléfono del canal del cliente (para la huella del canje), si lo tiene."""
    from core.db.models import Customer
    from core.provisioning.antiabuso import telefono_del_canal

    async with get_session() as session:
        cliente = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == get_current_tenant_id(),
                    Customer.id == customer_id,
                    Customer.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if cliente is None:
            raise SubscriptionError("El cliente no existe.", code="cliente_no_encontrado")
        return telefono_del_canal(cliente)


async def _antiabuso(
    customer_id: uuid.UUID,
    terminos: dict[str, Any] | None,
    datos: dict[str, str],
    documento: str | None,
    precio: Decimal,
    origen: str,
):  # type: ignore[no-untyped-def]
    """Antiabuso del core (HU-SH-07, ADR-0125): términos, cupo por origen y un Free por persona. Solo
    desde el chat; el core decide y solo actúa si el negocio vende planes de la plataforma."""
    if origen != "agente":
        return None
    from core.provisioning.antiabuso import (
        FreeNoDisponible,
        LimiteDeAltas,
        PedidoPreparado,
        TerminosPendientes,
        preparar_pedido_de_alta,
    )

    contexto = terminos or {}
    preparado = await preparar_pedido_de_alta(
        customer_id=customer_id,
        conversation_id=_uuid(contexto.get("conversation_id")),
        canal=contexto.get("canal"),
        telefono=datos.get("telefono"),
        documento=documento,
        gratis=precio == 0,
        acepta_terminos=contexto.get("acepta_terminos"),
    )
    if preparado is None or isinstance(preparado, PedidoPreparado):
        return preparado
    if isinstance(preparado, TerminosPendientes):
        raise SubscriptionError(
            "Falta que el cliente acepte los términos.",
            code="terminos_pendientes",
            data={
                "documentos": preparado.documentos,
                "token": preparado.token,
                "enlaces_enviados": preparado.enlaces_enviados,
            },
        )
    if isinstance(preparado, LimiteDeAltas):
        raise SubscriptionError("Se superó el cupo de inscripciones de este origen.", code="limite_de_altas")
    if isinstance(preparado, FreeNoDisponible):
        raise SubscriptionError("Este plan gratuito no está disponible para este cliente.", code="free_no_disponible")
    raise SubscriptionError("La inscripción fue frenada.", code="inscripcion_frenada")


def _uuid(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor)) if valor else None
    except (ValueError, TypeError):
        return None


async def _siguiente_numero(session, prefijo: str) -> str:  # type: ignore[no-untyped-def]
    """`SUB-0001`: contador atómico por negocio (`pack_subscriptions_next_seq`); no se trunca desde 10.000."""
    seq = (
        await session.execute(text("select pack_subscriptions_next_seq(:t)"), {"t": get_current_tenant_id()})
    ).scalar_one()
    return f"{prefijo}-{int(seq):04d}"


async def _crear(
    session,  # type: ignore[no-untyped-def]
    decision: Decision,
    customer_id: uuid.UUID,
    plan: Plan,
    datos: dict[str, str],
    origen: str,
    config,  # type: ignore[no-untyped-def]
    pedido,  # type: ignore[no-untyped-def]
    evaluacion: promociones.Evaluacion | None = None,
    huella_: str | None = None,
) -> dict[str, Any]:
    ahora = datetime.now(UTC)
    for previa in decision.reemplazar:
        await _reemplazar(session, previa)
    precio = Decimal(plan.precio or 0)
    numero = await _siguiente_numero(session, config.prefijo_numero)
    sub = Subscription(
        tenant_id=get_current_tenant_id(),
        customer_id=customer_id,
        plan_id=plan.id,
        numero_publico=numero,
        datos_alta=datos or None,
        origen=origen,
        concepto=plan.nombre,
        valor_periodo=precio,
        moneda=plan.moneda,
        periodicidad=plan.periodicidad,
    )
    cargo: SubscriptionCharge | None = None
    if precio == 0:
        # Free: nace en el acto y el alta automática arranca ahí (D-16: solo $0 tiene prueba).
        trial = int(plan.trial_dias or 0)
        sub.estado = "trialing" if trial else "activa"
        sub.trial_termina = (ahora + timedelta(days=trial)) if trial else None
        sub.dia_ancla = ahora.day
        sub.periodo_actual_inicio = ahora
        sub.proximo_cargo = sub.trial_termina or sumar_periodo(ahora, plan.periodicidad, ahora.day)
        sub.fecha_alta = ahora
        session.add(sub)
        await session.flush()
        await _emitir_activada(session, sub, None, cambio=False, monto_cobrado=Decimal("0"))
    else:
        # De pago: existe desde la elección pero no vive hasta que pague (no cuenta en MRR ni en los crons).
        sub.estado = PENDIENTE_PAGO
        session.add(sub)
        await session.flush()
        cargo = SubscriptionCharge(
            tenant_id=get_current_tenant_id(),
            subscription_id=sub.id,
            periodo=CARGO_INSCRIPCION,
            monto=precio,
            estado="pendiente",
            tipo=CARGO_INSCRIPCION,
            fecha_vencimiento=ahora + timedelta(days=config.dias_pago_inscripcion),
        )
        if evaluacion is not None:
            promociones.aplicar_al_cargo(cargo, evaluacion)
        session.add(cargo)
        await session.flush()
        if evaluacion is not None:
            await promociones.reservar(session, evaluacion, sub=sub, cargo=cargo, customer_id=customer_id, huella_=huella_)
    if pedido is not None:
        from core.provisioning.antiabuso import registrar_pedido_de_alta

        registrar_pedido_de_alta(session, pedido, referencia=numero)
    return {"sub": sub, "cargo": cargo, "inscripcion": "nueva"}


async def _cambio_desde_free(
    session,  # type: ignore[no-untyped-def]
    decision: Decision,
    plan: Plan,
    evaluacion: promociones.Evaluacion | None = None,
    customer_id: uuid.UUID | None = None,
    huella_: str | None = None,
) -> dict[str, Any]:
    """Free (vigente o vencida) que contrata un plan de pago = cambio de plan `desde_cero`, sobre la
    misma suscripción y el mismo negocio (D-SPEC-43-3). Reusa las reglas de ADR-0111. Con un código, el
    cobro sale neto y cuenta como el período 1 del descuento."""
    ahora = datetime.now(UTC)
    free = decision.sub
    assert free is not None
    for previa in decision.reemplazar:
        await _reemplazar(session, previa)
    await session.refresh(free, with_for_update=True)
    evaluado = evaluar_cambio(free, variant_id=str(plan.id), plan=planes.snapshot(plan), ahora=ahora)
    destino = {"variante_id": str(plan.id), "plan_id": str(plan.id), "plan": planes.snapshot(plan)}
    monto = evaluacion.neto if evaluacion is not None else evaluado.monto
    # Un cobro de cambio pendiente al MISMO plan, monto y promoción se reusa (reintento); otro se anula y libera.
    cargo = await _anular_cambios_pendientes(
        session,
        free,
        salvo=(str(plan.id), monto),
        salvo_promocion=str(evaluacion.promocion_id) if evaluacion is not None else None,
    )
    if cargo is None:
        cargo = SubscriptionCharge(
            tenant_id=get_current_tenant_id(),
            subscription_id=free.id,
            periodo=f"cambio-{ahora:%Y%m%d%H%M%S}",
            monto=evaluado.monto,
            estado="pendiente",
            fecha_vencimiento=ahora,
            tipo="cambio_plan",
            cambio_plan={
                **destino,
                "desde_cero": evaluado.desde_cero,
                "proximo_cargo": free.proximo_cargo.isoformat() if free.proximo_cargo else None,
            },
        )
        if evaluacion is not None:
            promociones.aplicar_al_cargo(cargo, evaluacion)
        session.add(cargo)
        await session.flush()
        if evaluacion is not None and customer_id is not None:
            await promociones.reservar(
                session, evaluacion, sub=free, cargo=cargo, customer_id=customer_id, huella_=huella_
            )
    await session.flush()
    return {"sub": free, "cargo": cargo, "inscripcion": "cambio_de_plan"}


async def _reemplazar(session, previa: Subscription) -> None:  # type: ignore[no-untyped-def]
    """Una inscripción pendiente que el cliente cambia por otro plan: queda cancelada (`reemplazada`) y
    su cargo `anulado` (su link deja de aplicar y un pago tardío se avisa sin activar)."""
    previa.estado = "cancelada"
    previa.motivo_cancelacion = "reemplazada"
    previa.fecha_cancelacion = datetime.now(UTC)
    await anular_cargos_pendientes(session, previa)
    log.info("subs_inscripcion_reemplazada", subscription_id=str(previa.id), numero_publico=previa.numero_publico)


async def anular_cargos_pendientes(session, sub: Subscription) -> list[SubscriptionCharge]:  # type: ignore[no-untyped-def]
    """Deja `anulado` el cobro pendiente de una inscripción (o de su cambio de plan)."""
    cargos = (
        await session.execute(
            select(SubscriptionCharge)
            .where(
                SubscriptionCharge.tenant_id == get_current_tenant_id(),
                SubscriptionCharge.subscription_id == sub.id,
                SubscriptionCharge.tipo.in_((CARGO_INSCRIPCION, "cambio_plan")),
                SubscriptionCharge.estado == "pendiente",
                SubscriptionCharge.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    for cargo in cargos:
        cargo.estado = "anulado"
    # HU-SUB-33: un cobro anulado sin pagarse devuelve el cupo de su promoción.
    await promociones.liberar_por_cargos(session, list(cargos))
    return list(cargos)


async def cargo_de_inscripcion(session, sub: Subscription) -> SubscriptionCharge | None:  # type: ignore[no-untyped-def]
    """Cobro de inscripción pendiente de la suscripción, si lo hay."""
    return (
        await session.execute(
            select(SubscriptionCharge).where(
                SubscriptionCharge.tenant_id == get_current_tenant_id(),
                SubscriptionCharge.subscription_id == sub.id,
                SubscriptionCharge.tipo == CARGO_INSCRIPCION,
                SubscriptionCharge.estado == "pendiente",
                SubscriptionCharge.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _salida_de_repetida(
    sub: Subscription, cargo: SubscriptionCharge | None, plan: Plan, metodo_pago: str, config  # type: ignore[no-untyped-def]
) -> SubscriptionView:
    """Replay: la misma inscripción, el mismo cargo y el mismo link (se crea si faltaba)."""
    vista = _view(sub, plan_codigo=plan.codigo)
    vista["inscripcion"] = "repetida"
    if cargo is not None:
        vista["cargo_inscripcion"] = await _cargo_en_vista(cargo, sub, plan, metodo_pago, config)
    return vista


async def _cargo_en_vista(
    cargo: SubscriptionCharge, sub: Subscription, plan: Plan, metodo_pago: str, config  # type: ignore[no-untyped-def]
) -> dict[str, Any]:
    """`{monto, link, referencia}` del primer cobro. El link se crea después del commit y solo si el
    cliente paga en línea; sin pasarela o bajo su mínimo la inscripción sigue (sin link), con las
    instrucciones de transferencia del negocio."""
    link: dict[str, Any] = {}
    if metodo_pago == "pago_en_linea":
        try:
            link = await _link_de_cargo(cargo, sub, f"Inscripción {plan.nombre}")
        except Exception:
            # La inscripción ya está confirmada: un fallo de la pasarela no la deshace. Se reintenta con
            # `charge_link` / `sub_link_pago`.
            log.warning("subs_inscripcion_link_fallo", charge_id=str(cargo.id), exc_info=True)
    salida: dict[str, Any] = {
        "monto": float(cargo.monto or 0),
        "link": link.get("url"),
        "referencia": link.get("referencia"),
        "metodo_pago": metodo_pago,
        "vence": cargo.fecha_vencimiento.isoformat() if cargo.fecha_vencimiento else None,
    }
    if cargo.monto_bruto is not None:
        salida["monto_bruto"] = float(cargo.monto_bruto)
        salida["descuento"] = float(Decimal(cargo.monto_bruto) - Decimal(cargo.monto))
        salida["promocion"] = (cargo.descuento or {}).get("etiqueta")
    if not link and config.instrucciones_transferencia:
        salida["instrucciones_transferencia"] = config.instrucciones_transferencia
    return salida


# ----------------------------------------------------------------------------- activar por pago
def motivo_no_aplicable(sub: Subscription, cargo: SubscriptionCharge) -> str | None:
    """Por qué NO se puede activar con este pago (None si se puede): la inscripción ya no espera pago, o el
    cambio de plan ya no aplica. Con motivo, el dinero llegó pero hay que resolverlo a mano."""
    if cargo.tipo == CARGO_INSCRIPCION:
        return None if sub.estado == PENDIENTE_PAGO else f"la inscripción está {sub.estado}"
    if cargo.tipo == "cambio_plan" and cargo.cambio_plan:
        return cambio_no_aplicable(sub, cargo.cambio_plan)
    return None


async def _activar_por_pago(
    session,  # type: ignore[no-untyped-def]
    sub: Subscription,
    cargo: SubscriptionCharge,
    *,
    ahora: datetime | None = None,
) -> str | None:
    """ÚNICO camino de activación: lo llaman el webhook `payment.confirmed` y la confirmación manual.

    El cargo queda `pagado` siempre (la plata llegó). Devuelve el motivo si no se pudo aplicar (nada más
    cambia) o None si activó. Quien llama ya tiene el lock de la suscripción y del cargo (advisory →
    suscripción → cargo) y validó el monto."""
    ahora = ahora or datetime.now(UTC)
    cargo.estado = "pagado"
    motivo = motivo_no_aplicable(sub, cargo)
    if motivo:
        # El dinero llegó pero no se aplicó: un canje `reservado` se queda así (lo resuelve una persona).
        return motivo
    await promociones.confirmar_por_cargo(session, cargo)
    if cargo.tipo == CARGO_INSCRIPCION:
        sub.estado = "activa"
        sub.motivo_pausa = None
        sub.trial_termina = None
        sub.fecha_alta = ahora
        sub.dia_ancla = ahora.day
        sub.periodo_actual_inicio = ahora
        sub.proximo_cargo = sumar_periodo(ahora, sub.periodicidad, ahora.day)
        await session.flush()
        await _emitir_activada(session, sub, None, cambio=False, charge_id=cargo.id, monto_cobrado=Decimal(cargo.monto))
        log.info("subs_inscripcion_activada", subscription_id=str(sub.id), numero_publico=sub.numero_publico)
        return None
    if cargo.tipo == "cambio_plan" and cargo.cambio_plan:
        # Upgrade pagado (HU-SUB-07, ADR-0111): el plan cambia en el acto.
        aplicar_plan(sub, cargo.cambio_plan, ahora)
        await session.flush()
        await _emitir_activada(session, sub, None, cambio=True, charge_id=cargo.id, monto_cobrado=Decimal(cargo.monto))
        log.info("subs_cambio_de_plan_pagado", subscription_id=str(sub.id), plan=sub.concepto)
        return None
    return None


async def confirmar_pago(
    subscription_id: uuid.UUID, *, referencia: str, actor: str = ""
) -> dict[str, Any]:
    """Un operador confirma el pago por transferencia de la inscripción (o del cambio de plan): activa por
    el MISMO camino que el webhook. Idempotente: repetir con la misma referencia responde `ya_confirmado`."""
    referencia = (referencia or "").strip()
    if len(referencia) < 3:
        raise SubscriptionError(
            "Indica el número del comprobante o una referencia de al menos 3 caracteres.",
            code="referencia_invalida",
        )
    async with get_session() as session:
        sub = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == get_current_tenant_id(),
                    Subscription.id == subscription_id,
                    Subscription.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sub is None:
            raise SubscriptionError("La suscripción no existe.", code="suscripcion_no_encontrada")
        cargo = (
            await session.execute(
                select(SubscriptionCharge)
                .where(
                    SubscriptionCharge.tenant_id == get_current_tenant_id(),
                    SubscriptionCharge.subscription_id == sub.id,
                    SubscriptionCharge.tipo.in_((CARGO_INSCRIPCION, "cambio_plan")),
                    SubscriptionCharge.estado != "anulado",
                    SubscriptionCharge.deleted_at.is_(None),
                )
                .order_by(SubscriptionCharge.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalars().first()
        if cargo is None:
            raise SubscriptionError("No hay un cobro de inscripción pendiente.", code="sin_cobro_pendiente")
        if cargo.estado == "pagado":
            if (cargo.pago_externo or {}).get("referencia") == referencia:
                return _salida_confirmacion("ya_confirmado", sub, cargo)
            raise SubscriptionError("Este cobro ya está pagado.", code="cargo_ya_pagado")
        if sub.estado == "cancelada":
            raise SubscriptionError("La suscripción está cancelada: no hay nada que cobrar.", code="suscripcion_cancelada")
        motivo = motivo_no_aplicable(sub, cargo)
        if motivo:
            raise SubscriptionError(f"No se puede aplicar el pago: {motivo}.", code="pago_no_aplicable")
        cargo.pago_externo = {
            "canal": CANAL_TRANSFERENCIA,
            "referencia": referencia,
            "registrado_en": datetime.now(UTC).isoformat(),
        }
        await _activar_por_pago(session, sub, cargo)
        if actor:
            from core.db.models import AuditLog

            session.add(
                AuditLog(
                    tenant_id=sub.tenant_id,
                    actor=actor,
                    action="subs_pago_inscripcion_confirmado",
                    input={"subscription_id": str(sub.id), "charge_id": str(cargo.id), "referencia": referencia},
                    output={"estado": sub.estado},
                )
            )
        salida = _salida_confirmacion("confirmado", sub, cargo)
    log.info("subs_inscripcion_pago_manual", subscription_id=str(sub.id), numero_publico=sub.numero_publico)
    return salida


def _salida_confirmacion(estado: str, sub: Subscription, cargo: SubscriptionCharge) -> dict[str, Any]:
    return {
        "estado": estado,
        "subscription_id": str(sub.id),
        "charge_id": str(cargo.id),
        "suscripcion_estado": sub.estado,
        "numero_publico": sub.numero_publico,
    }


# ----------------------------------------------------------------------------- expirar
async def expirar_una(subscription_id: uuid.UUID, *, limite: datetime) -> bool:
    """Expira UNA inscripción `pendiente_pago` creada antes de `limite`: queda `cancelada` con motivo
    `pago_no_recibido` y su cargo `anulado`. Bloquea la suscripción y relee el estado: si el pago llegó a la
    vez, gana quien tomó el lock primero (si pagó, esto no hace nada; si expiró, el pago tardío se avisa
    como pago sin aplicar y no activa). Idempotente: solo actúa si sigue `pendiente_pago`."""
    async with get_session() as session:
        sub = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == get_current_tenant_id(),
                    Subscription.id == subscription_id,
                    Subscription.estado == PENDIENTE_PAGO,
                    Subscription.deleted_at.is_(None),
                    Subscription.created_at < limite,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sub is None:
            return False
        sub.estado = "cancelada"
        sub.motivo_cancelacion = "pago_no_recibido"
        sub.fecha_cancelacion = datetime.now(UTC)
        await anular_cargos_pendientes(session, sub)
        await session.flush()
    log.info("subs_inscripcion_expirada", subscription_id=str(subscription_id))
    return True

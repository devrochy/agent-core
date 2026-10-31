"""Promociones de plan y canjes (SPEC-43, HU-SUB-33, ADR-0177 §Promociones de plan).

Modelo propio de suscripciones (las promociones de commerce apuntan a productos y no pueden descontar un
plan). Aquí viven las reglas, el CRUD y el ciclo del canje; los endpoints son `admin_api.py` y quien las
usa al inscribir o cambiar de plan es `inscripcion.py` / `capability.py`. Este módulo NO importa ninguno de
los dos (evita el ciclo): recibe sesión, plan y cargo como argumentos.

**Alcance del PR-2b-1:** descuento del PRIMER cobro (inscripción de un plan de pago o cambio de plan
`desde_cero`, p. ej. Free → pago). `periodos` está en el modelo, pero la API solo acepta 1 hasta HU-SUB-35.
Solo se evalúa una promoción cuando el cliente manda un código: la aplicación automática (promoción sin
código) no está en este PR.

Ciclo del canje (un canje por cargo con descuento):

    reservado  --pago-->  confirmado        (con 1 período, la 0012 lo pasa a `agotado`)
    reservado  --cargo anulado-->  liberado  (devuelve el cupo y el derecho a canjear otra vez)

`agotado` y `terminado` SIGUEN contando para el límite por cliente (cancelar y reinscribirse no recupera
el descuento). Un pago que llega a un cargo `pendiente` cuya inscripción ya no puede activarse (dinero
recibido, plan sin aplicar) deja el canje `reservado` a propósito: el cobro ya se hizo con el descuento y
alguien tiene que resolverlo a mano; el gauge `canjes_reservados_antiguos` lo hace visible.

Concurrencia: orden de locks en todo el pack: advisory (negocio, cliente) → suscripción → cargo → canje →
fila de la promoción (la última). El cupo se reserva con un `UPDATE … WHERE canjes_usados < max_canjes
RETURNING`: atómico, y el perdedor de la carrera ve 0 filas. Un código no se escribe nunca en los logs.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from opentelemetry import metrics
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import SubscriptionError
from pack_subscriptions.models import (
    CANJE_AGOTADO,
    CANJE_CONFIRMADO,
    CANJE_LIBERADO,
    CANJE_RESERVADO,
    CANJE_TERMINADO,
    Canje,
    Plan,
    Promocion,
    Subscription,
    SubscriptionCharge,
)

log = get_logger(__name__)

# HU-SUB-35: la API acepta de 1 a 12 períodos (el monto fijo solo con 1: lo valida `validar`).
MAX_PERIODOS_HABILITADOS = 12
MAX_VALOR = Decimal("999999999999.99")
MAX_CANJES_TOPE = 1_000_000
_FORMA_CODIGO = re.compile(r"[a-z0-9][a-z0-9_-]{2,39}")
# Intentos con un código por hora: por cliente cuando se sabe quién es; sin cliente (cotizar), por negocio.
_INTENTOS_POR_CLIENTE = 10
_INTENTOS_ANONIMOS = 120

_meter = metrics.get_meter("agent-core")
_canjes_total = _meter.create_counter("promo_canjes_total", description="Canjes de promociones por estado")
_consumidos_total = _meter.create_counter(
    "descuento_periodos_consumidos_total", description="Períodos de descuento consumidos al generar cargos"
)
_descuento_total = _meter.create_counter(
    "promo_descuento_otorgado_total", description="Descuento de promociones cobrado (canjes confirmados)"
)
_rechazos_total = _meter.create_counter("promo_rechazos_total", description="Códigos de promoción rechazados")

_MENSAJES = {
    "codigo_no_valido": "El código de promoción no es válido.",
    "promo_ya_usada": "Esta promoción ya fue canjeada.",
    "promo_agotada": "La promoción se agotó.",
    "promo_no_aplica_a_free": "Un plan gratuito no recibe promociones.",
    "descuento_excede_precio": "El descuento deja el valor en cero o menos.",
    "demasiados_intentos": "Demasiados intentos con códigos de promoción: espera un momento e inténtalo de nuevo.",
}


class PromoError(Exception):
    """Rechazo de negocio del CRUD de promociones, con código estable (la API lo traduce a 404, 409 o 422)."""

    def __init__(self, code: str, message: str, *, campo: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.campo = campo


# ----------------------------------------------------------------------------- reglas y CRUD
@dataclass(frozen=True)
class DatosDePromocion:
    """Lo que el dueño edita de una promoción. El servidor valida; nada de esto es texto del LLM."""

    etiqueta: str
    tipo_descuento: str  # porcentaje | monto
    valor: Decimal
    codigo: str | None = None
    periodos: int = 1
    plan_id: uuid.UUID | None = None
    vigente_desde: datetime | None = None
    vigente_hasta: datetime | None = None
    max_canjes: int | None = None
    activa: bool = True


def normalizar_codigo(valor: Any) -> str | None:
    """Código de cupón en minúsculas y sin espacios; None si viene vacío."""
    texto = str(valor or "").strip().lower()
    return texto or None


def _valor(raw: Any) -> Decimal:
    try:
        valor = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PromoError("valor_invalido", "El valor no es un número válido.", campo="valor") from exc
    if not valor.is_finite() or valor <= 0 or valor > MAX_VALOR:
        raise PromoError("valor_invalido", "El valor debe ser mayor que 0.", campo="valor")
    return valor.quantize(Decimal("0.01"))


def validar(datos: DatosDePromocion, *, max_periodos: int = MAX_PERIODOS_HABILITADOS) -> DatosDePromocion:
    """Reglas de una promoción (las mismas de los CHECK del esquema, con mensajes para el panel).

    Porcentaje de 1 a 99 (el 0 no descuenta y el 100 no es un cobro); un monto fijo solo con 1 período;
    `max_periodos` es el tope que la API habilita hoy (la base permite hasta 12)."""
    etiqueta = (datos.etiqueta or "").strip()
    if not 1 <= len(etiqueta) <= 80:
        raise PromoError("etiqueta_invalida", "La etiqueta lleva de 1 a 80 caracteres.", campo="etiqueta")
    codigo = normalizar_codigo(datos.codigo)
    if codigo is not None and not _FORMA_CODIGO.fullmatch(codigo):
        raise PromoError(
            "codigo_invalido",
            "El código lleva de 3 a 40 letras, números, guion o guion bajo, y empieza con letra o número.",
            campo="codigo",
        )
    if datos.tipo_descuento not in ("porcentaje", "monto"):
        raise PromoError("tipo_invalido", "El descuento es un porcentaje o un monto.", campo="tipo_descuento")
    valor = _valor(datos.valor)
    if datos.tipo_descuento == "porcentaje" and not Decimal(1) <= valor <= Decimal(99):
        raise PromoError("porcentaje_invalido", "El porcentaje va de 1 a 99.", campo="valor")
    periodos = int(datos.periodos)
    if not 1 <= periodos <= 12:
        raise PromoError("periodos_invalidos", "Los períodos van de 1 a 12.", campo="periodos")
    if datos.tipo_descuento == "monto" and periodos > 1:
        raise PromoError("monto_un_periodo", "Un monto fijo se descuenta una sola vez (1 período).", campo="periodos")
    if periodos > max_periodos:
        raise PromoError(
            "periodos_no_disponibles",
            "Por ahora el descuento dura un solo período; el descuento de varios períodos llega más adelante.",
            campo="periodos",
        )
    if datos.vigente_desde and datos.vigente_hasta and datos.vigente_hasta <= datos.vigente_desde:
        raise PromoError("vigencia_invalida", "La vigencia termina antes de empezar.", campo="vigente_hasta")
    if datos.max_canjes is not None and not 1 <= int(datos.max_canjes) <= MAX_CANJES_TOPE:
        raise PromoError("max_canjes_invalido", "El tope de canjes debe ser un número positivo.", campo="max_canjes")
    return DatosDePromocion(
        etiqueta=etiqueta,
        tipo_descuento=datos.tipo_descuento,
        valor=valor,
        codigo=codigo,
        periodos=periodos,
        plan_id=datos.plan_id,
        vigente_desde=datos.vigente_desde,
        vigente_hasta=datos.vigente_hasta,
        max_canjes=int(datos.max_canjes) if datos.max_canjes is not None else None,
        activa=bool(datos.activa),
    )


def _tenant() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("promociones sin tenant_scope")
    return tenant_id


def vista(promo: Promocion) -> dict[str, Any]:
    """Foto de la promoción para la auditoría (antes y después). Sin el código: es un secreto de cupón."""
    return {
        "id": str(promo.id),
        "etiqueta": promo.etiqueta,
        "con_codigo": promo.codigo is not None,
        "tipo_descuento": promo.tipo_descuento,
        "valor": str(promo.valor),
        "periodos": promo.periodos,
        "plan_id": str(promo.plan_id) if promo.plan_id else None,
        "vigente_desde": promo.vigente_desde.isoformat() if promo.vigente_desde else None,
        "vigente_hasta": promo.vigente_hasta.isoformat() if promo.vigente_hasta else None,
        "max_canjes": promo.max_canjes,
        "canjes_usados": promo.canjes_usados,
        "activa": promo.activa,
    }


def _aplicar(promo: Promocion, datos: DatosDePromocion) -> None:
    promo.etiqueta = datos.etiqueta
    promo.codigo = datos.codigo
    promo.tipo_descuento = datos.tipo_descuento
    promo.valor = datos.valor
    promo.periodos = datos.periodos
    promo.plan_id = datos.plan_id
    promo.vigente_desde = datos.vigente_desde
    promo.vigente_hasta = datos.vigente_hasta
    promo.max_canjes = datos.max_canjes
    promo.activa = datos.activa


async def _verificar_plan(session, tenant: uuid.UUID, plan_id: uuid.UUID | None) -> None:  # type: ignore[no-untyped-def]
    if plan_id is None:
        return
    plan = (
        await session.execute(select(Plan).where(Plan.tenant_id == tenant, Plan.id == plan_id, Plan.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if plan is None:
        raise PromoError("plan_no_encontrado", "El plan de la promoción no existe.", campo="plan_id")
    if Decimal(plan.precio or 0) == 0:
        raise PromoError("plan_gratis", "Un plan gratuito no recibe promociones.", campo="plan_id")


async def listar(*, incluir_inactivas: bool = True) -> list[Promocion]:
    filtros = [Promocion.tenant_id == _tenant(), Promocion.deleted_at.is_(None)]
    if not incluir_inactivas:
        filtros.append(Promocion.activa.is_(True))
    async with get_session() as session:
        return list(
            (await session.execute(select(Promocion).where(*filtros).order_by(Promocion.created_at.desc())))
            .scalars()
            .all()
        )


async def obtener(promocion_id: uuid.UUID) -> Promocion | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(Promocion).where(
                    Promocion.tenant_id == _tenant(), Promocion.id == promocion_id, Promocion.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()


async def crear(datos: DatosDePromocion, *, actor: str = "") -> Promocion:
    """Crea una promoción. 409 `codigo_duplicado` si ya hay una vigente con ese código en el negocio."""
    datos = validar(datos)
    tenant = _tenant()
    promo = Promocion(tenant_id=tenant)
    _aplicar(promo, datos)
    try:
        async with get_session() as session:
            await _verificar_plan(session, tenant, datos.plan_id)
            session.add(promo)
            await session.flush()
            _auditar(session, tenant, actor, "subs_promocion_creada", None, promo)
    except IntegrityError as exc:
        if "uq_pack_subscriptions_promos_tenant_codigo" in str(exc):
            raise PromoError("codigo_duplicado", "Ya existe una promoción con ese código.", campo="codigo") from exc
        raise
    log.info("subs_promocion_creada", promocion_id=str(promo.id), tipo=promo.tipo_descuento, periodos=promo.periodos)
    return promo


async def actualizar(promocion_id: uuid.UUID, datos: DatosDePromocion, *, actor: str = "") -> Promocion:
    """Reemplaza los datos de una promoción (el último gana; queda auditado el antes y el después).

    Los canjes ya hechos conservan su fotografía: editar la promoción no cambia lo ya canjeado."""
    datos = validar(datos)
    tenant = _tenant()
    try:
        async with get_session() as session:
            promo = (
                await session.execute(
                    select(Promocion)
                    .where(Promocion.tenant_id == tenant, Promocion.id == promocion_id, Promocion.deleted_at.is_(None))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if promo is None:
                raise PromoError("promocion_no_encontrada", "La promoción no existe.")
            if datos.max_canjes is not None and datos.max_canjes < promo.canjes_usados:
                raise PromoError(
                    "max_canjes_menor_a_usados",
                    f"Ya se canjeó {promo.canjes_usados} veces: el tope no puede ser menor.",
                    campo="max_canjes",
                )
            await _verificar_plan(session, tenant, datos.plan_id)
            antes = vista(promo)
            _aplicar(promo, datos)
            await session.flush()
            _auditar(session, tenant, actor, "subs_promocion_editada", antes, promo)
    except IntegrityError as exc:
        if "uq_pack_subscriptions_promos_tenant_codigo" in str(exc):
            raise PromoError("codigo_duplicado", "Ya existe una promoción con ese código.", campo="codigo") from exc
        raise
    log.info("subs_promocion_editada", promocion_id=str(promo.id))
    return promo


async def borrar(promocion_id: uuid.UUID, *, actor: str = "") -> None:
    """Borrado lógico: el código deja de aplicar. Los canjes reservados siguen su curso."""
    tenant = _tenant()
    async with get_session() as session:
        promo = (
            await session.execute(
                select(Promocion)
                .where(Promocion.tenant_id == tenant, Promocion.id == promocion_id, Promocion.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if promo is None:
            raise PromoError("promocion_no_encontrada", "La promoción no existe.")
        antes = vista(promo)
        promo.deleted_at = datetime.now(UTC)
        promo.activa = False
        await session.flush()
        _auditar(session, tenant, actor, "subs_promocion_borrada", antes, promo)
    log.info("subs_promocion_borrada", promocion_id=str(promocion_id))


async def canjes_de(promocion_id: uuid.UUID, *, limit: int, offset: int) -> tuple[list[tuple[Canje, SubscriptionCharge | None]], int]:
    """Canjes de la promoción, del más reciente al más antiguo, con su cargo (para ver bruto, descuento y neto)."""
    tenant = _tenant()
    async with get_session() as session:
        existe = (
            await session.execute(
                select(Promocion.id).where(
                    Promocion.tenant_id == tenant, Promocion.id == promocion_id, Promocion.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise PromoError("promocion_no_encontrada", "La promoción no existe.")
        total = (
            await session.execute(
                select(func.count()).select_from(Canje).where(Canje.tenant_id == tenant, Canje.promocion_id == promocion_id)
            )
        ).scalar_one()
        filas = (
            await session.execute(
                select(Canje, SubscriptionCharge)
                .outerjoin(
                    SubscriptionCharge,
                    (SubscriptionCharge.tenant_id == Canje.tenant_id) & (SubscriptionCharge.id == Canje.charge_id),
                )
                .where(Canje.tenant_id == tenant, Canje.promocion_id == promocion_id)
                .order_by(Canje.created_at.desc(), Canje.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return [(c, cargo) for c, cargo in filas], int(total)


def _auditar(session, tenant: uuid.UUID, actor: str, accion: str, antes: dict | None, promo: Promocion) -> None:  # type: ignore[no-untyped-def]
    """`audit_log` en la MISMA transacción que la mutación (quién y cuándo). Sin `actor` no se audita."""
    if not actor:
        return
    from core.db.models import AuditLog

    session.add(
        AuditLog(
            tenant_id=tenant,
            actor=actor,
            action=accion,
            input={"promocion_id": str(promo.id), "antes": antes},
            output=vista(promo),
        )
    )


# ----------------------------------------------------------------------------- cálculo y evaluación
def _paso(moneda: str | None) -> Decimal:
    """Redondeo del dinero (el mismo de `mismo_monto`/`prorrateo`): al peso en COP y al centavo en las demás."""
    return Decimal("1") if (moneda or "COP").upper() == "COP" else Decimal("0.01")


def calcular(precio: Decimal, tipo: str, valor: Decimal, moneda: str | None) -> tuple[Decimal, Decimal]:
    """`(descuento, neto)` sobre el precio de lista. Lanza `descuento_excede_precio` si el neto no queda
    positivo; el descuento se redondea con `ROUND_HALF_UP` al peso (COP) o al centavo."""
    paso = _paso(moneda)
    bruto = Decimal(precio).quantize(paso, rounding=ROUND_HALF_UP)
    bruto_exacto = Decimal(precio)
    if tipo == "porcentaje":
        descuento = (bruto_exacto * Decimal(valor) / Decimal(100)).quantize(paso, rounding=ROUND_HALF_UP)
    else:
        descuento = Decimal(valor).quantize(paso, rounding=ROUND_HALF_UP)
    if descuento >= bruto:
        raise SubscriptionError(_MENSAJES["descuento_excede_precio"], code="descuento_excede_precio")
    if descuento <= 0:
        # Una promoción que no descuenta nada (p. ej. 1 % de un valor mínimo) no es una promoción.
        raise SubscriptionError(_MENSAJES["codigo_no_valido"], code="codigo_no_valido")
    return descuento, bruto - descuento


@dataclass(frozen=True)
class Evaluacion:
    """Lo que una promoción hace al primer cobro (fotografía al momento de evaluar)."""

    promocion_id: uuid.UUID
    etiqueta: str
    tipo: str
    valor: Decimal
    periodos_total: int
    bruto: Decimal
    descuento: Decimal
    neto: Decimal

    def fotografia(self) -> dict[str, Any]:
        """Lo que queda en `charges.descuento` y `canjes.promocion`: inmutable aunque la promoción se edite."""
        return {
            "promocion_id": str(self.promocion_id),
            "etiqueta": self.etiqueta,
            "tipo": self.tipo,
            "valor": str(self.valor),
            "periodos_total": self.periodos_total,
            "descuento": str(self.descuento),
        }


def _rechazo(code: str, *, motivo: str | None = None) -> SubscriptionError:
    _rechazos_total.add(1, {"code": code})
    log.info("subs_promo_rechazada", code=code, motivo=motivo)
    return SubscriptionError(_MENSAJES[code], code=code)


async def _frenar_exceso(customer_id: uuid.UUID | None) -> None:
    """Cada intento con un código cuenta: por cliente si se sabe quién es, y por negocio si no. Pasado el cupo
    de la hora, se rechaza SIN evaluar (así adivinar códigos no sale gratis). Si Redis no responde, deja pasar."""
    from core.security.rate_limit import Bucket, hit

    clave = f"rl:promo:{_tenant()}:{customer_id or 'anon'}"
    cupo = _INTENTOS_POR_CLIENTE if customer_id else _INTENTOS_ANONIMOS
    if await hit(Bucket(key=clave, limit=cupo, window_s=3600), name="promo_codigo"):
        raise _rechazo("demasiados_intentos")


def _vigente(promo: Promocion, ahora: datetime) -> bool:
    if promo.vigente_desde is not None and ahora < promo.vigente_desde:
        return False
    return promo.vigente_hasta is None or ahora < promo.vigente_hasta


async def evaluar(
    session,  # type: ignore[no-untyped-def]
    *,
    codigo: str,
    plan_id: uuid.UUID,
    precio: Decimal,
    moneda: str | None,
    customer_id: uuid.UUID | None,
    huella_: str | None,
    ahora: datetime,
    contar_intento: bool = True,
    excluir_cargos: list[uuid.UUID] | None = None,
) -> Evaluacion:
    """Evalúa un código contra un plan, sin efectos (la reserva es `reservar`).

    Un código inexistente, apagado, vencido, agotado o de otro plan responde lo MISMO (`codigo_no_valido`):
    no revela si existe. `promo_ya_usada` solo se dice a quien ya canjeó (o comparte huella con quien canjeó)."""
    if Decimal(precio) <= 0:
        raise _rechazo("promo_no_aplica_a_free")
    texto = normalizar_codigo(codigo)
    if texto is None or not _FORMA_CODIGO.fullmatch(texto):
        if contar_intento:
            await _frenar_exceso(customer_id)
        raise _rechazo("codigo_no_valido", motivo="forma")
    if contar_intento:
        await _frenar_exceso(customer_id)
    tenant = _tenant()
    promo = (
        await session.execute(
            select(Promocion).where(
                Promocion.tenant_id == tenant,
                func.lower(Promocion.codigo) == texto,
                Promocion.activa.is_(True),
                Promocion.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if promo is None:
        raise _rechazo("codigo_no_valido", motivo="inexistente")
    if not _vigente(promo, ahora):
        raise _rechazo("codigo_no_valido", motivo="fuera_de_vigencia")
    if promo.plan_id is not None and promo.plan_id != plan_id:
        raise _rechazo("codigo_no_valido", motivo="otro_plan")
    if promo.max_canjes is not None and promo.canjes_usados >= promo.max_canjes:
        raise _rechazo("codigo_no_valido", motivo="agotada")
    if customer_id is not None:
        filtros = [Canje.tenant_id == tenant, Canje.promocion_id == promo.id, Canje.estado != CANJE_LIBERADO]
        if excluir_cargos:
            # Canjes de cobros que esta misma operación va a anular: no cuentan como «ya canjeó».
            filtros.append(Canje.charge_id.notin_(excluir_cargos))
        usado = (
            await session.execute(
                select(Canje.id).where(
                    *filtros,
                    or_(Canje.customer_id == customer_id, Canje.huella == huella_) if huella_ else Canje.customer_id == customer_id,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if usado is not None:
            raise _rechazo("promo_ya_usada")
    descuento, neto = calcular(Decimal(precio), promo.tipo_descuento, promo.valor, moneda)
    return Evaluacion(
        promocion_id=promo.id,
        etiqueta=promo.etiqueta,
        tipo=promo.tipo_descuento,
        valor=promo.valor,
        periodos_total=promo.periodos,
        bruto=Decimal(precio).quantize(_paso(moneda), rounding=ROUND_HALF_UP),
        descuento=descuento,
        neto=neto,
    )


async def id_por_codigo(session, codigo: str) -> uuid.UUID | None:  # type: ignore[no-untyped-def]
    """Id de la promoción con ese código, sin mirar vigencia ni cupo (para reconocer la que ya tiene aplicada
    una inscripción que se repite)."""
    texto = normalizar_codigo(codigo)
    if texto is None:
        return None
    return (
        await session.execute(
            select(Promocion.id).where(
                Promocion.tenant_id == _tenant(), func.lower(Promocion.codigo) == texto, Promocion.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


def huella_de(*, documento: str | None, telefono_canal: str | None, telefono_datos: str | None) -> str | None:
    """Huella de la persona para «un canje por persona»: el documento si lo hay; si no, el teléfono (el del
    canal, o el que dio en los datos). Es la misma del antiabuso del core (HMAC, nunca el dato en claro).

    Límite conocido: sin documento, dos teléfonos de la misma persona no se detectan."""
    from core.provisioning.antiabuso import huella, normalizar_documento, normalizar_telefono

    doc = normalizar_documento(documento)
    if doc:
        return huella("doc", doc)
    tel = normalizar_telefono(telefono_canal) or normalizar_telefono(telefono_datos)
    return huella("tel", tel) if tel else None


# ----------------------------------------------------------------------------- ciclo del canje
def aplicar_al_cargo(cargo: SubscriptionCharge, evaluacion: Evaluacion, *, consume: bool = True) -> None:
    """Pone en el cargo el descuento: `monto` es el NETO que se cobra, `monto_bruto` el de lista.

    `consume`: este cobro es el período 1 del descuento (inscripción, o contratar desde un plan gratuito).
    En un upgrade el descuento recae sobre la diferencia prorrateada y NO consume período: los N empiezan en el
    siguiente cargo completo (HU-SUB-35)."""
    cargo.monto = evaluacion.neto
    cargo.monto_bruto = evaluacion.bruto
    cargo.descuento = evaluacion.fotografia()
    cargo.periodo_n = 1 if consume else None
    cargo.consume_periodo = consume


def promocion_del_cargo(cargo: SubscriptionCharge) -> str | None:
    """Id (texto) de la promoción que descuenta este cargo, o None."""
    return (cargo.descuento or {}).get("promocion_id")


async def reservar(
    session,  # type: ignore[no-untyped-def]
    evaluacion: Evaluacion,
    *,
    sub,  # type: ignore[no-untyped-def]
    cargo: SubscriptionCharge,
    customer_id: uuid.UUID,
    huella_: str | None,
) -> Canje:
    """Reserva el canje del cargo (ya creado y con `flush`) en la MISMA transacción que la inscripción.

    Orden: canje (INSERT) → fila de la promoción (UPDATE atómico del cupo) → `charges.canje_id`. Si algo
    falla se lanza una `SubscriptionError` y se deshace TODO (suscripción, cargo y canje)."""
    tenant = _tenant()
    canje = Canje(
        tenant_id=tenant,
        promocion_id=evaluacion.promocion_id,
        customer_id=customer_id,
        subscription_id=sub.id,
        charge_id=cargo.id,
        huella=huella_,
        estado=CANJE_RESERVADO,
        promocion=evaluacion.fotografia(),
        periodos_total=evaluacion.periodos_total,
    )
    try:
        # Savepoint: un IntegrityError aborta la transacción entera de Postgres, y así se deshace solo esto.
        async with session.begin_nested():
            session.add(canje)
            await session.flush()
    except IntegrityError as exc:
        if "uq_pack_subscriptions_canjes_" in str(exc):
            raise _rechazo("promo_ya_usada", motivo="unico") from exc
        raise
    usada = (
        await session.execute(
            update(Promocion)
            .where(
                Promocion.tenant_id == tenant,
                Promocion.id == evaluacion.promocion_id,
                Promocion.activa.is_(True),
                Promocion.deleted_at.is_(None),
                or_(Promocion.max_canjes.is_(None), Promocion.canjes_usados < Promocion.max_canjes),
                # Lo que se evaluó sigue siendo lo que hay: si el dueño la editó a la vez, no se cobra a ciegas.
                Promocion.tipo_descuento == evaluacion.tipo,
                Promocion.valor == evaluacion.valor,
                Promocion.periodos == evaluacion.periodos_total,
            )
            .values(canjes_usados=Promocion.canjes_usados + 1)
            .returning(Promocion.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if usada is None:
        raise _rechazo(await _causa_sin_cupo(session, tenant, evaluacion.promocion_id), motivo="reserva")
    cargo.canje_id = canje.id
    await session.flush()
    _canjes_total.add(1, {"estado": CANJE_RESERVADO})
    log.info(
        "subs_canje_reservado",
        canje_id=str(canje.id),
        promocion_id=str(evaluacion.promocion_id),
        charge_id=str(cargo.id),
    )
    log.info("subs_promo_aplicada", promocion_id=str(evaluacion.promocion_id), subscription_id=str(sub.id))
    return canje


async def _causa_sin_cupo(session, tenant: uuid.UUID, promocion_id: uuid.UUID) -> str:  # type: ignore[no-untyped-def]
    """Por qué el UPDATE del cupo no tocó filas: sin cupo (`promo_agotada`) o la promoción cambió o se apagó."""
    promo = (
        await session.execute(
            select(Promocion).where(Promocion.tenant_id == tenant, Promocion.id == promocion_id)
        )
    ).scalar_one_or_none()
    if promo is not None and promo.activa and promo.deleted_at is None:
        if promo.max_canjes is not None and promo.canjes_usados >= promo.max_canjes:
            return "promo_agotada"
    return "codigo_no_valido"


async def liberar_por_cargos(session, cargos: list[SubscriptionCharge]) -> int:  # type: ignore[no-untyped-def]
    """Devuelve el cupo de los canjes `reservado` de estos cargos, que acaban de quedar `anulado`.

    Se llama desde TODO sitio que anula un cargo (ver la prueba que los recorre). Idempotente y condicional:
    solo toca canjes `reservado`, así dos caminos que anulan a la vez no descuentan dos veces. Los canjes se
    toman por promoción en orden (sin deadlock entre dos transacciones que liberan varias)."""
    ids = [c.id for c in cargos]
    if not ids:
        return 0
    tenant = _tenant()
    canjes = (
        await session.execute(
            select(Canje)
            .where(Canje.tenant_id == tenant, Canje.charge_id.in_(ids), Canje.estado == CANJE_RESERVADO)
            .order_by(Canje.promocion_id, Canje.id)
            .with_for_update()
        )
    ).scalars().all()
    for canje in canjes:
        canje.estado = CANJE_LIBERADO
        await session.flush()
        await session.execute(
            update(Promocion)
            .where(Promocion.tenant_id == tenant, Promocion.id == canje.promocion_id, Promocion.canjes_usados > 0)
            .values(canjes_usados=Promocion.canjes_usados - 1)
            .execution_options(synchronize_session=False)
        )
        _canjes_total.add(1, {"estado": CANJE_LIBERADO})
        log.info("subs_canje_liberado", canje_id=str(canje.id), promocion_id=str(canje.promocion_id))
    return len(canjes)


async def confirmar_por_cargo(session, cargo: SubscriptionCharge, sub: Subscription | None = None) -> bool:  # type: ignore[no-untyped-def]
    """El cargo con descuento se pagó y aplicó: su canje `reservado` pasa a `confirmado`. Idempotente.

    Con `sub` (la suscripción bloqueada), además deja el descuento vigente en ella: quedan `N − 1` períodos si
    este cobro consumió el primero (inscripción, desde un plan gratuito) o `N` si fue la diferencia de un upgrade.
    Sin períodos por delante (descuento de uno), el canje queda `agotado` y la suscripción sin descuento."""
    if cargo.canje_id is None:
        return False
    confirmado = (
        await session.execute(
            update(Canje)
            .where(Canje.tenant_id == _tenant(), Canje.id == cargo.canje_id, Canje.estado == CANJE_RESERVADO)
            .values(estado=CANJE_CONFIRMADO)
            .returning(Canje.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if confirmado is None:
        return False
    _canjes_total.add(1, {"estado": CANJE_CONFIRMADO})
    if cargo.monto_bruto is not None:
        _descuento_total.add(float(Decimal(cargo.monto_bruto) - Decimal(cargo.monto)))
    log.info("subs_canje_confirmado", canje_id=str(cargo.canje_id), charge_id=str(cargo.id))
    if sub is not None:
        await _otorgar_a_suscripcion(session, sub, cargo)
    return True


def _snapshot_vigente(foto: dict[str, Any], canje_id: uuid.UUID) -> dict[str, Any]:
    return {
        "promocion_id": foto["promocion_id"],
        "canje_id": str(canje_id),
        "etiqueta": foto.get("etiqueta"),
        "tipo": foto["tipo"],
        "valor": foto["valor"],
        "periodos_total": int(foto["periodos_total"]),
    }


async def _cerrar_canje(session, canje_id: uuid.UUID, estado: str) -> bool:  # type: ignore[no-untyped-def]
    """Pasa el canje `confirmado` a un estado terminal (`agotado`/`terminado`). Condicional e idempotente."""
    cerrado = (
        await session.execute(
            update(Canje)
            .where(Canje.tenant_id == _tenant(), Canje.id == canje_id, Canje.estado == CANJE_CONFIRMADO)
            .values(estado=estado)
            .returning(Canje.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if cerrado is not None:
        _canjes_total.add(1, {"estado": estado})
    return cerrado is not None


def _limpiar_descuento(sub: Subscription) -> None:
    sub.promocion_id = None
    sub.descuento = None
    sub.descuento_periodos_restantes = None


async def _otorgar_a_suscripcion(session, sub: Subscription, cargo: SubscriptionCharge) -> None:  # type: ignore[no-untyped-def]
    foto = cargo.descuento or {}
    total = int(foto.get("periodos_total") or 1)
    restantes = total - (1 if cargo.consume_periodo else 0)
    if restantes <= 0:
        _limpiar_descuento(sub)
        await _cerrar_canje(session, cargo.canje_id, CANJE_AGOTADO)
        log.info("subs_descuento_agotado", subscription_id=str(sub.id), canje_id=str(cargo.canje_id))
        return
    sub.promocion_id = uuid.UUID(foto["promocion_id"])
    sub.descuento = _snapshot_vigente(foto, cargo.canje_id)
    sub.descuento_periodos_restantes = restantes
    await session.flush()
    log.info(
        "subs_descuento_otorgado",
        subscription_id=str(sub.id),
        canje_id=str(cargo.canje_id),
        periodos_total=total,
        restantes=restantes,
    )


async def terminar_descuento(session, sub: Subscription, *, motivo: str) -> bool:  # type: ignore[no-untyped-def]
    """El descuento vigente de la suscripción acaba antes de tiempo (cancelación o cualquier cambio de plan):
    el canje queda `terminado` y la suscripción sin descuento. Sigue contando para el límite por cliente."""
    if sub.promocion_id is None:
        return False
    canje_id = uuid.UUID((sub.descuento or {})["canje_id"])
    restantes = sub.descuento_periodos_restantes
    _limpiar_descuento(sub)
    await _cerrar_canje(session, canje_id, CANJE_TERMINADO)
    await session.flush()
    log.info(
        "subs_descuento_terminado",
        subscription_id=str(sub.id),
        canje_id=str(canje_id),
        motivo=motivo,
        restantes=restantes,
    )
    return True


async def aplicar_descuento_vigente(session, sub: Subscription, cargo: SubscriptionCharge) -> bool:  # type: ignore[no-untyped-def]
    """Si la suscripción tiene descuento vigente, lo pone en el cargo de período recién creado (a precio de
    lista) y consume UN período, en la MISMA transacción: el único `uq_pack_subscriptions_charges_periodo`
    impide un segundo cargo del período, así que no se consume dos veces. La suscripción debe venir bloqueada.

    Devuelve True si descontó. El agotamiento del último período deja el canje `agotado`."""
    if sub.promocion_id is None or not sub.descuento_periodos_restantes:
        return False
    d = sub.descuento or {}
    bruto = Decimal(sub.valor_periodo or 0)
    try:
        descuento, neto = calcular(bruto, d["tipo"], Decimal(d["valor"]), sub.moneda)
    except SubscriptionError:
        # El precio de la suscripción ya no admite el descuento (no debería pasar: cambiar de plan lo termina).
        log.warning("subs_descuento_no_aplicable", subscription_id=str(sub.id), canje_id=d.get("canje_id"))
        return False
    total = int(d["periodos_total"])
    restantes = int(sub.descuento_periodos_restantes)
    cargo.monto = neto
    cargo.monto_bruto = bruto
    cargo.descuento = {
        "promocion_id": d["promocion_id"],
        "etiqueta": d.get("etiqueta"),
        "tipo": d["tipo"],
        "valor": d["valor"],
        "periodos_total": total,
        "descuento": str(descuento),
    }
    cargo.canje_id = uuid.UUID(d["canje_id"])
    cargo.periodo_n = total - restantes + 1
    cargo.consume_periodo = True
    if restantes - 1 <= 0:
        canje_id = cargo.canje_id
        _limpiar_descuento(sub)
        await _cerrar_canje(session, canje_id, CANJE_AGOTADO)
        log.info("subs_descuento_agotado", subscription_id=str(sub.id), canje_id=str(canje_id))
    else:
        sub.descuento_periodos_restantes = restantes - 1
    _consumidos_total.add(1)
    log.info(
        "subs_descuento_consumido",
        subscription_id=str(sub.id),
        periodo_n=cargo.periodo_n,
        restantes=restantes - 1,
    )
    return True


async def descuentos_inconsistentes(session) -> list[uuid.UUID]:  # type: ignore[no-untyped-def]
    """Suscripciones con descuento vigente cuyo `restantes` no es `periodos_total − cargos no anulados del canje
    que consumen período` (HU-SUB-35, invariante). El cron diario las loguea; no corrige nada solo."""
    tenant = _tenant()
    consumidos = (
        select(SubscriptionCharge.canje_id, func.count().label("n"))
        .where(
            SubscriptionCharge.tenant_id == tenant,
            SubscriptionCharge.consume_periodo.is_(True),
            SubscriptionCharge.estado != "anulado",
            SubscriptionCharge.canje_id.isnot(None),
            SubscriptionCharge.deleted_at.is_(None),
        )
        .group_by(SubscriptionCharge.canje_id)
        .subquery()
    )
    filas = (
        await session.execute(
            select(Subscription.id, Subscription.descuento, Subscription.descuento_periodos_restantes, consumidos.c.n)
            .select_from(Subscription)
            .outerjoin(
                consumidos,
                consumidos.c.canje_id == func.cast(Subscription.descuento["canje_id"].astext, UUID(as_uuid=True)),
            )
            .where(
                Subscription.tenant_id == tenant,
                Subscription.promocion_id.isnot(None),
                Subscription.deleted_at.is_(None),
            )
        )
    ).all()
    malas = []
    for sub_id, descuento, restantes, n in filas:
        total = int((descuento or {}).get("periodos_total") or 0)
        if int(restantes or 0) != total - int(n or 0):
            malas.append(sub_id)
    return malas


async def resumen_de_descuentos(session) -> tuple[int, Decimal]:  # type: ignore[no-untyped-def]
    """`(suscripciones con descuento, descuento mensual vigente en la moneda del negocio)` para los gauges."""
    filas = (
        await session.execute(
            select(Subscription.valor_periodo, Subscription.descuento).where(
                Subscription.tenant_id == _tenant(),
                Subscription.promocion_id.isnot(None),
                Subscription.estado == "activa",
                Subscription.deleted_at.is_(None),
            )
        )
    ).all()
    total = Decimal(0)
    for valor, d in filas:
        try:
            descuento, _ = calcular(Decimal(valor or 0), (d or {})["tipo"], Decimal(d["valor"]), "COP")
        except SubscriptionError:
            continue
        total += descuento
    return len(filas), total


async def contar_reservados_antiguos(session, *, dias: int) -> int:  # type: ignore[no-untyped-def]
    """Canjes `reservado` de hace más de `dias` días: la fuga de cupo que el gauge `canjes_reservados_antiguos`
    vigila (un cargo que se quedó sin anular, o un pago recibido que no se pudo aplicar)."""
    limite = datetime.now(UTC) - timedelta(days=dias)
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Canje)
                .where(
                    Canje.tenant_id == _tenant(),
                    Canje.estado == CANJE_RESERVADO,
                    Canje.created_at < limite,
                )
            )
        ).scalar_one()
    )

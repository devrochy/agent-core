"""Antiabuso y cumplimiento del alta automática (HU-SH-07, ADR-0125).

Dos momentos:

1. **Al pedir el plan** (la tool `crear_pedido` de commerce, solo en un negocio vendedor):
   `preparar_pedido_de_alta` decide si el pedido sigue y `registrar_pedido_de_alta` deja su
   registro en la misma transacción del pedido.
   - Términos, privacidad y DPA: el comprador acepta el juego de versiones vigentes. Sin
     aceptación, el pedido no se crea y la tool devuelve los enlaces.
   - Cupo de pedidos de alta por hora desde un mismo origen (`ALTA_PEDIDOS_POR_HORA`).
   - Un Free por persona: si el teléfono o el documento ya tuvo un Free en los últimos 12
     meses, la tool pide ofrecer un plan pago.
2. **Al dar de alta** (el consumidor de `subscription.activated`): `motivos_de_revision`
   junta lo que manda el alta a revisión humana: el interruptor del master, la aceptación
   que falta, un Free repetido que se coló y las señales de riesgo (dominio desechable,
   muchos intentos, datos inconsistentes).

**Datos mínimos.** El teléfono y el documento se guardan como HMAC-SHA256 con una clave
derivada de `APP_SECRET_KEY` (`tel:<hex>`, `doc:<hex>`), nunca en claro: alcanza para saber
si se repiten, y sin la clave no se pueden recuperar probando números (un SHA-256 a secas de
un celular se revierte en minutos). Se vacían a los 12 meses (`olvidar_huellas_vencidas`).

Este módulo lo puede importar un pack (como `core.provisioning.fuentes`): no da de alta
negocios.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select, update

from core.db.models import Customer, Message, PedidoDeAlta, Tenant
from core.db.session import get_session
from core.provisioning import interruptor, legal
from core.provisioning.vendedores import es_vendedor
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope


@asynccontextmanager
async def _sesion_del(vendedor: uuid.UUID) -> AsyncIterator[Any]:
    """Sesión bajo el `tenant_scope` del vendedor, sea cual sea el de quien llama: si no,
    la RLS devolvería 0 filas en silencio y las reglas dejarían pasar el alta."""
    async with tenant_scope(vendedor):
        async with get_session() as session:
            yield session

log = get_logger(__name__)

DOCE_MESES = timedelta(days=365)
VENTANA_INTENTOS = timedelta(hours=24)
_CONTEXTO_CLAVE = b"agentcore:antiabuso-alta:v1"
_NO_DIGITOS = re.compile(r"\D+")
_NO_ALFANUM = re.compile(r"[^0-9A-Z]+")
# NIT con dígito de verificación: «900.123.456-7» y «900123456» son el mismo.
_NIT_CON_DV = re.compile(r"^[\d.\s]+-\s*\d$")
_TIPO_DE_DOCUMENTO = re.compile(r"^(?:C\.?\s?C|C\.?\s?E|T\.?\s?I|NIT)[\s.:#-]*(?=\d)")

# Señales de datos inconsistentes que se guardan (sin el dato).
SENAL_DOCUMENTO = "documento_invalido"
SENAL_TELEFONO = "telefono_invalido"


# ------------------------------------------------------------------ huellas
def _clave() -> bytes:
    from core.config import get_settings

    return hmac.new(get_settings().app_secret_key.encode(), _CONTEXTO_CLAVE, hashlib.sha256).digest()


def normalizar_telefono(valor: Any) -> str | None:
    """Últimos 10 dígitos: «+57 300 123 4567», «573001234567» y «3001234567» son el mismo.
    None si no parece un teléfono (menos de 7 o más de 15 dígitos)."""
    digitos = _NO_DIGITOS.sub("", str(valor or ""))
    if not 7 <= len(digitos) <= 15:
        return None
    return digitos[-10:]


def normalizar_documento(valor: Any) -> str | None:
    """Mayúsculas y alfanuméricos, sin el tipo delante (CC, CE, TI, NIT), sin el
    dígito de verificación de un NIT escrito con guion y sin ceros a la izquierda. None si
    no parece un documento (menos de 5 o más de 15 caracteres, o un solo carácter repetido).

    Un NIT con el dígito de verificación pegado (sin guion) no se puede distinguir de otro
    número: queda como otra huella."""
    texto = str(valor or "").strip().upper()
    texto = _TIPO_DE_DOCUMENTO.sub("", texto)
    if _NIT_CON_DV.match(texto):
        texto = texto.rsplit("-", 1)[0]
    limpio = _NO_ALFANUM.sub("", texto)
    if limpio.isdigit():
        limpio = limpio.lstrip("0")
    if not 5 <= len(limpio) <= 15 or len(set(limpio)) == 1:
        return None
    return limpio


def huella(tipo: str, valor: str) -> str:
    """`<tipo>:<HMAC-SHA256(clave, tipo:valor)>`. El tipo separa un teléfono de un documento
    con los mismos dígitos."""
    digest = hmac.new(_clave(), f"{tipo}:{valor}".encode(), hashlib.sha256).hexdigest()
    return f"{tipo}:{digest}"


def telefono_del_canal(cliente: Customer | None) -> str | None:
    """El número del que escribe (WhatsApp): el origen que cuesta rotar."""
    if cliente is None:
        return None
    if cliente.channel == "whatsapp" and cliente.external_id:
        return cliente.external_id.split(":", 1)[-1]
    return cliente.contact_phone


# ------------------------------------------------------------------ dominios desechables
@lru_cache(maxsize=1)
def _dominios_desechables() -> frozenset[str]:
    ruta = Path(__file__).with_name("dominios_desechables.txt")
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    return frozenset(
        linea.strip().lower() for linea in lineas if linea.strip() and not linea.lstrip().startswith("#")
    )


def dominio_desechable(email: str) -> bool:
    """¿El correo es de un servicio de correo temporal? Mira el dominio y sus padres
    (`x.mailinator.com` también cuenta)."""
    dominio = (email or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")
    partes = dominio.split(".")
    lista = _dominios_desechables()
    return any(".".join(partes[i:]) in lista for i in range(len(partes) - 1))


# ------------------------------------------------------------------ al pedir el plan
@dataclass
class PedidoPreparado:
    """Lo que `crear_pedido` guarda junto al pedido de un plan de la plataforma."""

    vendedor: uuid.UUID
    customer_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    canal: str | None
    gratis: bool
    huellas: list[str] = field(default_factory=list)
    senales: list[str] = field(default_factory=list)
    aceptacion: dict[str, Any] | None = None
    aceptado_en: datetime | None = None


async def _cliente(vendedor: uuid.UUID, customer_id: uuid.UUID | None) -> Customer | None:
    if customer_id is None:
        return None
    async with _sesion_del(vendedor) as session:
        return (
            await session.execute(
                select(Customer).where(Customer.tenant_id == vendedor, Customer.id == customer_id)
            )
        ).scalar_one_or_none()


def _huellas_y_senales(
    telefono_canal: str | None, telefono: Any, documento: Any
) -> tuple[list[str], list[str]]:
    huellas: list[str] = []
    senales: list[str] = []
    for valor in (telefono_canal, telefono):
        normal = normalizar_telefono(valor)
        if normal:
            huellas.append(huella("tel", normal))
    if str(telefono or "").strip() and not normalizar_telefono(telefono):
        senales.append(SENAL_TELEFONO)
    doc = normalizar_documento(documento)
    if doc:
        huellas.append(huella("doc", doc))
    elif str(documento or "").strip():
        senales.append(SENAL_DOCUMENTO)
    return sorted(set(huellas)), senales


def _misma_persona(huellas: list[str], customer_id: uuid.UUID | None) -> Any:
    condiciones = []
    if huellas:
        condiciones.append(PedidoDeAlta.huellas.overlap(huellas))
    if customer_id is not None:
        condiciones.append(PedidoDeAlta.customer_id == customer_id)
    return or_(*condiciones) if condiciones else None


async def free_previo(
    vendedor: uuid.UUID,
    *,
    huellas: list[str],
    customer_id: uuid.UUID | None,
    excluir_referencia: str | None = None,
    ahora: datetime | None = None,
) -> bool:
    """¿Esta persona (mismo cliente del chat, teléfono o documento) ya pidió un Free en los
    últimos 12 meses?"""
    misma = _misma_persona(huellas, customer_id)
    if misma is None:
        return False
    desde = (ahora or datetime.now(UTC)) - DOCE_MESES
    consulta = select(PedidoDeAlta.id).where(
        PedidoDeAlta.tenant_id == vendedor,
        PedidoDeAlta.deleted_at.is_(None),
        PedidoDeAlta.gratis.is_(True),
        PedidoDeAlta.created_at >= desde,
        misma,
    )
    if excluir_referencia:
        consulta = consulta.where(PedidoDeAlta.referencia != excluir_referencia)
    async with _sesion_del(vendedor) as session:
        return (await session.execute(consulta.limit(1))).first() is not None


async def intentos_recientes(
    vendedor: uuid.UUID, *, huellas: list[str], customer_id: uuid.UUID | None, ahora: datetime | None = None
) -> int:
    """Pedidos de alta de esta persona en las últimas 24 h (incluido el actual)."""
    misma = _misma_persona(huellas, customer_id)
    if misma is None:
        return 0
    desde = (ahora or datetime.now(UTC)) - VENTANA_INTENTOS
    async with _sesion_del(vendedor) as session:
        return int(
            (
                await session.execute(
                    select(func.count(PedidoDeAlta.id)).where(
                        PedidoDeAlta.tenant_id == vendedor,
                        PedidoDeAlta.deleted_at.is_(None),
                        PedidoDeAlta.created_at >= desde,
                        misma,
                    )
                )
            ).scalar_one()
        )


# ------------------------------------------------------------------ resultados que frenan
# El core decide; el pack que llama arma la respuesta de su tool (y su texto para el modelo).
@dataclass(frozen=True)
class TerminosPendientes:
    """Falta que el cliente acepte el juego de versiones vigente. `token` es lo que la tool
    tiene que recibir de vuelta cuando el cliente acepte, en esta misma conversación."""

    documentos: list[dict[str, str]]
    token: str


@dataclass(frozen=True)
class LimiteDeAltas:
    """El origen superó el cupo de pedidos de alta de la última hora."""


@dataclass(frozen=True)
class FreeNoDisponible:
    """Esta persona ya usó un Free en los últimos 12 meses: ofrecer un plan pago."""


Frenado = TerminosPendientes | LimiteDeAltas | FreeNoDisponible

_TTL_TERMINOS_S = 24 * 3600


def token_de_terminos(conversation_id: uuid.UUID | None, version: str) -> str:
    """Token de la aceptación: HMAC de la conversación y del juego de versiones. No sirve en
    otra conversación ni para otro juego (si se publica una versión, cambia)."""
    mensaje = f"terminos:{conversation_id}:{version}".encode()
    return hmac.new(_clave(), mensaje, hashlib.sha256).hexdigest()[:16]


def _clave_de_terminos(vendedor: uuid.UUID, conversation_id: uuid.UUID | None) -> str:
    return f"alta:terminos:{vendedor}:{conversation_id}"


async def _ultimo_inbound(vendedor: uuid.UUID, conversation_id: uuid.UUID) -> datetime | None:
    """Hora (de la base) del último mensaje del cliente en la conversación."""
    async with _sesion_del(vendedor) as session:
        return (
            await session.execute(
                select(func.max(Message.created_at)).where(
                    Message.tenant_id == vendedor,
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                )
            )
        ).scalar_one_or_none()


async def _anotar_terminos_mostrados(vendedor: uuid.UUID, conversation_id: uuid.UUID | None) -> None:
    """Hasta qué mensaje del cliente se mostraron los enlaces: la aceptación tiene que venir
    en un mensaje POSTERIOR. Se guarda la hora de la base del último mensaje del cliente (no
    el reloj de la app), así el mensaje que disparó este turno nunca cuenta como aceptación."""
    from core.memory.working import get_redis

    if conversation_id is None:
        return
    ultimo = await _ultimo_inbound(vendedor, conversation_id)
    marca = ultimo.isoformat() if ultimo else datetime(1970, 1, 1, tzinfo=UTC).isoformat()
    try:
        await get_redis().set(_clave_de_terminos(vendedor, conversation_id), marca, ex=_TTL_TERMINOS_S)
    except Exception:
        log.warning("alta_terminos_sin_redis", tenant_id=str(vendedor), exc_info=True)


async def _mensaje_que_acepta(vendedor: uuid.UUID, conversation_id: uuid.UUID | None) -> str | None:
    """El último mensaje del cliente posterior a que se mostraran los enlaces, o None. Sin
    ese mensaje (el modelo repitió el token en el mismo turno, lo trajo de otra conversación
    o Redis no responde), no hay aceptación: falla cerrado."""
    from core.memory.working import get_redis

    if conversation_id is None:
        return None
    try:
        mostrado = await get_redis().get(_clave_de_terminos(vendedor, conversation_id))
    except Exception:
        log.warning("alta_terminos_sin_redis", tenant_id=str(vendedor), exc_info=True)
        return None
    if not mostrado:
        return None
    desde = datetime.fromisoformat(mostrado.decode() if isinstance(mostrado, bytes) else str(mostrado))
    async with _sesion_del(vendedor) as session:
        mensaje = (
            await session.execute(
                select(Message.id)
                .where(
                    Message.tenant_id == vendedor,
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                    Message.created_at > desde,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return str(mensaje) if mensaje else None


async def _limite_por_origen(vendedor: uuid.UUID, origen: str) -> bool:
    """True si el origen superó el cupo de pedidos de alta de la última hora."""
    from core.config import get_settings
    from core.security.rate_limit import Bucket, hit

    cupo = Bucket(
        key=f"rl:alta:{vendedor}:{origen}",
        limit=get_settings().alta_pedidos_por_hora,
        window_s=3600,
    )
    return await hit(cupo, name="alta_pedidos")


async def preparar_pedido_de_alta(
    *,
    customer_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    canal: str | None,
    telefono: Any,
    documento: Any,
    gratis: bool,
    acepta_terminos: Any,
) -> PedidoPreparado | Frenado | None:
    """Antes de crear el pedido de un plan en el negocio actual.

    - None: el negocio no vende planes de la plataforma; nada que hacer.
    - `TerminosPendientes`, `LimiteDeAltas` o `FreeNoDisponible`: el pedido no se crea; el
      pack arma la respuesta de su tool.
    - `PedidoPreparado`: el pedido sigue; se registra con `registrar_pedido_de_alta`.

    `gratis` lo decide quien llama según el plan de la plataforma de la línea (no el total).
    """
    vendedor = get_current_tenant_id()
    if vendedor is None or not await es_vendedor(vendedor):
        return None

    # 1. Términos, privacidad y DPA vigentes. Sin los tres publicados, el pedido sigue sin
    #    aceptación y el alta va a revisión (no se frena la venta por un olvido del master).
    vigentes = await legal.documentos_vigentes(vendedor)
    faltan = legal.faltantes(vigentes)
    aceptacion: dict[str, Any] | None = None
    if not faltan:
        version = legal.version_legal(vigentes)
        token = token_de_terminos(conversation_id, version)
        mensaje_id = None
        if hmac.compare_digest(str(acepta_terminos or "").strip().encode(), token.encode()):
            mensaje_id = await _mensaje_que_acepta(vendedor, conversation_id)
        if mensaje_id is None:
            await _anotar_terminos_mostrados(vendedor, conversation_id)
            return TerminosPendientes(
                documentos=[vigentes[tipo].para_aceptar() for tipo in legal.TIPOS], token=token
            )
        aceptacion = {
            "documentos": [vigentes[tipo].para_aceptar() for tipo in legal.TIPOS],
            "version_legal": version,
            "mensaje_id": mensaje_id,
        }
    else:
        log.warning("alta_sin_documentos_legales", tenant_id=str(vendedor), faltan=faltan)

    # 2. Cupo por origen (el cliente del chat). Cuenta cada pedido que llega hasta acá. Sin
    #    origen no hay cupo propio: se frena (falla cerrado) en vez de compartir uno.
    origen = customer_id or conversation_id
    if origen is None or await _limite_por_origen(vendedor, str(origen)):
        log.warning("alta_limite_por_origen", tenant_id=str(vendedor), customer_id=str(customer_id))
        return LimiteDeAltas()

    cliente = await _cliente(vendedor, customer_id)
    huellas, senales = _huellas_y_senales(telefono_del_canal(cliente), telefono, documento)

    # 3. Un Free por persona (12 meses). La respuesta no dice qué dato coincidió.
    if gratis and await free_previo(vendedor, huellas=huellas, customer_id=customer_id):
        log.info("alta_free_repetido", tenant_id=str(vendedor), customer_id=str(customer_id))
        return FreeNoDisponible()

    return PedidoPreparado(
        vendedor=vendedor,
        customer_id=customer_id,
        conversation_id=conversation_id,
        canal=(canal or None),
        gratis=gratis,
        huellas=huellas,
        senales=senales,
        aceptacion=aceptacion,
        aceptado_en=datetime.now(UTC) if aceptacion else None,
    )


def registrar_pedido_de_alta(session: Any, pedido: PedidoPreparado, *, referencia: str) -> None:
    """Agrega el registro del pedido de alta a la sesión del pedido: se escribe en la misma
    transacción (si el pedido no se crea, tampoco queda la aceptación)."""
    session.add(
        PedidoDeAlta(
            id=uuid.uuid4(),
            tenant_id=pedido.vendedor,
            referencia=referencia,
            customer_id=pedido.customer_id,
            conversation_id=pedido.conversation_id,
            canal=pedido.canal,
            gratis=pedido.gratis,
            huellas=pedido.huellas,
            senales=pedido.senales,
            aceptacion=pedido.aceptacion,
            aceptado_en=pedido.aceptado_en,
        )
    )


# ------------------------------------------------------------------ al dar de alta
async def pedido_de_alta(vendedor: uuid.UUID, referencia: str) -> PedidoDeAlta | None:
    async with _sesion_del(vendedor) as session:
        return (
            await session.execute(
                select(PedidoDeAlta).where(
                    PedidoDeAlta.tenant_id == vendedor,
                    PedidoDeAlta.referencia == referencia,
                    PedidoDeAlta.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


async def motivos_de_revision(
    vendedor: uuid.UUID,
    *,
    referencia: str,
    email: str,
    gratis: bool,
    customer_id: uuid.UUID | None,
) -> list[str]:
    """Por qué el alta tiene que pasar por una persona; vacío si puede seguir sola."""
    from core.config import get_settings

    motivos: list[str] = []
    if await interruptor.pausado(vendedor):
        motivos.append("el alta automática está pausada por el master")
    pedido = await pedido_de_alta(vendedor, referencia)
    if pedido is None or pedido.aceptacion is None:
        motivos.append("no hay aceptación registrada de términos, privacidad y DPA")
    huellas = list(pedido.huellas or []) if pedido else []
    cliente = customer_id or (pedido.customer_id if pedido else None)
    if gratis and await free_previo(
        vendedor, huellas=huellas, customer_id=cliente, excluir_referencia=referencia
    ):
        motivos.append("un Free por persona: ese teléfono o documento ya tuvo un Free en los últimos 12 meses")
    if dominio_desechable(email):
        motivos.append("señal de riesgo: correo de un dominio desechable")
    intentos = await intentos_recientes(vendedor, huellas=huellas, customer_id=cliente)
    if intentos >= get_settings().alta_intentos_revision:
        motivos.append(f"señal de riesgo: {intentos} pedidos de alta de la misma persona en 24 h")
    if pedido is not None and pedido.senales:
        motivos.append(f"señal de riesgo: datos inconsistentes ({', '.join(sorted(pedido.senales))})")
    return motivos


async def aceptacion_del_pedido(vendedor: uuid.UUID, referencia: str) -> dict[str, Any] | None:
    """Versiones aceptadas y cuándo, para dejarlas en el negocio nuevo."""
    pedido = await pedido_de_alta(vendedor, referencia)
    if pedido is None or pedido.aceptacion is None or pedido.aceptado_en is None:
        return None
    return {
        "documentos": [
            {"tipo": d.get("tipo"), "version": d.get("version"), "url": d.get("url")}
            for d in (pedido.aceptacion.get("documentos") or [])
        ],
        "aceptado_en": pedido.aceptado_en.isoformat(),
    }


# ------------------------------------------------------------------ retención
async def olvidar_huellas_vencidas(ahora: datetime | None = None) -> int:
    """Vacía las huellas de más de 12 meses en cada vendedor: ya no sirven para ninguna
    regla. La fila (con la aceptación) se conserva: es la prueba del contrato."""
    limite = (ahora or datetime.now(UTC)) - DOCE_MESES
    total = 0
    # Todos los negocios, no solo la allowlist de hoy: uno que dejó de vender también
    # tiene que olvidar sus huellas. `tenants` es tabla de sistema (sin RLS).
    async with get_session() as session:
        negocios = list((await session.execute(select(Tenant.id))).scalars().all())
    for vendedor in negocios:
        try:
            async with _sesion_del(vendedor) as session:
                total += (
                    await session.execute(
                        update(PedidoDeAlta)
                        .where(
                            PedidoDeAlta.tenant_id == vendedor,
                            PedidoDeAlta.created_at < limite,
                            func.cardinality(PedidoDeAlta.huellas) > 0,
                        )
                        .values(huellas=[], updated_at=func.now())
                    )
                ).rowcount or 0
        except Exception:
            log.exception("alta_huellas_retencion_fallida", tenant_id=str(vendedor))
    if total:
        log.info("alta_huellas_olvidadas", filas=total)
    return total


__all__ = [
    "DOCE_MESES",
    "SENAL_DOCUMENTO",
    "SENAL_TELEFONO",
    "FreeNoDisponible",
    "Frenado",
    "LimiteDeAltas",
    "PedidoPreparado",
    "TerminosPendientes",
    "aceptacion_del_pedido",
    "dominio_desechable",
    "free_previo",
    "huella",
    "intentos_recientes",
    "motivos_de_revision",
    "normalizar_documento",
    "normalizar_telefono",
    "olvidar_huellas_vencidas",
    "pedido_de_alta",
    "preparar_pedido_de_alta",
    "registrar_pedido_de_alta",
    "telefono_del_canal",
    "token_de_terminos",
]

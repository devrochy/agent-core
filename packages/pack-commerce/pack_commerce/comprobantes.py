"""Lectura y comparación de comprobantes de transferencia (SPEC-48, HU-AI-01, ADR-0203).

La visión LLM (`core.ia.estructurado.reconocer_imagen`, playbook de kernel `comprobar_comprobante`)
solo COPIA lo que se ve en la imagen al esquema `ComprobanteTransferencia`; es dato no confiable.
La decisión es de `evaluar_comprobante`, una función pura y determinista (regla D4 de SPEC-48):

- `pagado` solo con coincidencia fuerte: monto exacto (±1 de la moneda), cuenta del negocio
  (últimos 4 dígitos + entidad; el número completo si el comprobante lo muestra), estado exitoso,
  fecha no anterior al pedido, no duplicado y lectura con confianza suficiente.
- `rechazado` si el monto se lee y es menor que el total, o el comprobante dice que la
  transferencia falló.
- `en_revision` en cualquier otro caso (una persona decide), con los motivos.

Nada de aquí escribe en la base ni manda mensajes; la acción `comprobar_comprobante` (HU-COM-24) lo
usa y guarda la evidencia. Los datos que se guardan llevan la cuenta solo con sus últimos 4 dígitos.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PLAYBOOK = "comprobar_comprobante"
PROPOSITO = "comprobante_transferencia"
MAX_TOKENS = 400
TOLERANCIA = Decimal("1")
CONFIANZA_MINIMA = 0.6
_DIGITOS = re.compile(r"\D")

Resultado = Literal["pagado", "en_revision", "rechazado"]


class ComprobanteTransferencia(BaseModel):
    """Lo que la visión copia del comprobante. Todo opcional: lo que no se ve queda nulo."""

    model_config = ConfigDict(extra="ignore")

    monto: float | None = Field(default=None, ge=0, description="Valor transferido, sin símbolos ni separadores.")
    moneda: str | None = Field(default=None, max_length=8)
    fecha: str | None = Field(default=None, max_length=25, description="AAAA-MM-DD o AAAA-MM-DDTHH:MM")
    estado: Literal["exitoso", "pendiente", "rechazado"] | None = None
    cuenta_destino: str | None = Field(default=None, max_length=40, description="Dígitos visibles de la cuenta que recibe")
    titular_destino: str | None = Field(default=None, max_length=80)
    banco: str | None = Field(default=None, max_length=60)
    referencia: str | None = Field(default=None, max_length=60)
    confianza: float = Field(default=0, ge=0, le=1)


@dataclass(frozen=True)
class PedidoAComparar:
    total: Decimal
    creado: datetime
    moneda: str = "COP"


@dataclass(frozen=True)
class Evaluacion:
    status: Resultado
    coincidencias: dict[str, bool]
    motivos: list[str] = field(default_factory=list)

    def como_json(self) -> dict[str, Any]:
        return {"resultado": self.status, "coincidencias": self.coincidencias, "motivos": self.motivos}


def _digitos(texto: str | None) -> str:
    return _DIGITOS.sub("", texto or "")


def _normal(texto: str | None) -> str:
    plano = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", plano)


def _misma_entidad(leida: str | None, registrada: str | None) -> bool:
    a, b = _normal(leida), _normal(registrada)
    return bool(a and b) and (a in b or b in a)


def _fecha(texto: str | None) -> date | datetime | None:
    if not texto:
        return None
    for formato in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            valor = datetime.strptime(texto.strip()[:16], formato)
            return valor.date() if formato == "%Y-%m-%d" else valor
        except ValueError:
            continue
    return None


def _monto(valor: float | None) -> Decimal | None:
    if valor is None:
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def cuenta_coincide(leida: str | None, banco_leido: str | None, cuentas: list[dict[str, Any]]) -> bool:
    """¿La cuenta del comprobante es una del negocio? Últimos 4 + entidad; con 6 o más dígitos
    visibles sin máscara, el número entero tiene que coincidir."""
    digitos = _digitos(leida)
    if len(digitos) < 4:
        return False
    enmascarada = bool(re.search(r"[*xX•·]", leida or ""))
    for cuenta in cuentas:
        numero = _digitos(str(cuenta.get("accountNumber") or ""))
        if len(numero) < 4 or numero[-4:] != digitos[-4:]:
            continue
        if not enmascarada and len(digitos) >= 6 and digitos != numero:
            continue
        if _misma_entidad(banco_leido, str(cuenta.get("entity") or "")):
            return True
    return False


def evaluar_comprobante(
    extraido: ComprobanteTransferencia,
    pedido: PedidoAComparar,
    cuentas: list[dict[str, Any]],
    *,
    duplicado: bool = False,
) -> Evaluacion:
    """Regla D4 de SPEC-48. Pura: mismo comprobante, mismo resultado."""
    monto = _monto(extraido.monto)
    fecha = _fecha(extraido.fecha)
    creado = pedido.creado.replace(tzinfo=None)
    if isinstance(fecha, datetime):
        fecha_ok = fecha >= creado.replace(second=0, microsecond=0)
    elif isinstance(fecha, date):
        fecha_ok = fecha >= creado.date()
    else:
        fecha_ok = False
    coincidencias = {
        "monto": monto is not None and abs(monto - pedido.total) <= TOLERANCIA,
        "cuenta": cuenta_coincide(extraido.cuenta_destino, extraido.banco, cuentas),
        "estado": extraido.estado == "exitoso",
        "fecha": fecha_ok,
        "no_duplicado": not duplicado,
        "legible": extraido.confianza >= CONFIANZA_MINIMA,
    }
    motivos: list[str] = []
    if monto is None:
        motivos.append("monto_ilegible")
    elif monto + TOLERANCIA < pedido.total:
        motivos.append("monto_menor")
    elif not coincidencias["monto"]:
        motivos.append("monto_mayor")
    if extraido.estado == "rechazado":
        motivos.append("transferencia_fallida")
    elif not coincidencias["estado"]:
        motivos.append("estado_no_exitoso")
    if not coincidencias["cuenta"]:
        motivos.append("cuenta_no_coincide" if _digitos(extraido.cuenta_destino) else "sin_cuenta")
    if not fecha_ok:
        motivos.append("fecha_anterior" if fecha is not None else "sin_fecha")
    if duplicado:
        motivos.append("duplicado")
    if not coincidencias["legible"]:
        motivos.append("lectura_dudosa")

    if "monto_menor" in motivos or "transferencia_fallida" in motivos:
        return Evaluacion("rechazado", coincidencias, motivos)
    if all(coincidencias.values()):
        return Evaluacion("pagado", coincidencias, [])
    return Evaluacion("en_revision", coincidencias, motivos)


def extraido_para_guardar(extraido: ComprobanteTransferencia) -> dict[str, Any]:
    """Lo que se guarda como evidencia: la cuenta solo con sus últimos 4 dígitos."""
    datos = extraido.model_dump(mode="json")
    digitos = _digitos(extraido.cuenta_destino)
    datos["cuenta_destino"] = digitos[-4:] if digitos else None
    return datos


# ------------------------------------------------------------------ lectura (con I/O)
class ComprobanteNoLeido(Exception):
    """No se pudo leer el comprobante. `motivo` es estable: `sin_cuota`, `vision_no_disponible`,
    `vision_no_respondio` o `ilegible`. `cobrado` dice si el proveedor ya cobró la llamada."""

    def __init__(self, motivo: str, *, cobrado: bool = False) -> None:
        super().__init__(motivo)
        self.motivo = motivo
        self.cobrado = cobrado


async def _verificar_cuota(tenant_id: uuid.UUID, key_mode: str) -> None:
    """Antes de llamar: si el crédito del mes no alcanza para una visión, no se llama (ni se cobra)."""
    from core.billing.creditos import alcanza, cotizar
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    creditos = cotizar("vision", 1, key_mode)  # type: ignore[arg-type]
    async with tenant_scope(tenant_id), get_session() as session:
        await alcanza(session, tenant_id, creditos)


async def leer_comprobante(
    tenant_id: uuid.UUID,
    contenido: bytes,
    mime_type: str,
    *,
    resultado_ledger: dict[str, Any],
    moneda: str = "COP",
) -> ComprobanteTransferencia:
    """Imagen → `ComprobanteTransferencia`, con la cuota verificada antes. El ledger mide el
    consumo pero no guarda la salida (datos de la cuenta sin enmascarar)."""
    import json

    from core.billing.enforcement import QuotaExceeded
    from core.ia.estructurado import (
        ErrorFormato,
        ErrorProveedor,
        Imagen,
        _app_settings,
        reconocer_imagen,
    )
    from core.media.config import MediaConfigError, resolve_vision_config

    try:
        config = resolve_vision_config(await _app_settings(tenant_id))
    except MediaConfigError as exc:
        raise ComprobanteNoLeido("vision_no_disponible") from exc
    try:
        await _verificar_cuota(tenant_id, config.key_mode)
    except QuotaExceeded as exc:
        raise ComprobanteNoLeido("sin_cuota") from exc
    entrada = json.dumps(
        {"negocio": {"moneda": moneda, "pais": "CO"}, "esquema": ComprobanteTransferencia.model_json_schema()},
        ensure_ascii=False,
    )
    try:
        leido = await reconocer_imagen(
            tenant_id,
            playbook=PLAYBOOK,
            esquema=ComprobanteTransferencia,
            imagen=Imagen(contenido=contenido, mime_type=mime_type),
            entrada=entrada,
            kind="vision",
            result={"proposito": PROPOSITO, **resultado_ledger},
            max_tokens=MAX_TOKENS,
            config=config,
            # La salida NO se guarda en el ledger: llevaría la cuenta y la referencia sin enmascarar.
            # La idempotencia la da `pack_commerce_payment_proofs` (image_sha256 + order_id).
            guardar_salida=False,
        )
    except ErrorProveedor as exc:
        raise ComprobanteNoLeido("vision_no_respondio") from exc
    except ErrorFormato as exc:
        raise ComprobanteNoLeido("ilegible", cobrado=True) from exc
    return leido.datos

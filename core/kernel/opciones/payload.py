"""Payload firmado de los botones de las opciones directas (SPEC-40, ADR-0172 §3).

- `opt:<send_id>:<option_id>:<firma>`: tocar una opción.
- `optc:` y `optn:`: confirmar y cancelar una acción sensible; la firma se ata además a los
  argumentos que el cliente vio (`args_hash`), igual que `motor.payload_boton_confirmar`.

La firma es HMAC-SHA256 (64 bits, 16 hex) de `tipo|negocio|conversación|envío|opción|args_hash` con una
clave derivada de `APP_SECRET_KEY`. Lleva el TIPO dentro del mensaje: un `optn:` no se vuelve un `optc:`
cambiando el prefijo. Lleva el negocio y la conversación: un botón reenviado a otra conversación (o
fabricado por quien arma un webhook) no valida. Nadie puede construir el de otro envío sin la clave.

El `send_id` va como uuid hex (32) y el `option_id` se acota a 12 caracteres: el payload más largo
(`optc:` + 32 + `:` + 12 + `:` + 16) mide 67, dentro de los 70 que se fijan aquí (Meta admite 256 en el id
interactivo; ese tope sigue sin verificarse contra la doc primaria, ADR-0172 §riesgos).

Puro: el secreto lo recibe quien llama (`secreto_del_servidor()` lo deriva de la configuración).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from core.kernel.procedures.linter import PREFIJOS_OPCIONES

Tipo = Literal["opt", "optc", "optn"]
TIPOS: tuple[Tipo, ...] = ("opt", "optc", "optn")
MAX_PAYLOAD = 70
_DOMINIO = b"opciones-directas/v1"
_PAYLOAD = re.compile(r"^(opt|optc|optn):([0-9a-f]{32}):([a-z0-9_]{1,12}):([0-9a-f]{16})$")
_HASH = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class PayloadLeido:
    tipo: Tipo
    send_id: uuid.UUID
    option_id: str
    firma: str


def secreto_del_servidor() -> str:
    """Clave de las opciones directas: separada por dominio de las demás firmas que derivan de
    `APP_SECRET_KEY` (rotarla invalida los botones ya enviados, que vencen con su envío)."""
    from core.config import get_settings

    return hmac.new(get_settings().app_secret_key.encode(), _DOMINIO, hashlib.sha256).hexdigest()


def hash_de_argumentos(tool: str, args: dict[str, Any] | None) -> str:
    """Huella (32 hex) de la tool y sus argumentos ya resueltos: la confirmación vale para ESOS."""
    canon = json.dumps({"tool": tool, "args": args or {}}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


def es_de_opciones(payload: str | None) -> bool:
    """¿Es un payload de este módulo (aunque esté mal formado o manipulado)? Lo usa el consumer para
    decidir quién lo atiende: lo que empieza por `opt:`, `optc:` o `optn:` nunca llega al LLM."""
    return bool(payload) and str(payload).startswith(PREFIJOS_OPCIONES)


def _firma(
    secreto: str, tipo: str, tenant_id: uuid.UUID, conversation_id: uuid.UUID, send_id: uuid.UUID, option_id: str, args_hash: str
) -> str:
    mensaje = f"{tipo}|{tenant_id}|{conversation_id}|{send_id.hex}|{option_id}|{args_hash}".encode()
    return hmac.new(secreto.encode(), mensaje, hashlib.sha256).hexdigest()[:16]


def _exigir_forma(tipo: str, option_id: str, args_hash: str) -> None:
    if tipo not in TIPOS:
        raise ValueError(f"tipo de payload desconocido: {tipo!r}")
    if not re.fullmatch(r"[a-z0-9_]{1,12}", option_id or ""):
        raise ValueError("el id de la opción debe ser [a-z0-9_] de 1 a 12 caracteres")
    if tipo == "opt" and args_hash:
        raise ValueError("`opt:` no lleva argumentos firmados")
    if tipo != "opt" and not _HASH.match(args_hash or ""):
        raise ValueError(f"`{tipo}:` exige el hash de los argumentos que vio el cliente")


def construir(
    secreto: str,
    tipo: Tipo,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    send_id: uuid.UUID,
    option_id: str,
    args_hash: str = "",
) -> str:
    """Id del botón o payload del QUICK_REPLY. Lanza `ValueError` si algún dato no tiene la forma
    firmable (nunca se firma algo que `leer` no pueda volver a leer)."""
    _exigir_forma(tipo, option_id, args_hash)
    firma = _firma(secreto, tipo, tenant_id, conversation_id, send_id, option_id, args_hash)
    payload = f"{tipo}:{send_id.hex}:{option_id}:{firma}"
    if len(payload) > MAX_PAYLOAD:  # por construcción mide 67 como máximo
        raise ValueError("el payload excede el largo permitido")
    return payload


def leer(payload: str | None) -> PayloadLeido | None:
    """La forma del payload, SIN verificar la firma. None si no tiene la forma exacta."""
    m = _PAYLOAD.fullmatch(payload or "")
    if m is None:
        return None
    return PayloadLeido(tipo=m.group(1), send_id=uuid.UUID(m.group(2)), option_id=m.group(3), firma=m.group(4))  # type: ignore[arg-type]


def firma_valida(
    leido: PayloadLeido, secreto: str, tenant_id: uuid.UUID, conversation_id: uuid.UUID, args_hash: str = ""
) -> bool:
    """¿La firma corresponde a ESTE negocio, ESTA conversación y (en `optc`/`optn`) estos argumentos?
    El `send_id` y la opción salen del propio payload: la firma los ata entre sí. Tiempo constante."""
    if leido.tipo == "opt" and args_hash:
        return False
    if leido.tipo != "opt" and not _HASH.match(args_hash or ""):
        return False
    esperada = _firma(secreto, leido.tipo, tenant_id, conversation_id, leido.send_id, leido.option_id, args_hash)
    return hmac.compare_digest(esperada, leido.firma)

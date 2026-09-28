"""Política anti-baneo del canal `waweb` (ADR-0074), por tenant.

El canal no oficial puede terminar en suspensión de la cuenta. La mitigación es
de comportamiento: por defecto **solo respuesta** (contestar a quien escribió
primero), con delays humanos y tope diario. Todo es CONFIG por tenant
(`AppSettings.behavior_config`, JSONB), nunca valores hardcodeados (invariante #3).

El `connector` lee esta política para decidir si drena la cola de salida y con
qué cadencia. Las funciones de construcción son puras y testeables sin BD.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

MODE_REPLY_ONLY = "reply_only"
MODE_ALLOW_OUTBOUND = "allow_outbound"
_MODES = frozenset({MODE_REPLY_ONLY, MODE_ALLOW_OUTBOUND})

DEFAULT_MIN_DELAY_MS = 1_500
DEFAULT_MAX_DELAY_MS = 4_000
DEFAULT_DAILY_OUTBOUND_CAP = 50
DEFAULT_WARMUP_ENABLED = True

# Versión del aviso de riesgo aceptado por el admin. Cambiarla obliga a
# re-aceptar en las conexiones nuevas (el texto legal cambió).
RISK_ACK_VERSION = "2026-09-27"

_MIN_DELAY_BOUNDS = (0, 60_000)
_MAX_DELAY_BOUNDS = (0, 120_000)
_CAP_BOUNDS = (0, 10_000)


@dataclass(frozen=True)
class WaWebPolicy:
    mode: str = MODE_REPLY_ONLY
    send_min_delay_ms: int = DEFAULT_MIN_DELAY_MS
    send_max_delay_ms: int = DEFAULT_MAX_DELAY_MS
    daily_outbound_cap: int = DEFAULT_DAILY_OUTBOUND_CAP
    warmup_enabled: bool = DEFAULT_WARMUP_ENABLED

    @property
    def allows_outbound(self) -> bool:
        """True solo si el tenant habilitó explícitamente el envío no solicitado."""
        return self.mode == MODE_ALLOW_OUTBOUND


def _clamped_int(behavior: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = behavior.get(key)
    try:
        value = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _clamped_bool(behavior: dict, key: str, default: bool) -> bool:
    raw = behavior.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "on", "1", "yes", "si", "sí")


def policy_from_behavior(behavior: dict | None) -> WaWebPolicy:
    """Construye la política desde `behavior_config`, con clamps defensivos.

    Un `behavior_config` malformado (o un `None` explícito en una clave) nunca
    debe tumbar el turno: mismo criterio que `_clamped_int` de `loop.py`.
    """
    b = behavior if isinstance(behavior, dict) else {}
    mode = str(b.get("waweb_mode") or MODE_REPLY_ONLY)
    if mode not in _MODES:
        mode = MODE_REPLY_ONLY

    min_delay = _clamped_int(b, "waweb_send_min_delay_ms", DEFAULT_MIN_DELAY_MS, *_MIN_DELAY_BOUNDS)
    max_delay = _clamped_int(b, "waweb_send_max_delay_ms", DEFAULT_MAX_DELAY_MS, *_MAX_DELAY_BOUNDS)
    if max_delay < min_delay:
        max_delay = min_delay

    return WaWebPolicy(
        mode=mode,
        send_min_delay_ms=min_delay,
        send_max_delay_ms=max_delay,
        daily_outbound_cap=_clamped_int(b, "waweb_daily_outbound_cap", DEFAULT_DAILY_OUTBOUND_CAP, *_CAP_BOUNDS),
        warmup_enabled=_clamped_bool(b, "waweb_warmup_enabled", DEFAULT_WARMUP_ENABLED),
    )


async def load_policy(tenant_id: uuid.UUID | None = None) -> WaWebPolicy:
    """Política del tenant activo (o del `tenant_id` dado). Default si no hay config."""
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id, tenant_scope

    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    if tid is None:
        return WaWebPolicy()
    # El connector corre sin scope ambiente (rol app_runtime, RLS): hay que
    # abrir `tenant_scope`, o `app_settings` devuelve 0 filas y se usaría la
    # política por defecto en vez de la configurada por el tenant.
    async with tenant_scope(tid):
        async with get_session() as session:
            app_settings = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tid,
                        AppSettings.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
    return policy_from_behavior(dict(app_settings.behavior_config) if app_settings else {})

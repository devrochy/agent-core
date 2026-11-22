"""Fila de `usage_ledger` de un gasto de media del negocio (ADR-0208 §11).

Notas de voz (`stt`) e imágenes (`vision`) de la ingesta de WhatsApp, y voz de salida (`tts`) de
pack-voice: cada una escribe su fila con el costo real y los créditos congelados al escribir
(`tarifario.creditos_congelados`). Vive aparte de la ingesta para que pack-voice no cargue el
consumer; el pack llama a este servicio del core y nunca toca la base.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.billing.tarifario import creditos_congelados
from core.db.models import UsageLedger
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


async def registrar_consumo(
    *,
    kind: str,
    concepto: str,
    conversation_id: uuid.UUID | None,
    model: str,
    cost_usd: float,
    key_mode: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    resultado: dict[str, Any] | None = None,
) -> None:
    """Fila de `usage_ledger` de un gasto de media (`stt`, `vision`, `tts`) del negocio del
    `tenant_scope` vigente (nunca de un argumento), con créditos congelados al escribir. Best-effort:
    si falla se avisa y el flujo sigue."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        log.warning("media_ledger_sin_scope", kind=kind)
        return
    try:
        async with get_session() as session:
            session.add(
                UsageLedger(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_usd=cost_usd,
                    kind=kind,
                    **creditos_congelados(cost_usd, concepto, key_mode),  # type: ignore[arg-type]
                    result=resultado or {},
                )
            )
    except Exception:
        log.warning("media_ledger_failed", kind=kind, exc_info=True)


__all__ = ["registrar_consumo"]

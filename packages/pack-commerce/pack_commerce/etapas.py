"""La etapa del lead: una sola puerta para validarla, cambiarla y dejar historia (HU-CRM-18, ADR-0181).

`cambiar_etapa` es el único lugar del pack que escribe `Lead.status` (lo vigila
`tests/test_pack_commerce_etapa_unica.py`). La usan `actualizar_lead`, `calificar_lead`, el hook de
edición de la ficha (ADR-0149) y el seguimiento automático (`jobs.py`); cada uno dice quién actúa.

Reglas:
- Las etapas válidas son cinco (`ETAPAS`); cualquier otra se rechaza con la lista de las válidas.
- Dejar el lead en la misma etapa no hace nada ni deja registro.
- Solo una persona saca a un lead de «perdido» (reabrirlo).
- `calificar` (lo que hace `calificar_lead`) solo mueve a «calificado» un lead «nuevo».
- Cada cambio deja una fila en `pack_commerce_lead_stage_changes` (etapa anterior, nueva, actor,
  cuándo) y mueve `stage_changed_at`, en la misma transacción. Con `idempotency_key`, repetir el
  mismo cambio (reintento) no lo vuelve a aplicar ni a registrar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select

from pack_commerce.models import Lead, LeadStageChange

Actor = Literal["agente", "persona", "sistema"]
ACTORES: tuple[str, ...] = ("agente", "persona", "sistema")

ETAPAS: tuple[str, ...] = ("nuevo", "calificado", "contactado", "convertido", "perdido")
ETAPA_INICIAL = "nuevo"
ETAPA_CALIFICADA = "calificado"
ETAPA_DE_PERDIDA = "perdido"


class EtapaInvalida(ValueError):
    """La etapa pedida no es una de las cinco."""

    def __init__(self, pedida: Any) -> None:
        self.pedida = str(pedida)
        super().__init__(
            f"«{self.pedida}» no es una etapa válida. Las etapas permitidas son: {', '.join(ETAPAS)}."
        )


class TransicionNoPermitida(ValueError):
    """El actor no puede hacer este cambio (p. ej. el agente reabriendo un lead perdido)."""


def normalizar(etapa: Any) -> str:
    """Etapa en minúsculas y sin espacios sobrantes; vacío si no es texto."""
    return str(etapa).strip().lower() if etapa is not None else ""


def _validar_salida(actual: str, nueva: str, actor: str) -> None:
    if actual == ETAPA_DE_PERDIDA and nueva != ETAPA_DE_PERDIDA and actor != "persona":
        raise TransicionNoPermitida(
            f"Este lead está en «{ETAPA_DE_PERDIDA}»: solo una persona del equipo puede reabrirlo "
            "desde el panel o la APK. No lo muevas a otra etapa."
        )


async def cambiar_etapa(
    session: Any,
    lead: Lead,
    nueva: Any,
    *,
    actor: Actor,
    idempotency_key: str | None = None,
) -> bool:
    """Lleva el lead a `nueva`. True si cambió la etapa; False si ya estaba ahí o si este mismo
    cambio (`idempotency_key`) ya se aplicó.

    Lanza `EtapaInvalida` o `TransicionNoPermitida` sin tocar nada. Corre dentro de la sesión
    (y la transacción) de quien la llama, con el lead bloqueado para que dos cambios a la vez
    no se pisen ni se registren dos veces."""
    if actor not in ACTORES:
        raise ValueError(f"actor desconocido: {actor!r}")
    destino = normalizar(nueva)
    if destino not in ETAPAS:
        raise EtapaInvalida(nueva)

    # Primero lo pendiente del llamador (un lead nuevo, otros campos): el refresh los pisaría.
    await session.flush()
    await session.refresh(lead, with_for_update=True)

    if idempotency_key is not None:
        previo = (
            await session.execute(
                select(LeadStageChange.id).where(
                    LeadStageChange.tenant_id == lead.tenant_id,
                    LeadStageChange.idempotency_key == idempotency_key,
                )
            )
        ).first()
        if previo is not None:
            return False

    actual = lead.status
    if destino == actual:
        return False
    _validar_salida(actual, destino, actor)

    ahora = datetime.now(UTC)
    lead.status = destino
    lead.stage_changed_at = ahora
    lead.last_activity_at = ahora
    session.add(
        LeadStageChange(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            customer_id=lead.customer_id,
            from_stage=actual,
            to_stage=destino,
            actor=actor,
            idempotency_key=idempotency_key,
            changed_at=ahora,
        )
    )
    await session.flush()
    return True


async def calificar(
    session: Any, lead: Lead, score: int, *, actor: Actor, idempotency_key: str | None = None
) -> str:
    """Pone el puntaje y, solo si el lead es «nuevo», lo pasa a «calificado». Un lead que ya está
    en otra etapa conserva la suya (calificar no hace retroceder). Devuelve la etapa en que queda."""
    lead.score = score
    if lead.status == ETAPA_INICIAL:
        await cambiar_etapa(session, lead, ETAPA_CALIFICADA, actor=actor, idempotency_key=idempotency_key)
    lead.last_activity_at = datetime.now(UTC)
    return lead.status

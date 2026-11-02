"""La etapa del lead: una sola puerta para validarla, cambiarla y dejar historia (HU-CRM-18, ADR-0181).

`cambiar_etapa` es el único lugar del pack que escribe `Lead.status` (lo vigila
`tests/test_pack_commerce_etapa_unica.py`). La usan `actualizar_lead`, `calificar_lead`, el hook de
edición de la ficha (ADR-0149) y el seguimiento automático (`jobs.py`); cada uno dice quién actúa.

Reglas:
- Las etapas válidas son cinco (`ETAPAS`); cualquier otra se rechaza con la lista de las válidas.
- Dejar el lead en la misma etapa no hace nada ni deja registro.
- Solo una persona saca a un lead de «perdido» (reabrirlo).
- `calificar` (lo que hace `calificar_lead`) solo mueve a «calificado» un lead «nuevo».
- Un lead solo pasa a «perdido» con un motivo del catálogo cerrado (`MOTIVOS_DE_PERDIDA`, HU-CRM-23) y una
  nota opcional; sin motivo válido no cambia nada. Reabrirlo limpia el motivo vigente del lead; el de la
  pérdida queda en la fila del cambio de etapa.
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


# Por qué se pierde un lead (HU-CRM-23): cerrado y fijo; que cada negocio defina los suyos es otra decisión.
MOTIVOS_DE_PERDIDA: tuple[tuple[str, str], ...] = (
    ("precio", "Precio"),
    ("sin_respuesta", "Sin respuesta"),
    ("compro_en_otro_lado", "Compró en otro lado"),
    ("no_era_para_el", "No era para él"),
    ("otro", "Otro"),
)
CODIGOS_DE_MOTIVO: tuple[str, ...] = tuple(c for c, _ in MOTIVOS_DE_PERDIDA)
NOTA_DE_PERDIDA_MAX = 300
SIN_MOTIVO = "Sin motivo registrado"


class MotivoDePerdidaInvalido(ValueError):
    """Un lead se pierde con un motivo del catálogo: falta o no es uno de los permitidos."""

    def __init__(self, pedido: Any = None) -> None:
        self.pedido = None if pedido is None else str(pedido)
        validos = ", ".join(CODIGOS_DE_MOTIVO)
        if not self.pedido:
            detalle = "Para marcar un lead como «perdido» hay que indicar el motivo (loss_reason)."
        else:
            detalle = f"«{self.pedido}» no es un motivo de pérdida válido."
        super().__init__(f"{detalle} Los motivos permitidos son: {validos}.")


def motivo_valido(motivo: Any) -> str | None:
    """El código del motivo normalizado si es uno del catálogo; `None` si falta o no vale."""
    codigo = normalizar(motivo)
    return codigo if codigo in CODIGOS_DE_MOTIVO else None


def nota_de_perdida(nota: Any) -> str | None:
    """La nota sin espacios sobrantes (vacía = sin nota). Pasa del tope: `ValueError`, no se corta en silencio."""
    texto = str(nota).strip() if nota is not None else ""
    if len(texto) > NOTA_DE_PERDIDA_MAX:
        raise ValueError(f"La nota de la pérdida admite hasta {NOTA_DE_PERDIDA_MAX} caracteres.")
    return texto or None


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
    loss_reason: Any = None,
    loss_note: Any = None,
) -> bool:
    """Lleva el lead a `nueva`. True si cambió la etapa; False si ya estaba ahí o si este mismo
    cambio (`idempotency_key`) ya se aplicó.

    Lanza `EtapaInvalida`, `TransicionNoPermitida` o, al perder un lead sin motivo válido,
    `MotivoDePerdidaInvalido`, sin tocar nada. Corre dentro de la sesión
    (y la transacción) de quien la llama, con el lead bloqueado para que dos cambios a la vez
    no se pisen ni se registren dos veces."""
    if actor not in ACTORES:
        raise ValueError(f"actor desconocido: {actor!r}")
    destino = normalizar(nueva)
    if destino not in ETAPAS:
        raise EtapaInvalida(nueva)
    # Antes de bloquear el lead: un motivo que no vale se rechaza sin tocar nada (HU-CRM-23).
    motivo = nota = None
    if destino == ETAPA_DE_PERDIDA:
        motivo = motivo_valido(loss_reason)
        nota = nota_de_perdida(loss_note)

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
    if destino == ETAPA_DE_PERDIDA and motivo is None:
        raise MotivoDePerdidaInvalido(loss_reason)

    ahora = datetime.now(UTC)
    lead.status = destino
    lead.stage_changed_at = ahora
    lead.last_activity_at = ahora
    # El motivo es del lead mientras está perdido; al reabrirlo se va (queda en la fila del cambio).
    lead.loss_reason = motivo
    lead.loss_note = nota
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
            loss_reason=motivo,
            loss_note=nota,
        )
    )
    await session.flush()
    return True


def anotar_motivo(lead: Lead, loss_reason: Any, loss_note: Any = None) -> None:
    """Pone o corrige el motivo de un lead que YA está «perdido» (p. ej. uno anterior a la HU, sin motivo).
    Solo se anota el motivo de un lead perdido: en otra etapa no tiene sentido (`ValueError`)."""
    if lead.status != ETAPA_DE_PERDIDA:
        raise ValueError(f"El motivo de pérdida solo se anota en un lead «{ETAPA_DE_PERDIDA}».")
    motivo = motivo_valido(loss_reason)
    if motivo is None:
        raise MotivoDePerdidaInvalido(loss_reason)
    lead.loss_reason = motivo
    lead.loss_note = nota_de_perdida(loss_note)


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

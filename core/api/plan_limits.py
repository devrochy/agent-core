"""Error de la API para los topes estructurales del plan (D-35, ADR-0142).

Mismo contrato que `plan_limit_plugins` (P0 de pruebas 2026-10-17): 409 con
`error.code = plan_limit_<métrica>` y `error.details = {metric, used, limit}`, para que el panel
explique el límite y ofrezca mejorar el plan sin adivinar el estado. Una sola función para
todas las entradas que pueden levantar `QuotaExceeded` de `agentes` o `colaboradores`.
"""

from __future__ import annotations

from core.api.errors import ApiError
from core.billing.enforcement import QuotaExceeded

_MENSAJES = {
    "agentes": (
        "Tu plan permite {limit} agente(s) publicado(s) y ya tienes {used}. "
        "Mejora tu plan para publicar otro agente."
    ),
    "colaboradores": (
        "Tu plan permite {limit} colaborador(es) y ya tienes {used}, contando las invitaciones "
        "pendientes. Mejora tu plan para invitar a otro. No se envió nada."
    ),
}


def plan_limit_code(metric: str) -> str:
    return f"plan_limit_{metric}"


def plan_limit_message(exc: QuotaExceeded) -> str:
    plantilla = _MENSAJES.get(exc.metric, "Llegaste al tope de tu plan ({used}/{limit}). Mejora tu plan para seguir.")
    return plantilla.format(used=exc.used, limit=exc.limit)


def plan_limit_error(exc: QuotaExceeded) -> ApiError:
    return ApiError(
        409,
        plan_limit_code(exc.metric),
        plan_limit_message(exc),
        details={"metric": exc.metric, "used": exc.used, "limit": exc.limit},
    )


def plan_limit_responses(metric: str) -> dict[int | str, dict[str, str]]:
    """Documentación OpenAPI del 409 de una métrica estructural."""
    return {
        409: {
            "description": (
                f"Tope del plan: `error.code = {plan_limit_code(metric)}`, "
                "`error.details = {metric, used, limit}`; no se escribió nada."
            )
        }
    }


__all__ = ["plan_limit_code", "plan_limit_error", "plan_limit_message", "plan_limit_responses"]

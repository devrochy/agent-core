"""Error de la API para los topes estructurales del plan (D-35, ADR-0142).

409 con `error.code = plan_limit_<métrica>` y `error.details = {metric, used, limit}` (contrato
nacido con el tope de habilidades del P0 de pruebas 2026-10-17, retirado en HU-BILL-18), para que
el panel explique el límite y ofrezca mejorar el plan sin adivinar el estado. Una sola función
para todas las entradas que pueden levantar `QuotaExceeded` de un tope estructural o del crédito.

Desde HU-BILL-19 (ADR-0208 §9-10) también traduce lo que el plan no incluye (`PlanNoIncluye`): un canal
(`plan_channel_not_included`, `details = {channel, plan}`) o una habilidad (`plan_plugin_not_included`,
`details = {skills, min_plan, plan}`). Los dos son 409 y no escriben nada.

Desde HU-BILL-25, una feature del plan (`plan_feature_not_included`, `details = {feature, plan}`): la
exportación de conversaciones y las conversaciones fuera del historial del plan.
"""

from __future__ import annotations

from core.api.errors import ApiError
from core.billing.enforcement import PlanNoIncluye, QuotaExceeded

_MENSAJES = {
    "agentes": (
        "Tu plan permite {limit} agente(s) publicado(s) y ya tienes {used}. "
        "Mejora tu plan para crear otro agente."
    ),
    "colaboradores": (
        "Tu plan permite {limit} colaborador(es) y ya tienes {used}, contando las invitaciones "
        "pendientes. Mejora tu plan para invitar a otro. No se envió nada."
    ),
    "conexiones": (
        "Tu plan permite {limit} conexión(es) activa(s) y ya tienes {used}. "
        "Pausa o elimina una, o mejora tu plan para conectar otra. No se conectó nada."
    ),
    "correos": (
        "Tu plan permite {limit} correo(s) conectado(s) y ya tienes {used}. "
        "Elimina uno o mejora tu plan para conectar otro. No se conectó nada."
    ),
    "documentos": (
        "Tu plan permite {limit} documento(s) de conocimiento y ya tienes {used}. "
        "Borra uno o mejora tu plan para subir otro."
    ),
    "conocimiento_mb": (
        "Tu plan permite {limit} MB de documentos de conocimiento y ya usas {used} MB. "
        "Borra alguno o mejora tu plan para subir este archivo."
    ),
    "creditos": (
        "Tu plan incluye {limit} créditos al mes y ya usaste {used}; no alcanzan para esta acción. "
        "Mejora tu plan o espera al próximo mes. No se cobró nada."
    ),
}


def plan_limit_code(metric: str) -> str:
    return f"plan_limit_{metric}"


def plan_limit_message(exc: QuotaExceeded) -> str:
    plantilla = _MENSAJES.get(exc.metric, "Llegaste al tope de tu plan ({used}/{limit}). Mejora tu plan para seguir.")
    return plantilla.format(used=exc.used, limit=exc.limit)


def plan_limit_error(exc: QuotaExceeded | PlanNoIncluye) -> ApiError:
    if isinstance(exc, PlanNoIncluye):
        return ApiError(409, exc.code, exc.message, details=exc.details or None)
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


def plan_limit_responses_de(*metricas: str, canal: bool = False, habilidad: bool = False) -> dict[int | str, dict[str, str]]:
    """Documentación OpenAPI del 409 de una ruta que puede levantar varios topes del plan (HU-BILL-19):
    los `plan_limit_<métrica>` de `metricas`, `plan_channel_not_included` y `plan_plugin_not_included`."""
    codigos = [f"`{plan_limit_code(m)}`" for m in metricas]
    if canal:
        codigos.append("`plan_channel_not_included`")
    if habilidad:
        codigos.append("`plan_plugin_not_included`")
    partes = []
    if metricas:
        partes.append("`plan_limit_*` trae `error.details = {metric, used, limit}`")
    if canal:
        partes.append("`plan_channel_not_included` trae `error.details = {channel, plan}`")
    if habilidad:
        partes.append("`plan_plugin_not_included` trae `error.details = {skills, min_plan, plan}`")
    return {
        409: {
            "description": (
                f"Tope o alcance del plan: `error.code` es {', '.join(codigos)}; " + "; ".join(partes)
                + ". No se escribió nada."
            )
        }
    }


def plan_feature_responses(*features: str) -> dict[int | str, dict[str, str]]:
    """409 de una ruta que depende de una feature del plan (HU-BILL-25): `plan_feature_not_included`
    con `error.details = {feature, plan}` (`historial` suma `historial_dias`)."""
    nombres = ", ".join(f"`{f}`" for f in features)
    return {
        409: {
            "description": (
                "El plan no incluye la feature: `error.code = plan_feature_not_included`, "
                f"`error.details = {{feature, plan}}` (feature {nombres}"
                + ("; `historial` trae además `historial_dias`" if "historial" in features else "")
                + "). No se escribió nada."
            )
        }
    }


def plan_conexion_responses() -> dict[int | str, dict[str, str]]:
    """409 de una ruta que da de alta o activa una conexión (ADR-0208 §8-9)."""
    return plan_limit_responses_de("conexiones", "correos", canal=True)


__all__ = [
    "plan_conexion_responses",
    "plan_feature_responses",
    "plan_limit_code",
    "plan_limit_error",
    "plan_limit_message",
    "plan_limit_responses",
    "plan_limit_responses_de",
]

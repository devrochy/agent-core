"""Topes de las pruebas del agente por negocio (SPEC-53, ADR-0212 §3).

`k` es el número de intentos por escenario. El valor por defecto y el tope son de plataforma
(`PRUEBAS_K_POR_DEFECTO`, `PRUEBAS_K_MAXIMO`); el master cambia el tope de UN negocio en
`tenants.settings["pruebas"]["k_maximo"]`, nunca por encima de `K_TECHO` (el mismo máximo que acepta la tabla
`agent_test_runs`, `ck_agent_test_runs_k`). `fijar_k_maximo` es el ÚNICO escritor: un UPDATE atómico con `jsonb_set`
que no pisa el resto de `settings` ni `settings.pruebas.k_por_defecto`; lo llama la ruta de plataforma master-only.
Un valor ilegible cuenta como «sin override» (fail-closed hacia el tope de plataforma).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

K_TECHO = 10
CLAVE = "pruebas"
ACCION_AUDITORIA = "pruebas_k_maximo"


class NegocioNoEncontrado(LookupError):
    pass


def _entero_valido(valor: Any) -> int | None:
    """Un entero real de 1 a `K_TECHO`; un bool, un texto o un fuera de rango no cuentan."""
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= K_TECHO:
        return None
    return valor


def k_maximo_desde(settings: object, *, de_plataforma: int) -> int:
    """Tope de `k` del negocio: su override si es válido; si no, el de plataforma. Siempre entre 1 y `K_TECHO`."""
    bloque = settings.get(CLAVE) if isinstance(settings, dict) else None
    propio = _entero_valido(bloque.get("k_maximo")) if isinstance(bloque, dict) else None
    return min(K_TECHO, max(1, propio if propio is not None else de_plataforma))


def k_por_defecto_desde(settings: object, *, de_plataforma: int, tope: int) -> int:
    """`k` que se sugiere: el habitual que guardó el dueño (`settings.pruebas.k_por_defecto`) o el de plataforma,
    recortado al tope."""
    bloque = settings.get(CLAVE) if isinstance(settings, dict) else None
    propio = _entero_valido(bloque.get("k_por_defecto")) if isinstance(bloque, dict) else None
    return min(tope, max(1, propio if propio is not None else de_plataforma))


async def _settings_del_negocio(session, tenant_id: uuid.UUID) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """`tenants.settings` del negocio del `tenant_scope` en curso (`tenants` no tiene RLS: se exige el scope)."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("pruebas: fuera del tenant_scope del negocio")
    fila = (
        await session.execute(
            text("SELECT settings FROM tenants WHERE id = :t AND deleted_at IS NULL"), {"t": tenant_id}
        )
    ).first()
    if fila is None:
        raise NegocioNoEncontrado(str(tenant_id))
    return fila[0] if isinstance(fila[0], dict) else {}


async def topes_de_k(session, tenant_id: uuid.UUID) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """`(k por defecto, k máximo)` efectivos del negocio."""
    from core.config import get_settings

    ajustes = get_settings()
    settings = await _settings_del_negocio(session, tenant_id)
    tope = k_maximo_desde(settings, de_plataforma=ajustes.pruebas_k_maximo)
    return k_por_defecto_desde(settings, de_plataforma=ajustes.pruebas_k_por_defecto, tope=tope), tope


async def leer_k_maximo(tenant_id: uuid.UUID) -> tuple[int | None, int]:
    """`(override del negocio o None, tope efectivo)` para la ruta de plataforma. Dentro del `tenant_scope`."""
    from core.config import get_settings
    from core.db.session import get_session

    async with get_session() as session:
        settings = await _settings_del_negocio(session, tenant_id)
    bloque = settings.get(CLAVE)
    propio = _entero_valido(bloque.get("k_maximo")) if isinstance(bloque, dict) else None
    return propio, k_maximo_desde(settings, de_plataforma=get_settings().pruebas_k_maximo)


async def fijar_k_maximo(tenant_id: uuid.UUID, k_maximo: int | None, *, actor: str) -> int | None:
    """Fija (o, con `None`, quita) el tope de `k` del negocio y lo audita. `k_maximo` ya validado entre 1 y
    `K_TECHO`. Quien llama es la ruta master-only, dentro del `tenant_scope` del negocio."""
    from core.config import get_settings
    from core.db.models import AuditLog
    from core.db.session import get_session

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("pruebas: fuera del tenant_scope del negocio")
    if k_maximo is not None and _entero_valido(k_maximo) is None:
        raise ValueError(f"k_maximo fuera de 1..{K_TECHO}")
    async with get_session() as session:
        # `settings.pruebas || {...}` conserva `k_por_defecto`; con `None` se quita solo `k_maximo`.
        cambio = (
            "coalesce(settings->'pruebas', '{}'::jsonb) || cast(:valor as jsonb)"
            if k_maximo is not None
            else "coalesce(settings->'pruebas', '{}'::jsonb) - 'k_maximo'"
        )
        parametros: dict[str, Any] = {"t": tenant_id}
        if k_maximo is not None:
            parametros["valor"] = json.dumps({"k_maximo": k_maximo})
        fila = (
            await session.execute(
                text(
                    "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{pruebas}', "
                    f"{cambio}) WHERE id = :t AND deleted_at IS NULL RETURNING id"
                ),
                parametros,
            )
        ).first()
        if fila is None:
            raise NegocioNoEncontrado(str(tenant_id))
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor[:120],
                action=ACCION_AUDITORIA,
                input={"k_maximo": k_maximo, "tope_de_plataforma": get_settings().pruebas_k_maximo},
            )
        )
    log.info(ACCION_AUDITORIA, tenant_id=str(tenant_id), k_maximo=k_maximo)
    return k_maximo


__all__ = [
    "ACCION_AUDITORIA",
    "K_TECHO",
    "NegocioNoEncontrado",
    "fijar_k_maximo",
    "k_maximo_desde",
    "k_por_defecto_desde",
    "leer_k_maximo",
    "topes_de_k",
]

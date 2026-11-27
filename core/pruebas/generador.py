"""Proponer escenarios de prueba con IA a partir de la configuración del agente (HU-AOS-101, ADR-0212 §4).

1. Arma el contexto: negocio (descripción, voz, moneda, idioma, horario), agente (nombre, descripción, reglas,
   tools con lo que hace cada una) y los títulos que ya existen. **Solo configuración**: nunca datos de clientes
   reales ni de otros negocios (ADR-0212 §5).
2. Llama a `core.ia.estructurado.generar_estructurado` con el playbook del core `escenarios_de_prueba` (cero
   prompts en código, invariante 3) y créditos de plataforma. La llamada queda en `usage_ledger` con
   `kind='generacion'` y `result.proposito='escenarios_de_prueba'`, aunque la salida no valide.
3. Depura la salida (dato no confiable, invariante 7): descarta lo malformado, los títulos repetidos o vacíos y
   las tools que el agente no tiene; recorta a lo que cabe (10 por pedido, 50 por agente).
4. Guarda lo que queda en `borrador` con `origen='ia'`.

Antes de gastar verifica que el negocio tenga saldo (`alcanza`, 409 `plan_limit_creditos`). Un pedido a la vez por
agente (409 `en_curso`): dos clics no cobran dos veces.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text

from core.db.models import AgentRecord, AppSettings
from core.db.session import get_session
from core.kernel.horario_de_servicio import resumen as resumen_de_horario
from core.pruebas import escenarios
from core.pruebas.escenarios import ErrorPruebas
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "escenarios_de_prueba"
MAX_TOKENS = 3000
TEMPERATURA = 0.5
TIMEOUT_LLM_S = 90
# Mínimo con el que el negocio puede pedir una propuesta: el costo real se mide y se muestra después.
CREDITOS_MINIMOS = 1


class Propuesta(BaseModel):
    """Salida del LLM. Cada escenario es un dict suelto: se depura uno por uno, un item malo no tira a los demás."""

    escenarios: list[Any]


def _proveedor() -> Any:
    from core.llm import resolve_provider

    return resolve_provider(thinking=False)


async def _contexto(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tools: Mapping[str, str],
    cantidad: int,
    existentes: list[str],
) -> str:
    async with get_session() as session:
        agente = (
            await session.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == tenant_id, AgentRecord.id == agent_id
                )
            )
        ).scalar_one()
        ajustes = (
            await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
        ).scalar_one_or_none()
        borrador = (
            await session.execute(
                text(
                    "SELECT content FROM agent_drafts WHERE tenant_id = :t AND agent_id = :a AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "a": agent_id},
            )
        ).scalar_one_or_none()
    contenido = dict(borrador) if isinstance(borrador, Mapping) else {}
    reglas = [
        {"nombre": g.get("name"), "tipo": g.get("kind"), "frases": g.get("phrases") or []}
        for g in contenido.get("guardrails") or []
        if isinstance(g, Mapping)
    ]
    idioma = getattr(ajustes, "language", "") or "es"
    horario = resumen_de_horario(
        ((getattr(ajustes, "escalation_config", None) or {}).get("business_hours")) or None, idioma
    )
    return json.dumps(
        {
            "negocio": {
                "descripcion": getattr(ajustes, "business_description", "") or "",
                "voz_de_marca": getattr(ajustes, "brand_voice", "") or "",
                "moneda": getattr(ajustes, "currency", "") or "COP",
                "idioma": idioma,
                "zona_horaria": getattr(ajustes, "timezone", "") or "America/Bogota",
                "horario": horario,
            },
            "agente": {
                "nombre": agente.name,
                "descripcion": agente.description or str(contenido.get("description") or ""),
                "reglas": reglas,
                "tools": [{"nombre": n, "que_hace": d} for n, d in sorted(tools.items())],
            },
            "cantidad": cantidad,
            "ya_existentes": existentes,
        },
        ensure_ascii=False,
    )


def _texto(v: Any, tope: int) -> str:
    return v.strip()[:tope] if isinstance(v, str) else ""


def _lista(v: Any, tope: int, cuantos: int) -> list[str]:
    if not isinstance(v, list):
        return []
    return [t for t in (_texto(x, tope) for x in v) if t][:cuantos]


def depurar(
    propuestos: list[Any], tools: Mapping[str, str], existentes: list[str], cantidad: int
) -> tuple[list[dict[str, Any]], int]:
    """Lo que de la propuesta se puede guardar, y cuántos se descartaron. Solo tools del agente."""
    vistos = {t.casefold() for t in existentes}
    salida: list[dict[str, Any]] = []
    descartados = 0
    for p in propuestos:
        if not isinstance(p, dict):
            descartados += 1
            continue
        titulo, objetivo = _texto(p.get("titulo"), 120), _texto(p.get("objetivo_cliente"), 2000)
        if not titulo or not objetivo or titulo.casefold() in vistos or len(salida) >= cantidad:
            descartados += 1
            continue
        esperadas = [t for t in _lista(p.get("tools_esperadas"), 100, 50) if t in tools]
        prohibidas = [
            t
            for t in _lista(p.get("tools_prohibidas"), 100, 50)
            if t in tools and t not in esperadas
        ]
        datos = p.get("datos_cliente")
        vistos.add(titulo.casefold())
        salida.append(
            {
                "titulo": titulo,
                "objetivo_cliente": objetivo,
                "datos_cliente": dict(datos) if isinstance(datos, dict) else {},
                "criterios": _lista(p.get("criterios"), 500, 20),
                "tools_esperadas": esperadas,
                "tools_prohibidas": prohibidas,
            }
        )
    return salida, descartados


async def proponer(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, *, created_by: uuid.UUID | None
) -> dict[str, Any]:
    """Pide a la IA hasta 10 escenarios para el agente y los guarda en `borrador`."""
    from core.billing.creditos import alcanza
    from core.ia.estructurado import ErrorFormato, ErrorProveedor, generar_estructurado

    await escenarios.exigir_agente(tenant_id, agent_id)
    cantidad = min(escenarios.TOPE_POR_PROPUESTA, await escenarios.cupo(tenant_id, agent_id))
    if cantidad <= 0:
        raise ErrorPruebas(
            422,
            "tope_de_escenarios",
            f"Este agente ya tiene {escenarios.TOPE_ESCENARIOS} escenarios. Borra alguno para pedir más.",
        )
    # Transacción corta y aparte: `alcanza` toma el lock de créditos del negocio y no debe retenerse durante la IA.
    async with get_session() as saldo:
        await alcanza(saldo, tenant_id, CREDITOS_MINIMOS)  # QuotaExceeded → 409 plan_limit_creditos
    async with get_session() as bloqueo:
        tomado = (
            await bloqueo.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"pruebas_proponer:{tenant_id}:{agent_id}"},
            )
        ).scalar_one()
        if not tomado:
            raise ErrorPruebas(
                409, "en_curso", "Ya se están proponiendo escenarios para este agente."
            )
        tools = await escenarios.tools_del_agente(tenant_id, agent_id)
        existentes = await escenarios.titulos_vivos(tenant_id, agent_id)
        try:
            generado = await generar_estructurado(
                tenant_id,
                playbook=PLAYBOOK,
                esquema=Propuesta,
                entrada=await _contexto(tenant_id, agent_id, tools, cantidad, existentes),
                kind="generacion",
                result={"proposito": "escenarios_de_prueba", "agent_id": str(agent_id)},
                # Créditos de plataforma: proponer pruebas no usa la llave del negocio (ADR-0212 §4).
                proveedor=_proveedor,
                key_mode="platform",
                max_tokens=MAX_TOKENS,
                temperatura=TEMPERATURA,
                timeout_s=TIMEOUT_LLM_S,
            )
        except ErrorProveedor as exc:
            log.warning("pruebas_proponer_proveedor", error=exc.causa)
            raise ErrorPruebas(
                503,
                "ia_no_disponible",
                "No pudimos generar los escenarios ahora. Intenta de nuevo en un rato.",
            ) from exc
        except ErrorFormato as exc:  # ya quedó en el ledger: el proveedor cobró
            log.warning("pruebas_proponer_formato", error=exc.causa)
            raise ErrorPruebas(
                502,
                "propuesta_invalida",
                "La IA no devolvió escenarios utilizables. Intenta de nuevo.",
            ) from exc
        validos, descartados = depurar(generado.datos.escenarios, tools, existentes, cantidad)
        guardados = await escenarios.guardar_propuestos(
            tenant_id, agent_id, validos, created_by=created_by
        )
    return {
        "escenarios": guardados,
        "descartados": descartados + (len(validos) - len(guardados)),
        "creditos": None if generado.consumo.credits is None else float(generado.consumo.credits),
    }

"""Captar leads: las tools del grupo `leads` del core (ADR-0193 §2, HU-AOS-70).

Vivían en `pack_commerce.skills.LeadSkill`. Son del core (`plugin_id="core"`) pero NO se dan a todos los agentes como
el resto de las tools del core: solo las recibe el agente que tiene la habilidad Captar leads (binding en su borrador
o concedidas por un procedimiento que declara). Esa excepción vive en `ACCIONES_CAPTAR_LEADS` y la aplican la oferta del
turno, los presets, el reporte de bindings y el panel de habilidades.

- `crear_lead` ya no crea un segundo lead: asegura el lead vivo del cliente (el turno ya lo aseguró si el agente tiene
  Captar leads) y le pone los datos que dio. Devuelve `created` solo si el lead nació en esta llamada.
- El origen sale del canal de la conversación (`catalogo.origen_del_canal`), nunca de un argumento del modelo.
- Los datos de contacto también van al perfil del cliente, que es lo que muestra la ficha (ADR-0149), con los
  `writeNamespaces` del agente (ADR-0114 §3).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from core.datos_cliente.guardar import guardar as guardar_datos
from core.db.session import get_session
from core.leads import sandbox as sandbox_prueba
from core.leads import servicio
from core.leads.catalogo import CODIGOS_DE_MOTIVO, ETAPAS, origen_del_canal
from core.leads.etapas import (
    EtapaInvalida,
    MotivoDePerdidaInvalido,
    TransicionNoPermitida,
    cambiar_etapa,
)
from core.leads.etapas import calificar as calificar_etapa
from core.mcp.types import BaseSkill, MCPTool, RegistroCreado, ToolContext
from core.memory.facts import HechoInvalido, NamespaceNoConcedido
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# El grupo de acciones Captar leads: tools del core que necesitan binding (o un procedimiento que las conceda).
ACCIONES_CAPTAR_LEADS: frozenset[str] = frozenset({"crear_lead", "calificar_lead", "actualizar_lead"})
GRUPO = "leads"

# Datos que el cliente deja al pedir que lo contacten: son operativos del lead (HU-CRM-16).
OPERATIVOS_DE_LEAD = ("name", "email", "phone", "crm_lead_id")
_TOPES = {"name": 200, "email": 254, "phone": 40}
_ESPACIOS = re.compile(r"\s+")


def es_de_captar_leads(tool: str) -> bool:
    return tool in ACCIONES_CAPTAR_LEADS


def _limpio(campo: str, valor: Any) -> str:
    texto = _ESPACIOS.sub(" ", str(valor or "")).strip()[: _TOPES.get(campo, 500)]
    return texto.lower() if campo == "email" else texto


async def _guardar_en_perfil(context: ToolContext, valores: dict[str, Any]) -> None:
    """Copia datos del cliente a su perfil por la puerta de los agentes (ADR-0179 §10). El lead ya quedó guardado:
    si el agente no tiene `perfil` concedido o el dato no se admite, solo se omite la copia."""
    valores = {k: v for k, v in valores.items() if v is not None and str(v).strip()}
    if not valores:
        return
    try:
        await guardar_datos(
            context.customer_id,
            valores,
            operativos=OPERATIVOS_DE_LEAD,
            source="tool",
            concedidos=context.write_namespaces,
            agent_id=context.agent_id,
        )
    except NamespaceNoConcedido:
        log.info("leads_perfil_no_concedido", n=len(valores))
    except HechoInvalido:
        log.warning("leads_perfil_dato_no_admitido", claves=sorted(valores))
    except Exception:
        log.warning("leads_perfil_no_guardado", exc_info=True)


async def avisar_lead_nuevo(session: Any, lead_id: uuid.UUID, customer_id: uuid.UUID | None = None) -> None:
    """Centro de notificaciones (ADR-0167): lead nuevo, en la transacción que lo crea. Sin datos del cliente en el
    texto; nunca propaga. El evento conserva el nombre de commerce: las preferencias de aviso guardadas lo usan."""
    from core.notifications.service import notify
    from core.notifications.spec import NotificationSpec

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    try:
        await notify(
            session,
            tenant_id,
            NotificationSpec(
                event_key="commerce.lead_nuevo",
                category="ventas",
                severity="info",
                title="Lead nuevo",
                body="Un cliente nuevo quedó registrado como lead. Revisa su ficha para darle seguimiento.",
                dedupe_key=f"lead_nuevo:{lead_id}",
                section="leads",
                data={"kind": "lead_nuevo", "lead_id": str(lead_id)},
                target=(
                    {"entity": "cliente", "id": str(customer_id), "section": "leads"}
                    if customer_id is not None
                    else {"entity": "lead", "id": str(lead_id), "section": "leads"}
                ),
            ),
        )
    except Exception:
        log.warning("leads_aviso_lead_nuevo_fallo", exc_info=True)


async def completar_lead(
    session: Any, customer_id: uuid.UUID, *, nombre: Any = None, email: Any = None, telefono: Any = None
) -> bool:
    """Completa el lead EXISTENTE del cliente con lo que dio en un recorrido guiado (las tools de lead no se ofrecen
    ahí). Nunca crea otro lead ni toca la memoria del cliente; pisa con el último valor dicho. True si había lead."""
    lead = await servicio.vivo(session, get_current_tenant_id(), customer_id)
    if lead is None:
        return False
    servicio.fijar_contacto(
        lead,
        name=_limpio("name", nombre),
        email=_limpio("email", email),
        phone=_limpio("phone", telefono),
    )
    lead.last_activity_at = datetime.now(UTC)
    return True


class LeadSkill(BaseSkill):
    name = "lead_skill"
    description = "Captura y calificación de leads (prospectos)"
    keywords: list[str] = []  # noqa: RUF012  (siempre registrada; la concesión es por binding)

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="crear_lead",
                description=(
                    "Registra los datos de un prospecto (lead) en el CRM. Úsala de inmediato cuando "
                    "el cliente se presenta (da su nombre) y muestra intención de comprar o "
                    "de que lo contacten. NO la uses dos veces para la misma persona: si ya "
                    "existe, usa actualizar_lead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nombre completo del prospecto"},
                        "email": {"type": "string", "description": "Email si lo dio"},
                        "phone": {"type": "string", "description": "Teléfono si lo dio"},
                        "interest": {"type": "string", "description": "Qué producto/servicio le interesa"},
                    },
                    "required": ["name", "interest"],
                },
                handler=self.crear_lead,
                side_effects=True,
                simular=sandbox_prueba.simular_crear_lead,
                # HU-AOS-47: procedencia del lead (solo cuenta el que nació en esta llamada).
                crea_registro=RegistroCreado(
                    kind="lead", id_key="lead_id", es_nuevo=lambda r: r.get("status") == "created"
                ),
            ),
            MCPTool(
                name="calificar_lead",
                description=(
                    "Asigna una puntuación 1-10 al lead según presupuesto, urgencia y poder de "
                    "decisión. Úsala cuando el lead confirme un presupuesto o monto de compra: "
                    "es el momento de calificar, no de consultar inventario ni de volver a "
                    "crear/actualizar el lead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "score": {"type": "integer", "description": "Puntuación 1-10"},
                        "notes": {"type": "string", "description": "Justificación breve"},
                    },
                    "required": ["score"],
                },
                handler=self.calificar_lead,
                side_effects=True,
                simular=sandbox_prueba.simular_calificar_lead,
            ),
            MCPTool(
                name="actualizar_lead",
                description=(
                    "Actualiza datos del lead ya registrado en esta conversación. Úsala DE "
                    "INMEDIATO cuando el lead (registrado con crear_lead) te dé un dato nuevo: "
                    "teléfono, email, interés o una corrección. No respondas 'ya anoté' sin "
                    "haberla llamado. Para marcar el lead como «perdido» (status=perdido) envía "
                    "SIEMPRE loss_reason con el motivo; sin él no se guarda el cambio."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nombre completo del prospecto"},
                        "email": {"type": "string"},
                        "phone": {"type": "string", "description": "Teléfono si lo dio"},
                        "interest": {"type": "string"},
                        "status": {"type": "string", "enum": list(ETAPAS)},
                        "loss_reason": {
                            "type": "string",
                            "enum": list(CODIGOS_DE_MOTIVO),
                            "description": "Por qué se perdió. Obligatorio si status es perdido",
                        },
                        "loss_note": {
                            "type": "string",
                            "description": "Detalle breve de la pérdida (máx. 300 caracteres), opcional",
                        },
                    },
                },
                handler=self.actualizar_lead,
                side_effects=True,
                simular=sandbox_prueba.simular_actualizar_lead,
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def crear_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        tenant_id = get_current_tenant_id()
        contacto = {k: _limpio(k, arguments.get(k)) for k in ("name", "email", "phone")}
        async with get_session() as session:
            lead, creado = await servicio.asegurar(
                session,
                tenant_id,
                context.customer_id,
                origen=origen_del_canal(context.channel),
                conversation_id=context.conversation_id,
            )
            servicio.fijar_contacto(lead, **contacto, interest=arguments.get("interest"))
            lead_id = str(lead.id)
            if creado:
                await avisar_lead_nuevo(session, lead.id, lead.customer_id)
        await _guardar_en_perfil(context, {**contacto, "crm_lead_id": lead_id})
        return {"lead_id": lead_id, "status": "created" if creado else "actualizado"}

    async def calificar_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        try:
            score = max(1, min(10, int(arguments.get("score", 0))))
        except (TypeError, ValueError):
            return {"error": "score inválido: debe ser un número entre 1 y 10."}
        async with get_session() as session:
            lead = await servicio.vivo(session, get_current_tenant_id(), context.customer_id)
            if lead is None:
                return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
            etapa = await calificar_etapa(
                session, lead, score, actor="agente", idempotency_key=f"agente:calificar_lead:{context.idempotency_key}"
            )
        return {"lead_id": str(lead.id), "score": score, "status": etapa}

    async def actualizar_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            lead = await servicio.vivo(session, get_current_tenant_id(), context.customer_id)
            if lead is None:
                return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
            # La etapa primero: si no vale, no se guarda nada del resto (HU-CRM-18).
            if arguments.get("status"):
                try:
                    await cambiar_etapa(
                        session,
                        lead,
                        arguments["status"],
                        actor="agente",
                        idempotency_key=f"agente:actualizar_lead:{context.idempotency_key}",
                        loss_reason=arguments.get("loss_reason"),
                        loss_note=arguments.get("loss_note"),
                    )
                except EtapaInvalida as exc:
                    return {"error": str(exc), "etapas_validas": list(ETAPAS)}
                except TransicionNoPermitida as exc:
                    return {"error": str(exc), "etapa_actual": lead.status}
                except MotivoDePerdidaInvalido as exc:
                    return {"error": str(exc), "motivos_validos": list(CODIGOS_DE_MOTIVO), "etapa_actual": lead.status}
                except ValueError as exc:  # nota de la pérdida demasiado larga
                    return {"error": str(exc), "etapa_actual": lead.status}
            contacto = {k: _limpio(k, arguments.get(k)) for k in ("name", "email", "phone")}
            servicio.fijar_contacto(lead, **contacto, interest=arguments.get("interest"))
            lead.last_activity_at = datetime.now(UTC)
            lead_id = str(lead.id)
        await _guardar_en_perfil(context, contacto)
        return {"lead_id": lead_id, "status": "actualizado"}

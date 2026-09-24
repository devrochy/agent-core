"""CobranzaModule: el BaseModule del pack (alineado a app-cobranza develop).

Registra la skill de cobranza, el guardrail de promesas, los cron de mora y el
panel propio (`admin.py`, `/cobranza/…`) con navegación por secciones.

Modelo (ADR-0060/0062): **el propietario es el tenant** → Cartera → Cliente →
Préstamo. Aislamiento multi-tenant (ADR-0059): 1 tenant = 1 propietario.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Importar `jobs` a nivel de módulo registra los `@register_cron` en Celery
# (worker y beat) apenas se escanea el pack -- no dentro de `on_activate()`.
import pack_cobranza.jobs

# Importar `providers` registra la contribución del pack a la vista global de
# leads (ADR-0056) al escanear el pack.
import pack_cobranza.providers  # noqa: F401
from core.mcp.types import BaseModule
from core.shared.logger import get_logger
from pack_cobranza.admin import router as admin_router
from pack_cobranza.config import CobranzaConfig
from pack_cobranza.guardrails import promesa_guardrail
from pack_cobranza.skills import CobranzaSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"


class CobranzaModule(BaseModule):
    id = PLUGIN_ID
    name = "Cobranza"
    version = "0.3.0"
    description = "Carteras, clientes, préstamos y negociación de cobranza por WhatsApp"
    requires_config = True
    config_schema = CobranzaConfig
    # Navegación por secciones (ADR-0061). El propietario ES el tenant (ADR-0062),
    # así que no hay item "Propietarios": su identidad y config viven en el core
    # (Tenants/Configuración del pack).
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Resumen", "path": "/cobranza/", "section": "General"},
        {"label": "Gestores", "path": "/cobranza/gestores", "section": "General"},
        {"label": "Carteras", "path": "/cobranza/carteras", "section": "General"},
        {"label": "Clientes", "path": "/cobranza/clientes", "section": "General"},
        {"label": "Cobros", "path": "/cobranza/cobros", "section": "General"},
        {"label": "Reglas de negociación", "path": "/cobranza/reglas", "section": "Monitoreo"},
        {"label": "Monitoreo IA", "path": "/cobranza/monitoreo", "section": "Monitoreo"},
    ]
    admin_router = admin_router
    declared_skills: list[type] = [CobranzaSkill]  # noqa: RUF012

    def on_activate(self, host: MCPHost) -> None:
        host.register_skill(CobranzaSkill())
        host.register_guardrail("cob_promesa_check", promesa_guardrail, priority=6)

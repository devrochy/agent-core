"""VoiceModule: respuestas por voz (ADR-0197).

Pack transversal: no aporta tools ni prompts. Registra la política que decide
si un turno va con nota de voz y el sintetizador que la produce en el worker.
La configuración es del negocio (o del agente, si se instala por agente).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from core.mcp.types import BaseModule

from .config import VoiceConfig
from .policy import decidir
from .tts import sintetizar

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "pack_voice"


class VoiceModule(BaseModule):
    id = PLUGIN_ID
    name = "Voz"
    display_name = "Respuestas por voz"
    version = "0.1.0"
    description = "Responde con notas de voz en WhatsApp Web cuando la política del negocio lo decide"
    tipo = "transversal"
    category = "Comunicación"
    min_plan = "free"
    requires_config = True
    config_schema = VoiceConfig
    network_allowlist = ("api.openai.com",)
    action_summary: ClassVar[list[str]] = ["Responder con notas de voz", "Mezclar voz y texto"]
    setup_guide = (
        "Elige cuándo responder con voz y la voz de OpenAI. Usa la llave de la "
        "plataforma o una propia."
    )

    def on_activate(self, host: MCPHost) -> None:
        host.register_voice_policy(decidir)
        host.register_voice_synthesizer(sintetizar)

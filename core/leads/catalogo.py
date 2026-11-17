"""Catálogos cerrados del lead (ADR-0193). La migración 0144 los copia a sus CHECK a propósito; el test de la
migración los compara con estos."""

from __future__ import annotations

# Etapas del embudo (HU-CRM-18). `status` del lead es texto libre por los leads viejos; los cambios nuevos usan estas.
ETAPAS: tuple[str, ...] = ("nuevo", "calificado", "contactado", "convertido", "perdido")
ETAPAS_TERMINALES: frozenset[str] = frozenset({"convertido", "perdido"})

ACTORES_DE_ETAPA: tuple[str, ...] = ("agente", "persona", "sistema")
ACTORES_DE_SEGUIMIENTO: tuple[str, ...] = ("persona", "sistema")
RESULTADOS_DE_SEGUIMIENTO: tuple[str, ...] = ("hecho", "reprogramado", "cancelado")

# De dónde llegó el lead. Sale del canal de la conversación o de la vía de alta, nunca de un argumento del modelo.
ORIGENES: tuple[str, ...] = ("landing", "chat_web", "whatsapp", "instagram", "telegram", "manual")

# Por qué se pierde un lead (HU-CRM-23): cerrado y fijo.
MOTIVOS_DE_PERDIDA: tuple[tuple[str, str], ...] = (
    ("precio", "Precio"),
    ("sin_respuesta", "Sin respuesta"),
    ("compro_en_otro_lado", "Compró en otro lado"),
    ("no_era_para_el", "No era para él"),
    ("otro", "Otro"),
)
CODIGOS_DE_MOTIVO: tuple[str, ...] = tuple(c for c, _ in MOTIVOS_DE_PERDIDA)

NOTA_DE_SEGUIMIENTO_MAX = 200
NOTA_DE_PERDIDA_MAX = 300

# Canal de la conversación (`Customer.channel` / `ToolContext.channel`) → origen del lead. Un canal sin origen en el
# catálogo (prueba del panel, messenger, correo) deja el origen en NULL: no se inventa.
_ORIGEN_POR_CANAL: dict[str, str] = {
    "whatsapp": "whatsapp",
    "web": "chat_web",
    "instagram": "instagram",
    "telegram": "telegram",
}


def origen_del_canal(canal: str | None) -> str | None:
    return _ORIGEN_POR_CANAL.get(canal or "")

"""Aviso por defecto fuera del horario de atención (adenda 2026-10-18 de ADR-0051).

Si el negocio configuró `business_hours` pero no escribió `out_of_hours_message`, el cliente
recibe un aviso corto en vez de silencio. El texto vive en el playbook del kernel
`fuera_de_horario` (`core/playbooks/kernel/`): viaja con el binario, ningún negocio lo
reemplaza y no entra al system prompt (no toca el replay). Fail-closed: un playbook roto lanza
y lo cubre un test de CI.

Para no responder cada mensaje, el aviso por defecto sale una vez por conversación cada
`AVISO_TTL_S` (llave de Redis con `SET NX EX`, como el aviso de conexión pausada). El camino
con mensaje configurado no pasa por aquí: sigue igual que antes.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import Any

from core.db.models import DEFAULT_ASSISTANT_NAME
from core.kernel.tarjeta import RotulosInvalidos
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "fuera_de_horario"
IDIOMAS = ("es", "en", "pt")
IDIOMA_POR_DEFECTO = "es"
# Una ventana fuera de horario rara vez pasa de una noche; un fin de semana largo recibe a lo
# sumo un aviso cada 12 h por conversación.
AVISO_TTL_S = 12 * 60 * 60
# Tope del nombre en el aviso: un nombre larguísimo no debe hacer fallar el envío (y escalar).
MAX_NOMBRE = 60


def parsear_avisos(contenido: str) -> dict[str, str]:
    """Secciones `## <idioma>` y `## <idioma>_con_nombre`, una línea cada una."""
    from core.kernel.procedures.textos import secciones

    crudas = secciones(contenido)
    claves = [c for idioma in IDIOMAS for c in (idioma, f"{idioma}_con_nombre")]
    faltan = [c for c in claves if len(crudas.get(c, [])) != 1]
    if faltan:
        raise RotulosInvalidos(f"avisos fuera de horario incompletos: {faltan}")
    avisos = {c: crudas[c][0] for c in claves}
    for clave, texto in avisos.items():
        campos = {"assistant_name": "x"} if clave.endswith("_con_nombre") else {}
        try:
            texto.format(**campos)
        except (KeyError, IndexError, ValueError) as exc:
            raise RotulosInvalidos(f"el aviso {clave} usa un campo desconocido: {exc}") from exc
    return avisos


@cache
def avisos() -> Mapping[str, str]:
    """Solo lectura: el cache es del proceso y lo comparten todos los negocios."""
    from core.agents.playbooks import load_core_playbook

    return MappingProxyType(parsear_avisos(load_core_playbook(PLAYBOOK)))


def idioma_del_negocio(language: str | None) -> str:
    """`es-CO` → `es`; lo desconocido cae a `es` (misma regla que el prompt)."""
    prefijo = str(language or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return prefijo if prefijo in IDIOMAS else IDIOMA_POR_DEFECTO


def texto_por_defecto(language: str | None, assistant_name: str | None) -> str:
    """El aviso en el idioma del negocio, con el nombre del asistente si el negocio puso uno."""
    idioma = idioma_del_negocio(language)
    nombre = " ".join(str(assistant_name or "").split())[:MAX_NOMBRE].strip()
    if nombre and nombre.casefold() != DEFAULT_ASSISTANT_NAME.casefold():
        return avisos()[f"{idioma}_con_nombre"].replace("{assistant_name}", nombre)
    return avisos()[idioma]


def _llave(tenant_id: Any, conversation_id: Any) -> str:
    return f"fuera_de_horario:aviso:{tenant_id}:{conversation_id}"


async def primer_aviso(tenant_id: Any, conversation_id: Any) -> bool:
    """True si toca mandar el aviso por defecto (no se mandó en las últimas `AVISO_TTL_S`).

    Si Redis no responde, se avisa igual: el defecto que se corrige es el silencio, y un aviso
    repetido mientras Redis está caído es el mal menor."""
    from core.memory.working import get_redis

    try:
        return bool(await get_redis().set(_llave(tenant_id, conversation_id), "1", nx=True, ex=AVISO_TTL_S))
    except Exception as exc:
        log.warning(
            "fuera_de_horario_aviso_sin_redis", conversation_id=str(conversation_id), error=type(exc).__name__
        )
        return True


async def soltar_aviso(tenant_id: Any, conversation_id: Any) -> None:
    """Mejor esfuerzo: si el turno falla después de tomar la llave, el reintento vuelve a avisar."""
    from core.memory.working import get_redis

    try:
        await get_redis().delete(_llave(tenant_id, conversation_id))
    except Exception as exc:
        log.warning(
            "fuera_de_horario_soltar_aviso_fallo", conversation_id=str(conversation_id), error=type(exc).__name__
        )

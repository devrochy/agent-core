"""Mensajes guardados del negocio: respuestas rápidas reutilizables entre canales (ADR-0178, pista D4).

Lo usa `core/api/routers/mensajes_guardados.py`; el router solo traduce HTTP. Aquí viven las reglas:

- **Qué es.** Un texto libre con título y clave que el operador reutiliza, con variables del dialecto común de
  `core/kernel/mensajes` (`{cliente.nombre}`). Hoy es SOLO almacenamiento y API: nada lo envía a un cliente ni lo
  consulta el runtime. Los textos de los packs (`plugin_configs`, cifrados) NO se leen ni se migran aquí.
- **Texto.** Se conservan los saltos de línea (una respuesta del operador los necesita, a diferencia de un parámetro
  de plantilla de Meta): solo se normalizan (`\\r\\n` → `\\n`), se quitan caracteres de control y se acota el largo.
  Variables permitidas: SOLO las que el servidor resuelve de la conversación (`contexto.VARIABLES_BASE`, no las de
  los eventos, que no existen cuando un operador responde). `{{n}}` es el dialecto de Meta y no vale aquí. Una
  variable fuera de la lista se rechaza al guardar: nunca llega a un cliente tal cual.
- **Canales.** `ConnectionType.id` de los tipos que producen conversaciones (`whatsapp_meta`, `webchat`…). Vacío o
  `todos` = todos los canales (se guarda como lista vacía). Un tipo desconocido se rechaza.
- **Estados.** `borrador`, `activo`, `archivado` (reversibles entre sí). El borrado es lógico y libera la clave.
- **Errores** como `ErrorMensajeGuardado` (status, código, mensaje, detalles): el router los vuelve `ApiError`.
- **Auditoría** de cada escritura con la clave y el estado, nunca el texto.

Toda consulta filtra por `tenant_id` además de la RLS (defensa en profundidad).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.db.models import MensajeGuardado, utcnow
from core.db.session import get_session
from core.gateway.connection_types import connection_types
from core.kernel.mensajes import PLACEHOLDER_META, render_llaves, variables_de
from core.kernel.opciones.contexto import VARIABLES_BASE
from core.shared.audit import record_audit
from core.shared.tenant import get_current_tenant_id

CLAVE = re.compile(r"^[a-z0-9_]{1,40}$")  # misma forma que `ck_mensajes_guardados_clave`
MAX_GUARDADOS = 100  # mensajes vivos por negocio (acota el listado y el abuso)
MAX_TITULO = 120
MAX_TEXTO = 1024
TODOS = "todos"
ESTADOS = ("borrador", "activo", "archivado")

# Valores de muestra de la vista previa (nunca datos reales de un cliente).
MUESTRAS = {"cliente.nombre": "Ana", "negocio.nombre": "Mi negocio"}

ACC_CREAR = "mensaje_guardado_create"
ACC_EDITAR = "mensaje_guardado_update"
ACC_ARCHIVAR = "mensaje_guardado_archive"
ACC_BORRAR = "mensaje_guardado_delete"

# Todo carácter de control salvo el salto de línea y la tabulación.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ErrorMensajeGuardado(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _scope(tenant_id: uuid.UUID) -> None:
    """El `tenant_id` explícito es el del `tenant_scope` en curso (como `plantillas_opciones_service`)."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("mensajes guardados: fuera del tenant_scope del negocio")


def _no_encontrado() -> ErrorMensajeGuardado:
    return ErrorMensajeGuardado(404, "not_found", "No existe ese mensaje guardado.")


def _clave_repetida() -> ErrorMensajeGuardado:
    return ErrorMensajeGuardado(409, "clave_repetida", "Ya tienes un mensaje guardado con esa clave.")


# ------------------------------------------------------------------ validación (pura)
def canales_permitidos() -> list[str]:
    """`ConnectionType.id` donde puede salir un mensaje guardado: los que producen conversaciones de clientes (no
    el playground), en el orden del catálogo."""
    return [t.id for t in connection_types() if t.produces_conversations and t.channel]


def variables_permitidas() -> list[str]:
    return list(VARIABLES_BASE)


def normalizar_titulo(titulo: str) -> str:
    limpio = re.sub(r"\s+", " ", _CONTROL.sub("", titulo or "")).strip()
    if not limpio:
        raise ErrorMensajeGuardado(422, "titulo_invalido", "El título no puede estar vacío.")
    if len(limpio) > MAX_TITULO:
        raise ErrorMensajeGuardado(422, "titulo_invalido", f"El título admite hasta {MAX_TITULO} caracteres.")
    return limpio


def normalizar_texto(texto: str) -> tuple[str, list[str]]:
    """El texto listo para guardar y las variables que cita, o `ErrorMensajeGuardado` 422. No usa `clean_param`: ese
    quita los saltos de línea, que aquí se conservan."""
    limpio = _CONTROL.sub("", (texto or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not limpio:
        raise ErrorMensajeGuardado(422, "texto_invalido", "El texto no puede estar vacío.")
    if len(limpio) > MAX_TEXTO:
        raise ErrorMensajeGuardado(422, "texto_invalido", f"El texto admite hasta {MAX_TEXTO} caracteres.")
    if PLACEHOLDER_META.search(limpio):
        raise ErrorMensajeGuardado(
            422, "variable_no_permitida", "Las variables van con una llave: {cliente.nombre}. El formato {{n}} es de las plantillas de Meta.",
            {"permitidas": variables_permitidas()},
        )
    variables = variables_de(limpio)
    desconocidas = [v[:40] for v in variables if v not in VARIABLES_BASE][:10]
    if desconocidas:
        raise ErrorMensajeGuardado(
            422, "variable_no_permitida", "El texto cita variables que el servidor no resuelve.",
            {"desconocidas": desconocidas, "permitidas": variables_permitidas()},
        )
    return limpio, variables


def normalizar_canales(canales: list[str] | None) -> list[str]:
    """Lista de `ConnectionType.id` sin repetir y en el orden del catálogo; `[]` = todos los canales (también si
    llega `todos`, solo o junto a otros). 422 si algún tipo no existe o no produce conversaciones."""
    pedidos = [c.strip() for c in (canales or []) if c and c.strip()]
    if TODOS in pedidos:
        return []
    permitidos = canales_permitidos()
    invalidos = sorted({c[:40] for c in pedidos if c not in permitidos})
    if invalidos:
        raise ErrorMensajeGuardado(
            422, "canales_invalidos", "Hay canales que no existen o no atienden conversaciones.",
            {"invalidos": invalidos[:10], "permitidos": permitidos},
        )
    return [c for c in permitidos if c in pedidos]


def normalizar_estado(estado: str) -> str:
    if estado not in ESTADOS:
        raise ErrorMensajeGuardado(422, "estado_invalido", "El estado es borrador, activo o archivado.", {"permitidos": list(ESTADOS)})
    return estado


def vista_previa(texto: str, ejemplos: Mapping[str, str] | None = None) -> dict[str, Any]:
    """El texto con valores de muestra, sin guardar ni enviar nada. Los `ejemplos` solo valen para variables
    permitidas; el resto de claves se ignora. Los valores se sanean como al enviar (`render_llaves`)."""
    limpio, variables = normalizar_texto(texto)
    valores = {ruta: (ejemplos or {}).get(ruta) or MUESTRAS.get(ruta, "ejemplo") for ruta in variables}
    return {"texto_renderizado": render_llaves(limpio, valores), "variables": variables, "ejemplos_usados": valores}


# ------------------------------------------------------------------ salida
def _salida(m: MensajeGuardado) -> dict[str, Any]:
    return {
        "clave": m.clave,
        "titulo": m.titulo,
        "texto": m.texto,
        "canales": list(m.canales or []),
        "todos_los_canales": not m.canales,
        "estado": m.estado,
        "variables": variables_de(m.texto),
        "creado_en": m.created_at,
        "actualizado_en": m.updated_at,
    }


def _vivo(tenant_id: uuid.UUID, clave: str) -> tuple[Any, ...]:
    return (MensajeGuardado.tenant_id == tenant_id, MensajeGuardado.clave == clave, MensajeGuardado.deleted_at.is_(None))


# ------------------------------------------------------------------ lectura
async def listar(tenant_id: uuid.UUID, estado: str | None = None) -> list[dict[str, Any]]:
    _scope(tenant_id)
    consulta = select(MensajeGuardado).where(MensajeGuardado.tenant_id == tenant_id, MensajeGuardado.deleted_at.is_(None))
    if estado is not None:
        consulta = consulta.where(MensajeGuardado.estado == normalizar_estado(estado))
    async with get_session() as session:
        filas = (await session.execute(consulta.order_by(MensajeGuardado.clave).limit(MAX_GUARDADOS))).scalars().all()
        return [_salida(m) for m in filas]


async def obtener(tenant_id: uuid.UUID, clave: str) -> dict[str, Any]:
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrado()
    async with get_session() as session:
        m = (await session.execute(select(MensajeGuardado).where(*_vivo(tenant_id, clave)))).scalar_one_or_none()
        if m is None:
            raise _no_encontrado()
        return _salida(m)


# ------------------------------------------------------------------ escritura
async def crear(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    clave: str,
    titulo: str,
    texto: str,
    canales: list[str] | None,
    estado: str = "borrador",
) -> dict[str, Any]:
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise ErrorMensajeGuardado(422, "clave_invalida", "La clave admite minúsculas, números y _ (hasta 40).")
    titulo = normalizar_titulo(titulo)
    texto, _ = normalizar_texto(texto)
    canales = normalizar_canales(canales)
    if normalizar_estado(estado) == "archivado":
        raise ErrorMensajeGuardado(422, "estado_invalido", "Un mensaje nuevo es borrador o activo.", {"permitidos": ["borrador", "activo"]})
    async with get_session() as session:
        vivos = (
            await session.execute(
                select(func.count()).select_from(MensajeGuardado).where(MensajeGuardado.tenant_id == tenant_id, MensajeGuardado.deleted_at.is_(None))
            )
        ).scalar_one()
        if vivos >= MAX_GUARDADOS:
            raise ErrorMensajeGuardado(409, "limite_de_mensajes", f"Un negocio admite hasta {MAX_GUARDADOS} mensajes guardados.")
        if (await session.execute(select(MensajeGuardado.id).where(*_vivo(tenant_id, clave)))).first() is not None:
            raise _clave_repetida()
        m = MensajeGuardado(
            tenant_id=tenant_id, clave=clave, titulo=titulo, texto=texto, canales=canales, estado=estado,
            created_by=admin_id, updated_by=admin_id,
        )
        session.add(m)
        try:
            await session.flush()
        except IntegrityError:  # carrera con otra creación de la misma clave: el índice parcial único manda
            raise _clave_repetida() from None
        resultado = _salida(m)
    await record_audit(f"admin:{admin_id}", ACC_CREAR, input_data={"clave": clave}, output_data={"estado": estado})
    return resultado


async def editar(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    clave: str,
    *,
    titulo: str | None = None,
    texto: str | None = None,
    canales: list[str] | None = None,
    estado: str | None = None,
) -> dict[str, Any]:
    """Cambia solo los campos que llegan (la clave no cambia). Sin ninguno: 422."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrado()
    if titulo is None and texto is None and canales is None and estado is None:
        raise ErrorMensajeGuardado(422, "sin_cambios", "No llegó ningún campo para cambiar.")
    nuevo_titulo = normalizar_titulo(titulo) if titulo is not None else None
    nuevo_texto = normalizar_texto(texto)[0] if texto is not None else None
    nuevos_canales = normalizar_canales(canales) if canales is not None else None
    nuevo_estado = normalizar_estado(estado) if estado is not None else None
    async with get_session() as session:
        m = (await session.execute(select(MensajeGuardado).where(*_vivo(tenant_id, clave)).with_for_update())).scalar_one_or_none()
        if m is None:
            raise _no_encontrado()
        if nuevo_titulo is not None:
            m.titulo = nuevo_titulo
        if nuevo_texto is not None:
            m.texto = nuevo_texto
        if nuevos_canales is not None:
            m.canales = nuevos_canales
        if nuevo_estado is not None:
            m.estado = nuevo_estado
        m.updated_by = admin_id
        m.updated_at = utcnow()
        resultado = _salida(m)
    accion = ACC_ARCHIVAR if nuevo_estado == "archivado" else ACC_EDITAR
    await record_audit(f"admin:{admin_id}", accion, input_data={"clave": clave}, output_data={"estado": resultado["estado"]})
    return resultado


async def eliminar(tenant_id: uuid.UUID, admin_id: uuid.UUID, clave: str) -> None:
    """Borrado lógico: libera la clave. Un mensaje en cualquier estado se puede borrar (nada lo referencia aún)."""
    _scope(tenant_id)
    if not CLAVE.fullmatch(clave):
        raise _no_encontrado()
    async with get_session() as session:
        m = (await session.execute(select(MensajeGuardado).where(*_vivo(tenant_id, clave)).with_for_update())).scalar_one_or_none()
        if m is None:
            raise _no_encontrado()
        m.deleted_at = utcnow()
        m.updated_by = admin_id
    await record_audit(f"admin:{admin_id}", ACC_BORRAR, input_data={"clave": clave})

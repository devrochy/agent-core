"""Publicación de una versión en Meta: lo puro (SPEC-40, ADR-0172 §2, HU-OPC-04). La parte con base y red está en
`core/gateway/opciones_meta.py`.

- **Nombre.** Cada versión publicada es una plantilla NUEVA de Meta, `<clave>_v<n>` (Meta limita las ediciones de una
  aprobada, así que nunca se edita: la versión `n + 1` es otro nombre y la `n` sigue válida para los envíos que la
  usan). `clave` cumple `^[a-z0-9_]{1,40}$` (CHECK de `option_templates`), que ya es un nombre válido de Meta
  (minúsculas, dígitos y guion bajo).
- **Categoría.** La elige el dueño: `UTILITY` o `MARKETING` (cambia el costo y la aprobación). `AUTHENTICATION` no
  aplica a una plantilla con opciones y se rechaza.
- **Componentes.** Salen de `mapeo.componentes_meta` y se comprueba que la plantilla resultante pase
  `envio.compatibilidad_plantilla`: es la misma prueba que hace el envío (PR 5) antes de usarla, así que una versión
  que no la pase no se manda a Meta (no serviría para enviar).

Puro: ni base, ni red, ni entorno.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.kernel.opciones import envio, mapeo
from core.kernel.opciones.definicion import (
    MAX_CUERPO,
    MAX_OPCIONES,
    MAX_PIE,
    MAX_TITULO,
    Definicion,
)

IDIOMA_POR_DEFECTO = "es"
CATEGORIAS = ("UTILITY", "MARKETING")
FORMATO_PARAMETROS = "NAMED"
_IDIOMA = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
_CLAVE = re.compile(r"^[a-z0-9_]{1,40}$")

# Resultados de una publicación (etiqueta `outcome` del log, la métrica y la auditoría): conjunto cerrado.
PUBLICADA = "published"  # la plantilla existe en Meta (pendiente de revisión) y la versión quedó ligada
YA_PUBLICADA = "already_published"  # reintento: ya estaba creada; no se llamó a Meta de nuevo
VERSION_NO_ENCONTRADA = "version_not_found"
VERSION_RETIRADA = "version_retired"
PLANTILLA_BORRADA = "template_deleted"  # la plantilla de Meta ligada se borró: hace falta una versión nueva
NOMBRE_OCUPADO = "name_taken"  # ya existe en la cuenta una plantilla `<clave>_v<n>` que NO es esta versión
LIMITE_DE_CUPO = "template_quota_full"  # la cuenta de WhatsApp ya no admite más plantillas
CATEGORIA_INVALIDA = "invalid_category"
IDIOMA_INVALIDO = "invalid_language"
DEFINICION_INVALIDA = "invalid_definition"  # componentes que Meta rechazaría; no se llamó a Meta
CONEXION_NO_LISTA = "connection_not_ready"
OTRA_CUENTA = "other_account"  # la versión ya está ligada a una plantilla de otra WABA
EN_CURSO = "in_progress"  # otra publicación de la misma versión está corriendo
LIMITE_DE_CREACION = "rate_limited"  # 100 por hora y WABA (propio) o límite de plantillas de la cuenta
RECHAZADA_POR_META = "meta_rejected"  # Meta respondió con error de negocio (parámetro inválido, nombre repetido…)
ERROR_DE_RED = "network_error"  # no se sabe si Meta la creó: el reintento concilia antes de crear
ERROR_INTERNO = "internal_error"

# Los que se pueden reintentar tal cual: el reintento concilia con Meta antes de crear (un corte o un 5xx pudo haber
# creado la plantilla; un rechazo de negocio, no).
REINTENTABLES = frozenset({ERROR_DE_RED, LIMITE_DE_CREACION, EN_CURSO, ERROR_INTERNO})


@dataclass(frozen=True)
class ResultadoPublicacion:
    """Lo que devuelve `publicar_en_meta`. `message_template_id` es la fila de `message_templates` ligada (existe
    desde la reserva, aun sin id de Meta); `estado` el `status` actual de esa fila; `categoria` la que eligió el
    dueño, visible antes de que Meta responda. `motivo` es un código o un texto de Meta acotado, nunca un dato del
    cliente."""

    ok: bool
    outcome: str
    nombre: str = ""
    estado: str | None = None
    categoria: str | None = None
    message_template_id: uuid.UUID | None = None
    meta_template_id: str | None = None
    motivo: str = ""

    @property
    def reintentable(self) -> bool:
        return not self.ok and self.outcome in REINTENTABLES


def nombre_en_meta(clave: str, version_n: int) -> str:
    """`<clave>_v<n>`. `ValueError` si la clave o el número no son válidos (no debería ocurrir con filas reales)."""
    if not _CLAVE.fullmatch(clave or "") or int(version_n) < 1:
        raise ValueError("clave o versión inválidas")
    return f"{clave}_v{int(version_n)}"


def categoria_valida(categoria: str) -> str | None:
    """La categoría en mayúsculas si es una de las permitidas; si no, None."""
    c = (categoria or "").strip().upper()
    return c if c in CATEGORIAS else None


def idioma_valido(idioma: str) -> bool:
    return bool(_IDIOMA.fullmatch(idioma or ""))


def componentes_para_publicar(definicion: Definicion, ejemplos: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Los componentes que se mandan a Meta, o `ValueError` si la plantilla resultante no pasaría
    `compatibilidad_plantilla` (no serviría para enviar esta versión). La validación contra los límites de Meta
    (`validate_template_components`) la hace el gateway antes de llamar, igual que `create_template`."""
    # Defensa propia: la forma de `Definicion` es laxa y los topes los aplica el lint al publicar la versión. Aquí se
    # vuelven a mirar los de Meta para no reservar ni llamar con algo que Meta rechazaría (los números, sin verificar
    # contra la documentación primaria: ADR-0172, riesgos abiertos).
    if not 1 <= len(definicion.opciones) <= MAX_OPCIONES:
        raise ValueError(f"la plantilla necesita de 1 a {MAX_OPCIONES} opciones")
    if any(not o.titulo.strip() or len(o.titulo) > MAX_TITULO for o in definicion.opciones):
        raise ValueError(f"el título de cada opción debe tener de 1 a {MAX_TITULO} caracteres")
    if not definicion.body.strip() or len(definicion.body) > MAX_CUERPO:
        raise ValueError(f"el cuerpo debe tener de 1 a {MAX_CUERPO} caracteres")
    if definicion.footer is not None and len(definicion.footer) > MAX_PIE:
        raise ValueError(f"el pie debe tener hasta {MAX_PIE} caracteres")
    componentes = mapeo.componentes_meta(definicion, ejemplos)
    if envio.compatibilidad_plantilla(componentes, definicion) is not None:
        raise ValueError("la plantilla resultante no coincide con la definición")
    return componentes


def _texto(componentes: Any, tipo: str) -> str:
    for c in componentes or []:
        if isinstance(c, dict) and str(c.get("type", "")).upper() == tipo:
            return str(c.get("text") or "")
    return ""


def mismos_textos(componentes: Any, definicion: Definicion) -> bool:
    """True si la plantilla de Meta ya existente dice EXACTAMENTE lo que diría la de esta versión (cuerpo, pie,
    botones y variables). `compatibilidad_plantilla` solo mira botones y variables; para ADOPTAR una plantilla que no
    creó esta versión (mismo nombre tras borrar y recrear la clave, o creada a mano) hay que comparar también los
    textos: si no, la versión quedaría ligada a un cuerpo ajeno."""
    if envio.compatibilidad_plantilla(componentes, definicion) is not None:
        return False
    esperados = mapeo.componentes_meta(definicion)
    return _texto(componentes, "BODY") == _texto(esperados, "BODY") and _texto(componentes, "FOOTER") == _texto(esperados, "FOOTER")

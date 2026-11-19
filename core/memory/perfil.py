"""Perfil canónico del cliente para la sección Clientes.

La fuente sigue siendo `memory_facts` (namespace `perfil`) y `customers.attributes` su
proyección (ADR-0114/HU-AOS-36). Este módulo no escribe: normaliza lo que hay guardado,
valida lo que llega desde el panel y define las etiquetas y opciones que comparten la API y
la ficha.

Claves canónicas (las mismas que ya usan las tools del core y pack_commerce):

- identidad: `name`, `apellido`, `alias`, `avatar_url`
- contacto: `phone`, `pais_telefono`, `email`, `tipo_documento`, `id_document`
- ubicación: `address`, `municipio`, `departamento`, `pais`, `latitud`, `longitud`
- clasificación general: `clasificacion`

Los datos viejos escritos por las tools (`full_name`, `contact_phone`, `cedula`, `city`…)
se leen por alias, sin migración. La escritura nueva usa siempre las claves canónicas.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal
from urllib.parse import quote_plus

from core.shared.telefono import PAIS_POR_DEFECTO, dividir_telefono
from core.shared.telefono import pais as pais_telefonico

TipoCampo = Literal["texto", "email", "telefono", "opciones", "numero", "url"]

CLASIFICACIONES: tuple[tuple[str, str], ...] = (
    ("nuevo", "Nuevo"),
    ("prospecto", "Prospecto"),
    ("cliente", "Cliente"),
    ("vip", "VIP"),
    ("inactivo", "Inactivo"),
    ("bloqueado", "Bloqueado"),
)

TIPOS_DOCUMENTO: tuple[tuple[str, str], ...] = (
    ("cc", "Cédula de ciudadanía"),
    ("ce", "Cédula de extranjería"),
    ("nit", "NIT"),
    ("pasaporte", "Pasaporte"),
    ("otro", "Otro"),
)

_TIPO_DOCUMENTO_ALIAS = {
    "ci": "cc",
    "cedula": "cc",
    "cedula_ciudadania": "cc",
    "cedula_extranjeria": "ce",
    "documento": "cc",
    "ruc": "nit",
}

_CLASIFICACION_ALIAS = {
    "potencial": "prospecto",
    "activo": "cliente",
    "frecuente": "cliente",
    "premium": "vip",
}

# Clave canónica → (etiqueta, tipo, máximo, opciones, grupo, ayuda)
CAMPOS: tuple[tuple[str, str, TipoCampo, int, tuple[tuple[str, str], ...], str, str | None], ...] = (
    ("name", "Nombre", "texto", 120, (), "identidad", None),
    ("apellido", "Apellido", "texto", 120, (), "identidad", None),
    (
        "alias",
        "Alias",
        "texto",
        120,
        (),
        "identidad",
        "El nombre con el que escribe en el canal, si todavía no sabemos su nombre real.",
    ),
    ("avatar_url", "Foto", "url", 500, (), "identidad", "Solo si el canal la entrega."),
    ("phone", "Teléfono", "telefono", 40, (), "contacto", None),
    (
        "pais_telefono",
        "País del teléfono",
        "opciones",
        2,
        (),
        "contacto",
        "Se usa para interpretar un número local; si el número trae +, manda el número.",
    ),
    ("email", "Correo", "email", 254, (), "contacto", None),
    ("tipo_documento", "Tipo de documento", "opciones", 20, TIPOS_DOCUMENTO, "contacto", None),
    ("id_document", "Documento", "texto", 40, (), "contacto", None),
    ("address", "Dirección", "texto", 300, (), "ubicacion", None),
    ("municipio", "Municipio", "texto", 120, (), "ubicacion", None),
    ("departamento", "Departamento", "texto", 120, (), "ubicacion", None),
    ("pais", "País", "texto", 60, (), "ubicacion", None),
    ("latitud", "Latitud", "numero", 20, (), "ubicacion", None),
    ("longitud", "Longitud", "numero", 20, (), "ubicacion", None),
    ("clasificacion", "Clasificación", "opciones", 30, CLASIFICACIONES, "clasificacion", None),
)

_CAMPO_POR_CLAVE = {c[0]: c for c in CAMPOS}
_ETIQUETA = {c[0]: c[1] for c in CAMPOS}
_GRUPO = {c[0]: c[5] for c in CAMPOS}
_MAX = {c[0]: c[3] for c in CAMPOS}
_OPCIONES = {c[0]: c[4] for c in CAMPOS}

# Alias de lectura/escritura. La primera vez que aparece una clave canónica gana.
_ALIAS: dict[str, str] = {
    "nombre": "name",
    "full_name": "name",
    "nombres": "name",
    "apellidos": "apellido",
    "alias": "alias",
    "push_name": "alias",
    "avatar": "avatar_url",
    "foto": "avatar_url",
    "foto_perfil": "avatar_url",
    "telefono": "phone",
    "teléfono": "phone",
    "contact_phone": "phone",
    "celular": "phone",
    "movil": "phone",
    "whatsapp": "phone",
    "pais_telefono": "pais_telefono",
    "correo": "email",
    "mail": "email",
    "tipo_de_documento": "tipo_documento",
    "documento": "id_document",
    "numero_documento": "id_document",
    "cedula": "id_document",
    "cédula": "id_document",
    "nit": "id_document",
    "direccion": "address",
    "dirección": "address",
    "municipality": "municipio",
    "ciudad": "municipio",
    "city": "municipio",
    "department": "departamento",
    "estado": "departamento",
    "state": "departamento",
    "country": "pais",
    "lat": "latitud",
    "latitude": "latitud",
    "lng": "longitud",
    "lon": "longitud",
    "longitude": "longitud",
    "clasificacion": "clasificacion",
    "clasificación": "clasificacion",
}

_CAMPOS_DE_TEXTO = frozenset(
    {"name", "apellido", "alias", "avatar_url", "phone", "email", "id_document", "address",
     "municipio", "departamento", "pais", "pais_telefono", "tipo_documento", "clasificacion"}
)
_CORREO = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_URL = re.compile(r"^https?://", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalizar_clave(clave: str) -> str:
    """Clave canónica de un atributo (`Dirección` → `address`)."""
    sin_tildes = unicodedata.normalize("NFKD", str(clave or ""))
    limpia = "".join(c for c in sin_tildes if not unicodedata.combining(c)).strip().lower()
    limpia = re.sub(r"[\s.\-]+", "_", limpia).strip("_")
    return _ALIAS.get(limpia, limpia)


def es_campo_canonico(clave: str) -> bool:
    return normalizar_clave(clave) in _CAMPO_POR_CLAVE


def etiqueta(clave: str) -> str:
    return _ETIQUETA.get(normalizar_clave(clave), clave)


def opciones(clave: str) -> tuple[tuple[str, str], ...]:
    return _OPCIONES.get(normalizar_clave(clave), ())


def campos_editables() -> list[dict[str, Any]]:
    """Metadatos para que el panel dibuje la ficha sin duplicar etiquetas ni opciones."""
    salida: list[dict[str, Any]] = []
    for clave, etiqueta_, tipo, maximo, opciones_, grupo, ayuda in CAMPOS:
        if clave == "pais_telefono":
            from core.shared.telefono import opciones_de_pais

            opciones_ = opciones_de_pais()
        salida.append(
            {
                "key": clave,
                "label": etiqueta_,
                "tipo": tipo,
                "max_length": maximo,
                "options": [{"value": v, "label": lbl} for v, lbl in opciones_],
                "group": grupo,
                "help": ayuda,
                "required": clave == "name",
            }
        )
    return salida


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = _CONTROL.sub("", str(valor)).strip()
    return texto or None


def _normalizar_tipo_documento(valor: Any) -> str | None:
    texto = _texto(valor)
    if texto is None:
        return None
    clave = normalizar_clave(texto)
    if clave in _TIPO_DOCUMENTO_ALIAS:
        clave = _TIPO_DOCUMENTO_ALIAS[clave]
    return clave if clave in {v for v, _ in TIPOS_DOCUMENTO} else texto


def _normalizar_clasificacion(valor: Any) -> str | None:
    texto = _texto(valor)
    if texto is None:
        return None
    clave = normalizar_clave(texto)
    if clave in _CLASIFICACION_ALIAS:
        clave = _CLASIFICACION_ALIAS[clave]
    return clave if clave in {v for v, _ in CLASIFICACIONES} else texto


def _numero(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return None


def normalizar_atributos(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Atributos guardados → claves canónicas. Tolera las claves viejas de las tools."""
    crudos = attributes or {}
    directos: dict[str, Any] = {}
    alias: dict[str, Any] = {}
    for clave, valor in crudos.items():
        canonica = normalizar_clave(str(clave))
        if canonica not in _CAMPO_POR_CLAVE or valor is None or valor == "":
            continue
        # La clave canónica manda sobre un alias (`name` gana a `full_name`) sin importar
        # el orden en que JSONB devuelva las claves.
        destino = directos if str(clave).strip().lower() == canonica else alias
        destino.setdefault(canonica, valor)
    salida: dict[str, Any] = {**alias, **directos}

    for clave in ("tipo_documento", "clasificacion"):
        if clave in salida:
            normalizada = (
                _normalizar_tipo_documento(salida[clave])
                if clave == "tipo_documento"
                else _normalizar_clasificacion(salida[clave])
            )
            if normalizada:
                salida[clave] = normalizada
            else:
                salida.pop(clave, None)

    for clave in ("latitud", "longitud"):
        if clave in salida:
            numero = _numero(salida[clave])
            if numero is None:
                salida.pop(clave, None)
            else:
                salida[clave] = numero
    return salida


def nombre_visible(
    *,
    name: str | None,
    apellido: str | None = None,
    alias: str | None = None,
    nombre_cliente: str | None = None,
    external_id: str | None = None,
) -> str:
    """Nombre real si lo hay; si no, alias del canal; si no, identificador."""
    base = (name or nombre_cliente or "").strip()
    apellido = (apellido or "").strip()
    if base and apellido and not base.lower().endswith(apellido.lower()):
        base = f"{base} {apellido}".strip()
    if base:
        return base
    return (alias or "").strip() or (external_id or "").strip() or "Cliente"


def direccion_completa(valores: dict[str, Any]) -> str | None:
    partes = [
        str(valores.get(k) or "").strip()
        for k in ("address", "municipio", "departamento", "pais")
    ]
    consulta = ", ".join(p for p in partes if p)
    return consulta or None


def _mapa_url(valores: dict[str, Any]) -> str | None:
    lat, lng = _numero(valores.get("latitud")), _numero(valores.get("longitud"))
    if lat is not None and lng is not None:
        return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}#map=17/{lat}/{lng}"
    consulta = direccion_completa(valores)
    if consulta:
        return f"https://www.openstreetmap.org/search?query={quote_plus(consulta)}"
    return None


def leer_perfil(
    attributes: dict[str, Any] | None,
    *,
    telefono: str | None = None,
    nombre_cliente: str | None = None,
    external_id: str | None = None,
    pais_por_defecto: str = PAIS_POR_DEFECTO,
) -> dict[str, Any]:
    """Perfil listo para la API: claves canónicas + datos derivados (E.164, mapa, etiqueta)."""
    valores = normalizar_atributos(attributes)
    if not valores.get("phone") and telefono:
        valores["phone"] = telefono
    if not valores.get("name") and nombre_cliente:
        valores["name"] = nombre_cliente

    telefono_info = dividir_telefono(valores.get("phone"), valores.get("pais_telefono") or pais_por_defecto)
    # Un número internacional ya dice su país: no se muestra un selector que lo contradiga.
    if telefono_info["pais"] and (
        str(valores.get("phone") or "").startswith("+") or not valores.get("pais_telefono")
    ):
        valores["pais_telefono"] = telefono_info["pais"]

    clasificacion = valores.get("clasificacion")
    etiqueta_clasificacion = dict(CLASIFICACIONES).get(clasificacion) if clasificacion else None

    return {
        "name": valores.get("name"),
        "apellido": valores.get("apellido"),
        "alias": valores.get("alias"),
        "avatar_url": valores.get("avatar_url"),
        "nombre_visible": nombre_visible(
            name=valores.get("name"),
            apellido=valores.get("apellido"),
            alias=valores.get("alias"),
            nombre_cliente=nombre_cliente,
            external_id=external_id,
        ),
        "phone": valores.get("phone"),
        "phone_e164": telefono_info["e164"],
        "phone_indicativo": telefono_info["indicativo"],
        "phone_local": telefono_info["local"],
        "pais_telefono": valores.get("pais_telefono"),
        "email": valores.get("email"),
        "tipo_documento": valores.get("tipo_documento"),
        "id_document": valores.get("id_document"),
        "address": valores.get("address"),
        "municipio": valores.get("municipio"),
        "departamento": valores.get("departamento"),
        "pais": valores.get("pais"),
        "latitud": _numero(valores.get("latitud")),
        "longitud": _numero(valores.get("longitud")),
        "direccion_completa": direccion_completa(valores),
        "direccion_normalizada": _texto((attributes or {}).get("direccion_normalizada")),
        "geocodificado_en": _texto((attributes or {}).get("geocodificado_en")),
        "geocodificacion_precision": _texto((attributes or {}).get("geocodificacion_precision")),
        "geocodificacion_proveedor": _texto((attributes or {}).get("geocodificacion_proveedor")),
        "mapa_url": _mapa_url(valores),
        "clasificacion": clasificacion,
        "clasificacion_label": etiqueta_clasificacion,
    }


def validar_perfil(
    datos: dict[str, Any],
    *,
    pais_por_defecto: str = PAIS_POR_DEFECTO,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Valida lo que llega del panel y devuelve `(valores canónicos, errores por clave)`.

    Solo acepta claves canónicas. Un valor vacío borra el dato (salvo `name`, que no puede
    quedar vacío). Si llega `phone`, se calcula `pais_telefono` cuando no venga explícito.
    """
    valores: dict[str, Any] = {}
    errores: dict[str, str] = {}
    pais_forzado: str | None = None

    for clave_cruda, valor in datos.items():
        clave = normalizar_clave(str(clave_cruda))
        if clave not in _CAMPO_POR_CLAVE:
            errores[str(clave_cruda)] = "Este dato no se edita en la ficha del cliente."
            continue
        if clave in ("name", "apellido", "alias", "address", "municipio", "departamento", "pais", "email",
                     "id_document", "avatar_url", "phone"):
            texto = _texto(valor)
        else:
            texto = _texto(valor)

        if clave == "name":
            if texto is None:
                errores[clave] = "El nombre no puede quedar vacío."
                continue
        elif texto is None:
            # Borrar un dato es válido; queda vacío en el hecho.
            valores[clave] = ""
            continue

        maximo = _MAX.get(clave, 200)
        if texto is not None and len(texto) > maximo:
            errores[clave] = f"Máximo {maximo} caracteres."
            continue

        if clave == "email" and texto and not _CORREO.match(texto):
            errores[clave] = "Escribe un correo válido."
            continue
        if clave == "avatar_url" and texto and not _URL.match(texto):
            errores[clave] = "La foto debe ser una URL http(s)."
            continue
        if clave == "tipo_documento" and texto:
            normalizado = _normalizar_tipo_documento(texto)
            if normalizado not in {v for v, _ in TIPOS_DOCUMENTO}:
                errores[clave] = "Elige un tipo de documento."
                continue
            texto = normalizado
        if clave == "clasificacion" and texto:
            normalizado = _normalizar_clasificacion(texto)
            if normalizado not in {v for v, _ in CLASIFICACIONES}:
                errores[clave] = "Elige una clasificación."
                continue
            texto = normalizado
        if clave == "pais_telefono" and texto:
            if pais_forzado:
                texto = pais_forzado
            info = pais_telefonico(texto)
            if info is None:
                errores[clave] = "Elige un país del catálogo."
                continue
            texto = info.codigo
        if clave == "phone" and texto:
            # El país que eligió la persona manda sobre el default, aunque venga después
            # de `phone` en el diccionario.
            pais_elegido = str(datos.get("pais_telefono") or pais_por_defecto)
            info = dividir_telefono(texto, pais_elegido)
            digitos = re.sub(r"\D", "", str(info["e164"] or ""))
            if len(digitos) < 7 or len(digitos) > 15:
                errores[clave] = "Escribe un teléfono válido (7 a 15 dígitos)."
                continue
            if info["pais"] and (str(texto).startswith("+") or len(re.sub(r"\D", "", str(texto))) > 10):
                # Un número internacional ya trae su país: no se puede quedar con otro
                # solo porque el selector diga otra cosa.
                pais_forzado = str(info["pais"])
            texto = info["e164"]
            if pais_forzado:
                valores["pais_telefono"] = pais_forzado
            elif info["pais"] and not datos.get("pais_telefono"):
                valores["pais_telefono"] = info["pais"]
        if clave in ("latitud", "longitud"):
            numero = _numero(texto)
            if numero is None:
                errores[clave] = "Escribe un número válido."
                continue
            limite = 90 if clave == "latitud" else 180
            if not -limite <= numero <= limite:
                errores[clave] = f"Debe estar entre -{limite} y {limite}."
                continue
            valores[clave] = numero
            continue

        valores[clave] = texto

    if ("latitud" in valores) != ("longitud" in valores):
        errores["latitud" if "latitud" in valores else "longitud"] = "Latitud y longitud van juntas."
    return valores, errores

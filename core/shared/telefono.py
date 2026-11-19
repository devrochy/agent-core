"""Países y prefijos telefónicos para normalizar el teléfono del cliente.

Catálogo pequeño y explícito (mismo criterio que la APK): no se agrega una dependencia
externa solo para separar el indicativo. El teléfono se guarda en E.164 (`+573001234567`)
y el país se guarda como ISO-3166 alpha-2 (`CO`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PaisTelefono:
    codigo: str
    nombre: str
    prefijo: str


PAISES_TELEFONO: tuple[PaisTelefono, ...] = (
    PaisTelefono("BO", "Bolivia", "+591"),
    PaisTelefono("CO", "Colombia", "+57"),
    PaisTelefono("MX", "México", "+52"),
    PaisTelefono("PE", "Perú", "+51"),
    PaisTelefono("EC", "Ecuador", "+593"),
    PaisTelefono("CL", "Chile", "+56"),
    PaisTelefono("AR", "Argentina", "+54"),
    PaisTelefono("UY", "Uruguay", "+598"),
    PaisTelefono("PY", "Paraguay", "+595"),
    PaisTelefono("VE", "Venezuela", "+58"),
    PaisTelefono("PA", "Panamá", "+507"),
    PaisTelefono("CR", "Costa Rica", "+506"),
    PaisTelefono("GT", "Guatemala", "+502"),
    PaisTelefono("SV", "El Salvador", "+503"),
    PaisTelefono("HN", "Honduras", "+504"),
    PaisTelefono("NI", "Nicaragua", "+505"),
    PaisTelefono("US", "Estados Unidos", "+1"),
    PaisTelefono("DO", "Rep. Dominicana", "+1"),
    PaisTelefono("ES", "España", "+34"),
    PaisTelefono("BR", "Brasil", "+55"),
)

PAIS_POR_DEFECTO = "CO"
_POR_CODIGO = {p.codigo: p for p in PAISES_TELEFONO}
_POR_PREFIJO = sorted(
    ((p.prefijo.lstrip("+"), p) for p in PAISES_TELEFONO),
    key=lambda par: len(par[0]),
    reverse=True,
)
_DIGITOS = re.compile(r"\D+")


def pais(codigo: str | None) -> PaisTelefono | None:
    """País del catálogo por su código ISO, o `None` si no está."""
    return _POR_CODIGO.get((codigo or "").strip().upper())


def opciones_de_pais() -> tuple[tuple[str, str], ...]:
    """`(código, etiqueta)` para un selector, en el orden del catálogo."""
    return tuple((p.codigo, f"{p.nombre} ({p.prefijo})") for p in PAISES_TELEFONO)


def _pais_del_numero(digitos: str) -> tuple[PaisTelefono | None, str]:
    """El país cuyo prefijo coincide con el inicio, y el número local sin ese prefijo."""
    for prefijo, info in _POR_PREFIJO:
        if digitos.startswith(prefijo) and len(digitos) > len(prefijo):
            return info, digitos[len(prefijo) :]
    return None, digitos


def dividir_telefono(valor: object, pais_por_defecto: str = PAIS_POR_DEFECTO) -> dict[str, str | None]:
    """Separa un teléfono en E.164, país, indicativo y número local.

    Acepta `+57 300 123 4567`, `3001234567`, `573001234567` y separadores. Si no se
    reconoce el prefijo, devuelve el número tal cual en E.164 (con `+`) y sin país.
    """
    texto = str(valor or "").strip()
    digitos = _DIGITOS.sub("", texto)
    if not digitos:
        return {"e164": None, "pais": None, "indicativo": None, "local": None}

    internacional = texto.startswith("+") or len(digitos) > 10
    if internacional:
        info, local = _pais_del_numero(digitos)
        if info is not None:
            return {
                "e164": f"+{digitos}",
                "pais": info.codigo,
                "indicativo": info.prefijo,
                "local": local,
            }
        return {"e164": f"+{digitos}", "pais": None, "indicativo": None, "local": digitos}

    info = pais(pais_por_defecto) or pais(PAIS_POR_DEFECTO)
    assert info is not None
    return {
        "e164": f"{info.prefijo}{digitos}",
        "pais": info.codigo,
        "indicativo": info.prefijo,
        "local": digitos,
    }


def normalizar_telefono(valor: object, pais_por_defecto: str = PAIS_POR_DEFECTO) -> str | None:
    """El teléfono en E.164, o `None` si no hay dígitos."""
    return dividir_telefono(valor, pais_por_defecto)["e164"]

"""Lectura y validación del catálogo de pruebas (`11-pruebas/catalogo.yaml`, HU-QA-02).

Acepta el esquema v1 (pasos en texto y un `esperado` general) y el v2 (pasos
`{accion, esperado}`, `modulo`, `prioridad`, `plataforma`, `contexto`,
`precondiciones`, `datos`, `origen`). Desde la v6 cada prueba lleva `entorno` (dónde se
ejecuta), `area` (la sección del panel o la pantalla de la APK, HU-QA-08) y `requiere`
(slugs de la guía de datos, sección `datos`, HU-QA-07). Desde la v15 las pruebas llevan
`subseccion` (tercer nivel dentro del área, HU-QA-08) y `grupo` queda solo para las
secciones `test`, `prod` y `datos`. Las mismas reglas las aplica
`herramientas/regenerar.py --check` en el CI de solvershub-docs; aquí no se
verifica que las HU existan (eso vive en docs).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

SECCIONES = ("pruebas", "test", "prod", "datos")
PRIORIDADES = ("P0", "P1", "P2")
PLATAFORMAS = ("panel", "app", "whatsapp", "web", "api", "correo")
ENTORNOS = ("panel", "apk", "whatsapp", "web", "api")
_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_ITEMS = 2000


class CatalogoInvalido(ValueError):
    """El YAML no cumple el esquema; `errores` lista cada problema."""

    def __init__(self, errores: list[str]) -> None:
        super().__init__("; ".join(errores[:5]))
        self.errores = errores


@dataclass(slots=True)
class ItemCatalogo:
    slug: str
    seccion: str
    orden: int
    titulo: str
    grupo: str = ""
    modulo: str | None = None
    resumen: str | None = None
    contexto: str | None = None
    prioridad: str | None = None
    plataforma: list[str] = field(default_factory=list)
    precondiciones: list[str] = field(default_factory=list)
    datos: list[str] = field(default_factory=list)
    pasos: list[dict[str, str | None]] = field(default_factory=list)
    esperado: str | None = None
    porque: str | None = None
    como: str | None = None
    vars: list[str] = field(default_factory=list)
    quien: str | None = None
    hu: list[str] = field(default_factory=list)
    origen: list[str] = field(default_factory=list)
    entorno: str | None = None
    area: str | None = None
    subseccion: str | None = None
    requiere: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Catalogo:
    version: int
    fecha: str
    sha256: str
    items: list[ItemCatalogo]


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _lista(valor: Any) -> list[str]:
    if valor is None:
        return []
    if isinstance(valor, str):
        return [valor.strip()] if valor.strip() else []
    if isinstance(valor, list):
        return [str(v).strip() for v in valor if v is not None and str(v).strip()]
    return [str(valor)]


def _pasos(crudo: Any, donde: str, errores: list[str]) -> list[dict[str, str | None]]:
    if not isinstance(crudo, list) or not crudo:
        errores.append(f"{donde}: una prueba necesita `pasos`")
        return []
    pasos: list[dict[str, str | None]] = []
    for i, paso in enumerate(crudo, 1):
        if isinstance(paso, str):
            pasos.append({"accion": paso.strip(), "esperado": None})
        elif isinstance(paso, dict):
            accion = _texto(paso.get("accion"))
            if not accion:
                errores.append(f"{donde}: el paso {i} no tiene `accion`")
            extra = set(paso) - {"accion", "esperado"}
            if extra:
                errores.append(f"{donde}: el paso {i} tiene campos desconocidos {sorted(extra)}")
            pasos.append({"accion": accion or "", "esperado": _texto(paso.get("esperado"))})
        else:
            errores.append(f"{donde}: el paso {i} debe ser texto o {{accion, esperado}}")
    return pasos


def parse_catalogo(contenido: str | bytes) -> Catalogo:
    """Parsea y valida el YAML. Lanza `CatalogoInvalido` con todos los errores."""
    crudo = contenido.encode("utf-8") if isinstance(contenido, str) else contenido
    try:
        data = yaml.safe_load(crudo)
    except yaml.YAMLError as exc:
        raise CatalogoInvalido([f"YAML inválido: {exc}"]) from exc
    if not isinstance(data, dict):
        raise CatalogoInvalido(["el catálogo debe ser un mapa con `version`, `fecha` e `items`"])

    errores: list[str] = []
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errores.append("falta `version` entera positiva")
    fecha = _texto(data.get("fecha")) or ""
    if not fecha:
        errores.append("falta `fecha`")
    items_crudos = data.get("items")
    if not isinstance(items_crudos, list) or not items_crudos:
        errores.append("falta la lista `items`")
        items_crudos = []
    if len(items_crudos) > MAX_ITEMS:
        errores.append(f"el catálogo supera {MAX_ITEMS} ítems")

    items: list[ItemCatalogo] = []
    vistos: set[str] = set()
    for orden, it in enumerate(items_crudos):
        if not isinstance(it, dict):
            errores.append(f"ítem #{orden}: debe ser un mapa")
            continue
        slug = str(it.get("id") or "").strip()
        donde = slug or f"ítem #{orden}"
        if not _ID_RE.fullmatch(slug):
            errores.append(f"{donde}: id ausente o con caracteres no permitidos")
        if slug in vistos:
            errores.append(f"{donde}: id duplicado")
        vistos.add(slug)
        seccion = it.get("seccion")
        if seccion not in SECCIONES:
            errores.append(f"{donde}: seccion {seccion!r} inválida")
            continue
        titulo = _texto(it.get("titulo"))
        grupo = _texto(it.get("grupo"))
        if not titulo:
            errores.append(f"{donde}: falta titulo")
        # `grupo` es obligatorio salvo en las pruebas de la v15+, que usan `subseccion`.
        if not grupo and seccion != "pruebas":
            errores.append(f"{donde}: falta grupo")
        item = ItemCatalogo(
            slug=slug,
            seccion=str(seccion),
            orden=orden,
            titulo=titulo or "",
            grupo=grupo or "",
            modulo=_texto(it.get("modulo")),
            resumen=_texto(it.get("resumen")),
            contexto=_texto(it.get("contexto")),
            prioridad=_texto(it.get("prioridad")),
            plataforma=_lista(it.get("plataforma")),
            precondiciones=_lista(it.get("precondiciones")),
            datos=_lista(it.get("datos")),
            esperado=_texto(it.get("esperado")),
            porque=_texto(it.get("porque")),
            como=_texto(it.get("como")),
            vars=_lista(it.get("vars")),
            quien=_texto(it.get("quien")),
            hu=_lista(it.get("hu")),
            origen=_lista(it.get("origen")),
            entorno=_texto(it.get("entorno")),
            area=_texto(it.get("area")),
            subseccion=_texto(it.get("subseccion")),
            requiere=_lista(it.get("requiere")),
        )
        if seccion == "pruebas":
            item.pasos = _pasos(it.get("pasos"), donde, errores)
            if any(p["esperado"] is None for p in item.pasos) and not item.esperado:
                errores.append(f"{donde}: sin `esperado` general y con pasos sin resultado esperado")
            if item.prioridad and item.prioridad not in PRIORIDADES:
                errores.append(f"{donde}: prioridad {item.prioridad!r} inválida")
            for plataforma in item.plataforma:
                if plataforma not in PLATAFORMAS:
                    errores.append(f"{donde}: plataforma {plataforma!r} inválida")
            if item.entorno and item.entorno not in ENTORNOS:
                errores.append(f"{donde}: entorno {item.entorno!r} inválido")
        else:
            for campo in ("porque", "como"):
                if not getattr(item, campo):
                    errores.append(f"{donde}: falta {campo}")
        items.append(item)

    guia = {i.slug for i in items if i.seccion == "datos"}
    for item in items:
        for requerido in item.requiere:
            if requerido not in guia:
                errores.append(f"{item.slug}: requiere {requerido!r}, que no está en la sección datos")

    if errores:
        raise CatalogoInvalido(errores)
    return Catalogo(
        version=int(version),  # type: ignore[arg-type]
        fecha=fecha,
        sha256=hashlib.sha256(crudo).hexdigest(),
        items=items,
    )

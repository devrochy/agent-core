"""Controlador único de almacenamiento (ADR-0085 enmendado, D-SPEC-04-2).

Todo archivo que sube un usuario o descarga un canal pasa por aquí: valida tipo y tamaño,
resuelve la ruta contra `TENANTS_ROOT` sin traversal, escribe de forma atómica y devuelve una
URL firmada de vida corta. El backend es filesystem hoy; S3/R2 se enchufa después detrás de la
misma interfaz.

`key` es siempre la ruta relativa a `TENANTS_ROOT` (compatible con los archivos existentes de
QA y conocimiento), nunca una ruta absoluta ni la que manda el navegador.

El registro `stored_files` (retención, cuota, auditoría y limpieza) es el paso siguiente de la
HU: aquí no hay blobs ni metadatos en la base, solo archivos y firmas.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from core.almacenamiento import firmas
from core.almacenamiento.perfiles import PERFILES, Perfil, StorageInvalido, perfil, validar
from core.config import get_settings
from core.onboarding import carpeta_del_tenant, tenants_root

__all__ = [
    "PERFILES",
    "URL_TTL_CANAL",
    "URL_TTL_UI",
    "StorageInvalido",
    "StoredRef",
    "abrir",
    "borrar",
    "carpeta_de",
    "guardar",
    "key_limpia",
    "resolver_avatar",
    "url",
]

# Vida de la URL firmada: corta para el panel/APK, más larga para que Meta descargue la imagen
# al enviar una plantilla o un mensaje.
URL_TTL_UI = 15 * 60
URL_TTL_CANAL = 60 * 60

_SEGMENTO = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class StoredRef:
    key: str
    mimetype: str
    bytes: int
    sha256: str
    url: str


def _base(perfil_: Perfil, tenant_slug: str | None) -> Path:
    if perfil_.tenant_scoped:
        if not tenant_slug:
            raise StorageInvalido("tenant_requerido", "Falta el negocio del archivo.")
        return carpeta_del_tenant(tenant_slug)
    return tenants_root()


def _nombre_base(valor: str | None) -> str:
    if valor is None:
        return uuid.uuid4().hex
    if not _SEGMENTO.fullmatch(valor):
        raise StorageInvalido("nombre_invalido", "El nombre del archivo no es válido.")
    return valor


def key_limpia(key: str | None) -> bool:
    """`True` si la `key` es una ruta relativa sin trucos: sin segmentos vacíos, `.` ni `..`,
    sin barras invertidas, sin NUL y sin ser absoluta. Las keys que genera `guardar` siempre
    cumplen esto; una que no lo cumple vino de fuera (o de un dato viejo) y no se sirve ni se borra."""
    if not key or "\\" in key or "\x00" in key or key.startswith("/"):
        return False
    return all(segmento not in ("", ".", "..") for segmento in key.split("/"))


def carpeta_de(kind: str, tenant_slug: str | None = None) -> str:
    """Carpeta (relativa a `TENANTS_ROOT`) donde `guardar` deja los archivos de ese `kind`.

    Es el valor para `dentro_de` de `abrir`/`borrar`: p. ej. `carpeta_de("producto", "acme")` es
    `acme/productos`. Lanza `StorageInvalido` si el perfil es por negocio y falta el slug."""
    perfil_ = perfil(kind)
    base = _base(perfil_, tenant_slug)
    return base.relative_to(tenants_root().resolve()).joinpath(perfil_.carpeta).as_posix()


def _ruta(key: str, dentro_de: str | None = None) -> Path | None:
    """Ruta absoluta de una `key`; `None` si no es limpia o escapa de `TENANTS_ROOT`.

    Con `dentro_de` (carpeta relativa a `TENANTS_ROOT`, ver `carpeta_de`) además exige que el
    destino ya resuelto quede dentro de esa carpeta: así una key de un negocio no puede llegar al
    archivo de otro con `..`, ni por un enlace simbólico."""
    if not key_limpia(key):
        return None
    raiz = tenants_root().resolve()
    base = raiz
    if dentro_de is not None:
        if not key_limpia(dentro_de):
            return None
        base = (raiz / dentro_de).resolve()
        if base == raiz or raiz not in base.parents:
            return None
    destino = (raiz / key).resolve()
    if destino == base or base not in destino.parents:
        return None
    return destino


def url(key: str, ttl_s: int = URL_TTL_UI) -> str:
    """URL firmada para descargar el archivo (absoluta, con la base pública del backend)."""
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/api/v1/files/{firmas.firmar(key, ttl_s)}"


def guardar(
    kind: str,
    contenido: bytes,
    *,
    mimetype: str | None,
    tenant_slug: str | None = None,
    subcarpeta: str = "",
    nombre: str | None = None,
    nombre_base: str | None = None,
) -> StoredRef:
    """Guarda el archivo y devuelve su referencia. Lanza `StorageInvalido` si no sirve."""
    perfil_ = perfil(kind)
    tipo, extension = validar(perfil_, mimetype, nombre, contenido)
    carpeta = _base(perfil_, tenant_slug) / perfil_.carpeta
    if subcarpeta:
        if not _SEGMENTO.fullmatch(subcarpeta):
            raise StorageInvalido("subcarpeta_invalida", "La carpeta del archivo no es válida.")
        carpeta = carpeta / subcarpeta
    nombre_fisico = f"{_nombre_base(nombre_base)}{extension}"
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / nombre_fisico
    temporal = carpeta / f".{nombre_fisico}.tmp"
    temporal.write_bytes(contenido)
    temporal.replace(destino)

    key = destino.relative_to(tenants_root().resolve()).as_posix()
    return StoredRef(
        key=key,
        mimetype=tipo,
        bytes=len(contenido),
        sha256=hashlib.sha256(contenido).hexdigest(),
        url=url(key),
    )


def abrir(key: str, *, dentro_de: str | None = None) -> Path | None:
    """Ruta física de un archivo; `None` si la key no es limpia, escapa de `TENANTS_ROOT` (o de
    `dentro_de`, si se pasa) o no existe."""
    destino = _ruta(key, dentro_de)
    return destino if destino is not None and destino.is_file() else None


def borrar(key: str, *, dentro_de: str | None = None) -> None:
    """Borra el archivo (limpieza o reemplazo). Nunca toca algo fuera de `TENANTS_ROOT` (ni de
    `dentro_de`, si se pasa) ni una carpeta; una key hostil se ignora."""
    destino = _ruta(key, dentro_de)
    if destino is not None and destino.is_file():
        destino.unlink(missing_ok=True)


def resolver_avatar(valor: str | None) -> str | None:
    """URL para mostrar un avatar: respeta una URL externa o firma la `key` guardada."""
    if not valor:
        return None
    if valor.startswith(("http://", "https://")):
        return valor
    return url(valor)

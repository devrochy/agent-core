"""Documentos legales de un negocio vendedor y sus versiones (HU-SH-07, ADR-0125).

Quien compra un plan de la plataforma acepta términos, privacidad y DPA **en la versión
vigente**. Las versiones viven en `documentos_legales` (del vendedor) y se publican desde
Plataforma: cuando llegue el texto del abogado (HU-GTM-02) se publica una versión nueva, sin
tocar código. Ninguna fila se edita: cada aceptación apunta a la versión que se mostró.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import DocumentoLegal
from core.db.session import get_session
from core.shared.audit import record_audit
from core.shared.tenant import tenant_scope

# Los tres que se aceptan en toda alta. El orden es el de la lista que ve el comprador.
TIPOS: tuple[str, ...] = ("terminos", "privacidad", "dpa")
NOMBRES: dict[str, str] = {
    "terminos": "Términos y condiciones",
    "privacidad": "Aviso de privacidad",
    "dpa": "Acuerdo de tratamiento de datos (DPA)",
}
_MAX_URL = 500


class DocumentoInvalido(ValueError):
    """La versión o el enlace no sirven (el mensaje se puede mostrar al master)."""


class VersionRepetida(ValueError):
    """Ya hay una versión con ese nombre para ese tipo de documento."""


@dataclass(frozen=True)
class Documento:
    tipo: str
    version: str
    url: str
    vigente_desde: datetime
    publicado_por: str | None = None

    @property
    def nombre(self) -> str:
        return NOMBRES[self.tipo]

    def para_aceptar(self) -> dict[str, str]:
        """Lo que se muestra y se guarda en la aceptación (sin quién lo publicó)."""
        return {"tipo": self.tipo, "nombre": self.nombre, "version": self.version, "url": self.url}


def _documento(fila: DocumentoLegal) -> Documento:
    return Documento(
        tipo=fila.tipo,
        version=fila.version,
        url=fila.url,
        vigente_desde=fila.vigente_desde,
        publicado_por=fila.publicado_por,
    )


def url_valida(valor: str) -> str:
    """Solo `https://` con dominio, sin credenciales ni espacios. Va en el chat del comprador."""
    texto = (valor or "").strip()
    if not texto or len(texto) > _MAX_URL or any(c.isspace() for c in texto):
        raise DocumentoInvalido("El enlace tiene que ser una URL https de hasta 500 caracteres.")
    # Solo ASCII imprimible: sin dominios con letras parecidas (homógrafos) ni caracteres que
    # invierten el texto (U+202E), que servirían para phishing en el chat del comprador.
    if not all(33 <= ord(c) < 127 for c in texto):
        raise DocumentoInvalido("El enlace solo puede llevar caracteres ASCII.")
    partes = urlsplit(texto)
    if partes.scheme != "https" or not partes.hostname or "." not in partes.hostname:
        raise DocumentoInvalido("El enlace tiene que ser una URL https con dominio.")
    if partes.username or partes.password:
        raise DocumentoInvalido("El enlace no puede llevar usuario ni contraseña.")
    return texto


def version_valida(valor: str) -> str:
    texto = (valor or "").strip()
    if not texto or len(texto) > 40 or not texto[0].isalnum() or not all(
        c.isascii() and (c.isalnum() or c in "._-") for c in texto
    ):
        raise DocumentoInvalido("La versión va con letras, números, punto, guion o guion bajo (hasta 40).")
    return texto


async def documentos_vigentes(vendedor: uuid.UUID) -> dict[str, Documento]:
    """La versión vigente de cada tipo: la de `vigente_desde` más reciente que ya empezó."""
    ahora = datetime.now(UTC)
    async with tenant_scope(vendedor):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(DocumentoLegal)
                    .where(
                        DocumentoLegal.tenant_id == vendedor,
                        DocumentoLegal.deleted_at.is_(None),
                        DocumentoLegal.vigente_desde <= ahora,
                    )
                    .distinct(DocumentoLegal.tipo)
                    .order_by(
                        DocumentoLegal.tipo,
                        DocumentoLegal.vigente_desde.desc(),
                        DocumentoLegal.created_at.desc(),
                    )
                )
            ).scalars().all()
    return {fila.tipo: _documento(fila) for fila in filas if fila.tipo in TIPOS}


def faltantes(vigentes: dict[str, Documento]) -> list[str]:
    return [tipo for tipo in TIPOS if tipo not in vigentes]


def version_legal(vigentes: dict[str, Documento]) -> str:
    """Huella corta del juego de versiones vigentes. El comprador acepta ESTE juego: si se
    publica otra versión entre que se mostró y que aceptó, la huella cambia y se vuelve a
    mostrar."""
    clave = "|".join(f"{tipo}:{vigentes[tipo].version}" for tipo in TIPOS if tipo in vigentes)
    return hashlib.sha256(clave.encode()).hexdigest()[:12]


async def historial(vendedor: uuid.UUID, *, limite: int = 100) -> list[Documento]:
    async with tenant_scope(vendedor):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(DocumentoLegal)
                    .where(DocumentoLegal.tenant_id == vendedor, DocumentoLegal.deleted_at.is_(None))
                    .order_by(DocumentoLegal.vigente_desde.desc(), DocumentoLegal.created_at.desc())
                    .limit(limite)
                )
            ).scalars().all()
    return [_documento(fila) for fila in filas]


async def publicar(
    vendedor: uuid.UUID, *, tipo: str, version: str, url: str, por: str
) -> Documento:
    """Publica una versión nueva, vigente desde ya. No reemplaza ni edita las anteriores."""
    if tipo not in TIPOS:
        raise DocumentoInvalido(f"Tipo de documento desconocido: usa {', '.join(TIPOS)}.")
    fila = DocumentoLegal(
        id=uuid.uuid4(),
        tenant_id=vendedor,
        tipo=tipo,
        version=version_valida(version),
        url=url_valida(url),
        vigente_desde=datetime.now(UTC),
        publicado_por=(por or "desconocido")[:254],
    )
    try:
        async with tenant_scope(vendedor):
            async with get_session() as session:
                session.add(fila)
                await session.flush()
    except IntegrityError as exc:
        raise VersionRepetida(f"Ya existe la versión {fila.version} de {NOMBRES[tipo]}.") from exc
    async with tenant_scope(vendedor):
        await record_audit(
            actor=fila.publicado_por,
            action="documento_legal_publicado",
            input_data={"tipo": tipo, "version": fila.version, "url": fila.url},
        )
    return _documento(fila)


__all__ = [
    "NOMBRES",
    "TIPOS",
    "Documento",
    "DocumentoInvalido",
    "VersionRepetida",
    "documentos_vigentes",
    "faltantes",
    "historial",
    "publicar",
    "url_valida",
    "version_legal",
    "version_valida",
]

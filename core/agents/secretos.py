"""Secretos de las tools declarativas en la instalación `core` (ADR-0113 §2, ADR-0118 §5).

Un header sensible de una tool declarativa (`Authorization`, `Cookie`, `X-Api-Key`...) se
declara por referencia, `{"secret": "<nombre>"}`, y su valor vive cifrado en
`pack_installations.config_encrypted` de la instalación `core` de scope negocio. Esa instalación
solo guarda secretos: no habilita nada, `enabled_packs` no la ve y no cuenta para el plan.

- Cifrado AES-GCM de `core.shared.crypto` con AAD `(tenant_id, installation_id)`: el texto
  cifrado de un negocio no se descifra en la fila de otro.
- **Cada secreto está atado a sus dominios:** se guarda con los dominios a los que puede
  viajar (los `allowed_domains` de la tool que lo usa al escribirlo) y el resolver solo lo
  entrega para una petición a uno de ellos. Quien edita tools sin conocer el valor no puede
  apuntar una referencia a un dominio suyo para sacarlo: para sumar un dominio hay que volver
  a escribir el secreto.
- Servicio único: lo usan el editor de tools declarativas (`/api/v1` y SSR), el resolver de
  `core/mcp/declarative.py` al ejecutar y el backfill de HU-AOS-13 (que mueve los literales
  v2 que saca el lector tolerante antes de crear la primera `agent_version`).
- Siempre dentro del `tenant_scope` del negocio (RLS), y además filtra por `tenant_id`.
- Nunca loguea ni devuelve valores, salvo `resolver_secretos` al ejecutar la tool.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import PackInstallationRecord
from core.db.session import get_session
from core.mcp.types import public_fqdn
from core.shared.crypto import seal, unseal
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

CORE_INSTALLATION_PACK = "core"
_CLAVE = "declarativeSecrets"
SECRET_NAME_RE = re.compile(r"[a-z0-9_]{1,60}")
MAX_SECRET_BYTES = 8000
MAX_SECRETOS = 100
MAX_DOMINIOS = 20


class SecretoInvalido(ValueError):
    """Nombre, valor o dominios de un secreto fuera de formato (sin el valor en el mensaje)."""


@dataclass(frozen=True)
class SecretoDeclarativo:
    """Un secreto nuevo: su valor y los dominios a los que puede viajar."""

    valor: SecretStr
    dominios: tuple[str, ...]


def aad_instalacion(tenant_id: uuid.UUID, installation_id: uuid.UUID) -> bytes:
    """AAD de `pack_installations.config_encrypted` (ADR-0118 §5). Formato único: el
    `load_config` de HU-AOS-16 debe usar este mismo helper."""
    return f"pack_installations:{tenant_id}:{installation_id}".encode()


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    """El llamador debe estar en el `tenant_scope` de ESE negocio (RLS): nunca se lee ni se
    escribe la instalación de un negocio desde el contexto de otro."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("secretos: fuera del tenant_scope del negocio")


async def _instalacion(session, tenant_id: uuid.UUID, *, bloquear: bool) -> PackInstallationRecord | None:  # type: ignore[no-untyped-def]
    _exigir_scope(tenant_id)
    query = select(PackInstallationRecord).where(
        PackInstallationRecord.tenant_id == tenant_id,
        PackInstallationRecord.pack_id == CORE_INSTALLATION_PACK,
        PackInstallationRecord.scope == "tenant",
        PackInstallationRecord.deleted_at.is_(None),
    )
    if bloquear:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def _leer(fila: PackInstallationRecord) -> dict[str, dict[str, object]]:
    """`{nombre: {"value": str, "domains": [str]}}` descifrado. Entradas malformadas fuera."""
    if not fila.config_encrypted:
        return {}
    data = json.loads(unseal(fila.config_encrypted, aad_instalacion(fila.tenant_id, fila.id)))
    secretos = data.get(_CLAVE) if isinstance(data, dict) else None
    limpios: dict[str, dict[str, object]] = {}
    for nombre, entrada in (secretos or {}).items() if isinstance(secretos, dict) else ():
        if isinstance(entrada, dict) and isinstance(entrada.get("value"), str):
            dominios = [str(d) for d in entrada.get("domains") or [] if isinstance(d, str)]
            limpios[str(nombre)] = {"value": entrada["value"], "domains": dominios}
    return limpios


def _validar(secretos: Mapping[str, SecretoDeclarativo]) -> dict[str, dict[str, object]]:
    limpios: dict[str, dict[str, object]] = {}
    for nombre, secreto in secretos.items():
        if not SECRET_NAME_RE.fullmatch(nombre):
            raise SecretoInvalido(f"Nombre de secreto inválido: se espera [a-z0-9_]{{1,60}} ({nombre[:60]!r}).")
        texto = secreto.valor.get_secret_value()
        if len(texto.encode()) > MAX_SECRET_BYTES:
            raise SecretoInvalido(f"El secreto '{nombre}' debe tener hasta {MAX_SECRET_BYTES} bytes.")
        if any(c in texto for c in "\r\n\x00"):
            # Va como valor de un header HTTP: un salto de línea sería inyección de headers.
            raise SecretoInvalido(f"El secreto '{nombre}' no puede tener saltos de línea.")
        dominios = sorted({d for d in (public_fqdn(x) for x in secreto.dominios) if d})
        if not dominios or len(dominios) > MAX_DOMINIOS:
            raise SecretoInvalido(
                f"El secreto '{nombre}' necesita entre 1 y {MAX_DOMINIOS} dominios públicos a los que viajar."
            )
        limpios[nombre] = {"value": texto, "domains": dominios}
    return limpios


async def guardar_secretos_declarativos(
    tenant_id: uuid.UUID,
    secretos: Mapping[str, SecretoDeclarativo],
    *,
    quitar: Iterable[str] = (),
    actor: uuid.UUID | None = None,
) -> dict[str, list[str]]:
    """Guarda (o reemplaza) secretos y quita los de `quitar`, en una transacción y con la fila
    bloqueada. Crea la instalación `core` del negocio si falta. Devuelve los configurados
    (nombre → dominios). Lanza `SecretoInvalido` sin escribir nada si algo no valida."""
    _exigir_scope(tenant_id)
    nuevos = _validar(secretos)
    quitar = set(quitar)
    for intento in range(2):
        try:
            async with get_session() as session:
                fila = await _instalacion(session, tenant_id, bloquear=True)
                if fila is None:
                    fila = PackInstallationRecord(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        pack_id=CORE_INSTALLATION_PACK,
                        scope="tenant",
                        status="active",
                        installed_by=actor,
                    )
                    session.add(fila)
                    actuales: dict[str, dict[str, object]] = {}
                else:
                    actuales = _leer(fila)
                actuales = {k: v for k, v in actuales.items() if k not in quitar} | nuevos
                if len(actuales) > MAX_SECRETOS:
                    raise SecretoInvalido(f"Máximo {MAX_SECRETOS} secretos por negocio.")
                fila.config_encrypted = seal(
                    json.dumps({_CLAVE: actuales}, ensure_ascii=False), aad_instalacion(tenant_id, fila.id)
                )
                resultado = {k: list(v["domains"]) for k, v in actuales.items()}  # type: ignore[arg-type]
            log.info(
                "secretos_declarativos_guardados",
                tenant_id=str(tenant_id),
                nombres=sorted(nuevos),
                quitados=sorted(quitar),
            )
            return resultado
        except IntegrityError:
            # Dos altas concurrentes de la instalación: la segunda reintenta sobre la primera.
            if intento:
                raise
    raise AssertionError("inalcanzable")


async def secretos_configurados(tenant_id: uuid.UUID) -> dict[str, list[str]]:
    """Nombres de los secretos del negocio con sus dominios (nunca los valores)."""
    async with get_session() as session:
        fila = await _instalacion(session, tenant_id, bloquear=False)
        actuales = _leer(fila) if fila is not None else {}
    return {k: list(v["domains"]) for k, v in actuales.items()}  # type: ignore[arg-type]


async def resolver_secretos(tenant_id: uuid.UUID, nombres: Iterable[str], host: str) -> dict[str, str]:
    """Valores de los secretos pedidos que existen Y pueden viajar a `host` (al ejecutar)."""
    pedidos = set(nombres)
    destino = public_fqdn(host or "")
    if not pedidos or destino is None:
        return {}
    async with get_session() as session:
        fila = await _instalacion(session, tenant_id, bloquear=False)
        actuales = _leer(fila) if fila is not None else {}
    return {
        k: str(v["value"])
        for k, v in actuales.items()
        if k in pedidos and destino in v["domains"]  # type: ignore[operator]
    }


__all__ = [
    "CORE_INSTALLATION_PACK",
    "MAX_SECRET_BYTES",
    "SECRET_NAME_RE",
    "SecretoDeclarativo",
    "SecretoInvalido",
    "aad_instalacion",
    "guardar_secretos_declarativos",
    "resolver_secretos",
    "secretos_configurados",
]

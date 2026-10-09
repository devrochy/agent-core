"""Aprovisionamiento automático de negocios (HU-SH-03, ADR-0121).

Un negocio VENDEDOR (allowlist `PROVISIONING_SELLER_TENANTS`, hoy `solvershub`) vende un
plan de la plataforma. Cuando su suscripción se activa (`subscription.activated` del
outbox, ADR-0107), el consumidor del core crea el negocio del comprador con ese plan, el
preset de su vertical y una invitación al owner, sin intervención humana.

**Identidad de servicio.** `provisioning` es un principal de tipo `service` que solo
existe dentro del core: no es una fila de `admins`, no tiene contraseña, token, sesión de
panel ni permisos, y ninguna ruta de la API la acepta. Sirve para una sola cosa: firmar
como actor lo que hace el consumidor en `audit_log`.

Módulos:

- `fuentes`: contrato `DatosDeAlta` y registro de fuentes. Es lo ÚNICO de este paquete
  que puede importar un pack (lo implementa `pack_commerce` con su pedido).
- `vendedores`: la allowlist.
- `consumer`: el handler de `subscription.activated` y el alta.
- `owner`: identidad del owner, invitación y entrega del enlace.

Ninguna tool expuesta al LLM importa el alta (test «Sin el LLM» y contrato
`alta-fuera-del-llm` de `.importlinter`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IdentidadDeServicio:
    """Principal interno del core. Sin credenciales: solo nombra al actor de la auditoría."""

    nombre: str
    tipo: str = "service"

    @property
    def actor(self) -> str:
        return self.nombre


PROVISIONING = IdentidadDeServicio("provisioning")
ACTOR = PROVISIONING.actor


class AltaEnRevision(Exception):
    """Falta algo que no va a llegar con un reintento: el alta queda en revisión humana."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class AltaTransitoria(RuntimeError):
    """Algo que puede resolverse solo (el pedido todavía no es visible, la entrega falló):
    el outbox reintenta con backoff; al agotar los intentos del alta, queda en revisión."""


class AltaOcupada(RuntimeError):
    """Otro worker está procesando la misma alta: el outbox la reintenta más tarde."""


__all__ = [
    "ACTOR",
    "PROVISIONING",
    "AltaEnRevision",
    "AltaOcupada",
    "AltaTransitoria",
    "IdentidadDeServicio",
]

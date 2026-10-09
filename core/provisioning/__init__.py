"""Aprovisionamiento automático de negocios (HU-SH-03, ADR-0121).

Un negocio VENDEDOR (allowlist `PROVISIONING_SELLER_TENANTS`, por UUID) vende un plan de la
plataforma. Cuando su suscripción se activa (`subscription.activated` del outbox, ADR-0107),
el consumidor del core crea el negocio del comprador con ese plan, el preset de su vertical
y una invitación al owner, sin intervención humana.

**Identidad de servicio.** `provisioning` es un actor que solo existe dentro del core: no es
una fila de `admins`, no tiene contraseña, token, sesión de panel ni permisos, y ninguna
ruta de la API lo acepta. Solo firma lo que hace el consumidor en `audit_log`.

Módulos:

- `fuentes`: contrato `DatosDeAlta` y registro de fuentes. Es lo ÚNICO de este paquete
  que puede importar un pack (lo implementa `pack_commerce` con su pedido).
- `vendedores`: la allowlist.
- `consumer`: el handler de `subscription.activated` y el alta.
- `owner`: cuenta del owner, invitación, correo con el enlace y aviso por WhatsApp.

Ninguna tool expuesta al LLM llega al alta (test «Sin el LLM» y contrato
`alta-fuera-del-llm` de `.importlinter`).
"""

from __future__ import annotations

# Actor de la auditoría del aprovisionamiento: la identidad de servicio, sin credenciales.
ACTOR = "provisioning"


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


__all__ = ["ACTOR", "AltaEnRevision", "AltaOcupada", "AltaTransitoria"]

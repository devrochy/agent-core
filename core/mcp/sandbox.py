"""Sandbox de la prueba del panel (HU-ARCH-14, D-SPEC-02-3).

El canal `test` no debe alterar la data del negocio. Las tools que escriben declaran
`side_effects=True` y un `simular`; el host ejecuta el simulador en vez del handler real.
Los simuladores guardan su estado ficticio (cotizaciones, pedidos, citas, suscripciones…)
en Redis, por negocio y conversación, para que el flujo funcione entre varios mensajes.

No es una fuente de verdad ni se mezcla con las tablas del negocio: vive con TTL corto y se
borra al cerrar/reiniciar la prueba (`core.admin.playground.close_test_conversation`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

TTL_S = 86_400
_PREFIX = "sandbox"


def _redis():  # type: ignore[no-untyped-def]
    from core.memory.working import get_redis

    return get_redis()


class SandboxPrueba:
    """Estado ficticio de una prueba: un hash Redis por negocio + conversación."""

    def __init__(self, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id

    @classmethod
    def de_contexto(cls, conversation_id: uuid.UUID) -> SandboxPrueba:
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            raise RuntimeError("SandboxPrueba sin tenant en contexto")
        return cls(tenant_id, conversation_id)

    def _clave(self) -> str:
        return f"{_PREFIX}:{self.tenant_id}:{self.conversation_id}"

    def _clave_seq(self, nombre: str) -> str:
        return f"{_PREFIX}:seq:{self.tenant_id}:{self.conversation_id}:{nombre}"

    async def guardar(self, espacio: str, clave: str, valor: dict[str, Any]) -> None:
        """Guarda/actualiza un objeto del sandbox (`espacio` = quotes, orders, leads…)."""
        try:
            r = _redis()
            campo = f"{espacio}:{clave}"
            await r.hset(self._clave(), campo, json.dumps(valor, ensure_ascii=False, default=str))
            await r.expire(self._clave(), TTL_S)
        except Exception:
            log.warning("sandbox_guardar_falló", espacio=espacio, clave=clave, exc_info=True)

    async def leer(self, espacio: str, clave: str) -> dict[str, Any] | None:
        try:
            crudo = await _redis().hget(self._clave(), f"{espacio}:{clave}")
        except Exception:
            log.warning("sandbox_leer_falló", espacio=espacio, clave=clave, exc_info=True)
            return None
        if not crudo:
            return None
        try:
            return json.loads(crudo)
        except (TypeError, ValueError):
            return None

    async def listar(self, espacio: str) -> list[dict[str, Any]]:
        try:
            datos = await _redis().hgetall(self._clave())
        except Exception:
            log.warning("sandbox_listar_falló", espacio=espacio, exc_info=True)
            return []
        prefijo = f"{espacio}:"
        salida: list[dict[str, Any]] = []
        for campo, crudo in datos.items():
            if not campo.startswith(prefijo):
                continue
            try:
                valor = json.loads(crudo)
            except (TypeError, ValueError):
                continue
            if isinstance(valor, dict):
                salida.append(valor)
        return salida

    async def buscar(self, espacio: str, campo: str, valor: Any) -> dict[str, Any] | None:
        for fila in await self.listar(espacio):
            if fila.get(campo) == valor:
                return fila
        return None

    async def siguiente(self, nombre: str) -> int:
        """Contador propio del sandbox (nunca toca `crm_order_counters` ni secuencias reales)."""
        try:
            r = _redis()
            clave = self._clave_seq(nombre)
            n = int(await r.incr(clave))
            await r.expire(clave, TTL_S)
            return n
        except Exception:
            log.warning("sandbox_siguiente_falló", nombre=nombre, exc_info=True)
            return 0

    async def limpiar(self) -> None:
        try:
            r = _redis()
            await r.delete(self._clave())
            for nombre in ("order", "quote", "lead", "appointment", "subscription", "payment"):
                await r.delete(self._clave_seq(nombre))
        except Exception:
            log.warning("sandbox_limpiar_falló", exc_info=True)


def numeracion(prefijo: str, n: int) -> str:
    """Número visible de un objeto simulado: `PRUEBA-0001` (nunca `OC-…` real)."""
    return f"PRUEBA-{n:04d}" if n else "PRUEBA-0000"


__all__ = ["SandboxPrueba", "numeracion"]

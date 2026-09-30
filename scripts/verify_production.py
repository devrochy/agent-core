"""Checklist de promoción a producción -- corre los 9 checks automáticos
de `core/gateway/meta_production_readiness.py` y sale con código distinto
de 0 si alguno da `False` (un `None`/no-configurado se imprime como
advertencia, nunca como fallo duro -- mismo criterio tri-estado que el
resto del proyecto).

Uso: `python scripts/verify_production.py` -- pensado para correr con las
env vars REALES de producción cargadas (Coolify las inyecta al
contenedor; se puede correr con `docker exec` o como un paso puntual del
runbook de promoción, ver docs/despliegue-coolify.md). La mayoría de los
checks solo dan una respuesta con sentido contra un despliegue real --
corrido en cualquier otro entorno, es normal ver varios en rojo o "?".

Requiere `--tenant-id` para el check de salud de conexiones (nº9) -- el
resto de los checks son a nivel de app/infraestructura, no por tenant.

SEGURIDAD: `--tenant-id` NO tiene autenticación ni autorización propia --
quien corre este script ya tiene acceso al entorno de producción (env
vars reales cargadas), mismo nivel de confianza que leer el `.env`
directamente. Es un script de operador, pensado para correrse a mano o
desde el runbook de despliegue (ver docs/despliegue-coolify.md), NUNCA
para envolverse en un endpoint HTTP o un webhook de CI/CD accesible
remotamente sin agregarle autenticación explícita primero (hallazgo del
auditor-multitenant, ver ADR-0035).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def _run(tenant_id: uuid.UUID | None) -> int:
    from core.config import get_settings
    from core.gateway import meta_production_readiness as readiness

    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"

    checks = []
    checks.append(await readiness.check_webhook_handshake(callback_url, settings.whatsapp_verify_token))
    checks.append(await readiness.check_app_subscriptions_complete(settings.meta_app_id))
    checks.append(readiness.check_prod_secret_differs_from_dev(settings.meta_app_secret))
    checks.append(readiness.check_token_keys_configured())
    checks.append(await readiness.check_worker_heartbeat_and_dlq())
    checks.append(await readiness.check_rls_forced())
    checks.append(readiness.check_rls_tests_pass())
    checks.append(readiness.check_no_secret_leak_in_templates())
    if tenant_id is not None:
        checks.append(await readiness.check_connections_health_except_billing(tenant_id))
    else:
        print("!  connections_health: omitido (pasá --tenant-id para incluirlo)")

    any_failed = False
    for check in checks:
        if check.ok is True:
            symbol = "OK"
        elif check.ok is None:
            symbol = "? "
        else:
            symbol = "X "
            any_failed = True
        print(f"[{symbol}] {check.key}: {check.detail}")

    return 1 if any_failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=str, default=None, help="Tenant para el check de salud de conexiones (nº9)")
    args = parser.parse_args()

    tenant_id = uuid.UUID(args.tenant_id) if args.tenant_id else None
    exit_code = asyncio.run(_run(tenant_id))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

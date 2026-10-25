"""Cuentas de transferencia del negocio para el resto de packs (HU-SUB-24, adenda de ADR-0067).

`transfer_accounts` vive en la configuración de este pack; se ofrece al core como
`TransferDetailsProvider` para que suscripciones las use en sus avisos sin importar este pack
(invariante nº5).
"""

from __future__ import annotations

from typing import Any

PLUGIN_ID = "pack_commerce"


class CuentasDeTransferenciaDelNegocio:
    plugin_id = PLUGIN_ID

    async def cuentas_de_transferencia(self) -> list[dict[str, Any]]:
        from pack_commerce.skills import _plugin_config

        config = await _plugin_config()
        cuentas = getattr(config, "transfer_accounts", None) or []
        return [dict(c) for c in cuentas if isinstance(c, dict)]

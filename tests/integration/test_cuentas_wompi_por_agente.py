"""HU-AOS-62 · cuentas Wompi del negocio y pagos en línea por agente (ADR-0190 §6-§7, pack_payments_0004).

Sin red: el link no se pide a Wompi; se prueban la resolución de la cuenta, el cifrado, el borrado y el webhook por
cuenta con un payload firmado a mano. Criterios cubiertos:
- **Dos cuentas, dos agentes:** cada agente cobra con la llave pública de la cuenta que eligió.
- **Secretos cifrados:** la llave privada y el secreto de eventos no quedan en claro en la base.
- **Rotar llaves:** el agente cobra con la llave nueva sin tocar su instalación.
- **Cuenta en uso:** no se borra mientras un agente la use.
- **Webhook por cuenta:** firma de otra cuenta → `firma_invalida`; link de otra cuenta → no se confirma; firma y
  cuenta correctas → `aplicado`.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PAGOS = "pack_payments"


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.sync_registry()


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("wompi")
    tid, _ = await onboard_tenant(name=f"W {slug}", slug=slug, packs=[], sembrar_ejemplos=False, plan="premium")
    return tid


async def _agente_con_cuenta(tid: uuid.UUID, cuenta_id: uuid.UUID) -> uuid.UUID:
    from core.agents.instalaciones_agente import guardar_config_del_agente, instalar_en_agente

    aid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agents (id, tenant_id, key, kind, name) values (:a, :t, :k, 'specialist', 'Ventas')",
        a=aid, t=tid, k=f"ventas_{uuid.uuid4().hex[:8]}",
    )
    await instalar_en_agente(tid, aid, PAGOS, requiere_config=True)
    await guardar_config_del_agente(tid, aid, PAGOS, {"cuenta_id": str(cuenta_id)})
    return aid


async def _cuenta(nombre: str, *, secreto: str = "ev_secreto") -> Any:
    from pack_payments.cuentas import crear_cuenta

    return await crear_cuenta(
        nombre=nombre, public_key=f"pub_test_{nombre}", private_key=f"prv_test_{nombre}", events_secret=secreto
    )


def _evento(link_id: str, *, secreto: str, centavos: int = 5_000_000) -> dict[str, Any]:
    data = {"transaction": {"id": "tx-1", "status": "APPROVED", "amount_in_cents": centavos,
                            "payment_link_id": link_id, "reference": "OC-1"}}
    props = ["transaction.id", "transaction.status", "transaction.amount_in_cents"]
    timestamp = 1_700_000_000
    concatenado = "tx-1" + "APPROVED" + str(centavos) + str(timestamp) + secreto
    return {"event": "transaction.updated", "data": data, "timestamp": timestamp,
            "signature": {"properties": props, "checksum": hashlib.sha256(concatenado.encode()).hexdigest()}}


async def test_cada_agente_cobra_con_su_cuenta_y_se_rota() -> None:
    from pack_payments.cuentas import actualizar_cuenta, config_de_cobro

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        uno, dos = await _cuenta("uno"), await _cuenta("dos")
        agente_uno, agente_dos = await _agente_con_cuenta(tid, uno.id), await _agente_con_cuenta(tid, dos.id)
        config_uno, cuenta_uno = await config_de_cobro(agent_id=agente_uno)
        config_dos, _ = await config_de_cobro(agent_id=agente_dos)
        assert (config_uno.public_key, cuenta_uno) == ("pub_test_uno", uno.id)
        assert config_dos.public_key == "pub_test_dos"
        assert config_uno.private_key.get_secret_value() == "prv_test_uno"

        await actualizar_cuenta(uno.id, public_key="pub_test_rotada", private_key="prv_test_rotada")
        rotada, _ = await config_de_cobro(agent_id=agente_uno)
    assert (rotada.public_key, rotada.private_key.get_secret_value()) == ("pub_test_rotada", "prv_test_rotada")
    assert rotada.events_secret.get_secret_value() == "ev_secreto"  # el que no se mandó se conserva
    ((cifrado,),) = await _sql(tid, "select secretos_cifrados from pack_payments_accounts where id = :i", i=uno.id)
    assert cifrado.startswith("gcm:v1:") and "prv_test" not in cifrado and "ev_secreto" not in cifrado


async def test_cuenta_en_uso_no_se_borra() -> None:
    from pack_payments.cuentas import CuentaEnUso, borrar_cuenta, listar_cuentas

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        usada, libre = await _cuenta("usada"), await _cuenta("libre")
        agente = await _agente_con_cuenta(tid, usada.id)
        with pytest.raises(CuentaEnUso) as exc:
            await borrar_cuenta(usada.id)
        assert exc.value.agentes == [agente]
        await borrar_cuenta(libre.id)
        assert [c.nombre for c in await listar_cuentas()] == ["usada"]


async def test_webhook_por_cuenta() -> None:
    from pack_payments.module import FIRMA_INVALIDA, procesar_evento_de_wompi

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        cuenta_a, cuenta_b = await _cuenta("a", secreto="sec_a"), await _cuenta("b", secreto="sec_b")
    link_a = f"link-{uuid.uuid4().hex[:8]}"
    await _sql(
        tid,
        "insert into pack_payments_transactions (id, tenant_id, reference, provider, provider_reference, status, "
        "amount, currency, idempotency_key, cuenta_id) values (:i, :t, 'OC-1', 'wompi', :l, 'pendiente', 50000, "
        "'COP', :k, :c)",
        i=uuid.uuid4(), t=tid, l=link_a, k=uuid.uuid4().hex, c=cuenta_a.id,
    )
    async with tenant_scope(tid):
        # Firmado con el secreto de B pero entregado a la URL de A: no pasa la firma.
        assert await procesar_evento_de_wompi(_evento(link_a, secreto="sec_b"), cuenta_id=cuenta_a.id) == FIRMA_INVALIDA
        # Bien firmado por B, pero el link es de A: no se confirma.
        assert await procesar_evento_de_wompi(_evento(link_a, secreto="sec_b"), cuenta_id=cuenta_b.id) == "sin_transaccion"
        assert await procesar_evento_de_wompi(_evento(link_a, secreto="sec_a"), cuenta_id=cuenta_a.id) == "aplicado"
    ((estado,),) = await _sql(tid, "select status from pack_payments_transactions where provider_reference = :l", l=link_a)
    assert estado == "aprobado"

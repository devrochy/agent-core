"""HU-CFG-05 (SPEC-37): palabras de baja (dueño) y política de WhatsApp Web (solo master)."""

from __future__ import annotations

import os

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/settings"
PASSWORD = "Colab-Cfg05-2026!"
POLITICA = {
    "mode": "reply_only",
    "send_min_delay_ms": 2000,
    "send_max_delay_ms": 5000,
    "daily_outbound_cap": 30,
    "warmup_enabled": False,
}


async def _negocio():
    from core.alta_negocio import onboard_tenant

    slug = uniq("cfg05")
    tid, _ = await onboard_tenant(name=f"N {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local")
    return tid


async def _dueno(tid) -> dict[str, str]:
    """Colaborador con escritura en Configuración (no es master)."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, section="settings", access="write", tenant_id=tid))
    return email


async def _auditoria(tid, accion: str) -> list:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == accion))).scalars()
        )


async def test_palabras_de_baja_se_guardan_limpias_y_el_cliente_se_da_de_baja(app_client) -> None:
    from core.admin.envios_segmento import es_palabra_de_baja

    tid = await _negocio()
    email = await _dueno(tid)
    h = await api_headers(app_client, tid, email=email, password=PASSWORD)

    inicial = (await app_client.get(f"{BASE}/bajas", headers=h)).json()
    assert inicial["usa_por_defecto"] is True and inicial["palabras"] == [] and "baja" in inicial["por_defecto"]

    r = await app_client.put(f"{BASE}/bajas", headers=h, json={"palabras": ["baja", "STOP", "stop", "", "no más"]})
    assert r.status_code == 200, r.text
    assert r.json()["palabras"] == ["baja", "STOP", "no más"] and r.json()["usa_por_defecto"] is False
    assert es_palabra_de_baja("Stop!", r.json()["palabras"])
    assert not es_palabra_de_baja("baja el precio", r.json()["palabras"])
    assert len(await _auditoria(tid, "settings.palabras_de_baja")) == 1

    # Lista vacía = restablecer las de por defecto.
    r = await app_client.put(f"{BASE}/bajas", headers=h, json={"palabras": []})
    assert r.status_code == 200 and r.json()["usa_por_defecto"] is True

    r = await app_client.put(f"{BASE}/bajas", headers=h, json={"palabras": ["x" * 61]})
    assert r.status_code == 422 and "palabras" in r.json()["error"]["fields"]


async def test_el_duenio_no_ve_ni_edita_la_politica_de_whatsapp_web(app_client) -> None:
    tid = await _negocio()
    email = await _dueno(tid)
    h = await api_headers(app_client, tid, email=email, password=PASSWORD)
    assert (await app_client.get(f"{BASE}/whatsapp-web", headers=h)).status_code == 403
    assert (await app_client.put(f"{BASE}/whatsapp-web", headers=h, json=POLITICA)).status_code == 403


async def test_master_edita_la_politica_con_rangos_coherencia_y_aviso_de_riesgo(app_client) -> None:
    from core.waweb.policy import RISK_ACK_VERSION, WaWebPolicy, load_policy

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    actual = (await app_client.get(f"{BASE}/whatsapp-web", headers=h)).json()
    assert actual["mode"] == "reply_only" and actual["risk_version"] == RISK_ACK_VERSION

    r = await app_client.put(f"{BASE}/whatsapp-web", headers=h, json=POLITICA)
    assert r.status_code == 200, r.text
    assert (r.json()["send_min_delay_ms"], r.json()["daily_outbound_cap"], r.json()["warmup_enabled"]) == (2000, 30, False)

    # Retardo máximo menor que el mínimo: 422 con el campo, sin igualarlos en silencio.
    r = await app_client.put(f"{BASE}/whatsapp-web", headers=h, json={**POLITICA, "send_min_delay_ms": 6000})
    assert r.status_code == 422 and "send_max_delay_ms" in r.json()["error"]["fields"]
    r = await app_client.put(f"{BASE}/whatsapp-web", headers=h, json={**POLITICA, "daily_outbound_cap": 10_001})
    assert r.status_code == 422 and "daily_outbound_cap" in r.json()["error"]["fields"]

    # Pasar a allow_outbound exige aceptar el aviso vigente y queda auditado.
    abierto = {**POLITICA, "mode": "allow_outbound"}
    r = await app_client.put(f"{BASE}/whatsapp-web", headers=h, json=abierto)
    assert r.status_code == 422 and "acepta_riesgo" in r.json()["error"]["fields"]
    assert (await app_client.get(f"{BASE}/whatsapp-web", headers=h)).json()["mode"] == "reply_only"
    r = await app_client.put(f"{BASE}/whatsapp-web", headers=h, json={**abierto, "acepta_riesgo": True})
    assert r.status_code == 200 and r.json()["mode"] == "allow_outbound"
    filas = await _auditoria(tid, "settings.waweb_allow_outbound")
    assert len(filas) == 1
    assert filas[0].actor.startswith("admin:") and filas[0].input["risk_version"] == RISK_ACK_VERSION
    assert filas[0].input["antes"]["mode"] == "reply_only" and filas[0].input["despues"]["mode"] == "allow_outbound"

    # El conector del negocio lee exactamente esa política.
    politica = await load_policy(tid)
    assert isinstance(politica, WaWebPolicy) and politica.allows_outbound and politica.daily_outbound_cap == 30


async def test_la_politica_de_un_negocio_no_cambia_la_del_otro(app_client) -> None:
    from core.waweb.policy import load_policy

    a, b = await _negocio(), await _negocio()
    r = await app_client.put(f"{BASE}/whatsapp-web", headers=await api_headers(app_client, a), json=POLITICA)
    assert r.status_code == 200, r.text
    assert (await load_policy(a)).daily_outbound_cap == 30
    assert (await load_policy(b)).daily_outbound_cap == 50

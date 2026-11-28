"""HU-FB-04 · revisión master de reportes de clientes contra el stack real (ADR-0213 §6-8).

- aprobar acredita un abono `bonificacion` con `origen = feedback:<id>`, notifica al autor y audita;
- dos aprobaciones a la vez: un solo abono y un 409; el doble clic del mismo revisor: 200 sin otro abono;
- aprobar un rechazado: 409; topes por reporte y por mes: 422; sin ciclo: 409 y el reporte sigue abierto;
- tomar: el mismo master repite con 200, otro recibe 409;
- el tester ve la bandeja y el detalle pero no decide (403);
- el master opera sobre un negocio que no es el activo de su sesión, sin tocar a los demás;
- si la notificación falla, la aprobación se confirma igual;
- `GET /admin/plan` del negocio muestra el regalo en `bonuses`.

Los reportes se insertan directo bajo `tenant_scope` (el envío del cliente se prueba en HU-FB-03).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # decisiones entre negocios y abonos por negocio (ADR-0213)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/platform/feedback/reports"
PASSWORD_TESTER = "clave-del-tester-123"


@pytest.fixture(autouse=True)
def _feedback_on(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "feedback_enabled", True)
    monkeypatch.setattr(get_settings(), "feedback_creditos_max_reporte", 5000)
    monkeypatch.setattr(get_settings(), "feedback_creditos_max_mes_negocio", 10000)


@asynccontextmanager
async def _scope(tid: uuid.UUID) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


async def _negocio(*, con_ciclo: bool = True) -> uuid.UUID:
    from _habilidades import onboard_tenant

    from core.db.models import TenantSubscription

    slug = f"fb-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Reportes {slug}", slug=slug, sembrar_ejemplos=False, plan="free")
    inicio = datetime.now(UTC) - timedelta(days=3)
    async with _scope(tid) as s:
        await s.execute(
            TenantSubscription.__table__.update()
            .where(TenantSubscription.tenant_id == tid)
            .values(
                plan_id="basic",
                status="active",
                period_start=inicio,
                period_end=inicio + timedelta(days=60),
                dia_ancla=inicio.day,
                trial_ends_at=None,
                # `period_start` es NOT NULL: «sin ciclo» es no tener suscripción viva.
                deleted_at=None if con_ciclo else datetime.now(UTC),
            )
        )
    return tid


async def _admin_id(email: str = "admin@local") -> uuid.UUID:
    from sqlalchemy import select

    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as s:
        return (await s.execute(select(Admin.id).where(Admin.email == email))).scalar_one()


async def _reporte(tid: uuid.UUID, autor: uuid.UUID, *, tipo: str = "error") -> uuid.UUID:
    rid = uuid.uuid4()
    async with _scope(tid) as s:
        await s.execute(
            text(
                "INSERT INTO feedback_reports (id, tenant_id, autor_admin_id, tipo, area, titulo, descripcion, "
                "idempotency_key) VALUES (:id, :t, :a, :tipo, 'otra', 'No carga el resumen', "
                "'Al abrir el resumen se queda cargando para siempre.', :k)"
            ),
            {"id": rid, "t": tid, "a": autor, "tipo": tipo, "k": uuid.uuid4().hex},
        )
    return rid


async def _bonificaciones(tid: uuid.UUID) -> list[tuple[Any, ...]]:
    async with _scope(tid) as s:
        return [
            tuple(f)
            for f in (
                await s.execute(
                    text("SELECT origen, creditos FROM credit_grants WHERE tipo = 'bonificacion' ORDER BY origen")
                )
            ).all()
        ]


async def _estado(tid: uuid.UUID, rid: uuid.UUID) -> str:
    async with _scope(tid) as s:
        return (await s.execute(text("SELECT estado FROM feedback_reports WHERE id = :id"), {"id": rid})).scalar_one()


async def _notificaciones(tid: uuid.UUID, rid: uuid.UUID) -> list[tuple[Any, ...]]:
    async with _scope(tid) as s:
        return [
            tuple(f)
            for f in (
                await s.execute(
                    text("SELECT event_key, audience_admin_id FROM notifications WHERE data->>'reportId' = :r"),
                    {"r": str(rid)},
                )
            ).all()
        ]


async def _auditoria(tid: uuid.UUID, accion: str) -> int:
    async with _scope(tid) as s:
        return (
            await s.execute(text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": accion})
        ).scalar_one()


async def _master(app_client, activo: uuid.UUID) -> dict[str, str]:
    return await api_headers(app_client, activo)


# ------------------------------------------------------------------------------ aprobar
async def test_aprobar_acredita_notifica_y_audita_y_el_plan_muestra_el_regalo(app_client, tenant_setup) -> None:
    t1, _t2 = tenant_setup
    tid = await _negocio()
    autor = await _admin_id()
    rid = await _reporte(tid, autor)
    # Sesión del master con OTRO negocio activo: el reporte se decide en el suyo, no en el de la sesión.
    master = await _master(app_client, t1)

    r = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 3000}, headers=master)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["estado"], body["creditos_otorgados"], body["tenant_id"]) == ("aprobado", 3000, str(tid))

    assert await _bonificaciones(tid) == [(f"feedback:{rid}", 3000)]
    assert await _bonificaciones(t1) == []  # nada en el negocio activo del master
    assert await _notificaciones(tid, rid) == [("core.feedback_approved", autor)]
    assert await _auditoria(tid, "feedback.aprobado") == 1

    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    assert r.json()["credit"]["balance"]["bonuses"] == 3000.0


async def test_doble_clic_del_mismo_revisor_responde_200_sin_otro_abono(app_client, tenant_setup) -> None:
    t1, _ = tenant_setup
    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    master = await _master(app_client, t1)

    primero = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 2000}, headers=master)
    segundo = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 2000}, headers=master)
    assert (primero.status_code, segundo.status_code) == (200, 200), segundo.text
    # Con otro monto ya no es la misma decisión.
    otro = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 2500}, headers=master)
    assert otro.status_code == 409 and otro.json()["error"]["code"] == "reporte_ya_decidido"
    assert await _bonificaciones(tid) == [(f"feedback:{rid}", 2000)]
    assert len(await _notificaciones(tid, rid)) == 1


async def test_dos_aprobaciones_concurrentes_dan_un_solo_abono_y_un_409(app_client, tenant_setup) -> None:
    from core.feedback import revision

    t1, _ = tenant_setup
    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    # Dos masters distintos a la vez (el segundo no es el mismo revisor: no hay 200 idempotente).
    a, b = uuid.uuid4(), uuid.uuid4()
    resultados = await asyncio.gather(
        revision.aprobar(rid, revisor_id=a, revisor_email="a@test", creditos=1000),
        revision.aprobar(rid, revisor_id=b, revisor_email="b@test", creditos=1000),
        return_exceptions=True,
    )
    ok = [x for x in resultados if isinstance(x, revision.Resultado)]
    errores = [x for x in resultados if isinstance(x, revision.ErrorDeRevision)]
    assert len(ok) == 1 and not ok[0].repetido
    assert [(e.status, e.code) for e in errores] == [(409, "reporte_ya_decidido")]
    assert await _bonificaciones(tid) == [(f"feedback:{rid}", 1000)]
    _ = t1


async def test_aprobar_un_rechazado_es_409(app_client, tenant_setup) -> None:
    t1, _ = tenant_setup
    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    master = await _master(app_client, t1)
    r = await app_client.post(
        f"{BASE}/{rid}/rechazar", json={"motivo_codigo": "no_reproducible", "motivo": "No lo pudimos ver."}, headers=master
    )
    assert r.status_code == 200, r.text
    r = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 1000}, headers=master)
    assert r.status_code == 409 and r.json()["error"]["code"] == "reporte_ya_decidido"
    assert await _bonificaciones(tid) == []
    assert await _notificaciones(tid, rid) == [("core.feedback_rejected", await _admin_id())]


async def test_topes_por_reporte_y_por_mes_dan_422_sin_acreditar(app_client, tenant_setup) -> None:
    t1, _ = tenant_setup
    tid = await _negocio()
    autor = await _admin_id()
    master = await _master(app_client, t1)

    r = await app_client.post(f"{BASE}/{await _reporte(tid, autor)}/aprobar", json={"creditos": 8000}, headers=master)
    assert r.status_code == 422 and r.json()["error"]["code"] == "tope_reporte_excedido"

    # 5.000 + 4.000 = 9.000 en el ciclo; uno más de 2.000 pasa el tope de 10.000.
    r1 = await app_client.post(f"{BASE}/{await _reporte(tid, autor)}/aprobar", json={"creditos": 5000}, headers=master)
    r2 = await app_client.post(f"{BASE}/{await _reporte(tid, autor)}/aprobar", json={"creditos": 4000}, headers=master)
    assert (r1.status_code, r2.status_code) == (200, 200)
    tercero = await _reporte(tid, autor)
    r = await app_client.post(f"{BASE}/{tercero}/aprobar", json={"creditos": 2000}, headers=master)
    assert r.status_code == 422 and r.json()["error"]["code"] == "tope_mensual_excedido"
    assert r.json()["error"]["details"]["disponible"] == 1000
    assert await _estado(tid, tercero) == "recibido"
    assert sum(c for _o, c in await _bonificaciones(tid)) == 9000

    for malo in (0, -5, "100", 1.5):
        r = await app_client.post(f"{BASE}/{tercero}/aprobar", json={"creditos": malo}, headers=master)
        assert r.status_code == 422, malo


async def test_sin_ciclo_de_creditos_es_409_y_el_reporte_sigue_abierto(app_client, tenant_setup) -> None:
    t1, _ = tenant_setup
    tid = await _negocio(con_ciclo=False)
    rid = await _reporte(tid, await _admin_id())
    r = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 1000}, headers=await _master(app_client, t1))
    assert r.status_code == 409 and r.json()["error"]["code"] == "sin_ciclo_de_creditos"
    assert await _estado(tid, rid) == "recibido"


async def test_si_falla_la_notificacion_la_aprobacion_se_confirma(app_client, tenant_setup, monkeypatch) -> None:
    from core.notifications import catalog

    def _rota(*_a, **_k):
        return catalog.NotificationSpec(event_key="", category="x", severity="x", title="", dedupe_key="")

    monkeypatch.setattr(catalog, "feedback_aprobado", _rota)  # `notify` la valida, falla y no lanza
    t1, _ = tenant_setup
    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    r = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 1000}, headers=await _master(app_client, t1))
    assert r.status_code == 200, r.text
    assert await _estado(tid, rid) == "aprobado"
    assert await _bonificaciones(tid) == [(f"feedback:{rid}", 1000)]
    assert await _notificaciones(tid, rid) == []


async def test_abono_que_no_entra_deshace_la_aprobacion(app_client, tenant_setup, monkeypatch) -> None:
    import core.billing.bonificacion as bonificacion

    async def _ya(*_a, **_k):
        return "ya_acreditada"

    alertas: list[str] = []

    async def _alerta(title, detail, severity="warning"):
        alertas.append(severity)

    monkeypatch.setattr(bonificacion, "acreditar_bonificacion", _ya)
    monkeypatch.setattr("core.shared.alerts.send_alert", _alerta)
    t1, _ = tenant_setup
    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    r = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 1000}, headers=await _master(app_client, t1))
    assert r.status_code == 500 and r.json()["error"]["code"] == "abono_inconsistente"
    assert await _estado(tid, rid) == "recibido"
    assert alertas == ["critical"]


# ------------------------------------------------------------------------- tomar y rechazar
async def test_tomar_repetido_por_el_mismo_200_y_por_otro_409(app_client, tenant_setup) -> None:
    from core.feedback import revision

    t1, _ = tenant_setup
    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    master = await _master(app_client, t1)
    for _ in range(2):
        r = await app_client.post(f"{BASE}/{rid}/tomar", headers=master)
        assert r.status_code == 200 and r.json()["estado"] == "en_revision", r.text
    with pytest.raises(revision.ErrorDeRevision) as exc:
        await revision.tomar(rid, revisor_id=uuid.uuid4(), revisor_email="otro@test")
    assert (exc.value.status, exc.value.code) == (409, "reporte_ya_tomado")


async def test_rechazar_como_duplicado_de_otro_negocio(app_client, tenant_setup) -> None:
    t1, _ = tenant_setup
    autor = await _admin_id()
    otro_negocio, tid = await _negocio(), await _negocio()
    original = await _reporte(otro_negocio, autor)
    rid = await _reporte(tid, autor)
    master = await _master(app_client, t1)

    r = await app_client.post(
        f"{BASE}/{rid}/rechazar", json={"motivo_codigo": "duplicado", "motivo": "Ya estaba reportado."}, headers=master
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicado_sin_original"
    r = await app_client.post(
        f"{BASE}/{rid}/rechazar",
        json={"motivo_codigo": "duplicado", "motivo": "Ya estaba reportado.", "duplicado_de": str(uuid.uuid4())},
        headers=master,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicado_invalido"
    r = await app_client.post(
        f"{BASE}/{rid}/rechazar",
        json={
            "motivo_codigo": "duplicado",
            "motivo": "Ya estaba reportado.",
            "nota_interna": "Mismo fallo que el de otro negocio.",
            "duplicado_de": str(original),
        },
        headers=master,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["duplicado_de"], r.json()["nota_interna"]) == (str(original), "Mismo fallo que el de otro negocio.")
    assert await _estado(otro_negocio, original) == "recibido"  # el original no se toca
    assert await _auditoria(tid, "feedback.rechazado") == 1


# --------------------------------------------------------------------------- bandeja y roles
async def test_bandeja_entre_negocios_con_filtros_y_cursor(app_client, tenant_setup) -> None:
    t1, _ = tenant_setup
    autor = await _admin_id()
    a, b = await _negocio(), await _negocio()
    ids_a = [await _reporte(a, autor, tipo="mejora") for _ in range(3)]
    id_b = await _reporte(b, autor, tipo="idea")
    master = await _master(app_client, t1)

    r = await app_client.get(BASE, params={"tenant_id": str(a), "limit": 2}, headers=master)
    assert r.status_code == 200, r.text
    pagina = r.json()
    assert [i["id"] for i in pagina["items"]] == [str(x) for x in reversed(ids_a)][:2]
    assert pagina["items"][0]["titulo"] == "No carga el resumen" and pagina["items"][0]["tenant_id"] == str(a)
    r = await app_client.get(
        BASE, params={"tenant_id": str(a), "limit": 2, "cursor": pagina["next_cursor"]}, headers=master
    )
    assert [i["id"] for i in r.json()["items"]] == [str(ids_a[0])] and r.json()["next_cursor"] is None

    r = await app_client.get(BASE, params={"tipo": "idea", "tenant_id": str(b)}, headers=master)
    assert [i["id"] for i in r.json()["items"]] == [str(id_b)]

    r = await app_client.get(f"{BASE}/{id_b}", headers=master)
    assert r.status_code == 200 and r.json()["tenant_id"] == str(b) and r.json()["adjuntos"] == []
    assert (await app_client.get(f"{BASE}/{uuid.uuid4()}", headers=master)).status_code == 404


async def test_tester_ve_la_bandeja_pero_no_decide(app_client, tenant_setup, monkeypatch) -> None:
    import core.gateway.system_email as system_email
    from core.config import get_settings

    async def _sin_correo(*_a, **_k):
        return None

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)
    monkeypatch.setattr(system_email, "send_system_email", _sin_correo)
    t1, _ = tenant_setup
    master = await _master(app_client, t1)
    email = f"tester-fb-{uuid.uuid4().hex[:6]}@example.test"
    r = await app_client.post("/api/v1/admin/qa/testers", json={"email": email}, headers=master)
    assert r.status_code == 201, r.text
    token = r.json()["enlace"].split("#", 1)[1]
    r = await app_client.post(
        "/api/v1/auth/accept-tester-invitation", json={"token": token, "new_password": PASSWORD_TESTER}
    )
    assert r.status_code == 200, r.text
    tester = await api_headers(app_client, t1, email=email, password=PASSWORD_TESTER)

    tid = await _negocio()
    rid = await _reporte(tid, await _admin_id())
    assert (await app_client.get(BASE, headers=tester)).status_code == 200
    assert (await app_client.get(f"{BASE}/{rid}", headers=tester)).status_code == 200
    for ruta, cuerpo in (
        ("tomar", None),
        ("aprobar", {"creditos": 1000}),
        ("rechazar", {"motivo_codigo": "otro", "motivo": "No aplica."}),
    ):
        r = await app_client.post(f"{BASE}/{rid}/{ruta}", json=cuerpo, headers=tester)
        assert r.status_code == 403, (ruta, r.text)
    assert await _estado(tid, rid) == "recibido"


async def _dueno(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"dueno-fb-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD_TESTER), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return email


async def test_un_dueno_no_entra_a_la_bandeja(app_client, tenant_setup) -> None:
    tid = await _negocio()
    dueno = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD_TESTER)
    assert (await app_client.get(BASE, headers=dueno)).status_code == 403
    rid = await _reporte(tid, await _admin_id())
    r = await app_client.post(f"{BASE}/{rid}/aprobar", json={"creditos": 1000}, headers=dueno)
    assert r.status_code == 403
    _ = tenant_setup


async def test_bandera_apagada_404(app_client, tenant_setup, monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "feedback_enabled", False)
    t1, _ = tenant_setup
    assert (await app_client.get(BASE, headers=await _master(app_client, t1))).status_code == 404

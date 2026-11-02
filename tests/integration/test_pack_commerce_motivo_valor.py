"""HU-CRM-23 · Motivo de pérdida y valor estimado del lead.

Con la BD real: perder un lead pide motivo (a una persona desde la ficha y al agente con la tool); reabrirlo
limpia el motivo vigente pero la historia lo conserva; el valor estimado se valida y se agrupa en tramos; la
lista filtra por motivo y por tramo y ordena por valor con los leads sin valor siempre al final; un
colaborador de solo lectura lo ve sin editarlo; los CHECK de la migración protegen los datos; nada cruza negocios.
"""

from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy.exc import IntegrityError

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/leads"
PACK = "pack_commerce"
MOTIVOS = ["precio", "sin_respuesta", "compro_en_otro_lado", "no_era_para_el", "otro"]


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("mot")
    tid, _ = await onboard_tenant(
        name=f"Motivo {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str = "Ana", **lead) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """(cliente, conversación, lead)."""
    from pack_commerce.models import Lead

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            c = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre)
            session.add(c)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=c.id)
            session.add(conv)
            la = Lead(tenant_id=tid, customer_id=c.id, conversation_id=conv.id, **lead)
            session.add(la)
            await session.flush()
            return c.id, conv.id, la.id


async def _lead(tid: uuid.UUID, lead_id: uuid.UUID):
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await session.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()


async def _cambios(tid: uuid.UUID, lead_id: uuid.UUID) -> list[tuple[str, str, str | None, str | None]]:
    from pack_commerce.models import LeadStageChange
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(
                        LeadStageChange.from_stage, LeadStageChange.to_stage,
                        LeadStageChange.loss_reason, LeadStageChange.loss_note,
                    )
                    .where(LeadStageChange.lead_id == lead_id)
                    .order_by(LeadStageChange.changed_at, LeadStageChange.id)
                )
            ).all()
    return [tuple(f) for f in filas]


def _patch(app_client, cliente, headers, **valores):
    return app_client.patch(f"{URL}/{cliente}/pack/{PACK}", json={"valores": valores}, headers=headers)


def _editables(r) -> dict:
    return {e["key"]: e for e in r.json()["editables"] if e["plugin_id"] == PACK}


async def _tool(tid: uuid.UUID, args: dict, cliente: uuid.UUID, conv: uuid.UUID):
    from pack_commerce.skills import LeadSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(conversation_id=conv, customer_id=cliente, idempotency_key=uniq("k"), channel="whatsapp")
    async with tenant_scope(tid):
        return await LeadSkill().actualizar_lead(args, ctx)


# ---------------------------------------------------------------- motivo al perder (persona)
async def test_perder_un_lead_pide_motivo_y_sin_motivo_no_cambia_nada(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="contactado")
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, estado_lead="perdido")
    assert r.status_code == 400, r.text
    mensaje = r.json()["error"]["message"]
    assert "loss_reason" not in mensaje  # a una persona se le habla en los nombres de la pantalla
    for etiqueta in ["Precio", "Sin respuesta", "Compró en otro lado", "No era para él", "Otro"]:
        assert etiqueta in mensaje
    assert (await _lead(tid, lead)).status == "contactado"
    assert await _cambios(tid, lead) == []

    # Un motivo que no es del catálogo lo rechaza el core por campo.
    r = await _patch(app_client, cliente, headers, estado_lead="perdido", motivo_perdida="porque sí")
    assert r.status_code == 400 and "motivo_perdida" in r.json()["error"]["fields"]
    assert (await _lead(tid, lead)).status == "contactado"


async def test_perder_con_motivo_y_nota_queda_en_la_ficha_la_lista_y_la_historia(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="contactado")
    headers = await api_headers(app_client, tid)

    r = await _patch(
        app_client, cliente, headers, estado_lead="perdido", motivo_perdida="precio", nota_perdida="Lo vio caro"
    )
    assert r.status_code == 200, r.text
    ed = _editables(r)
    assert ed["motivo_perdida"]["tipo"] == "opciones" and ed["motivo_perdida"]["value"] == "precio"
    assert ed["nota_perdida"]["value"] == "Lo vio caro"
    assert [o["value"] for o in ed["motivo_perdida"]["options"]] == MOTIVOS
    assert r.json()["extras"]["motivo_perdida"] == "Precio"
    assert r.json()["extras"]["motivo_perdida_codigo"] == "precio"

    la = await _lead(tid, lead)
    assert (la.status, la.loss_reason, la.loss_note) == ("perdido", "precio", "Lo vio caro")
    assert await _cambios(tid, lead) == [("contactado", "perdido", "precio", "Lo vio caro")]

    lista = await app_client.get(URL, headers=headers)
    fila = next(i for i in lista.json()["items"] if i["id"] == str(cliente))
    assert fila["extras"]["motivo_perdida"] == "Precio"
    assert any(c["key"] == "motivo_perdida" for c in lista.json()["columns"])
    assert any(f["key"] == "motivo_perdida" for f in lista.json()["filters"])

    eventos = (await app_client.get(f"{URL}/{cliente}/timeline", headers=headers)).json()["items"]
    etapa = next(e for e in eventos if e["kind"] == "etapa")
    assert etapa["detail"] == "Motivo: Precio — Lo vio caro · Lo hizo una persona"


async def test_la_nota_de_la_perdida_tiene_tope(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="contactado")
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, estado_lead="perdido", motivo_perdida="otro", nota_perdida="x" * 301)
    assert r.status_code == 400 and "nota_perdida" in r.json()["error"]["fields"]
    assert (await _lead(tid, lead)).status == "contactado"


# ---------------------------------------------------------------- agente
async def test_el_agente_no_pierde_un_lead_sin_motivo_valido(app_client) -> None:
    tid = await _negocio()
    cliente, conv, lead = await _cliente(tid, status="contactado", name="Ana", interest="tortas")

    r = await _tool(tid, {"status": "perdido", "name": "Otra", "interest": "pan"}, cliente, conv)
    assert "loss_reason" in r["error"]
    assert r["motivos_validos"] == MOTIVOS
    assert r["etapa_actual"] == "contactado"
    la = await _lead(tid, lead)
    assert (la.status, la.name, la.interest) == ("contactado", "Ana", "tortas")  # no guarda nada del llamado
    assert await _cambios(tid, lead) == []

    r = await _tool(tid, {"status": "perdido", "loss_reason": "descuento"}, cliente, conv)
    assert "descuento" in r["error"] and r["motivos_validos"] == MOTIVOS
    assert (await _lead(tid, lead)).status == "contactado"

    r = await _tool(tid, {"status": "perdido", "loss_reason": "sin_respuesta", "loss_note": "No contestó"}, cliente, conv)
    assert r["status"] == "actualizado"
    la = await _lead(tid, lead)
    assert (la.status, la.loss_reason, la.loss_note) == ("perdido", "sin_respuesta", "No contestó")
    assert await _cambios(tid, lead) == [("contactado", "perdido", "sin_respuesta", "No contestó")]


async def test_la_tool_ofrece_el_motivo_con_el_catalogo_cerrado() -> None:
    from pack_commerce.skills import LeadSkill

    tool = next(t for t in LeadSkill().get_tools() if t.name == "actualizar_lead")
    props = tool.parameters["properties"]
    assert props["loss_reason"]["enum"] == MOTIVOS
    assert "loss_reason" not in tool.parameters.get("required", [])  # los replays existentes no se rompen


# ---------------------------------------------------------------- reabrir
async def test_reabrir_deja_el_motivo_en_la_historia_y_no_como_vigente(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="contactado")
    headers = await api_headers(app_client, tid)
    await _patch(app_client, cliente, headers, estado_lead="perdido", motivo_perdida="precio", nota_perdida="Caro")

    r = await _patch(app_client, cliente, headers, estado_lead="contactado")
    assert r.status_code == 200, r.text
    la = await _lead(tid, lead)
    assert (la.status, la.loss_reason, la.loss_note) == ("contactado", None, None)
    assert r.json()["extras"]["motivo_perdida"] is None
    assert _editables(r)["motivo_perdida"]["value"] in (None, "")

    assert await _cambios(tid, lead) == [
        ("contactado", "perdido", "precio", "Caro"),
        ("perdido", "contactado", None, None),
    ]
    eventos = (await app_client.get(f"{URL}/{cliente}/timeline", headers=headers)).json()["items"]
    assert any("Motivo: Precio — Caro" in (e["detail"] or "") for e in eventos if e["kind"] == "etapa")

    # Y se puede volver a perder con otro motivo.
    r = await _patch(app_client, cliente, headers, estado_lead="perdido", motivo_perdida="otro")
    assert r.status_code == 200, r.text
    assert (await _lead(tid, lead)).loss_reason == "otro"


async def test_un_perdido_de_antes_se_muestra_sin_motivo_y_se_puede_anotar(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="perdido")  # como los de antes de la HU: sin motivo
    otro, _, _ = await _cliente(tid, nombre="Con motivo", status="perdido", loss_reason="precio")
    headers = await api_headers(app_client, tid)

    ficha = await app_client.get(f"{URL}/{cliente}", headers=headers)
    assert ficha.json()["extras"]["motivo_perdida"] == "Sin motivo registrado"
    assert ficha.json()["extras"]["motivo_perdida_codigo"] is None

    r = await app_client.get(URL, params={"filtro": "motivo_perdida:sin_motivo"}, headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [str(cliente)]

    r = await _patch(app_client, cliente, headers, motivo_perdida="compro_en_otro_lado")
    assert r.status_code == 200, r.text
    assert (await _lead(tid, lead)).loss_reason == "compro_en_otro_lado"
    assert await _cambios(tid, lead) == []  # no hubo cambio de etapa: no inventa historia

    # Marcar «perdido» otra vez con otro motivo también lo corrige.
    r = await _patch(app_client, cliente, headers, estado_lead="perdido", motivo_perdida="otro")
    assert r.status_code == 200, r.text
    assert (await _lead(tid, lead)).loss_reason == "otro"
    assert otro  # el otro lead no se tocó


async def test_el_motivo_solo_se_anota_en_un_lead_perdido(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="contactado")
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, motivo_perdida="precio")
    assert r.status_code == 400, r.text
    assert (await _lead(tid, lead)).loss_reason is None


# ---------------------------------------------------------------- valor estimado
async def test_valor_estimado_se_guarda_se_muestra_y_se_borra(app_client) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid)
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, valor_estimado="250000")
    assert r.status_code == 200, r.text
    ed = _editables(r)["valor_estimado"]
    assert ed["tipo"] == "numero" and ed["value"] == "250000"
    assert r.json()["extras"]["valor_estimado"] == 250000
    assert (await _lead(tid, lead)).estimated_value == Decimal("250000.00")

    r = await _patch(app_client, cliente, headers, valor_estimado="1500,50")  # coma decimal
    assert r.status_code == 200, r.text
    assert (await _lead(tid, lead)).estimated_value == Decimal("1500.50")
    assert _editables(r)["valor_estimado"]["value"] == "1500.5"

    lista = await app_client.get(URL, headers=headers)
    fila = next(i for i in lista.json()["items"] if i["id"] == str(cliente))
    assert fila["extras"]["valor_estimado"] == 1500.5
    assert any(c["key"] == "valor_estimado" for c in lista.json()["columns"])

    r = await _patch(app_client, cliente, headers, valor_estimado="")  # vacío lo quita
    assert r.status_code == 200, r.text
    assert (await _lead(tid, lead)).estimated_value is None
    assert r.json()["extras"]["valor_estimado"] is None


@pytest.mark.parametrize("malo", ["abc", "-5", "150.000", "12 000", "$500", "1e5", "99999999999"])
async def test_valor_invalido_se_rechaza_con_su_error_y_no_se_guarda(app_client, malo: str) -> None:
    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, estimated_value=Decimal("100"))
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, valor_estimado=malo)
    assert r.status_code == 400, r.text
    assert (await _lead(tid, lead)).estimated_value == Decimal("100.00")


async def test_valor_no_mueve_la_actividad_ni_la_etapa(app_client) -> None:
    from datetime import UTC, datetime, timedelta

    tid = await _negocio()
    antes = datetime.now(UTC) - timedelta(days=3)
    cliente, _, lead = await _cliente(tid, status="calificado", last_activity_at=antes)
    headers = await api_headers(app_client, tid)

    r = await _patch(app_client, cliente, headers, valor_estimado="1000")
    assert r.status_code == 200, r.text
    la = await _lead(tid, lead)
    assert (la.status, la.last_activity_at) == ("calificado", antes)  # ADR-0149
    assert await _cambios(tid, lead) == []


# ---------------------------------------------------------------- filtrar y ordenar
async def test_filtrar_por_motivo_y_por_tramo_de_valor_con_sus_conteos(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    # (nombre, estado, motivo, valor)
    filas = [
        ("precio1", "perdido", "precio", Decimal("50000")),
        ("precio2", "perdido", "precio", Decimal("100000")),  # el corte es semiabierto: va al tramo siguiente
        ("otro", "perdido", "otro", Decimal("600000")),
        ("sin motivo", "perdido", None, None),
        ("activo", "contactado", None, Decimal("3000000")),
        ("sin valor", "nuevo", None, None),
    ]
    ids = {}
    for nombre, estado, motivo, valor in filas:
        ids[nombre], _, _ = await _cliente(tid, nombre=nombre, status=estado, loss_reason=motivo, estimated_value=valor)

    async def con(filtro: str) -> set[str]:
        r = await app_client.get(URL, params={"filtro": filtro}, headers=headers)
        assert r.status_code == 200, r.text
        return {i["cliente"] for i in r.json()["items"]}

    assert await con("motivo_perdida:precio") == {"precio1", "precio2"}
    assert await con("motivo_perdida:otro") == {"otro"}
    assert await con("motivo_perdida:sin_motivo") == {"sin motivo"}
    assert await con("valor:hasta_100000") == {"precio1"}
    assert await con("valor:100000_500000") == {"precio2"}
    assert await con("valor:500000_2000000") == {"otro"}
    assert await con("valor:mas_de_2000000") == {"activo"}
    assert await con("valor:sin_valor") == {"sin motivo", "sin valor"}

    r = await app_client.get(URL, params={"facetas": ["motivo_perdida", "valor"]}, headers=headers)
    por_clave = {f["key"]: f for f in r.json()["facetas"]}
    # Cuenta las opciones del filtro; un lead que no está perdido no cae en ninguna.
    assert por_clave["motivo_perdida"]["counts"] == {
        "precio": 2, "sin_respuesta": 0, "compro_en_otro_lado": 0, "no_era_para_el": 0, "otro": 1, "sin_motivo": 1,
    }
    assert por_clave["valor"]["counts"] == {
        "hasta_100000": 1, "100000_500000": 1, "500000_2000000": 1, "mas_de_2000000": 1, "sin_valor": 2,
    }
    assert por_clave["valor"]["total"] == 6


async def test_ordenar_por_valor_deja_los_sin_valor_al_final_en_los_dos_sentidos(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    for nombre, valor in [("barato", Decimal("10")), ("caro", Decimal("900")), ("medio", Decimal("300")),
                          ("nada1", None), ("nada2", None)]:
        await _cliente(tid, nombre=nombre, estimated_value=valor)

    asc = (await app_client.get(URL, params={"orden": "valor"}, headers=headers)).json()["items"]
    desc = (await app_client.get(URL, params={"orden": "valor_desc"}, headers=headers)).json()["items"]
    assert [i["cliente"] for i in asc[:3]] == ["barato", "medio", "caro"]
    assert [i["cliente"] for i in desc[:3]] == ["caro", "medio", "barato"]
    for lista in (asc, desc):
        assert {i["cliente"] for i in lista[3:]} == {"nada1", "nada2"}  # sin valor, al final siempre


# ---------------------------------------------------------------- permisos y aislamiento
async def test_un_colaborador_de_solo_lectura_ve_motivo_y_valor_pero_no_los_edita(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    tid = await _negocio()
    cliente, _, lead = await _cliente(tid, status="perdido", loss_reason="precio", estimated_value=Decimal("700"))
    email, clave = f"lec-{uniq('x')}@mot.test", "Colab-12345!"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(clave), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, section="leads", access="read", tenant_id=tid))
    headers = await api_headers(app_client, tid, email=email, password=clave)

    ficha = await app_client.get(f"{URL}/{cliente}", headers=headers)
    assert ficha.status_code == 200, ficha.text
    assert ficha.json()["extras"]["motivo_perdida"] == "Precio"
    assert ficha.json()["extras"]["valor_estimado"] == 700

    for cambio in ({"valor_estimado": "1"}, {"estado_lead": "contactado"}, {"motivo_perdida": "otro"}):
        r = await _patch(app_client, cliente, headers, **cambio)
        assert r.status_code == 403, (cambio, r.text)
    la = await _lead(tid, lead)
    assert (la.status, la.loss_reason, la.estimated_value) == ("perdido", "precio", Decimal("700.00"))


async def test_los_motivos_y_valores_no_cruzan_negocios(app_client) -> None:
    ta, tb = await _negocio(), await _negocio()
    ca, _, _ = await _cliente(ta, nombre="de A", status="perdido", loss_reason="precio", estimated_value=Decimal("5"))
    cb, _, lead_b = await _cliente(tb, nombre="de B", status="perdido", loss_reason="otro", estimated_value=Decimal("9"))
    ha, hb = await api_headers(app_client, ta), await api_headers(app_client, tb)

    r = await app_client.get(URL, params={"filtro": "motivo_perdida:otro"}, headers=ha)
    assert r.json()["items"] == []  # el «otro» es de B
    r = await app_client.get(URL, params={"filtro": "motivo_perdida:precio"}, headers=hb)
    assert r.json()["items"] == []
    r = await app_client.get(URL, params={"facetas": "motivo_perdida"}, headers=ha)
    counts = next(f for f in r.json()["facetas"] if f["key"] == "motivo_perdida")["counts"]
    assert counts["precio"] == 1 and counts["otro"] == 0  # el «otro» de B no se cuenta en A
    r = await app_client.get(URL, params={"orden": "valor_desc"}, headers=ha)
    assert [i["cliente"] for i in r.json()["items"]] == ["de A"]

    # Ni se lee ni se escribe el lead de otro negocio por su id.
    assert (await app_client.get(f"{URL}/{cb}", headers=ha)).status_code == 404
    r = await _patch(app_client, cb, ha, valor_estimado="1")
    assert r.status_code == 404
    assert (await _lead(tb, lead_b)).estimated_value == Decimal("9.00")
    assert ca


# ---------------------------------------------------------------- la migración
async def test_los_check_de_la_migracion_protegen_los_datos() -> None:
    from pack_commerce.models import Lead, LeadStageChange

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente, _, lead = await _cliente(tid)

    async def intentar(modelo, **campos) -> bool:
        """True si la base rechaza la fila (un CHECK)."""
        try:
            async with tenant_scope(tid):
                async with get_session() as session:
                    if modelo is Lead:
                        session.add(Lead(tenant_id=tid, customer_id=cliente, **campos))
                    else:
                        session.add(
                            LeadStageChange(
                                tenant_id=tid, lead_id=lead, customer_id=cliente, from_stage="nuevo",
                                to_stage="perdido", actor="persona", **campos,
                            )
                        )
        except IntegrityError:
            return True
        return False

    assert await intentar(Lead, loss_reason="inventado")
    assert await intentar(Lead, loss_note="x" * 301)
    assert await intentar(Lead, estimated_value=Decimal("-1"))
    assert await intentar(LeadStageChange, loss_reason="inventado")
    assert await intentar(LeadStageChange, loss_note="x" * 301)
    for motivo in MOTIVOS:
        assert not await intentar(Lead, loss_reason=motivo, loss_note="x" * 300, estimated_value=Decimal("0"))


def test_el_catalogo_de_la_migracion_es_el_del_codigo() -> None:
    from pack_commerce.etapas import CODIGOS_DE_MOTIVO

    migracion = Path(__file__).resolve().parents[2] / "packages/pack-commerce/alembic/versions/0019_lead_motivo_y_valor.py"
    copiado = re.search(r"_MOTIVOS = \"\((.*?)\)\"", migracion.read_text()).group(1)
    assert [m.strip("'") for m in copiado.split(",")] == list(CODIGOS_DE_MOTIVO)

"""HU-QA-12 · Importar datos existentes por agente desde un CSV (ADR-0180).

- **Los tres módulos con plantilla** (los planes salieron de commerce con SPEC-43/HU-COM-17): productos, tipos de cita y clientes y préstamos (sin teléfono) se crean
  desde su plantilla descargable y quedan en un lote con `origen = 'importacion'`.
- **Vista previa:** errores por fila sin escribir; con errores confirmar da 422 salvo `solo_validas`.
- **Idempotente:** subir dos veces el mismo archivo no duplica; lo creado a mano no se toca.
- **Borrar lo importado** retira solo lo importado; reimportar después crea de nuevo (el SKU se libera);
  «Generar datos de prueba» y su borrado no tocan lo importado.
- **Permisos y aislamiento:** la sección del pack en escritura; un agente de otro negocio es 404 sin eco
  y no escribe nada; sin `QA_TESTERS_ENABLED` todo es 404.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
from _api_helpers import api_headers
from test_backfill_agentes import _sql
from test_fiabilidad_api import PASSWORD, _colaborador
from test_qa_datos_generados import (
    CATALOGO,
    ProveedorGuionado,
    _agente,
    _negocio,
    _proveedor,
    qa_on,  # noqa: F401  (fixture)
)

pytestmark = [
    pytest.mark.aislamiento,  # agente de otro negocio, permisos y bandera de entorno (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_BASE = "/api/v1/admin/agents"


def _csv(texto: str) -> dict:
    return {"archivo": ("datos.csv", texto.encode("utf-8"), "text/csv")}


async def _subir(app_client, h, agente, ruta: str, texto: str, **datos):
    return await app_client.post(f"{_BASE}/{agente}/importar-datos{ruta}", headers=h, files=_csv(texto), data=datos)


PRODUCTOS = (
    "sku,nombre,precio,categoria,descripcion,stock\r\n"
    "CERA-1,Cera mate,28.000,Cabello > Ceras,Fijación media,12\r\n"
    "ACE-2,Aceite para barba,35000,Barba,,8\r\n"
)


# ---------------------------------------------------------------- productos (Ventas)
async def test_productos_vista_previa_importar_idempotente_y_borrar(app_client, monkeypatch, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)

    estado = (await app_client.get(f"{_BASE}/{ventas}/importar-datos", headers=h)).json()
    assert estado["disponible"] is True and estado["plantilla"]["id"] == "productos" and estado["importado"] is None
    assert [c["nombre"] for c in estado["plantilla"]["columnas"]][:3] == ["sku", "nombre", "precio"]

    plantilla = await app_client.get(f"{_BASE}/{ventas}/importar-datos/plantilla.csv", headers=h)
    assert plantilla.status_code == 200 and plantilla.content.startswith(b"\xef\xbb\xbfsku,nombre,precio")
    assert "attachment" in plantilla.headers["content-disposition"]

    previa = (await _subir(app_client, h, ventas, "/validar", PRODUCTOS)).json()
    assert (previa["nuevas"], previa["existentes"], previa["con_errores"]) == (2, 0, 0)
    assert (await _sql(tid, "select count(*) from pack_commerce_products where tenant_id = :t", t=tid))[0][0] == 0

    r = await _subir(app_client, h, ventas, "", PRODUCTOS)
    assert r.status_code == 201, r.text
    assert r.json()["creadas"] == 2 and r.json()["conteos"]["producto"] == 2
    filas = await _sql(
        tid,
        "select v.sku, v.price, i.stock_quantity from pack_commerce_product_variants v "
        "join pack_commerce_inventory i on i.variant_id = v.id where v.tenant_id = :t order by v.sku",
        t=tid,
    )
    assert [(s, float(p), q) for s, p, q in filas] == [("ACE-2", 35000.0, 8), ("CERA-1", 28000.0, 12)]
    origenes = await _sql(tid, "select distinct origen from test_data_records where tenant_id = :t", t=tid)
    assert origenes == [("importacion",)]

    # Dos veces el mismo archivo: no duplica y lo reporta como «ya existía».
    previa = (await _subir(app_client, h, ventas, "/validar", PRODUCTOS)).json()
    assert (previa["nuevas"], previa["existentes"]) == (0, 2)
    r = await _subir(app_client, h, ventas, "", PRODUCTOS)
    assert r.status_code == 201 and r.json()["creadas"] == 0 and r.json()["existentes"] == 2
    assert (await _sql(tid, "select count(*) from pack_commerce_product_variants where tenant_id = :t", t=tid))[0][0] == 2

    # Generar datos de prueba (HU-QA-10) y borrarlos no tocan lo importado.
    _proveedor(monkeypatch, ProveedorGuionado(__import__("json").dumps(CATALOGO)))
    assert (await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 201
    assert (await app_client.delete(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 200
    vivos = await _sql(
        tid,
        "select count(*) from pack_commerce_products where tenant_id = :t and deleted_at is null and name = 'Cera mate'",
        t=tid,
    )
    assert vivos[0][0] == 1
    estado = (await app_client.get(f"{_BASE}/{ventas}/importar-datos", headers=h)).json()
    assert estado["importado"]["conteos"]["producto"] == 2

    # Borrar lo importado retira solo eso; reimportar crea de nuevo (el SKU quedó libre).
    r = await app_client.delete(f"{_BASE}/{ventas}/importar-datos", headers=h)
    assert r.status_code == 200 and r.json()["retirados"] >= 4
    vivos = await _sql(
        tid, "select count(*) from pack_commerce_products where tenant_id = :t and deleted_at is null", t=tid
    )
    assert vivos[0][0] == 0
    r = await _subir(app_client, h, ventas, "", PRODUCTOS)
    assert r.status_code == 201 and r.json()["creadas"] == 2
    activos = await _sql(
        tid,
        "select count(*) from pack_commerce_product_variants where tenant_id = :t and active and sku in ('CERA-1','ACE-2')",
        t=tid,
    )
    assert activos[0][0] == 2

    auditoria = await _sql(
        tid, "select input->>'accion' from audit_log where tenant_id = :t and action = 'qa_importar_datos' order by created_at", t=tid
    )
    assert [a[0] for a in auditoria] == ["importar", "importar", "borrar", "importar"]


async def test_errores_por_fila_solo_validas_y_lo_creado_a_mano_no_se_toca(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    # Un producto del negocio con el SKU del archivo (creado a mano): se omite y no se modifica.
    await _sub_producto_a_mano(app_client, h, tid)

    texto = (
        "sku,nombre,precio\r\n"
        "A-1,Producto válido,1000\r\n"
        "A-2,X,500\r\n"
        "A-3,Sin precio,abc\r\n"
        "A-1,Repetido,10\r\n"
        "MANO-1,Pisado,1\r\n"
    )
    previa = (await _subir(app_client, h, ventas, "/validar", texto)).json()
    assert (previa["nuevas"], previa["existentes"], previa["con_errores"]) == (1, 1, 3)
    assert [(e["fila"], e["columna"]) for e in previa["errores"]] == [(3, "nombre"), (4, "precio"), (5, "sku")]
    assert previa["errores_total"] == 3

    r = await _subir(app_client, h, ventas, "", texto)
    assert r.status_code == 422 and r.json()["error"]["code"] == "archivo_con_errores"
    assert (await _sql(tid, "select count(*) from pack_commerce_products where tenant_id = :t", t=tid))[0][0] == 1

    r = await _subir(app_client, h, ventas, "", texto, solo_validas="true")
    assert r.status_code == 201 and r.json()["creadas"] == 1 and r.json()["con_errores"] == 3
    nombres = await _sql(tid, "select name from pack_commerce_products where tenant_id = :t order by name", t=tid)
    assert [n[0] for n in nombres] == ["Hecho a mano", "Producto válido"]

    # Borrar lo importado no toca el producto de la mano.
    assert (await app_client.delete(f"{_BASE}/{ventas}/importar-datos", headers=h)).status_code == 200
    vivos = await _sql(tid, "select name from pack_commerce_products where tenant_id = :t and deleted_at is null", t=tid)
    assert vivos == [("Hecho a mano",)]


async def _sub_producto_a_mano(app_client, h, tid) -> None:
    from pack_commerce.seed import seed_catalog

    await seed_catalog(
        tid,
        {
            "category_paths": [["Varios"]],
            "products": [
                {"name": "Hecho a mano", "category_path": ["Varios"], "variants": [{"sku": "MANO-1", "price": 7}]}
            ],
        },
    )


async def test_sku_de_un_producto_eliminado_a_mano_es_error_de_la_fila(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    await _sub_producto_a_mano(app_client, h, tid)
    await _sql(tid, "update pack_commerce_products set deleted_at = now() where tenant_id = :t", t=tid)
    previa = (await _subir(app_client, h, ventas, "/validar", "sku,nombre,precio\r\nMANO-1,Otra vez,5\r\n")).json()
    assert previa["nuevas"] == 0 and previa["errores"][0]["columna"] == "sku"
    assert "eliminaste" in previa["errores"][0]["mensaje"]


async def test_columnas_faltantes_y_archivo_invalido(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    previa = (await _subir(app_client, h, ventas, "/validar", "nombre,telefono\r\nCera,300\r\n")).json()
    assert previa["columnas_faltantes"] == ["sku", "precio"] and previa["columnas_ignoradas"] == ["telefono"]
    r = await _subir(app_client, h, ventas, "", "nombre\r\nCera\r\n")
    assert r.status_code == 422 and r.json()["error"]["code"] == "columnas_faltantes"
    r = await _subir(app_client, h, ventas, "/validar", "")
    assert r.status_code == 422 and r.json()["error"]["code"] == "archivo_vacio"
    grande = "sku,nombre,precio\r\n" + "A,Producto,1\r\n" * 90_000
    r = await _subir(app_client, h, ventas, "/validar", grande)
    assert r.status_code == 413 and r.json()["error"]["code"] == "archivo_muy_grande"


# ---------------------------------------------------------------- tipos de cita (Agenda)
async def test_tipos_de_cita(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    agenda = await _agente(tid, "agenda", ["pack_calendar.agendar_cita"])
    h = await api_headers(app_client, tid)
    texto = "Nombre;Duración_minutos;Descripción\r\nCorte;30;Clásico\r\nBarba;20;\r\nCorte;45;repetido\r\n"
    previa = (await _subir(app_client, h, agenda, "/validar", texto)).json()
    assert (previa["nuevas"], previa["con_errores"]) == (2, 1)
    r = await _subir(app_client, h, agenda, "", texto, solo_validas="true")
    assert r.status_code == 201 and r.json()["conteos"] == {"tipo": 2}
    r = await _subir(app_client, h, agenda, "", texto, solo_validas="true")
    assert r.json()["creadas"] == 0 and r.json()["existentes"] == 2
    filas = await _sql(
        tid, "select name, duration_minutes from pack_calendar_appointment_types where tenant_id = :t and deleted_at is null order by name", t=tid
    )
    assert filas == [("Barba", 20), ("Corte", 30)]
    assert (await app_client.delete(f"{_BASE}/{agenda}/importar-datos", headers=h)).json()["retirados"] == 2
    vivos = await _sql(tid, "select count(*) from pack_calendar_appointment_types where tenant_id = :t and deleted_at is null", t=tid)
    assert vivos[0][0] == 0


# ---------------------------------------------------------------- cobranza (sin teléfono)
async def test_cobranza_clientes_y_prestamos_sin_telefono(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    cobranza = await _agente(tid, "cobranza", ["pack_cobranza.registrar_promesa"])
    h = await api_headers(app_client, tid)
    hace_10 = (date.today() - timedelta(days=10)).isoformat()
    texto = (
        "cartera,numero_documento,nombre,apellido,telefono,correo,riesgo,valor,num_cuotas,interes_porcentaje,"
        "dias_entre_cuotas,fecha_desembolso,cuotas_pagadas\r\n"
        f"Tenderos Sur,10203040,Ana,Ríos,3001234567,ana@correo.test,rojo,200000,10,20,1,{hace_10},3\r\n"
        "Tenderos Sur,10203041,Eva,Paz,,,,,,,,,\r\n"
    )
    previa = (await _subir(app_client, h, cobranza, "/validar", texto)).json()
    assert (previa["nuevas"], previa["con_errores"]) == (2, 0)
    assert previa["columnas_ignoradas"] == ["telefono", "correo"]
    r = await _subir(app_client, h, cobranza, "", texto)
    assert r.status_code == 201, r.text
    conteos = r.json()["conteos"]
    assert conteos["cartera"] == 1 and conteos["cliente"] == 2 and conteos["prestamo"] == 1
    assert conteos["cuota"] == 10 and conteos["pago"] == 3

    filas = await _sql(
        tid,
        "select telefono_whatsapp, color_riesgo from pack_cobranza_clientes where tenant_id = :t order by numero_documento",
        t=tid,
    )
    assert filas == [(None, "rojo"), (None, "blanco")]  # el teléfono del archivo no se importa
    estatus = await _sql(
        tid,
        "select estatus, count(*) from pack_cobranza_cuotas where tenant_id = :t group by estatus order by estatus",
        t=tid,
    )
    assert dict(estatus) == {"pagada": 3, "atrasada": 7} or dict(estatus) == {"pagada": 3, "atrasada": 6, "pendiente": 1}
    caja = (await _sql(tid, "select saldo_actual from pack_cobranza_cajas where tenant_id = :t", t=tid))[0][0]
    assert caja >= 0

    r = await _subir(app_client, h, cobranza, "", texto)
    assert r.json()["creadas"] == 0 and r.json()["existentes"] == 2
    assert (await _sql(tid, "select count(*) from pack_cobranza_clientes where tenant_id = :t", t=tid))[0][0] == 2

    assert (await app_client.delete(f"{_BASE}/{cobranza}/importar-datos", headers=h)).status_code == 200
    vivos = await _sql(
        tid,
        "select (select count(*) from pack_cobranza_clientes where tenant_id = :t and deleted_at is null), "
        "(select count(*) from pack_cobranza_carteras where tenant_id = :t and deleted_at is null)",
        t=tid,
    )
    assert vivos[0] == (0, 0)


# ---------------------------------------------------------------- permisos y aislamiento
async def test_permisos_por_seccion_del_pack(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    solo_lee = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"agents": "write", "pack_commerce": "read"}), password=PASSWORD
    )
    r = await _subir(app_client, solo_lee, ventas, "", PRODUCTOS)
    assert r.status_code == 403, r.text
    assert (await app_client.delete(f"{_BASE}/{ventas}/importar-datos", headers=solo_lee)).status_code == 403
    assert (await _sql(tid, "select count(*) from pack_commerce_products where tenant_id = :t", t=tid))[0][0] == 0

    sin_agentes = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"agents": "read", "pack_commerce": "write"}), password=PASSWORD
    )
    assert (await app_client.get(f"{_BASE}/{ventas}/importar-datos", headers=sin_agentes)).status_code == 200
    assert (await _subir(app_client, sin_agentes, ventas, "", PRODUCTOS)).status_code == 403

    escribe = await api_headers(
        app_client, tid, email=await _colaborador(tid, {"agents": "write", "pack_commerce": "write"}), password=PASSWORD
    )
    assert (await _subir(app_client, escribe, ventas, "", PRODUCTOS)).status_code == 201


async def test_aislamiento_entre_negocios_y_bandera(app_client, monkeypatch, qa_on) -> None:  # noqa: F811
    from core.config import get_settings

    mio = await _negocio()
    suyo = await _negocio()
    mio_ventas = await _agente(mio, "ventas", ["pack_commerce.tomar_pedido"])
    suyo_ventas = await _agente(suyo, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, mio)
    h_suyo = await api_headers(app_client, suyo)

    # Mi sesión con el agente del otro negocio: 404 sin eco, y no se escribe nada en ninguno.
    for metodo, ruta in (
        ("get", ""),
        ("get", "/plantilla.csv"),
        ("delete", ""),
    ):
        r = await getattr(app_client, metodo)(f"{_BASE}/{suyo_ventas}/importar-datos{ruta}", headers=h)
        assert r.status_code == 404 and str(suyo_ventas) not in r.text, (metodo, ruta, r.text)
    for ruta in ("/validar", ""):
        r = await _subir(app_client, h, suyo_ventas, ruta, PRODUCTOS)
        assert r.status_code == 404 and str(suyo_ventas) not in r.text
    for tid in (mio, suyo):
        assert (await _sql(tid, "select count(*) from pack_commerce_products where tenant_id = :t", t=tid))[0][0] == 0
        assert (await _sql(tid, "select count(*) from test_data_records where tenant_id = :t", t=tid))[0][0] == 0

    # Lo mío no se mezcla con lo suyo: el mismo SKU en dos negocios es válido y cada uno ve lo suyo.
    assert (await _subir(app_client, h, mio_ventas, "", PRODUCTOS)).status_code == 201
    assert (await _subir(app_client, h_suyo, suyo_ventas, "", PRODUCTOS)).status_code == 201
    r = await app_client.delete(f"{_BASE}/{mio_ventas}/importar-datos", headers=h)
    assert r.status_code == 200
    assert (await _sql(mio, "select count(*) from pack_commerce_products where tenant_id = :t and deleted_at is null", t=mio))[0][0] == 0
    assert (await _sql(suyo, "select count(*) from pack_commerce_products where tenant_id = :t and deleted_at is null", t=suyo))[0][0] == 2

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", False)
    assert (await app_client.get(f"{_BASE}/{mio_ventas}/importar-datos", headers=h)).status_code == 404
    assert (await app_client.get(f"{_BASE}/{mio_ventas}/importar-datos/plantilla.csv", headers=h)).status_code == 404
    assert (await _subir(app_client, h, mio_ventas, "/validar", PRODUCTOS)).status_code == 404
    assert (await _subir(app_client, h, mio_ventas, "", PRODUCTOS)).status_code == 404
    assert (await app_client.delete(f"{_BASE}/{mio_ventas}/importar-datos", headers=h)).status_code == 404


async def test_agente_sin_plantilla(app_client, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    general = await _agente(tid, "general", [], kind="core")
    h = await api_headers(app_client, tid)
    estado = (await app_client.get(f"{_BASE}/{general}/importar-datos", headers=h)).json()
    assert estado["disponible"] is False and estado["motivo"] and estado["plantilla"] is None
    assert (await _subir(app_client, h, general, "", PRODUCTOS)).status_code == 409
    assert (await app_client.get(f"{_BASE}/{general}/importar-datos/plantilla.csv", headers=h)).status_code == 409
    assert uuid.UUID(str(general))

"""API pública del pack (ADR-0057): la app de campo (gestores).

Superficie pública `/api/pack/pack_cobranza/…`. El pack **emite y valida su
propio JWT** (ver `auth.py`); el core no conoce a los usuarios de campo. El login
es por **dispositivo** (código + API key) contra el slug del tenant.

Endpoints:
- `POST /auth/dispositivo` — login de dispositivo → token. (Era `/auth/login`; se
  renombró en ADR-0081 para liberar `/auth/login`, que ahora es el login unificado
  de usuario/contraseña en `api_gestor.py`.)
- `GET  /mis-carteras` — carteras del gestor.
- `GET  /carteras/{id}/clientes-dia` — clientes del día.
- `POST /sync/eventos` — sincronización offline idempotente (visita|pago|abono).
- `POST /clientes/{id}/evidencia` — sube foto facial o de documento (multipart).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.admin.auth import verify_password
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from pack_cobranza.auth import issue_token, pack_secret, token_tenant
from pack_cobranza.evidencias import EvidenciaError, guardar_evidencia
from pack_cobranza.models import (
    Abono,
    Cartera,
    Cliente,
    ClienteEvidencia,
    Cuota,
    Device,
    IntentoAcceso,
    Pago,
    Prestamo,
    SyncEvento,
    Visita,
)

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"
router = APIRouter(prefix="/api/pack/pack_cobranza", tags=[PLUGIN_ID])


async def _auth(authorization: str | None) -> tuple[uuid.UUID, dict[str, Any]] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return await token_tenant(authorization.split(" ", 1)[1].strip())


@router.post("/auth/dispositivo")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    slug = str(body.get("tenant") or "").strip()
    codigo = str(body.get("codigo") or "").strip()
    api_key = str(body.get("api_key") or "")
    if not (slug and codigo and api_key):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)

    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            device = (
                await session.execute(select(Device).where(Device.tenant_id == tenant_id, Device.codigo == codigo, Device.deleted_at.is_(None)))
            ).scalar_one_or_none()
            ok = device is not None and device.estado == "activo" and verify_password(api_key, device.api_key_hash)
            session.add(
                IntentoAcceso(
                    tenant_id=tenant_id,
                    device_id=device.id if device else None,
                    gestor_id=device.gestor_id if device else None,
                    imei=str(body.get("imei") or "") or None,
                    whatsapp_number=str(body.get("whatsapp") or "") or None,
                    resultado="ok" if ok else "denegado",
                    motivo=None if ok else "device o api_key inválidos",
                    ip=request.client.host if request.client else None,
                )
            )
            if not ok:
                return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
            token = issue_token(
                {"tenant_id": str(tenant_id), "gestor_id": str(device.gestor_id) if device.gestor_id else None, "device_id": str(device.id)},
                secret,
            )
        return JSONResponse({"token": token, "tenant": slug})


@router.get("/mis-carteras")
async def mis_carteras(authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    gestor_id = payload.get("gestor_id")
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            q = select(Cartera).where(Cartera.tenant_id == tenant_id, Cartera.deleted_at.is_(None))
            if gestor_id:
                q = q.where(Cartera.gestor_id == uuid.UUID(gestor_id))
            carteras = (await session.execute(q)).scalars().all()
            return JSONResponse({"carteras": [{"id": str(c.id), "nombre": c.nombre, "moneda": c.moneda} for c in carteras]})


@router.get("/carteras/{cartera_id}/clientes-dia")
async def clientes_dia(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, _payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await get_owned(session, Cartera, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            clientes = (await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id, Cliente.cartera_id == cid, Cliente.deleted_at.is_(None)))).scalars().all()
            out = []
            for c in clientes:
                prestamos = (await session.execute(select(Prestamo.id).where(Prestamo.tenant_id == tenant_id, Prestamo.cliente_id == c.id, Prestamo.estatus == "vigente", Prestamo.deleted_at.is_(None)))).scalars().all()
                if not prestamos:
                    continue
                cuotas = (await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id.in_(prestamos), Cuota.estatus.in_(("pendiente", "atrasada")), Cuota.deleted_at.is_(None)))).scalars().all()
                saldo = sum((q.valor_esperado for q in cuotas), Decimal(0))
                mora = max(((date.today() - q.fecha_vencimiento).days for q in cuotas if q.fecha_vencimiento < date.today()), default=0)
                out.append({"id": str(c.id), "nombre": f"{c.nombre} {c.apellido or ''}".strip(), "telefono": c.telefono_whatsapp or "", "lat": c.lat, "lng": c.lng, "saldo": float(saldo), "mora": mora, "riesgo": c.color_riesgo})
            return JSONResponse({"clientes": out})


@router.post("/sync/eventos")
async def sync_eventos(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    device_id = payload.get("device_id")
    body = await request.json()
    eventos = body.get("eventos") or []
    aplicados = 0
    duplicados = 0
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for ev in eventos:
                evento_id = str(ev.get("evento_id_cliente") or "").strip()
                tipo = str(ev.get("tipo") or "").strip()
                data = ev.get("payload") or {}
                if not evento_id or tipo not in ("visita", "pago", "abono"):
                    continue
                existe = (
                    await session.execute(select(SyncEvento.id).where(SyncEvento.tenant_id == tenant_id, SyncEvento.evento_id_cliente == evento_id))
                ).scalar_one_or_none()
                if existe is not None:
                    duplicados += 1
                    continue
                try:
                    await _aplicar_evento(session, tenant_id, tipo, data)
                except Exception as exc:  # dato inválido del dispositivo: no tumba el lote
                    session.add(SyncEvento(tenant_id=tenant_id, device_id=uuid.UUID(device_id) if device_id else None, evento_id_cliente=evento_id, tipo=tipo, payload=data, estado="error"))
                    log.warning("sync_evento_error", tipo=tipo, error=type(exc).__name__)
                    continue
                session.add(SyncEvento(tenant_id=tenant_id, device_id=uuid.UUID(device_id) if device_id else None, evento_id_cliente=evento_id, tipo=tipo, payload=data))
                aplicados += 1
    return JSONResponse({"aplicados": aplicados, "duplicados": duplicados})


async def _aplicar_evento(session, tenant_id: uuid.UUID, tipo: str, data: dict[str, Any]) -> None:
    cliente_id = uuid.UUID(str(data.get("cliente_id")))
    cliente = await session.get(Cliente, cliente_id)
    if cliente is None or cliente.tenant_id != tenant_id:
        raise ValueError("cliente inválido")
    if tipo == "visita":
        session.add(
            Visita(
                tenant_id=tenant_id, cartera_id=cliente.cartera_id, cliente_id=cliente.id,
                fecha=date.fromisoformat(str(data.get("fecha"))) if data.get("fecha") else date.today(),
                resultado=str(data.get("resultado") or "no_pago"),
                motivo_no_pago=data.get("motivo"),
                valor_pagado=Decimal(str(data["valor"])) if data.get("valor") else None,
                creado_por="gestor",
            )
        )
    elif tipo == "pago":
        prestamo_id = uuid.UUID(str(data.get("prestamo_id")))
        cuota_id = uuid.UUID(str(data["cuota_id"])) if data.get("cuota_id") else None
        session.add(Pago(tenant_id=tenant_id, cliente_id=cliente.id, prestamo_id=prestamo_id, cuota_id=cuota_id, valor=Decimal(str(data.get("valor") or 0)), metodo_pago=str(data.get("metodo_pago") or "efectivo"), registrado_por="gestor"))
        if cuota_id:
            cuota = await session.get(Cuota, cuota_id)
            if cuota is not None and cuota.tenant_id == tenant_id:
                cuota.estatus = "pagada"
    else:  # abono
        prestamo_id = uuid.UUID(str(data.get("prestamo_id")))
        session.add(Abono(tenant_id=tenant_id, prestamo_id=prestamo_id, cliente_id=cliente.id, valor=Decimal(str(data.get("valor") or 0)), metodo_pago=str(data.get("metodo_pago") or "efectivo"), registrado_por="gestor"))


@router.post("/clientes/{cliente_id}/evidencia")
async def subir_evidencia(cliente_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Registra una evidencia del cliente (foto facial o documento) subida por el gestor.

    Multipart: `tipo` (foto_facial|documento_frente|documento_reverso) + `archivo`.
    Idempotente no: cada llamada crea una evidencia nueva (el gestor puede reintentar
    una foto distinta). El archivo se guarda en `TENANTS_ROOT/<slug>/evidencias/`.
    """
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, _payload = auth
    try:
        cid = uuid.UUID(cliente_id)
    except ValueError:
        return JSONResponse({"error": "cliente inválido"}, status_code=400)
    form = await request.form()
    tipo = str(form.get("tipo") or "").strip()
    archivo = form.get("archivo")
    if not hasattr(archivo, "read"):
        return JSONResponse({"error": "falta el archivo"}, status_code=400)
    contenido = await archivo.read()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await get_owned(session, Cliente, cid) is None:
                return JSONResponse({"error": "cliente no encontrado"}, status_code=404)
            try:
                meta = await guardar_evidencia(tenant_id, cid, tipo, getattr(archivo, "filename", None), getattr(archivo, "content_type", None), contenido)
            except EvidenciaError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            evidencia = ClienteEvidencia(tenant_id=tenant_id, cliente_id=cid, tipo=tipo, **meta)  # type: ignore[arg-type]
            session.add(evidencia)
            await session.flush()
            return JSONResponse({"id": str(evidencia.id), "tipo": tipo, "ruta": meta["ruta_archivo"]}, status_code=201)

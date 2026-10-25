"""Qué procedimientos tocó una candidata frente a la activa (HU-AOS-21, adenda de ADR-0117). Puro."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from agentcore_contracts.procedure import Procedure

from core.releases.cargar import ReleaseCongelada
from core.releases.diferencias import SIN_LINEA_BASE, procedimientos_tocados
from core.shared.release_vigente import AgenteCongelado

RAIZ = Path(__file__).resolve().parents[1]
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
CONEXION = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
KERNEL = uuid.UUID("00000000-0000-0000-0000-000000000001")
VENTAS = uuid.UUID("00000000-0000-0000-0000-000000000002")
AGENDA = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _proc(pack: str, nombre: str, **cambios: Any) -> Procedure:
    datos = json.loads((RAIZ / "packages" / pack / "procedures" / f"{nombre}.json").read_text(encoding="utf-8"))
    datos.update(cambios)
    return Procedure.model_validate(datos)


PLAN = _proc("pack-commerce", "contratar_plan")
PEDIDO = _proc("pack-commerce", "tomar_pedido")
CITA = _proc("pack-calendar", "agendar_cita")


def _agente(instancia: uuid.UUID, key: str, role: str, procs: list[Procedure], **extra: Any) -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=instancia,
        agent_id=uuid.uuid4(),
        key=key,
        role=role,
        version_id=uuid.uuid4(),
        version=1,
        contract="kernelspec/1" if role == "core" else "agentspec/3",
        content=extra.pop("content", {}),
        is_default=extra.pop("is_default", False),
        procedimientos=tuple((p.id, p.version) for p in procs),
    )


def _release(
    agentes: list[AgenteCongelado],
    *,
    hashes: dict[uuid.UUID, str] | None = None,
    app_settings: dict[str, Any] | None = None,
    playbooks: list[dict[str, str]] | None = None,
    skills: list[dict[str, str]] | None = None,
    schema: str = "release/2",
) -> ReleaseCongelada:
    hashes = hashes or {}
    procs = {p.id: p for a in agentes for p in _todos if (p.id, p.version) in (a.procedimientos or ())}
    manifest = {
        "schema": schema,
        "components": [
            {"instance_id": str(a.instance_id), "content_hash": hashes.get(a.instance_id, "h0")} for a in agentes
        ],
        "skills": skills
        if skills is not None
        else [
            {"pack_id": "pack_commerce", "version": "2.4.0", "manifest_hash": "c"},
            {"pack_id": "pack_calendar", "version": "2.2.0", "manifest_hash": "k"},
        ],
        "app_settings": app_settings or {"brand_voice": "cercana"},
        "playbook_overrides": playbooks or [],
    }
    return ReleaseCongelada(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        connection_id=CONEXION,
        number=1,
        manifest_hash="x",
        manifest=manifest,
        idle_minutes=30,
        bytes=0,
        componentes=tuple(agentes),
        congelados={(p.id, p.version): p for p in procs.values()},
        _procedimientos=procs,
    )


_todos: list[Procedure] = [PLAN, PEDIDO, CITA]


def _base(**kw: Any) -> ReleaseCongelada:
    return _release(
        [
            _agente(KERNEL, "coordinador", "core", []),
            _agente(VENTAS, "ventas", "specialist", [PLAN, PEDIDO]),
            _agente(AGENDA, "agenda", "specialist", [CITA]),
        ],
        **kw,
    )


def test_sin_cambios_no_toca_nada() -> None:
    assert procedimientos_tocados(_base(), _base()) == {}


def test_sin_activa_o_release_1_todo_sin_linea_base() -> None:
    candidata = _base()
    esperado = {pid: SIN_LINEA_BASE for pid in ("pack_calendar.agendar_cita", "pack_commerce.contratar_plan", "pack_commerce.tomar_pedido")}
    assert procedimientos_tocados(candidata, None) == esperado
    assert procedimientos_tocados(candidata, _base(schema="release/1")) == esperado


def test_cambio_de_contenido_toca_solo_ese_procedimiento() -> None:
    global _todos
    nuevo_plan = _proc("pack-commerce", "contratar_plan", title="Contratar un plan (v2)")
    activa = _base()
    _todos = [nuevo_plan, PEDIDO, CITA]
    try:
        candidata = _base()
    finally:
        _todos = [PLAN, PEDIDO, CITA]
    assert procedimientos_tocados(candidata, activa) == {"pack_commerce.contratar_plan": "contenido"}


def test_agente_cambiado_toca_sus_procedimientos() -> None:
    activa = _base()
    candidata = _base(hashes={VENTAS: "otro"})
    assert procedimientos_tocados(candidata, activa) == {
        "pack_commerce.contratar_plan": "agente:ventas",
        "pack_commerce.tomar_pedido": "agente:ventas",
    }


def test_coordinador_cambiado_toca_todos() -> None:
    tocados = procedimientos_tocados(_base(hashes={KERNEL: "voz-nueva"}), _base())
    assert tocados == {pid: "coordinador" for pid in ("pack_calendar.agendar_cita", "pack_commerce.contratar_plan", "pack_commerce.tomar_pedido")}


def test_app_settings_y_habilidad_tocan_todos() -> None:
    assert set(procedimientos_tocados(_base(app_settings={"brand_voice": "formal"}), _base()).values()) == {
        "app_settings"
    }
    habilidad = [
        {"pack_id": "pack_commerce", "version": "2.5.0", "manifest_hash": "c2"},
        {"pack_id": "pack_calendar", "version": "2.2.0", "manifest_hash": "k"},
    ]
    assert set(procedimientos_tocados(_base(skills=habilidad), _base()).values()) == {"habilidad:pack_commerce"}


def test_playbook_de_un_agente_toca_solo_ese_agente_y_uno_general_toca_todos() -> None:
    def con_playbook(nombre: str, texto: str) -> ReleaseCongelada:
        return _release(
            [
                _agente(KERNEL, "coordinador", "core", []),
                _agente(VENTAS, "ventas", "specialist", [PLAN, PEDIDO], content={"playbooks": [{"name": "ventas"}]}),
                _agente(AGENDA, "agenda", "specialist", [CITA]),
            ],
            playbooks=[{"name": nombre, "text": texto, "hash": texto}],
        )

    assert procedimientos_tocados(con_playbook("ventas", "b"), con_playbook("ventas", "a")) == {
        "pack_commerce.contratar_plan": "playbook:ventas",
        "pack_commerce.tomar_pedido": "playbook:ventas",
    }
    assert set(procedimientos_tocados(con_playbook("general", "b"), con_playbook("general", "a"))) == {
        "pack_calendar.agendar_cita",
        "pack_commerce.contratar_plan",
        "pack_commerce.tomar_pedido",
    }


def test_alta_y_baja_de_especialista() -> None:
    sin_agenda = _release(
        [_agente(KERNEL, "coordinador", "core", []), _agente(VENTAS, "ventas", "specialist", [PLAN, PEDIDO, CITA])]
    )
    # Baja de Agenda: su procedimiento sigue en la candidata (lo atiende Ventas) y cuenta como tocado.
    tocados = procedimientos_tocados(sin_agenda, _base())
    assert tocados["pack_calendar.agendar_cita"] == "agente_retirado:agenda"
    # Alta de Agenda: sus procedimientos cuentan como tocados.
    assert procedimientos_tocados(_base(), sin_agenda)["pack_calendar.agendar_cita"] == "agente_nuevo:agenda"

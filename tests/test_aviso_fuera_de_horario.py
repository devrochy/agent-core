"""Aviso por defecto fuera de horario (adenda 2026-10-18 de ADR-0051): playbook, idioma, nombre
y antirrebote, sin base de datos ni LLM."""

from __future__ import annotations

import uuid

import pytest

from core.agents.playbooks import load_core_playbook
from core.kernel import fuera_de_horario as fh
from core.kernel.tarjeta import RotulosInvalidos

ES = "¡Hola! Ahora estamos fuera de nuestro horario de atención. Te respondemos apenas volvamos."


def test_el_playbook_del_kernel_parsea_completo() -> None:
    avisos = fh.parsear_avisos(load_core_playbook(fh.PLAYBOOK))
    assert set(avisos) == {f"{i}{s}" for i in fh.IDIOMAS for s in ("", "_con_nombre")}
    assert avisos["es"] == ES


def test_un_playbook_incompleto_o_con_campo_raro_lanza() -> None:
    contenido = load_core_playbook(fh.PLAYBOOK)
    with pytest.raises(RotulosInvalidos):
        fh.parsear_avisos(contenido.replace("## pt_con_nombre\n", "## otro\n"))
    with pytest.raises(RotulosInvalidos):
        fh.parsear_avisos(contenido.replace("{assistant_name}. Ahora", "{nombre}. Ahora", 1))


@pytest.mark.parametrize(
    ("language", "esperado"),
    [("es", "es"), ("es-CO", "es"), ("ES_co", "es"), ("en-US", "en"), ("pt", "pt"), ("fr", "es"), (None, "es")],
)
def test_idioma_por_prefijo_y_lo_desconocido_cae_a_es(language, esperado) -> None:
    assert fh.idioma_del_negocio(language) == esperado


def test_nombre_solo_si_el_negocio_puso_uno() -> None:
    assert fh.texto_por_defecto("es-CO", None) == ES
    assert fh.texto_por_defecto("es", "  asistente ") == ES, "el nombre por defecto no se presenta"
    assert fh.texto_por_defecto("es", "Lupe").startswith("¡Hola! Soy Lupe. Ahora")
    assert fh.texto_por_defecto("en", "Ana").startswith("Hi! I'm Ana.")
    assert fh.texto_por_defecto("es", "{business_description}").startswith(
        "¡Hola! Soy {business_description}. Ahora"
    ), "el nombre es dato: no se vuelve a expandir"
    largo = fh.texto_por_defecto("es", "N" * 500)
    assert ("N" * fh.MAX_NOMBRE + ".") in largo and ("N" * (fh.MAX_NOMBRE + 1)) not in largo


class _Redis:
    def __init__(self, falla: bool = False) -> None:
        self.llaves: dict[str, int] = {}
        self.falla = falla

    async def set(self, llave, _valor, *, nx, ex):
        if self.falla:
            raise ConnectionError("redis caído")
        assert nx and ex == fh.AVISO_TTL_S
        if llave in self.llaves:
            return None
        self.llaves[llave] = ex
        return True

    async def delete(self, llave):
        self.llaves.pop(llave, None)


async def test_antirrebote_por_conversacion(monkeypatch) -> None:
    redis = _Redis()
    monkeypatch.setattr("core.memory.working.get_redis", lambda: redis)
    tid, conv, otra = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await fh.primer_aviso(tid, conv) is True
    assert await fh.primer_aviso(tid, conv) is False
    assert await fh.primer_aviso(tid, otra) is True, "la llave es por conversación"
    await fh.soltar_aviso(tid, conv)
    assert await fh.primer_aviso(tid, conv) is True
    assert all(k.startswith(f"fuera_de_horario:aviso:{tid}:") for k in redis.llaves)


async def test_sin_redis_avisa_igual(monkeypatch) -> None:
    monkeypatch.setattr("core.memory.working.get_redis", lambda: _Redis(falla=True))
    assert await fh.primer_aviso(uuid.uuid4(), uuid.uuid4()) is True

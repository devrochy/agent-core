"""HU-AOS-14 · El contenido de una habilidad no cambia sin subir su versión (ADR-0118 §1).

La versión registrada en `pack_versions` es inmutable y el build de releases exige la registrada
con el mismo `manifest_hash`: si cambia un playbook, el `flow.json` o las acciones de un pack sin
subir su versión, el sync del arranque no la registra y ningún negocio que lo use puede
construir releases. Este test lo frena antes del merge, contra el lock commiteado
(`packages/pack-versions.lock.json`, `python scripts/pack_versions_lock.py`).
"""

from __future__ import annotations

from core.releases.packs import (
    es_de_plataforma,
    leer_lock,
    manifiesto_de_pack,
    manifiestos,
    problemas_del_lock,
)


def _modulos() -> list[object]:
    from core.plugin_manager import plugin_manager

    return [m for m in plugin_manager.scan() if es_de_plataforma(m)]


def test_el_lock_coincide_con_el_binario() -> None:
    problemas = problemas_del_lock(leer_lock(), manifiestos(_modulos()))
    assert not problemas, "\n".join(problemas)


def test_el_manifiesto_de_un_pack_es_determinista() -> None:
    for modulo in _modulos():
        a, b = manifiesto_de_pack(modulo), manifiesto_de_pack(modulo)
        assert a.texto == b.texto and a.manifest_hash == b.manifest_hash


def test_problemas_del_lock_distingue_version_sin_registrar_y_contenido_cambiado() -> None:
    vigentes = manifiestos(_modulos())
    pid, man = next(iter(sorted(vigentes.items())))
    assert any("sin registrar" in p for p in problemas_del_lock({}, {pid: man}))
    assert any(
        "sin subir" in p for p in problemas_del_lock({pid: {man.version: "0" * 64}}, {pid: man})
    )
    assert problemas_del_lock({pid: {man.version: man.manifest_hash}}, {pid: man}) == []


def test_solo_los_packs_de_la_plataforma_entran_al_catalogo() -> None:
    assert _modulos(), "el binario trae habilidades de la plataforma"

    class _PluginDelNegocio:
        id = "plugin_privado"
        version = "1.0.0"

    assert not es_de_plataforma(_PluginDelNegocio())

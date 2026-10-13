"""Informe de una ejecución de pruebas (D-SPEC-30-5): JSON congelado y su versión Markdown.

El informe se arma una sola vez, al enviar la ejecución, y se guarda en
`qa_runs.informe`. Así lo que lee el dueño no cambia aunque después se importe
otra versión del catálogo.
"""

from __future__ import annotations

from typing import Any

ETIQUETA = {
    "ok": "OK",
    "falla": "Falla",
    "bloqueada": "Bloqueada",
    "bloqueado": "Bloqueado",
    "omitida": "Omitida",
    "omitido": "Omitido",
    "en_curso": "En curso",
    "sin_correr": "Sin correr",
}
_ORDEN = {"falla": 0, "bloqueada": 1, "en_curso": 2, "omitida": 3, "ok": 4}


def armar_informe(
    *,
    ciclo: dict[str, Any],
    catalogo: dict[str, Any],
    tester: dict[str, Any],
    iniciada_en: str,
    enviada_en: str,
    comentario: str | None,
    progreso: dict[str, int],
    items: list[dict[str, Any]],
    resultados: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """`items`: pruebas del catálogo (con `slug`, `titulo`, `pasos`…); `resultados`: por slug,
    `{estado, nota, pasos: {n: {estado, nota}}, adjuntos: {n: [{id, nombre, mimetype}]}}`."""
    corridas: list[dict[str, Any]] = []
    sin_correr: list[dict[str, Any]] = []
    for item in items:
        res = resultados.get(item["slug"]) or {}
        estado = res.get("estado") or "sin_correr"
        base = {"slug": item["slug"], "titulo": item["titulo"], "grupo": item.get("grupo") or ""}
        if estado == "sin_correr":
            sin_correr.append(base)
            continue
        pasos_res = res.get("pasos") or {}
        adjuntos = res.get("adjuntos") or {}
        pasos = []
        for n, paso in enumerate(item.get("pasos") or [], 1):
            marcado = pasos_res.get(n) or {}
            pasos.append(
                {
                    "n": n,
                    "accion": paso.get("accion") or "",
                    "esperado": paso.get("esperado"),
                    "estado": marcado.get("estado"),
                    "nota": marcado.get("nota"),
                    "adjuntos": adjuntos.get(n, []),
                }
            )
        corridas.append(
            {
                **base,
                "modulo": item.get("modulo"),
                "prioridad": item.get("prioridad"),
                "hu": item.get("hu") or [],
                "esperado": item.get("esperado"),
                "estado": estado,
                "nota": res.get("nota"),
                "pasos": pasos,
            }
        )
    corridas.sort(key=lambda p: _ORDEN.get(p["estado"], 9))
    return {
        "formato": 1,
        "ciclo": ciclo,
        "catalogo": catalogo,
        "tester": tester,
        "iniciada_en": iniciada_en,
        "enviada_en": enviada_en,
        "comentario": comentario,
        "progreso": progreso,
        "pruebas": corridas,
        "sin_correr": sin_correr,
    }


def _linea(texto: str | None) -> str:
    return " ".join((texto or "").split())


def informe_markdown(informe: dict[str, Any], enlace_adjunto: str = "") -> str:
    """Markdown del informe. `enlace_adjunto` es el prefijo de la URL de cada captura."""
    p = informe.get("progreso") or {}
    tester = informe.get("tester") or {}
    ciclo = informe.get("ciclo") or {}
    cat = informe.get("catalogo") or {}
    lineas = [
        f"# Informe de pruebas · {_linea(ciclo.get('nombre'))}",
        "",
        f"- **Tester:** {_linea(tester.get('nombre') or tester.get('email'))}",
        f"- **Catálogo:** versión {cat.get('version', '—')} ({_linea(cat.get('fecha'))})",
        f"- **Iniciada:** {informe.get('iniciada_en', '—')} · **Enviada:** {informe.get('enviada_en', '—')}",
        "",
        "| Total | Terminadas | OK | Falla | Bloqueada | Omitida | En curso | Sin correr |",
        "|---|---|---|---|---|---|---|---|",
        "| {total} | {terminadas} | {ok} | {falla} | {bloqueada} | {omitida} | {en_curso} | {sin_correr} |".format(
            **{k: p.get(k, 0) for k in (
                "total", "terminadas", "ok", "falla", "bloqueada", "omitida", "en_curso", "sin_correr"
            )}
        ),
    ]
    if informe.get("comentario"):
        lineas += ["", "## Comentario", "", informe["comentario"].strip()]
    pruebas = informe.get("pruebas") or []
    if pruebas:
        lineas += ["", "## Pruebas ejecutadas"]
    for prueba in pruebas:
        hu = f" · {', '.join(prueba['hu'])}" if prueba.get("hu") else ""
        lineas += [
            "",
            f"### {ETIQUETA.get(prueba['estado'], prueba['estado'])} · {_linea(prueba['titulo'])}",
            "",
            f"`{prueba['slug']}` · {_linea(prueba.get('grupo'))}{hu}",
        ]
        if prueba.get("nota"):
            lineas += ["", f"> {_linea(prueba['nota'])}"]
        lineas.append("")
        for paso in prueba.get("pasos") or []:
            marca = ETIQUETA.get(paso.get("estado") or "", "Sin marcar")
            lineas.append(f"{paso['n']}. **{marca}** · {_linea(paso['accion'])}")
            if paso.get("esperado"):
                lineas.append(f"   - Esperado: {_linea(paso['esperado'])}")
            if paso.get("nota"):
                lineas.append(f"   - Nota: {_linea(paso['nota'])}")
            for adj in paso.get("adjuntos") or []:
                nombre = _linea(adj.get("nombre")) or "captura"
                destino = f"{enlace_adjunto}{adj['id']}" if enlace_adjunto else adj["id"]
                lineas.append(f"   - Captura: [{nombre}]({destino})")
    sin = informe.get("sin_correr") or []
    if sin:
        lineas += ["", f"## Sin correr ({len(sin)})", ""]
        lineas += [f"- {_linea(s['titulo'])} (`{s['slug']}`)" for s in sin]
    return "\n".join(lineas) + "\n"


def cuerpo_correo(informe: dict[str, Any], enlace: str | None) -> str:
    p = informe.get("progreso") or {}
    tester = informe.get("tester") or {}
    fallas = [x for x in informe.get("pruebas") or [] if x["estado"] in ("falla", "bloqueada")]
    lineas = [
        f"{tester.get('nombre') or tester.get('email')} envió su informe del ciclo "
        f"«{(informe.get('ciclo') or {}).get('nombre')}».",
        "",
        f"Terminadas: {p.get('terminadas', 0)} de {p.get('total', 0)} · OK {p.get('ok', 0)} · "
        f"Falla {p.get('falla', 0)} · Bloqueada {p.get('bloqueada', 0)}",
    ]
    if fallas:
        lineas += ["", "Fallas y bloqueos:"]
        lineas += [f"- {ETIQUETA[f['estado']]}: {_linea(f['titulo'])}" for f in fallas[:20]]
        if len(fallas) > 20:
            lineas.append(f"- … y {len(fallas) - 20} más")
    if enlace:
        lineas += ["", f"Ver el informe completo: {enlace}"]
    return "\n".join(lineas) + "\n"

"""Plantillas HTML de los correos de SolversHub.

Los `.html` de `html/` son **generados** desde `src/*.mjml` con MJML: no editarlos a
mano. Para cambiar el diseño se edita el `.mjml` y se corre
`bash scripts/build_email_templates.sh` (ver `README.md`).

Los datos que llegan desde fuera (nombre del negocio, asunto, cuerpo del asistente)
se renderizan con autoescape de Jinja2: son datos, no instrucciones (invariante 7).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

_HTML_DIR = Path(__file__).parent / "html"

_env = Environment(
    loader=FileSystemLoader(str(_HTML_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
    auto_reload=False,
    trim_blocks=False,
    lstrip_blocks=False,
)

# Isotipo público (SVG no es compatible con Outlook/Gmail: se usa el PNG del sitio).
LOGO_URL = "https://www.solvershub.site/logo.png"

# Etiquetas de estado del informe de pruebas: color AA sobre blanco (tokens.contrast).
_COLOR_ESTADO = {"falla": "#B42E2E", "bloqueada": "#8A6100"}

# Enlace "agregar a mi calendario" que arma el pack de agenda (HU-CAL-05).
_ENLACE_CALENDARIO = re.compile(r"https?://(?:www\.)?calendar\.google\.com/calendar/render\?\S+", re.IGNORECASE)
_ROTULOS_DE_ENLACE = ("agregá", "agrega", "agregar", "agregala", "agrégalo", "añadí", "añade")


def render(template: str, **context: Any) -> str:
    """Renderiza `html/<template>.html` con `context` (autoescape activo)."""
    return _env.get_template(f"{template}.html").render(**context)


def _parrafos_html(texto: str) -> Markup:
    """Texto plano → párrafos HTML escapados, conservando los saltos de línea simples."""
    bloques = [b.strip() for b in (texto or "").replace("\r\n", "\n").split("\n\n") if b.strip()]
    partes: list[Markup] = []
    for bloque in bloques:
        lineas = Markup("<br />").join(escape(linea) for linea in bloque.split("\n"))
        partes.append(Markup('<p style="margin:0 0 12px;">') + lineas + Markup("</p>"))
    return Markup("").join(partes)


def _preview(texto: str, *, maximo: int = 140) -> str:
    linea = next((linea.strip() for linea in (texto or "").splitlines() if linea.strip()), "")
    return linea if len(linea) <= maximo else f"{linea[: maximo - 1].rstrip()}…"


def _separar_enlace_calendario(cuerpo: str) -> tuple[str, str]:
    """Saca el enlace de Google Calendar del texto para mostrarlo como botón.

    El correo de una cita lo trae como URL larga (``…render?action=TEMPLATE…``); como
    botón se lee mejor y conserva el mismo destino. Sin enlace, el texto queda igual.
    """
    encontrado = _ENLACE_CALENDARIO.search(cuerpo or "")
    if encontrado is None:
        return cuerpo, ""
    enlace = encontrado.group(0).rstrip(".,;:)]'\"")
    lineas: list[str] = []
    for linea in (cuerpo or "").replace("\r\n", "\n").split("\n"):
        if enlace not in linea:
            lineas.append(linea)
            continue
        resto = linea.replace(enlace, "").strip().rstrip(":").strip()
        # Si la línea era solo el rótulo del enlace ("Agregá la cita a tu calendario:"), se va.
        if not resto or resto.lower().startswith(_ROTULOS_DE_ENLACE):
            continue
        lineas.append(resto)
    limpio = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    return limpio, enlace


def _fallas_html(fallas: Iterable[dict[str, Any]], restantes: int = 0) -> Markup:
    partes: list[Markup] = []
    for falla in fallas:
        color = _COLOR_ESTADO.get(str(falla.get("estado")), "#B42E2E")
        etiqueta = escape(falla.get("etiqueta") or "Falla")
        titulo = escape(falla.get("titulo") or "")
        partes.append(
            Markup('<div style="margin:0 0 8px;font-size:14px;line-height:22px;color:#2B3648;">')
            + Markup(
                f'<span style="display:inline-block;min-width:88px;font-size:11px;line-height:18px;'
                f'font-weight:600;letter-spacing:0.6px;text-transform:uppercase;color:{color};">'
            )
            + etiqueta
            + Markup("</span> ")
            + titulo
            + Markup("</div>")
        )
    if restantes > 0:
        partes.append(
            Markup('<div style="margin:0;font-size:13px;line-height:19px;color:#616F88;">')
            + Markup(f"… y {restantes} más")
            + Markup("</div>")
        )
    return Markup("").join(partes)


def html_password_reset(*, codigo: str, minutos: int) -> str:
    return render("password_reset", logo_url=LOGO_URL, codigo=codigo, minutos=minutos)


def html_acceso(
    *,
    titulo: str,
    preview: str,
    eyebrow: str,
    saludo: str,
    mensaje: str,
    cta: str,
    enlace: str,
    detalle: str,
    aviso: str,
) -> str:
    return render(
        "access_invite",
        logo_url=LOGO_URL,
        titulo=titulo,
        preview=preview,
        eyebrow=eyebrow,
        saludo=saludo,
        mensaje=mensaje,
        cta=cta,
        enlace=enlace,
        detalle=detalle,
        aviso=aviso,
    )


def html_renovacion(
    *,
    titulo: str,
    mensaje: str,
    vence: str,
    monto: str,
    referencia: str,
    enlace: str,
) -> str:
    return render(
        "renewal",
        logo_url=LOGO_URL,
        titulo=titulo,
        preview=mensaje,
        mensaje=mensaje,
        vence=vence,
        monto=monto,
        referencia=referencia,
        enlace=enlace,
    )


def html_informe_qa(
    *,
    titulo: str,
    ciclo: str,
    resumen: str,
    terminadas: int,
    total: int,
    ok: int,
    falla: int,
    bloqueada: int,
    fallas: Iterable[dict[str, Any]],
    enlace: str | None,
    restantes: int = 0,
) -> str:
    fallas_html = _fallas_html(fallas, restantes)
    return render(
        "qa_report",
        logo_url=LOGO_URL,
        titulo=titulo,
        preview=_preview(resumen),
        ciclo=ciclo,
        resumen=resumen,
        terminadas=terminadas,
        total=total,
        ok=ok,
        falla=falla,
        bloqueada=bloqueada,
        fallas_html=fallas_html,
        enlace=enlace or "",
    )


def html_cita(
    *,
    asunto: str,
    accion: str,
    negocio: str,
    cita: str,
    fecha: str,
    hora: str,
    enlace: str = "",
) -> str:
    """Correo de una cita (agendada, reagendada o cancelada) con ficha y botón de calendario.

    Los datos son los de la cita, no el texto libre: así la ficha se lee igual sin importar
    cómo esté redactada la plantilla del negocio (que sigue siendo el respaldo en texto).
    """
    estado = {"confirmed": "Confirmada", "moved": "Reagendada", "cancelled": "Cancelada"}.get(
        accion, "Confirmada"
    )
    if accion == "cancelled":
        preview = f"Tu cita «{cita}» del {fecha} a las {hora} quedó cancelada."
        nota = "Si querés reagendarla, avisanos."
    else:
        preview = f"Tu cita «{cita}» es el {fecha} a las {hora}."
        nota = "Si necesitás cambiarla, respondé a este correo."
    return render(
        "appointment",
        logo_url=LOGO_URL,
        titulo=asunto,
        preview=preview,
        estado=estado,
        cita=cita,
        fecha=fecha,
        hora=hora,
        negocio=negocio,
        nota=nota,
        enlace=enlace,
    )


def html_generico(*, asunto: str, cuerpo: str) -> str:
    """Correo del asistente: el texto lo redacta el modelo y aquí recibe el diseño."""
    titulo = asunto.strip() or "Mensaje"
    texto, cta = _separar_enlace_calendario(cuerpo)
    return render(
        "generic",
        logo_url=LOGO_URL,
        titulo=titulo,
        preview=_preview(cuerpo) or titulo,
        cuerpo_html=_parrafos_html(texto),
        cta=cta,
    )


__all__ = [
    "html_acceso",
    "html_cita",
    "html_generico",
    "html_informe_qa",
    "html_password_reset",
    "html_renovacion",
    "render",
]

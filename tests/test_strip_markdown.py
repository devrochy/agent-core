"""`strip_markdown`: WhatsApp no interpreta Markdown -- un cliente real vio
mensajes con "**Pollo Broaster**" literal (asteriscos incluidos) en vez de
negrita. La regla ya está en el playbook (`reglas-operacion.md`), esto es el
respaldo determinístico: no depender solo de que el LLM la siga siempre.
"""

from __future__ import annotations

from core.orchestrator.loop import strip_markdown


def test_strips_bold():
    assert strip_markdown("¡Listo, **Robinson**! Tu pedido de **Pollo Broaster**.") == (
        "¡Listo, Robinson! Tu pedido de Pollo Broaster."
    )


def test_strips_single_asterisk_italic():
    assert strip_markdown("Esto es *importante* para vos.") == "Esto es importante para vos."


def test_strips_bold_with_price():
    assert strip_markdown("Total: **$60.000**") == "Total: $60.000"


def test_leaves_plain_text_untouched():
    text = "Hola, ¿qué querés pedir hoy?"
    assert strip_markdown(text) == text


def test_multiple_bold_segments_in_one_message():
    assert strip_markdown("**Pollo Broaster** — **$60.000**") == "Pollo Broaster — $60.000"


def test_does_not_touch_a_single_asterisk_used_as_bullet_or_multiplication():
    """Un solo `*` sin pareja de cierre no debe romper el resto del texto."""
    assert strip_markdown("2 * 3 = 6") == "2 * 3 = 6"

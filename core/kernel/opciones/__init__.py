"""Opciones directas (SPEC-40, ADR-0172): plantillas con opciones que ejecutan una tool sin LLM.

Este paquete es el NÚCLEO (HU-OPC-01): la definición, su lint, el mapeo puro a WhatsApp, el payload
firmado y la bandera por negocio. El resolver del toque, el envío, la API y la publicación a Meta son
de PRs posteriores y se montan encima sin cambiar nada de aquí.
"""

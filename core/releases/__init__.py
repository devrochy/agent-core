"""Releases por conexión (ADR-0117, HU-AOS-14): build, promoción, rollback y lectura con caché.

- `packs`: manifiesto y sync de las versiones de las habilidades (`pack_versions`).
- `manifiesto`: forma `release/1` y su hash (puro).
- `build`: candidata inmutable por conexión.
- `promocion`: promoción total, rollback en un clic, lectura.
- `cargar`: release congelada con caché en proceso.
- `release_uno`: backfill de la release 1 por conexión conversacional.
"""

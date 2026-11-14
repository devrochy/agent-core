## Voz y calidez con efecto real (2026-11-14)

- El playbook `tono.md` interpola `{tone}`, `{warmth}` y `{emojis}` y explica cada nivel: tono, calidez
  0–5 y emojis dejan de ser texto fijo y cambian el prompt de verdad.
- `sembrar_voz_del_negocio` publica una versión nueva del Coordinador cuando la persona del negocio
  cambió, antes de construir la release. El primer agente del onboarding hereda la voz elegida.
- La calidez se alinea al contrato 0–5 en panel y APK, con seis etiquetas (`Seco`…`Muy cálido`) y un
  preview que distingue Cálido (4) de Muy cálido (5).
- Tests nuevos de interpolación del prompt y de la siembra de voz; se regraban las grabaciones del gate
  de evals.

## Onboarding: el agente de la bienvenida no apaga el aviso (2026-11-20)

- HU-UX-25 / HU-AOS-84 (SPEC-49), agent-core#856. La bienvenida de 5 estaciones crea su agente al salir de la
  estación 1, y la regla `con_agente` (HU-SH-10) apagaba desde ahí el aviso persistente del panel y la
  banda de la APK. Ahora `recorrido.avisar` y `recorrido.abrir` solo se apagan con un agente propio
  **ajeno a la bienvenida**: no cuentan el Coordinador (`kind = 'core'`) ni el agente que guardó
  `dejar-listo` en `settings.onboarding.agente_id`. Un agente creado por «Nuevo agente», o antes de
  existir el recorrido, sigue apagando la insistencia aunque el recorrido esté omitido. Sin migración ni
  cambio de contrato (solo las descripciones de `abrir` y `avisar` en el OpenAPI).

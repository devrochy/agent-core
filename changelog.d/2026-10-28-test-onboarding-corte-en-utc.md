## Prueba del corte de onboarding: el «mañana» ahora se calcula en UTC (2026-10-28)

- `test_abre_solo_al_propietario_y_los_negocios_anteriores_al_corte_no_aplican` fijaba el corte con
  `date.today() + 1 día` (hora local del runner), pero `aplica_al_negocio` compara con la fecha UTC de
  `created_at`. Entre las 00:00 UTC y el fin del día local el negocio recién creado seguía «aplicando» y
  el job `integracion` salía rojo. Solo cambia la prueba; el código de producción no se toca.

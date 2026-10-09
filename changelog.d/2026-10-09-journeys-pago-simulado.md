## Los journeys vuelven a cobrar con la pasarela simulada (2026-10-09)

- Desde #324, `pack_payments` llama a `build_provider(config, http=…)`. El reemplazo de los journeys
  (`PagoSimulado._proveedor`) no aceptaba `http`: la tool fallaba, el core apagaba `pack_payments` para el
  negocio y todas las `sub_*` desaparecían del flujo. El primer nightly con k = 4 dio pass@1 43 % por eso.
  Ahora el simulado tiene la misma firma y un test compara las dos. Medición local del set: 9/11.

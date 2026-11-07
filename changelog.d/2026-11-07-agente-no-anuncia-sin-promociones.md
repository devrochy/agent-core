## El agente no dice que no hay promociones (2026-11-07)

- `aplicar_promocion` ya no devuelve las frases «No hay promociones vigentes aplicables» ni «No aplicó ninguna promoción», que el modelo repetía al cliente. Devuelve datos (`applied: false`, `motivo` = `sin_vigentes` o `no_aplica`, sin la palabra «promociones» para no invitar al modelo a decirla, y el `subtotal`/`total` de la cotización, que la primera rama no traía).
- El playbook `ventas` (pack_commerce 3.1.0) dice cuándo hablar de promociones: solo si `aplicar_promocion` devolvió `applied: true`. Sin una vigente, el agente no saca el tema ni dice que no existen; si el cliente pregunta, responde con naturalidad y lo orienta a lo que sí ofrece el negocio.
- El playbook cambia el prompt de los negocios con `pack_commerce` y sin override de `ventas`: las grabaciones de evals (`evals/cassettes/`) hay que regrabarlas con OK del dueño. Los negocios con `ventas` propio (p. ej. `solvershub-comercial`) no reciben la regla.

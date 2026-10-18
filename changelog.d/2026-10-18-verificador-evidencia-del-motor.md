## El verificador acepta los datos que fijó el motor en el turno (2026-10-18)

- Verificador (ADR-0119) con el motor de procedimientos activo: una afirmación de cierre también la respalda un
  dato de la tarea en foco que el motor fijó en ESTE turno por un `actualizar_tarea` aceptado (todo o nada,
  validado y, si es personal, dicho por el cliente), solo si la afirmación declara ese dato
  (`Afirmacion.datos`, campo nuevo y opcional). No respalda montos, links ni otras afirmaciones; sin el comando,
  o con uno rechazado, sigue escalando.
- `pack_commerce`: `dato_de_contacto_guardado` declara los datos de contacto de sus procedimientos ("Anoté tu
  dirección" tras fijarla escalaba el 75 % de los pedidos a domicilio en v3) y `cotizacion_generada` también la
  respalda `cotizar_plan` (la cotización de una unidad de `contratar_plan`).

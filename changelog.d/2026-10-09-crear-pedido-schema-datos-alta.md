## Datos del alta en el schema de `crear_pedido` (2026-10-09)

- `crear_pedido` (pack-commerce) declara en `customer_data` los campos opcionales `business_name`,
  `vertical` (alta automática, ADR-0121) y `acepta_terminos` (el token de `terminos_pendientes`,
  ADR-0125). Antes el modelo los mandaba solo por el `hint` y el playbook de ventas. Van sin enum: las
  opciones de `vertical` siguen en la respuesta y el schema no cambia con los presets (ADR-0129).
- La tool sigue validando: token de términos atado a la conversación y a las versiones vigentes,
  `vertical` como id de un preset y `business_name` sin enlaces.
- Tests: el schema los declara opcionales y sin enum (reemplaza el test que fijaba lo contrario); el token de términos de un vendedor no vale en
  otro (`aislamiento`).

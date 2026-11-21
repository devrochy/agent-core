## Captar leads: reglas del agente y campo «negocio» (2026-11-21, HU-AOS-70 parte 4)

- Playbook del kernel `captar-leads`: el agente confirma el dato con el cliente, pregunta antes de reemplazar uno que
  contradice y nunca lo deduce. Entra al prompt de todo agente con alguna acción de Captar leads encendida, tenga o no
  comercio (invariante 3: el texto vive en el `.md`).
- `actualizar_lead` acepta `negocio` (el negocio del cliente): se guarda en `custom` del lead, sin migración, y se copia
  al perfil con los `writeNamespaces` del agente. La lista de Clientes del agente `leads` lo muestra en la columna
  «Negocio», y un lead que solo tiene el negocio ya cuenta «con datos».
- El intake público también guarda el negocio del formulario en ese campo (el interés sigue igual).
- La config de Captar leads trae títulos en español: el panel muestra «Secreto del formulario», «Llave secreta de
  Turnstile», etc., en vez de los nombres técnicos.
- Evals: aserción `tool_arguments_absent` en el golden, spec `captar-leads` (Suscripciones sin comercio) y escenarios
  40 (no deduce datos) y 41 (pregunta antes de reemplazar el correo), sumados al gate. Grabaciones del gate regrabadas.
- `ventas.md` remite a las reglas de Captar leads ante un dato que contradice uno guardado (pack_commerce 4.5.0).
- No hace falta republicar: el bloque sale del binding de la versión y del playbook que viaja con el binario.

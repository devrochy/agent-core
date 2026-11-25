## Cobranza declara sus datos del cliente (HU-AOS-93, ADR-0198) (2026-11-25)

- El agente de Cobranza declara la operación `cobranza.promesa_de_pago` (documento recomendado; teléfono, correo y
  dirección sin pedir) y la ejecutan `cob_registrar_promesa_pago` y `cob_registrar_promesa_saldo`. Con eso los cuatro
  especialistas (Ventas, Agenda, Cobranza y Suscripciones) tienen sus operaciones en `GET/PUT
  /admin/agents/{id}/datos-cliente` y en `consultar_datos_cliente`. Los defaults no bloquean la promesa: el cliente de
  cartera ya existe. Sin migraciones ni cambios de contrato.

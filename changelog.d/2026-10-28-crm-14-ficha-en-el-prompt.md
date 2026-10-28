## Ficha de la relación del cliente en el prompt y tool de historial (HU-CRM-14, ADR-0179)

- Cada módulo (Ventas, Agenda, Suscripciones, Cobranza) aporta `LeadRelacion.ficha` y `detalle`. La ficha es una línea corta por módulo (p. ej. «2 pedido(s); el último OC-0012 está entregado»), sin montos, documento, correo ni dirección.
- `PromptContext.relacion`: la ficha entra al bloque dinámico después de lo que sabemos del cliente, nunca al prefijo cacheado. Topes: 4 módulos, 240 caracteres por línea, 1.600 el bloque, 300 ms por módulo (un módulo lento o roto se omite y el turno sigue). Solo módulos habilitados para el negocio. Rótulos en el playbook del kernel `relacion-con-el-negocio`; cada línea se escapa y delimita como dato.
- Nueva tool de solo lectura `consultar_historial_cliente(modulo?, limite ≤ 10)`: detalle del cliente de la conversación (nunca un id que mande el modelo), con importes en las claves de dinero del core donde existen.

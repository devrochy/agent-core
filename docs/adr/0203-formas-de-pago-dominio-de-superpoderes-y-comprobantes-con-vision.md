# ADR-0203 — Formas de pago: dominio de superpoderes y comprobación de transferencias con visión

- **Estado:** aceptado (plan aprobado por el dueño el 2026-11-17, SPEC-48).
- **Fecha:** 2026-11-17
- **Usa:** ADR-0113 (procedimientos), ADR-0118 (procedimientos por agente), ADR-0190 (habilidades por agente y `config_agente`), ADR-0194 (`reconocer_imagen`).
- **Enmienda:** ADR-0113 y ADR-0118 solo en la vista del editor; el contrato `procedure/1` no cambia.

## Contexto

Las formas de pago de un pedido están fijas en `pack_commerce.tomar_pedido`. «Pago en línea» se ve
como una habilidad transversal suelta, el dueño no puede activar o desactivar métodos y nadie lee el
comprobante de una transferencia: el pedido queda `pendiente` hasta que una persona lo revisa.

## Decisión

1. **Dominios de superpoderes.** Cada pack declara `grupos_de_procedimientos` (`{id del procedimiento:
   clave}`) y `etiquetas_de_grupos_de_procedimientos` en su `BaseModule`. El editor expone
   `Superpower.group` / `group_label`; un procedimiento sin grupo cae en `otros`. El grupo es metadato
   de la vista: no se guarda en el AgentSpec ni en el JSON del procedimiento.
2. **«Formas de pago» es un dominio del agente de Ventas** con dos superpoderes: `pack_commerce.pago_en_linea`
   (pedidos existentes: reenvío, link vencido, «ya pagué») y `pack_commerce.comprobar_transferencia`.
   «Contra entrega» es una forma sin procedimiento. `tomar_pedido` conserva el link al cerrar la compra.
3. **Formas configurables por agente** en `CRMConfig.delivery_payment_forms` y cuentas en
   `transfer_accounts`, ambos en `config_agente` (JSON, sin migración). El agente recibe solo las
   etiquetas de las formas activas; las cuentas salen por `_safe_append`, nunca por el texto del LLM.
4. **Comprobación con visión LLM**, sin OCR tradicional: la acción `comprobar_comprobante` lee la última
   imagen entrante de la conversación, llama a `reconocer_imagen` (playbook de kernel
   `comprobar_comprobante.md`) y una función pura `evaluar_comprobante` decide. Se marca `pagado` solo
   con monto exacto + cuenta (últimos 4 + entidad) + estado exitoso + fecha posterior al pedido + no
   duplicado; si no, `en_revision` o `rechazado`.
5. **Evidencia e idempotencia.** Cada comprobante se guarda en `pack_commerce_payment_proofs`
   (RLS forzada); la acción es idempotente por `(tenant_id, order_id, image_sha256)`. La cuota de visión se
   valida antes de llamar; sin cuota se escala sin cobrar. El texto de la imagen es dato no confiable.

## Consecuencias

- El panel agrupa superpoderes por dominio sin cambiar el contrato; los clientes viejos ignoran los
  campos nuevos.
- `payment_status` suma `en_revision` (aditivo, sin backfill).
- Riesgo de falso positivo acotado por la regla determinista; la decisión nunca sale del LLM.

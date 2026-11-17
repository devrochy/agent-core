## Formas de pago por agente (2026-11-17, HU-COM-21)

- `CRMConfig.payment_forms` (contra entrega, transferencia, pago en línea, con interruptor e instrucciones), `transfer_proof_required` y `transfer_ocr_enabled`; los cuatro y `transfer_accounts` son de cada agente (`config_agente`). Sin configurar se deduce como antes.
- Nuevo `pack_commerce/formas_de_pago.py`: estado de cada forma («Activa», «Falta configurar», «Apagada») y métodos que el agente puede ofrecer. Transferencia sin cuenta o pago en línea sin proveedor no se ofrecen.
- `GET/PUT /api/v1/pack/commerce/agents/{agent_id}/payment-forms`. El resolver `metodo_de_pago` y `crear_pedido` solo aceptan formas activas; el bloque de cuentas usa las del agente.
- Referencia del turno `formas_de_pago` (solo ids y si/no, nunca cuentas) y regla en `playbooks/ventas.md`.
- Las cuentas guardadas se sanean al cargar sin tumbar la config; la validación estricta es la del PUT.

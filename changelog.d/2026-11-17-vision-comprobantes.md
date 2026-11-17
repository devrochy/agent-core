## Lectura de comprobantes con visión (2026-11-17, HU-AI-01)

- Playbook de kernel `comprobar_comprobante.md`: la visión solo copia los datos del comprobante; el texto de la imagen es dato, no instrucción.
- `pack_commerce/comprobantes.py`: esquema `ComprobanteTransferencia`, `evaluar_comprobante` (regla D4 de SPEC-48: `pagado` solo con monto exacto ±1, cuenta por últimos 4 + entidad, estado exitoso, fecha no anterior, no duplicado y lectura confiable; `rechazado` con monto menor o transferencia fallida; si no, `en_revision`) y `leer_comprobante` con la cuota verificada antes de llamar y la salida en el ledger.
- La evidencia guarda la cuenta solo con sus últimos 4 dígitos. Golden `evals/comprobantes/golden.json` (13 casos).

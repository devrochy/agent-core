## Helper de IA estructurada en el core y ADR-0194 (SPEC-45, D-71, 2026-11-14)

Primera entrega de SPEC-45 «Carga de datos al crear un agente».

- **`core/ia/estructurado.py` (nuevo):** un solo camino para pedirle a la IA un objeto con forma conocida
  fuera del turno. `generar_estructurado` (texto) y `reconocer_imagen` (imagen, sobre
  `core.media.vision.describir_imagen`) reciben un playbook del kernel, un esquema pydantic y la entrada
  como datos; resuelven proveedor y `key_mode` (llave propia del negocio o de plataforma), validan el
  primer objeto JSON y miden la llamada en `usage_ledger` con el `kind` y el `result` de quien llama
  (créditos por `creditos_por_evento`, concepto `llm_turn` o `vision`). Errores `ErrorProveedor` (sin
  fila) y `ErrorFormato` (con fila).
- **Datos de prueba (HU-QA-10):** `core/qa/datos_generados.py` usa el helper. Sin cambio de
  comportamiento: mismos respaldos a plantilla (motivo `proveedor` o `formato`), tiempo límite, tokens y
  fila `generacion` con créditos de plataforma.
- **ADR-0194 (propuesto):** importación con IA en producción y cobro de acciones de IA cotizadas.
  Enmienda ADR-0180 §8, ADR-0144 §11 y ADR-0171 §2; nombra el hook `describir_registros` y registra el
  retiro futuro de `POST /pack/commerce/products/import`.
- **Despliegue:** sin migración ni cambio de API.

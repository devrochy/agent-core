## Agenda captura el lead del prospecto (HU-AOS-22, adenda de ADR-0139)

- La plantilla `agenda` suma `pack_commerce` como habilidad opcional, acotada a `crear_lead` con el campo
  nuevo `acciones_opcionales` de `PlantillaAgente`. Si el negocio tiene `pack_commerce`, Agenda registra el
  lead de quien se presenta y pide una demo. No recibe la cotización, el pedido, el pago ni el playbook `ventas`.
- `derivar_contenido` aplica el filtro: de una opcional con `acciones_opcionales` solo entran esas acciones (y
  avisa si una no es de esa habilidad). Una opcional sin entrada sigue entrando completa (Ventas con
  `pack_payments` no cambia). El editor de agentes usa la misma derivación.
- Un cliente que ya es lead no se duplica: `crear_lead` responde `ya_existe`.
- **Compatibilidad:** el cambio llega solo a los Agenda que se creen desde ahora (`presets-v2 --aplicar` en un
  negocio sin Agenda, o el editor). Re-aplicar el preset no publica sobre una Agenda existente (ADR-0139 §6).
  En un negocio que ya la tiene, se suma el binding `crear_lead` al borrador de Agenda (por la API: el panel
  todavía no edita `tools`) y se publica.
- Sin migración y sin cambios en el loop ni en el router. Con el ruteo apagado nada cambia.

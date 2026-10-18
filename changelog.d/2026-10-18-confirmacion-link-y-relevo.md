## Confirmar pidiendo el link y relevo por tool en los journeys (2026-10-18)

- Léxico de confirmación (`core/playbooks/kernel/confirmacion.md`, `## cortesia`): pedir el paso siguiente
  ("pasame/mandame/enviame el link (de pago)", "espero el link") ya no impide confirmar. "Sí, confirmo. Pasame el
  link de pago" confirma; el núcleo sigue siendo obligatorio, así que "Pasame el link", "Sí, pasame el link", "no,
  pasame el link" o "¿me pasás el link?" no confirman. Casos nuevos en el set etiquetado (precisión 1,0).
- Journeys: un `escalate_to_human` exitoso termina la conversación simulada como «relevo» (en el canal `test` la
  tool no pausa el bot y el intento llegaba al tope de turnos); el motivo dice `tool=escalate_to_human`.

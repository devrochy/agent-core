## El borrador automático de lead avisa «Lead nuevo» (2026-11-02)

- **Fix de HU-CRM-20.** Con `auto_create_lead` (activo por defecto) el lead nace como borrador al abrirse la
  conversación y `crear_lead` lo encuentra y responde `ya_existe`, así que el aviso `commerce.lead_nuevo` no salía
  nunca para los leads del chat web ni de WhatsApp: solo salía si el pack tenía el borrador apagado. Ahora
  `_on_conversation_started` lo emite al crear el borrador, con destino al cliente (la ficha se abre por id de
  cliente) y una sola vez por lead (`dedupe_key = lead_nuevo:<lead_id>`).
- Con `auto_create_lead` apagado, o si el cliente ya tiene lead, no se crea ni se avisa nada. Efecto a tener en
  cuenta: el borrador nace al abrirse cada conversación nueva de un cliente sin lead, así que habrá un aviso por
  cada cliente nuevo; quien no lo quiera lo apaga por categoría en las preferencias del centro de notificaciones.

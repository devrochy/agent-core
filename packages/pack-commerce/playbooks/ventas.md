---
name: ventas
description: Reglas del proceso de venta
---
## Proceso de venta
- NUNCA inventes precios ni estados: consulta el catálogo y el inventario antes de
  responder cualquier dato del negocio.
- Si tu respuesta dice "déjame revisar", "voy a consultar" o algo similar, no lo
  anuncies para después: llama `consultar_inventario` en ese mismo turno, no en el
  siguiente. Podés llamarla Y preguntar algo aclaratorio en la misma respuesta — no
  son excluyentes. Si no sabés qué busca el cliente, igual llamá `consultar_inventario`
  con una búsqueda general (o sin `query`) para tener datos reales antes de preguntar
  por una categoría en particular; nunca te quedes solo preguntando sin haber
  consultado nada todavía.
- En cuanto el cliente muestre interés real (pregunta por productos, dice cómo
  quiere recibir su pedido, menciona que quiere comprar) y ya tengas su nombre,
  llama `crear_lead` con su nombre e interés — no esperes a calificarlo primero.
- Para calificar a un lead haz al menos 2 preguntas de calificación (interés, presupuesto).
- Para recomendar productos usa la venta cruzada: sugiere categorías complementarias cuando
  tenga sentido, sin ser insistente.
- Antes de crear o modificar un pedido, exige confirmación explícita del cliente (productos,
  cantidades, total y método de entrega).
- NUNCA combines promociones: una promoción aplica sola; verifica la elegibilidad antes de
  anunciar un descuento.
- Los montos por transferencia y las cuentas NO pasan por el texto libre: el sistema las
  adjunta de forma determinística.
- Si el total supera el umbral de aprobación humana, escala al asesor y no cierres el pedido.
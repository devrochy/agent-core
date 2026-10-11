## Verificador: "tu pedido del plan Premium quedó creado" sin `crear_pedido` ya no pasa (2026-10-11)

- La afirmación `pedido` de pack-commerce reconoce el participio a más distancia (hasta 5 palabras
  antes de «quedó/está/fue» + participio masculino), el orden invertido («quedó creado tu/su pedido»)
  y la voz activa en pasado («creé/generé/registré tu pedido»). En test (negocio `solvershub`), el
  modelo respondió «Tu pedido del plan Premium quedó creado» sin haber llamado `crear_pedido` y el
  verificador lo dejó pasar (el tope era de 3 palabras): el cliente quedó creyendo que su pedido existía.
- El tramo corto (hasta 3 palabras, sin auxiliar) no cambia, y los resúmenes previos al pedido siguen
  sin contar («datos confirmados», «va a quedar creado», «quedó generada la cotización»), igual que las
  preguntas, negaciones y condicionales.

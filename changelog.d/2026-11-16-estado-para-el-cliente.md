## El agente no le dice «trialing» al cliente (2026-11-16)

- `sub_inscribir` y las tools que devuelven la suscripción (`sub_consultar` y las que usan la misma vista) traen `estado_para_el_cliente` con el estado en palabras simples («en período de prueba», «pendiente de pago»…), además del código `estado`. En la prueba de contratación del Free en test el cierre decía «Estado: trialing (prueba activa)».
- Sin cambio en las descripciones de las tools ni en los playbooks: las grabaciones de replay no cambian.

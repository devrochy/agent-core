## ADR-0186: ajustes del modo Sandbox al implementar (2026-11-15)

- HU-CONN-20: la adenda pasa a decir que solo un mensaje entrante firmado confirma la conexión sandbox (un estado no prueba que las URL se pegaron en la consola) y que el conteo de conexiones sin confirmar va en el log `twilio_salud_ronda`, no en un gauge. Solo documentación: el código ya lo hace así (#788 a #791).

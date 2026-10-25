## `tenant purge` y `tenant create` en un solo event loop (2026-10-25)

- `agent-core tenant purge` fallaba siempre con `RuntimeError: ... attached to a different loop` y no borraba
  nada: validaba con un `asyncio.run` y purgaba con otro, y el pool asyncpg quedaba atado al primer loop. Ahora
  valida, pide la frase (`questionary` con `ask_async`) y purga en un solo `asyncio.run`, y al final cierra Redis
  y desecha el engine. `tenant create` tenía el mismo patrón (alta y seed de packs en loops distintos) y se
  corrige igual. Test de integración nuevo que corre los dos comandos por `CliRunner`.

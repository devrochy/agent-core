# Prompt 1 — Inventario (modo plan)

> Activa modo plan (Shift+Tab dos veces) antes de pegar esto.
> No apruebes nada hasta leer el plan completo.

---

Lee `CLAUDE.md` y `docs/adr/`. Después analiza TODO el repositorio y produce
`docs/inventory.md` siguiendo la plantilla que ya existe en ese fichero.

Clasifica cada archivo de código en una de cuatro categorías:

- **CORE** — lo necesitan >=2 verticales, no menciona el dominio del cliente, su ausencia
  rompe el runtime.
- **PACK** — nombra una entidad de nicho (calzado, motos, citas), su tabla llevaría prefijo
  natural, un tenant podría no tenerlo.
- **CONFIG** — es un valor, umbral, texto o prompt que un cliente cambiaría solo.
- **APP** — UI o rutas del panel.

Presta atención especial a tres cosas:

1. **Todo prompt de sistema que viva en un archivo `.ts`.** Lístalos en la tabla
   correspondiente y clasifica cada uno como *identidad* (va a `AgentSpec`) o
   *procedimiento* (va a un `SKILL.md` de pack).
2. **Queries sin filtro de tenant** y tablas sin RLS. Anótalas como riesgo alto.
3. **Lógica que hoy es de CULTO pero es genérica**: máquina de estados de escalamiento,
   ledger de inventario, auditoría de pagos, segmentación de clientes, configuración de
   identidad del asistente. Estas son candidatas fuertes a CORE.

Al final del documento reporta:
- Porcentaje de líneas en cada categoría.
- Los 10 archivos con mayor riesgo de migración y por qué.
- Las decisiones que no pudiste tomar solo y necesitan mi criterio.

**No muevas, edites ni crees ningún archivo de código. Solo `docs/inventory.md`.**

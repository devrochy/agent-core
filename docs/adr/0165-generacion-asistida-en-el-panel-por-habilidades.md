# ADR-0165 — Generación asistida en el panel por habilidades

- **Estado:** propuesto. Se acepta al firmarse el piloto (D-31 de solvershub-docs) **y** cuando aparezca un segundo consumidor (decisión del dueño, 2026-10-24).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-37 (texto del aviso). **Usa:** ADR-0146 §3 y §7 (generación del core fuera del turno), ADR-0079 (rutas admin de packs) y ADR-0017.

## Contexto

El asesor quiere que el panel le proponga el título y la descripción del aviso con los datos registrados. Hoy el único precedente de generación con LLM fuera del turno es del core: ADR-0146, `core/qa/datos_generados.py`, con el playbook del kernel `load_core_playbook`, proveedor de plataforma, `kind='generacion'` y solo en test. Un pack no tiene cómo cargar su playbook fuera del turno ni un servicio de generación que pueda llamar desde su ruta admin.

## Decisión

El core ofrece un servicio de **generación asistida** que un pack invoca desde su ruta admin.

1. **Prompt y llamada.**
   - El prompt vive en un playbook `SKILL.md` del pack (invariante 3) y se carga con un cargador fail-closed equivalente a `load_core_playbook`, limitado a los playbooks declarados por el pack.
   - El pack pasa playbook, esquema pydantic de salida y datos.
   - El core hace una llamada `generate` sin tools, valida la salida contra el esquema y la devuelve.
2. **Los datos van como datos, nunca como instrucción.** Los atributos que vienen de un propietario son dato no confiable (invariante 7).
3. **Costo y llave.**
   - Fila `kind='generacion'` en `usage_ledger`, sin migrar `ck_usage_kind`, con `result={"proposito": <pack>.<uso>}`.
   - Créditos con `creditos_por_evento`.
   - Llave de plataforma, o la del negocio si tiene BYOK.
4. **Nada se publica solo.** El resultado es una propuesta que el humano edita y confirma.
5. **Lenguaje prohibido.** La lista de la plataforma y la del negocio viven en configuración o playbook, no en `.py`. El chequeo del servidor marca lo que no se puede publicar.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Que el pack llame al proveedor LLM directamente | Se saltaría la medición, la llave y los créditos, y duplicaría el manejo de proveedores |
| Usar el turno del agente para generar el texto | Mezcla una acción del panel con la conversación del cliente |

## Consecuencias

**Positivas:** descripciones de inmuebles, de productos de commerce o textos de campañas desde el panel, con costo medido y sin copy en el código.

**Negativas / lo que aceptamos pagar:** un servicio del core más, con su cargador de playbooks de pack.

**Qué haría que revisáramos esto:**
- Que no aparezca un segundo consumidor antes de la fase 3 de SPEC-28: entonces HU-INMO-37 se difiere y este ADR queda propuesto.
- Que un consumidor necesite tools o varios pasos: eso sería un agente de back-office (HU-AOS-30), no generación asistida.

## Impacto en el código

- `core/generacion/` (nuevo servicio).
- `core/agents/playbooks.py` (cargador de playbooks de pack).
- Pack inmobiliario (ruta admin y playbook).

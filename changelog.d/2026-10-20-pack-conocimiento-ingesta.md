## Pack de conocimiento, parte 2: ingesta de PDF, DOCX y TXT (HU-AOS-49, ADR-0145)

- **Nueva API del pack, en `/api/v1/pack/pack_knowledge/agents/{agent_id}/documents`:**
  - **subir** (multipart, hasta 20 MB): PDF, DOCX, TXT o Markdown. Responde 202 y el documento queda
    `pendiente`;
  - **listar** cada documento con su estado: `pendiente`, `procesando`, `listo` o `fallido`, este con
    un código como `sin_texto`, `formato` o `muy_grande`;
  - **borrar:** el borrado es lógico, vacía el contenido y elimina el archivo;
  - **reprocesar.**
  - Errores: 404 desde otro negocio, 409 `documento_duplicado` y 409 `plan_limit_documentos` /
    `plan_limit_conocimiento_mb`.
- **Worker:** el pack registra dos tareas.
  - `pack_knowledge.extraer`: extrae el texto con `pypdf` o `python-docx`, lo trocea en ~400 tokens con
    solape y cuenta datos personales para el aviso.
  - `pack_knowledge.embeber`: embebe lotes de 100, en cadena.
  - Fuera de producción los embeddings son `hash`, sin costo.
- **Core:**
  - `register_task`, para tareas puntuales de un pack;
  - `contar_pii`;
  - mensajes del 409 de los topes de conocimiento.
- **Purga de un negocio:** ahora también borra su carpeta `TENANTS_ROOT/<slug>/`. Si un negocio nuevo
  usa un slug con residuos de documentos o evidencias, esos residuos van a `TENANTS_ROOT/.purgados/`.
- Los agentes todavía no usan los documentos: la búsqueda y la tool `buscar_conocimiento` llegan en la
  parte 3.

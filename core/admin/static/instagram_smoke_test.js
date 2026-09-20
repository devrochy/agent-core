/* Prueba de humo de Instagram: el toggle "Allow access to messages" es un
 * fallo TOTALMENTE SILENCIOSO si está apagado (Meta no devuelve ningún
 * error) -- esto es el único diagnóstico fiable. Polling cada 3s, hasta
 * 90s; si no llega nada, muestra el checklist manual (paso 2).
 */
(function () {
  "use strict";

  const POLL_INTERVAL_MS = 3000;
  const TIMEOUT_MS = 90000;

  document.querySelectorAll("[data-ig-diagnose-button]").forEach(function (button) {
    const connectionId = button.dataset.connectionId;
    const igUserId = button.dataset.igUserId;
    const statusEl = document.querySelector('[data-ig-diagnose-status="' + igUserId + '"]');
    const checklistEl = document.querySelector('[data-ig-checklist="' + igUserId + '"]');

    function setStatus(text) {
      if (statusEl) statusEl.textContent = text;
    }

    button.addEventListener("click", function () {
      button.disabled = true;
      if (checklistEl) checklistEl.classList.add("d-none");
      setStatus("Enviá un DM de prueba a esta cuenta desde OTRO perfil de Instagram ahora. Esperando hasta 90s...");

      const startedAt = Date.now();
      const url = "/admin/conexiones/meta/" + connectionId + "/instagram/" + igUserId + "/diagnostico-estado";

      function poll() {
        fetch(url)
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data && data.llego) {
              setStatus("✅ Llegó el mensaje de prueba. El webhook está funcionando.");
              button.disabled = false;
              return;
            }
            if (Date.now() - startedAt >= TIMEOUT_MS) {
              setStatus("❌ No llegó ningún mensaje en 90s. Revisá el paso 2 del checklist (toggle silencioso).");
              if (checklistEl) checklistEl.classList.remove("d-none");
              button.disabled = false;
              return;
            }
            setTimeout(poll, POLL_INTERVAL_MS);
          })
          .catch(function () {
            setStatus("No se pudo consultar el estado -- reintentando...");
            setTimeout(poll, POLL_INTERVAL_MS);
          });
      }
      poll();
    });
  });
})();

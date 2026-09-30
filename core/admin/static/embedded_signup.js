/* Wizard de Embedded Signup de WhatsApp (vanilla JS, sin build step -- mismo
 * criterio que table.js). Carga el SDK de Meta, lanza FB.login() exactamente
 * como pide la doc de 2026 (sin featureType/sessionInfoVersion, esos son de
 * flujos de Solution Partner con solutionID), escucha los 3 eventos
 * terminales del popup vía postMessage, y canjea el `code` INMEDIATAMENTE
 * (TTL de 30s, nada de colas).
 */
(function () {
  "use strict";

  const root = document.getElementById("embedded-signup-root");
  if (!root) return;

  const wizardId = root.dataset.wizardId;
  const configId = root.dataset.configId;
  const appId = root.dataset.appId;
  const graphVersion = root.dataset.graphVersion;

  function loadFacebookSdk(onReady) {
    if (window.FB) {
      onReady();
      return;
    }
    window.fbAsyncInit = function () {
      window.FB.init({ appId: appId, cookie: true, xfbml: true, version: graphVersion });
      onReady();
    };
    const script = document.createElement("script");
    script.src = "https://connect.facebook.net/en_US/sdk.js";
    script.async = true;
    script.defer = true;
    script.crossOrigin = "anonymous";
    document.body.appendChild(script);
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return response.json().catch(function () {
      return {};
    });
  }

  function setStatus(message, isError) {
    const el = document.getElementById("embedded-signup-status");
    if (!el) return;
    el.textContent = message;
    el.className = isError ? "text-danger small mt-2" : "text-secondary small mt-2";
  }

  function fbLoginCallback(response) {
    if (!response || response.authResponse === undefined) {
      setStatus("No se recibió respuesta del diálogo de Meta.", true);
    }
    // El code real llega por postMessage (WA_EMBEDDED_SIGNUP), no acá --
    // esto solo confirma que el diálogo se cerró.
  }

  function launchSignup() {
    if (!window.FB) {
      setStatus("El SDK de Meta todavía no cargó, esperá un segundo y probá de nuevo.", true);
      return;
    }
    // Verbatim de la doc de Meta -- NO agregar featureType ni
    // sessionInfoVersion (solo aplican a flujos de Solution Partner con
    // solutionID, no a este).
    window.FB.login(fbLoginCallback, {
      config_id: configId,
      response_type: "code",
      override_default_response_type: true,
      extras: { setup: {} },
    });
  }

  window.addEventListener("message", function (event) {
    if (!event.origin || !event.origin.endsWith("facebook.com")) return;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      return; // no todo lo que llega por postMessage es JSON nuestro
    }
    if (!data || data.type !== "WA_EMBEDDED_SIGNUP") return;

    const eventName = data.event;
    const payload = data.data || {};

    if (eventName === "FINISH" || eventName === "FINISH_ONLY_WABA" || eventName === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") {
      setStatus("Conectando con WhatsApp...", false);
      postJson("/admin/conexiones/wizard/whatsapp/exchange", {
        wizard_id: wizardId,
        code: payload.code,
        event_type: eventName,
        waba_id: payload.waba_id,
        phone_number_id: payload.phone_number_id,
        business_id: payload.business_id,
      }).then(function (result) {
        if (result && result.error) {
          setStatus(result.error, true);
          return;
        }
        window.location.reload();
      });
      return;
    }

    if (eventName === "CANCEL") {
      setStatus("Conexión interrumpida. Podés continuar donde la dejaste.", true);
      postJson("/admin/conexiones/wizard/whatsapp/cancelar-evento", {
        wizard_id: wizardId,
        current_step: payload.current_step,
        error_code: payload.error_code,
        error_message: payload.error_message,
        error_id: payload.error_id,
        session_id: payload.session_id,
      });
      return;
    }
    // Cualquier otro evento se ignora silenciosamente (try/catch ya cubre parseo raro).
  });

  const button = document.getElementById("embedded-signup-button");
  if (button) {
    loadFacebookSdk(function () {
      button.disabled = false;
    });
    button.addEventListener("click", launchSignup);
  }
})();

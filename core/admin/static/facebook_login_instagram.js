/* Conexión de Instagram, ruta (a) "Facebook Login" (vanilla JS, sin build
 * step). Mismo patrón que facebook_login_messenger.js, pero usando la
 * Login Configuration DEDICADA de Instagram (`data-config-id`) -- nunca la
 * de Messenger, para no mezclar los dos juegos de permisos en un mismo
 * diálogo.
 */
(function () {
  "use strict";

  const root = document.getElementById("ig-facebook-login-root");
  if (!root) return;

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

  function setStatus(message, isError) {
    const el = document.getElementById("ig-facebook-login-status");
    if (!el) return;
    el.textContent = message;
    el.className = isError ? "text-danger small mt-2" : "text-secondary small mt-2";
  }

  async function exchangeCode(code) {
    const response = await fetch("/admin/conexiones/wizard/instagram/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code }),
    });
    return response.json().catch(function () {
      return {};
    });
  }

  function fbLoginCallback(response) {
    if (!response || !response.authResponse || !response.authResponse.code) {
      setStatus("No se recibió el código de autorización de Meta.", true);
      return;
    }
    setStatus("Conectando con Instagram...", false);
    exchangeCode(response.authResponse.code).then(function (result) {
      if (result && result.error) {
        setStatus(result.error, true);
        return;
      }
      window.location.reload();
    });
  }

  function launchLogin() {
    if (!window.FB) {
      setStatus("El SDK de Meta todavía no cargó, esperá un segundo y probá de nuevo.", true);
      return;
    }
    window.FB.login(fbLoginCallback, {
      config_id: configId,
      response_type: "code",
      override_default_response_type: true,
    });
  }

  const button = document.getElementById("ig-facebook-login-button");
  if (button) {
    loadFacebookSdk(function () {
      button.disabled = false;
    });
    button.addEventListener("click", launchLogin);
  }
})();

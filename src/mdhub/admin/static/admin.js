// mdhub admin console: passkey ceremonies, confirmation prompts, copy buttons.
// Loaded as a static file (CSP script-src 'self', no inline scripts).
"use strict";

(function () {
  const csrf = () => document.body.dataset.csrf || "";

  function b64uToBuf(value) {
    const pad = "=".repeat((4 - (value.length % 4)) % 4);
    const bin = atob((value + pad).replace(/-/g, "+").replace(/_/g, "/"));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function bufToB64u(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = {};
    }
    if (!res.ok) throw new Error(data.error || "Request failed (" + res.status + ").");
    return data;
  }

  function creationOptions(o) {
    o.challenge = b64uToBuf(o.challenge);
    o.user.id = b64uToBuf(o.user.id);
    (o.excludeCredentials || []).forEach((c) => (c.id = b64uToBuf(c.id)));
    return o;
  }

  function requestOptions(o) {
    o.challenge = b64uToBuf(o.challenge);
    (o.allowCredentials || []).forEach((c) => (c.id = b64uToBuf(c.id)));
    return o;
  }

  function attestationJSON(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        attestationObject: bufToB64u(r.attestationObject),
        transports: typeof r.getTransports === "function" ? r.getTransports() : [],
      },
    };
  }

  function assertionJSON(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        authenticatorData: bufToB64u(r.authenticatorData),
        signature: bufToB64u(r.signature),
        userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
      },
    };
  }

  function showError(button, message) {
    const box = button.parentElement.querySelector("[data-passkey-error]") ||
      document.querySelector("[data-passkey-error]");
    if (box) {
      box.textContent = message;
      box.hidden = false;
    }
  }

  async function register(button) {
    const nameInput = document.querySelector("[data-passkey-name]");
    const options = creationOptions(await post("/webauthn/register/options"));
    const cred = await navigator.credentials.create({ publicKey: options });
    const result = await post("/webauthn/register/verify", {
      credential: attestationJSON(cred),
      name: nameInput ? nameInput.value : "passkey",
    });
    window.location.assign(result.redirect || "/");
  }

  async function login(button) {
    const options = requestOptions(await post("/webauthn/login/options"));
    const cred = await navigator.credentials.get({ publicKey: options });
    const result = await post("/webauthn/login/verify", {
      credential: assertionJSON(cred),
      next: button.dataset.next || "/",
    });
    window.location.assign(result.redirect || "/");
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-passkey]");
    if (!button) return;
    event.preventDefault();
    if (!window.PublicKeyCredential) {
      showError(button, "This browser does not support passkeys.");
      return;
    }
    button.disabled = true;
    try {
      if (button.dataset.passkey === "register") await register(button);
      else await login(button);
    } catch (err) {
      const cancelled = err && (err.name === "NotAllowedError" || err.name === "AbortError");
      showError(button, cancelled ? "The passkey prompt was cancelled or timed out." : err.message);
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  document.addEventListener("click", (event) => {
    const el = event.target.closest("[data-copy]");
    if (!el || !navigator.clipboard) return;
    navigator.clipboard.writeText(el.textContent.trim()).then(() => {
      el.classList.add("copied");
      setTimeout(() => el.classList.remove("copied"), 1200);
    });
  });
})();

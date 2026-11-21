// The admin area's Second factor page (lc_site.second_factor): set up the authenticator, or rotate its key.
// While nothing is set up, the key to add is shown (kept, so a reload shows the same one). Once one is set up, a
// page load never shows a key: a new key appears only in the answer to "rotate", which needs a current code.
import { ADMIN_API, api, errorBox, h } from "../lib.js";

async function post(step, code) {
  const response = await fetch(`${ADMIN_API}/second-factor`, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ step, code }),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* answered without JSON */ }
  return { ...data, ok: response.ok && data.ok !== false, message: data.message || `${response.status}` };
}

function keyBlock(k) {
  return h("div", { class: "key-block" },
    k.qr ? h("img", { src: k.qr, alt: "QR code of the key", width: 220, height: 220, class: "qr" }) : null,
    h("p", { class: "muted" }, "Or add it by hand, as a time-based key:"),
    h("p", {}, h("code", { class: "mono key" }, k.key)));
}

// One step: a code field and a button; `onDone(result)` after a success.
function stepForm({ label, help, step, tone = "primary", onDone }) {
  const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}",
    maxlength: 6, required: true, placeholder: "6-digit code", "aria-label": "Authenticator code", class: "code-input" });
  const status = h("div", { class: "action-status", role: "status" });
  const button = h("button", { type: "submit", class: `btn ${tone}` }, label);
  return h("form", {
    class: "action-form", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      if (!/^[0-9]{6}$/.test(code.value)) {
        status.className = "action-status bad";
        status.textContent = "Enter the 6-digit code.";
        return;
      }
      button.disabled = true;
      status.className = "action-status";
      status.textContent = "Checking the code…";
      let result;
      try {
        result = await post(step, code.value);
      } catch (error) {
        result = { ok: false, message: `Couldn't reach the site (${error.message}).` };
      }
      status.className = `action-status ${result.ok ? "good" : "bad"}`;
      status.textContent = `${result.ok ? "✓" : "✕"} ${result.message}`;
      button.disabled = false;
      code.value = "";
      if (result.ok) onDone(result);
    },
  }, help ? h("p", { class: "muted", style: { marginTop: 0 } }, help) : null,
  h("div", { class: "action-fields" }, code, button), status);
}

function page(d, reload) {
  const wrap = h("div", { class: "wrap" },
    h("h1", { style: { fontSize: "28px", letterSpacing: "-.02em" } }, "Second factor"),
    h("p", { class: "muted" }, "Every owner action, here and on the other admin pages, needs a fresh code from your authenticator app. "
      + "Reaching the admin area already asked you to sign in again with Google's 2-step verification. "
      + "Each step below, and every refused code, is recorded in the decision log."));
  const card = h("section", { class: "card card-pad" });
  wrap.append(card);
  if (d.status === "setting-up") {
    card.append(h("h3", {}, "Set up your authenticator"),
      h("p", {}, "Scan this with your authenticator app, then enter the code it shows. The key stays the same if you reload."),
      keyBlock(d),
      stepForm({ label: "Set up", step: "setup", onDone: () => setTimeout(reload, 1200) }));
  } else if (d.status === "active") {
    const panel = h("div");
    const rotate = stepForm({
      label: "Rotate the key", step: "rotate", tone: "",
      help: "Enter a code from your current key.",
      onDone: (result) => {
        rotate.hidden = true;  // the rotation has started; finishing it is the only step left here
        panel.replaceChildren(h("p", { class: "action-status good" }, `✓ ${result.message}`),
          h("h3", {}, "Add the new key"), keyBlock(result),
          stepForm({ label: "Finish: use the new key", step: "confirm-rotation", help: "Enter a code from the new key.",
            onDone: () => setTimeout(reload, 1500) }));
      },
    });
    card.append(h("h3", {}, "Your authenticator is set up"),
      h("p", {}, "To move to a new phone or app, or if the key may have been seen, rotate it: a code from your current key "
        + "shows a new key; a code from the new key then replaces the current one. Until then the current key keeps working."),
      rotate, panel);
  } else if (d.status === "rotating") {
    card.append(h("h3", {}, "A key rotation is in progress"),
      h("p", {}, "A new key was shown when the rotation started and isn't shown again. Enter a code from it to finish, or cancel "
        + "with a code from your current key and start again."),
      stepForm({ label: "Finish: use the new key", step: "confirm-rotation", help: "A code from the new key.",
        onDone: () => setTimeout(reload, 1500) }),
      stepForm({ label: "Cancel the rotation", step: "cancel-rotation", tone: "", help: "A code from your current key.",
        onDone: () => setTimeout(reload, 1200) }));
  } else {
    card.append(h("p", {}, `Unexpected status: ${d.status}`));
  }
  return wrap;
}

export async function render(root) {
  const load = async () => {
    try {
      root.replaceChildren(page(await api(`${ADMIN_API}/second-factor`), load));
    } catch (error) {
      root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    }
  };
  await load();
}

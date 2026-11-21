// The admin area's Approvals page (lc_site.approvals): pull requests that change sensitive paths
// (config/approvals.yaml) wait here for the owner. Approving needs a reason and a fresh authenticator code, and
// covers the pull request's head commit as shown: a new push needs a new approval.
import { ADMIN_API, api, errorBox, h } from "../lib.js";

async function approve(pr, reason, code) {
  const response = await fetch(`${ADMIN_API}/approvals`, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ repo: pr.repo, number: pr.number, sha: pr.sha, reason, code }),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* answered without JSON */ }
  return { ok: response.ok && data.ok !== false, message: data.message || `${response.status}` };
}

function approveForm(pr, reload) {
  const reason = h("input", { type: "text", maxlength: 300, required: true, placeholder: "Why? (recorded in the decision log)",
    "aria-label": "Reason" });
  const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: 6,
    required: true, placeholder: "6-digit code", "aria-label": "Authenticator code", class: "code-input" });
  const status = h("div", { class: "action-status", role: "status" });
  const button = h("button", { type: "submit", class: "btn primary" }, `Approve ${pr.sha.slice(0, 7)}`);
  return h("form", {
    class: "action-form", novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      if (!reason.value.trim() || !/^[0-9]{6}$/.test(code.value)) {
        status.className = "action-status bad";
        status.textContent = "Give a reason and the 6-digit code your authenticator shows now.";
        return;
      }
      button.disabled = true;
      status.className = "action-status";
      status.textContent = "Checking the code…";
      let result;
      try {
        result = await approve(pr, reason.value, code.value);
      } catch (error) {
        result = { ok: false, message: `Couldn't reach the site (${error.message}).` };
      }
      status.className = `action-status ${result.ok ? "good" : "bad"}`;
      status.textContent = `${result.ok ? "✓" : "✕"} ${result.message}`;
      button.disabled = false;
      code.value = "";
      if (result.ok) setTimeout(reload, 1500);
    },
  }, h("div", { class: "action-fields" }, reason, code, button), status);
}

function card(pr, reload) {
  return h("section", { class: `card card-pad source-row ${pr.approved ? "approved" : "pending"}` },
    h("div", { class: "source-row-head" },
      h("div", {},
        h("div", { class: "source-name" }, h("a", { href: pr.url, target: "_blank", rel: "noopener noreferrer" }, pr.title), " ",
          h("span", { class: "muted mono" }, `${pr.repo}#${pr.number}`)),
        h("div", { class: "muted" }, `by ${pr.author || "?"} · head ${pr.sha.slice(0, 7)}${pr.draft ? " · draft" : ""}`)),
      h("span", { class: `pill ${pr.approved ? "ok" : "warn"}` }, pr.approved ? "✓ Approved" : "◷ Waiting for you")),
    h("p", { class: "muted", style: { marginBottom: "4px" } }, `Sensitive paths it changes (${pr.sensitive.length}):`),
    h("ul", { class: "mono sensitive-list" }, ...pr.sensitive.slice(0, 40).map((p) => h("li", {}, p)),
      pr.sensitive.length > 40 ? h("li", {}, `… and ${pr.sensitive.length - 40} more`) : null),
    pr.approved
      ? h("p", { class: "muted" }, "Approved for this commit. A new push needs a new approval.")
      : h("div", {},
        h("p", { class: "muted" }, "Review the changes on GitHub first. Approving covers exactly this commit."),
        approveForm(pr, reload)));
}

export async function render(root) {
  const load = async () => {
    try {
      const d = await api(`${ADMIN_API}/approvals`);
      const waiting = d.pullRequests.filter((p) => !p.approved).length;
      root.replaceChildren(h("div", { class: "wrap" },
        h("h1", { style: { fontSize: "28px", letterSpacing: "-.02em" } }, "Approvals"),
        h("p", { class: "muted" }, "Pull requests that change sensitive paths (how the system is built, deployed, given access, or "
          + "how you sign in) wait here for you. GitHub lets them merge only after you approve their exact head commit here, "
          + "with a fresh code from your authenticator."),
        h("p", {}, waiting ? `${waiting} waiting for you.` : "Nothing is waiting for you."),
        ...d.pullRequests.map((pr) => card(pr, load))));
    } catch (error) {
      root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    }
  };
  await load();
}

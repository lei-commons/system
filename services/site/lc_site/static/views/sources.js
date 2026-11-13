// Data sources: every source in the registry, its terms and Claude's reading of
// them, and the owner's decision. Approve, deny or keep pending at any time; each
// opens a small form right here, asking for a reason and a fresh authenticator code.
// Only approved sources are fetched, and only their records reach the snapshot,
// database and this site.
//
// The owner can also add a source, edit any of its attributes, remove one that isn't
// built, and leave Claude a suggestion (hints, URLs; up to 1,000 characters). Those
// edits are laid over the registry in git; a source's id, its approval and its
// pipeline (job, store, record id) are shown but can't be changed here.
import { actionBar, api, codeForm, errorBox, flag, fmt, h } from "../lib.js";

const STATUS = { approved: ["good", "✓", "Approved"], denied: ["bad", "✕", "Denied"], pending: ["warn", "◷", "Pending"],
  unknown: ["idle", "?", "Unknown"] };
const RISK = { low: "good", medium: "warn", high: "bad", unknown: "idle" };
const FILTERS = [["all", "All"], ["approved", "Approved"], ["pending", "Pending"], ["denied", "Denied"], ["built", "In use"],
  ["unbuilt", "Not built yet"], ["yours", "Added or edited by you"]];

const when = (t) => (t ? (String(t).length <= 10 ? t : new Date(t).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })) : "–");
const who = (by) => (by || "").replace(/^owner:/, "") || "you";

function statusPill(status) {
  const [kind, icon, word] = STATUS[status] || STATUS.unknown;
  return h("span", { class: `pill ${kind}` }, icon, " ", word);
}

function tile(value, label, kind) {
  return h("div", { class: `kpi${kind ? " " + kind : ""}` }, h("div", {}, h("div", { class: "kpi-value num" }, fmt(value)),
    h("div", { class: "kpi-label" }, label)));
}

function host(url) {
  try { return new URL(url).host; } catch (_) { return url; }
}

function link(href, text) {
  return href && /^https?:\/\//.test(href) ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text, " ↗")
    : h("span", { class: "muted" }, "–");
}

const SHORT = { "source-approve": "Approve", "source-deny": "Deny", "source-pending": "Keep pending" };

// ---- the edit form (also used, empty, to add a source) ----

const TEXT = [["name", "Name", "text"], ["country", "Country (two letters, * for global)", "text"], ["url", "Data URL", "url"],
  ["terms_url", "Licence or terms URL", "url"], ["licence", "Licence", "text"]];
const PICK = [["access", "Access"], ["method", "Method"], ["cadence", "Updates"]];
const ASSESS = [["commercial", "Commercial use"], ["attribution", "Attribution"]];

function input(name, value, type, max) {
  return h("input", { name, type, value: value ?? "", maxlength: max, spellcheck: type === "text" ? "true" : "false" });
}

function select(name, value, choices) {
  return h("select", { name }, h("option", { value: "" }, "–"),
    ...choices.map((c) => h("option", { value: c, selected: c === value }, c)));
}

function counted(textarea, max) {
  const counter = h("div", { class: "counter muted" });
  const update = () => {
    counter.textContent = `${textarea.value.length} / ${fmt(max)}`;
    counter.classList.toggle("over", textarea.value.length > max);
  };
  textarea.addEventListener("input", update);
  update();
  return counter;
}

function sourceFields(s, d) {
  const a = s.assessment || {};
  const lim = d.limits || {};
  const ch = d.choices || {};
  const els = {};
  const grid = h("div", { class: "edit-grid" });
  for (const [k, label, type] of TEXT) {
    els[k] = input(k, s[k], type, k === "name" ? lim.name : k === "country" ? 2 : lim.text);
    grid.append(h("label", {}, label, els[k]));
  }
  for (const [k, label] of PICK) {
    els[k] = select(k, s[k], ch[k] || []);
    grid.append(h("label", {}, label, els[k]));
  }
  els.risk = select("risk", a.risk, ch.risk || []);
  grid.append(h("label", {}, "Risk", els.risk));
  for (const [k, label] of ASSESS) {
    els[k] = input(k, a[k], "text", 200);
    grid.append(h("label", {}, label, els[k]));
  }
  els.summary = h("textarea", { name: "summary", maxlength: lim.summary, rows: 4 }, a.summary || "");
  grid.append(h("div", { class: "edit-wide" }, h("label", {}, "Assessment of the terms", els.summary), counted(els.summary, lim.summary)));
  els.verified = h("input", { type: "checkbox", name: "verified", checked: !!a.verified });
  grid.append(h("label", { class: "check edit-wide" }, els.verified, "I've checked these terms myself (it then stops counting as needing review)"));
  return { grid, els };
}

// The fields that differ from `s` (all filled ones, for a new source); assessment fields go under `assessment`.
function changes(els, s) {
  const a = s.assessment || {};
  const out = {};
  const assessment = {};
  for (const [k] of [...TEXT, ...PICK]) {
    const v = k === "country" ? els[k].value.trim().toUpperCase() : els[k].value.trim();
    if (v !== (s[k] ?? "")) out[k] = v;
  }
  for (const k of ["risk", "commercial", "attribution", "summary"]) {
    const v = els[k].value.trim();
    if (v !== (a[k] ?? "")) assessment[k] = v;
  }
  if (els.verified.checked !== !!a.verified) assessment.verified = els.verified.checked;
  if (Object.keys(assessment).length) out.assessment = assessment;
  return out;
}

function fixed(s) {
  const p = s.pipeline;
  return h("div", { class: "edit-fixed muted" }, "Not editable here: id ", h("code", {}, s.id),
    p ? [" · job ", h("code", {}, p.job), " · store ", h("code", {}, p.store), " · records as ", h("code", {}, p.records_as)] : null,
    " · approval (use its own buttons). Changing those needs a code change, so a running job can't break.");
}

function editButton(s, d, reload) {
  return { label: "Edit", help: "Change its name, URLs, licence or assessment", open: (panel) => {
    const { grid, els } = sourceFields(s, d);
    codeForm(panel, { title: `Edit ${s.name}`, help: "Only what you change is saved; the rest stays as it is.",
      fields: [grid, fixed(s)], action: `source-edit:${s.id}`, confirmLabel: "Save changes", reasonOptional: true,
      reasonHint: "Why? (optional; by default the changed fields are recorded)", onDone: reload,
      collect: () => {
        const fields = changes(els, s);
        if (!Object.keys(fields).length) return "Nothing has changed yet.";
        if ("name" in fields && !fields.name) return "A source needs a name.";
        return { fields };
      } });
  } };
}

function hintButton(s, d, reload) {
  const max = (d.limits || {}).hint || 1000;
  const current = (s.owner || {}).hint || "";
  return { label: current ? "Edit suggestion for Claude" : "Suggest to Claude", help: "Hints, URLs, anything useful for this source",
    open: (panel) => {
      const text = h("textarea", { name: "hint", maxlength: max, rows: 5, placeholder:
        "Hints for Claude about this source: where the bulk files are, an API, a contact page, terms to check, what to watch out for…" }, current);
      codeForm(panel, { title: `Suggestion for Claude: ${s.name}`, help: "Claude reads it at the start of each working session.",
        fields: [text, counted(text, max)], action: `source-hint:${s.id}`, confirmLabel: current ? "Save suggestion" : "Save suggestion",
        reasonOptional: true, onDone: reload,
        collect: () => {
          const hint = text.value.trim();
          if (hint.length > max) return `Keep it to ${fmt(max)} characters.`;
          if (hint === current.trim()) return "Nothing has changed yet.";
          return { hint };
        } });
    } };
}

function removeButton(s, reload) {
  if (!s.removable) return null;
  return { label: "Remove", tone: "danger", help: "Take it off the list", open: (panel) => codeForm(panel, {
    title: `Remove ${s.name}`, help: "It leaves this list; its history stays in the control bucket.", action: `source-remove:${s.id}`,
    confirmLabel: "Confirm: Remove", tone: "danger", reasonOptional: true, onDone: reload }) };
}

function addPanel(d, reload) {
  const panel = h("div", { class: "action-panel", hidden: true });
  const button = h("button", { type: "button", class: "btn primary", onclick: () => {
    const blank = { assessment: { risk: "unknown" } };
    const { grid, els } = sourceFields(blank, d);
    codeForm(panel, { title: "Add a data source", help: "It starts pending: nothing is fetched until it's built and you approve it.",
      fields: [grid], action: "source-add", confirmLabel: "Add source", reasonOptional: true, onDone: reload,
      collect: () => {
        const fields = changes(els, {});
        if (!fields.name || !fields.country) return "A new source needs at least a name and a country.";
        return { fields };
      } });
  } }, "+ Add a source");
  return { button, panel };
}

// ---- a row ----

function row(s, d, reload) {
  const a = s.assessment || {};
  const ap = s.approval || {};
  const o = s.owner || {};
  const held = s.records && ap.status !== "approved";
  const extra = s.editable ? [editButton(s, d, reload), hintButton(s, d, reload), removeButton(s, reload)].filter(Boolean) : [];
  return h("section", { class: `card card-pad source-row ${ap.status || ""}`, id: `source-${s.id}` },
    h("div", { class: "source-row-head" },
      h("span", { class: "flag big" }, s.country && s.country !== "*" ? flag(s.country) : "🌐"),
      h("div", { style: { flex: 1, minWidth: 0 } },
        h("div", { class: "source-name" }, s.name, " ", h("span", { class: "muted mono", style: { fontSize: "12px" } }, s.id)),
        h("div", { class: "muted" }, [s.country === "*" ? "global" : s.country, s.access, s.method, s.cadence].filter(Boolean).join(" · "))),
      statusPill(ap.status)),
    h("div", { class: "toolbar", style: { margin: "4px 0 0" } },
      h("span", { class: `pill ${RISK[a.risk] || "idle"}` }, `risk: ${a.risk || "unknown"}`),
      s.built ? h("span", { class: "chip" }, `in use · ${s.records ? fmt(s.records) + " records" : "no records yet"}`)
        : h("span", { class: "chip" }, "not built yet"),
      h("span", { class: "chip" }, `licence: ${s.licence || "unknown"}`),
      a.verified ? h("span", { class: "chip" }, "✓ terms checked by you") : h("span", { class: "chip warnish" }, "terms not yet verified by you"),
      o.added ? h("span", { class: "chip on" }, "added by you") : null),
    h("dl", { class: "mini wide" },
      h("div", {}, h("dt", {}, "Data"), h("dd", {}, link(s.url, s.url ? host(s.url) : ""))),
      h("div", {}, h("dt", {}, "Licence or terms"), h("dd", {}, link(s.terms_url, "Read the terms"))),
      h("div", {}, h("dt", {}, "Commercial use"), h("dd", {}, a.commercial || "unknown")),
      h("div", {}, h("dt", {}, "Attribution"), h("dd", {}, a.attribution || "unknown"))),
    h("p", { class: "assessment" }, h("strong", {}, "Assessment: "), a.summary || "Not assessed yet.",
      h("span", { class: "muted" }, " Not legal advice.")),
    o.hint ? h("div", { class: "hint-box" }, h("strong", {}, "Your suggestion for Claude"),
      h("span", { class: "muted" }, ` · ${when(o.hint_at)}`), h("div", { class: "hint-text" }, o.hint)) : null,
    held ? h("p", { class: "held" }, "Records are kept in the private store and ledger, but left out of the snapshot, database and site while this source isn't approved.") : null,
    h("div", { class: "decision muted" }, ap.decided ? `Last decision: ${ap.status} by ${ap.by} on ${when(ap.at)}${ap.reason ? " · “" + ap.reason + "”" : ""}`
      : `Starting status: ${ap.status}${ap.reason ? " · " + ap.reason : ""}`,
    o.edited_at ? ` · ${o.added ? "added" : "edited"} by ${who(o.edited_by)} on ${when(o.edited_at)}` : ""),
    s.history && s.history.length ? h("details", { class: "history" }, h("summary", {}, `Earlier decisions (${s.history.length})`),
      h("ul", {}, ...s.history.map((x) => h("li", {}, `${x.status} by ${x.by} on ${when(x.at)}${x.reason ? " · “" + x.reason + "”" : ""}`)))) : null,
    s.actions.length || extra.length ? h("div", { style: { marginTop: "10px" } },
      actionBar(s.actions, reload, (x) => SHORT[x.id.split(":")[0]] || x.label, extra)) : null,
    !s.removable && s.editable && s.removeBlocked && !s.built ? h("div", { class: "muted", style: { fontSize: "12px", marginTop: "4px" } },
      `Remove: ${s.removeBlocked}`) : null);
}

const CHANGE = { add: "added", edit: "edited", remove: "removed", hint: "suggestion for Claude" };

export async function render(root, arg) {
  let d;
  try {
    d = await api("/api/v1/sources");
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  const reload = () => render(root, arg);
  const filter = FILTERS.some(([k]) => k === arg) ? arg : "all";
  const shown = d.sources.filter((s) => filter === "all" || (filter === "built" ? s.built : filter === "unbuilt" ? !s.built
    : filter === "yours" ? !!(s.owner && (s.owner.edited_at || s.owner.hint)) : s.approval.status === filter));
  const review = d.review ?? d.sources.filter((s) => s.approval.status === "pending"
    || (s.approval.status === "approved" && !(s.assessment || {}).verified)).length;
  const add = d.editable ? addPanel(d, reload) : null;
  const names = Object.fromEntries(d.sources.map((s) => [s.id, s.name]));
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Data sources"),
      h("span", { class: "muted", style: { flex: 1 } }, "Approve each source before it is used. You can change a decision at any time."),
      add ? add.button : null),
    add ? add.panel : null,
    h("div", { class: "kpis" }, tile(d.total, "data sources in the registry"), tile(d.approved, "approved: fetched and published", "ok"),
      tile(d.pending, "pending: not used until you decide", "warnish"), tile(d.denied, "denied: never fetched", "badish"),
      tile(review, "need your review (pending, or terms not yet verified)")),
    d.decisionsError ? h("p", { class: "held" }, `Your decisions can't be read right now (${d.decisionsError}), so nothing is treated as approved and the jobs stop.`) : null,
    d.editsError ? h("p", { class: "held" }, `Your edits to sources can't be read right now (${d.editsError}), so the list shows the registry as built, and editing is off until they can be read.`) : null,
    h("p", { class: "muted" }, "Every ingestion job checks its source here before fetching anything, and the daily snapshot includes only approved sources, so denying a source also removes its records from the next snapshot, database and site. Each decision or edit asks, right here, for a fresh code from your authenticator. Edits change what this list says; a new source is fetched only once it's built and you approve it."),
    d.changes && d.changes.length ? h("details", { class: "history changes" }, h("summary", {}, `Your recent edits (${d.changes.length})`),
      h("ul", {}, ...d.changes.map((c) => h("li", {}, `${when(c.at)} · ${names[c.source] || c.source} · ${CHANGE[c.change] || c.change}`
        + (c.change === "edit" && c.fields.length ? `: ${c.fields.join(", ")}` : ""))))) : null,
    h("div", { class: "toolbar" }, ...FILTERS.map(([k, label]) => h("a", { class: `chip${k === filter ? " on" : ""}`, href: `#/sources/${k}` }, label))),
    h("div", { class: "grid", style: { gap: "14px" } }, ...shown.map((s) => row(s, d, reload)))));
}

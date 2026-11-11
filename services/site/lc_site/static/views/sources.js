// Data sources: every source in the registry, its terms and Claude's reading of
// them, and the owner's decision. Approve, deny or keep pending at any time; each
// opens the owner console, which asks for a reason and a fresh authenticator code.
// Only approved sources are fetched, and only their records reach the snapshot,
// database and this site.
import { api, errorBox, flag, fmt, h } from "../lib.js";

const STATUS = { approved: ["good", "✓", "Approved"], denied: ["bad", "✕", "Denied"], pending: ["warn", "◷", "Pending"],
  unknown: ["idle", "?", "Unknown"] };
const RISK = { low: "good", medium: "warn", high: "bad", unknown: "idle" };
const FILTERS = [["all", "All"], ["approved", "Approved"], ["pending", "Pending"], ["denied", "Denied"], ["built", "In use"],
  ["unbuilt", "Not built yet"]];

const when = (t) => (t ? (String(t).length <= 10 ? t : new Date(t).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })) : "–");

function statusPill(status) {
  const [kind, icon, word] = STATUS[status] || STATUS.unknown;
  return h("span", { class: `pill ${kind}` }, icon, " ", word);
}

function tile(value, label, kind) {
  return h("div", { class: `kpi${kind ? " " + kind : ""}` }, h("div", {}, h("div", { class: "kpi-value num" }, fmt(value)),
    h("div", { class: "kpi-label" }, label)));
}

function link(href, text) {
  return href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text, " ↗") : h("span", { class: "muted" }, "–");
}

function row(s, consoleUrl) {
  const a = s.assessment || {};
  const ap = s.approval || {};
  const held = s.records && ap.status !== "approved";
  return h("section", { class: `card card-pad source-row ${ap.status || ""}` },
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
      a.verified ? h("span", { class: "chip" }, "terms verified") : h("span", { class: "chip warnish" }, "terms not yet verified by you")),
    h("dl", { class: "mini wide" },
      h("div", {}, h("dt", {}, "Data"), h("dd", {}, link(s.url, s.url ? new URL(s.url).host : ""))),
      h("div", {}, h("dt", {}, "Licence or terms"), h("dd", {}, link(s.terms_url, "Read the terms"))),
      h("div", {}, h("dt", {}, "Commercial use"), h("dd", {}, a.commercial || "unknown")),
      h("div", {}, h("dt", {}, "Attribution"), h("dd", {}, a.attribution || "unknown"))),
    h("p", { class: "assessment" }, h("strong", {}, "Claude's assessment: "), a.summary || "Not assessed yet.",
      h("span", { class: "muted" }, " Not legal advice.")),
    held ? h("p", { class: "held" }, "Records are kept in the private store and ledger, but left out of the snapshot, database and site while this source isn't approved.") : null,
    h("div", { class: "decision muted" }, ap.decided ? `Last decision: ${ap.status} by ${ap.by} on ${when(ap.at)}${ap.reason ? " · “" + ap.reason + "”" : ""}`
      : `Starting status: ${ap.status}${ap.reason ? " · " + ap.reason : ""}`),
    s.history && s.history.length ? h("details", { class: "history" }, h("summary", {}, `Earlier decisions (${s.history.length})`),
      h("ul", {}, ...s.history.map((x) => h("li", {}, `${x.status} by ${x.by} on ${when(x.at)}${x.reason ? " · “" + x.reason + "”" : ""}`)))) : null,
    consoleUrl && s.actions.length ? h("div", { class: "toolbar", style: { margin: "10px 0 0" } }, ...s.actions.map((x) => h("a", {
      class: `btn${x.tone === "danger" ? " danger" : x.tone === "primary" ? " primary" : ""}`,
      href: `${consoleUrl}/act?do=${encodeURIComponent(x.id)}&back=${encodeURIComponent(location.origin)}`, target: "_blank", rel: "noopener",
      title: x.help }, x.label.split(" ")[0] === "Keep" ? "Keep pending" : x.label.split(" ")[0]))) : null);
}

export async function render(root, arg) {
  let d;
  try {
    d = await api("/api/v1/sources");
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  const filter = FILTERS.some(([k]) => k === arg) ? arg : "all";
  const shown = d.sources.filter((s) => filter === "all" || (filter === "built" ? s.built : filter === "unbuilt" ? !s.built
    : s.approval.status === filter));
  const review = d.sources.filter((s) => s.approval.status === "pending" || (s.approval.status === "approved" && !(s.assessment || {}).verified)).length;
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Data sources"),
      h("span", { class: "muted" }, "Approve each source before it is used. You can change a decision at any time.")),
    h("div", { class: "kpis" }, tile(d.total, "data sources in the registry"), tile(d.approved, "approved: fetched and published", "ok"),
      tile(d.pending, "pending: not used until you decide", "warnish"), tile(d.denied, "denied: never fetched", "badish"),
      tile(review, "need your review (pending, or terms not yet verified)")),
    d.decisionsError ? h("p", { class: "held" }, `Your decisions can't be read right now (${d.decisionsError}), so nothing is treated as approved and the jobs stop.`) : null,
    h("p", { class: "muted" }, "Every ingestion job checks its source here before fetching anything, and the daily snapshot includes only approved sources, so denying a source also removes its records from the next snapshot, database and site. Actions open the owner console, which asks for a reason and a fresh code."),
    h("div", { class: "toolbar" }, ...FILTERS.map(([k, label]) => h("a", { class: `chip${k === filter ? " on" : ""}`, href: `#/sources/${k}` }, label))),
    h("div", { class: "grid", style: { gap: "14px" } }, ...shown.map((s) => row(s, d.console)))));
}

// Trials (admin area): every source on trial and how good its data is (owner, 2026-11-22: "verify and test
// all the data sources, quality of data, etc within the test mode before processing them into production
// database"). A trial source runs into its own store and stays out of the snapshot, database and site; its
// latest run's quality report (lc_ingest.quality, trial-report.json) is shown here with a sample of records,
// against the checklist in docs/source-trials.md. A source moves to production only by a pull request the
// owner approves on the Approvals page.
import { ADMIN_API, api, errorBox, flag, fmt, h } from "../lib.js";

const pct = (v) => (v === undefined || v === null ? "–" : `${v}%`);

function tile(value, label, tint) {
  return h("div", { class: `kpi tinted ${tint}` }, h("div", {},
    h("div", { class: "kpi-value num" }, value), h("div", { class: "kpi-label" }, label)));
}

function pairs(rows, empty) {
  if (!rows || !rows.length) return h("p", { class: "muted" }, empty);
  return h("table", { class: "data compact" }, ...rows.map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", { class: "num" }, typeof v === "number" ? fmt(v) : v))));
}

// The checklist (docs/source-trials.md), judged from the numbers where they can be.
function checks(t) {
  const q = t.report || {};
  const anomalies = Object.values(q.anomalies || {}).reduce((a, b) => a + b, 0);
  const contact = (q.anomalies || {})["contact-like value (email or phone)"] || 0;
  return [
    ["A complete run with a quality report", !!t.report],
    ["Records mapped", (q.records || 0) > 0],
    ["Every status word mapped", t.report ? q.statusUnmapped === 0 : false],
    ["No contact-like values", t.report ? contact === 0 : false],
    [`Anomalies (${fmt(anomalies)}) under 1% of records`, t.report ? anomalies <= (q.records || 0) / 100 : false],
    [`One main shape of registration number (${pct(q.mainShapeShare)})`, (q.mainShapeShare || 0) >= 90],
  ];
}

function sampleTable(sample) {
  if (!sample || !sample.length) return h("p", { class: "muted" }, "No sample yet.");
  const cols = [["legalName", "Name"], ["registrationNumber", "Number"], ["legalForm", "Form"], ["legalStatus", "Status"],
    ["incorporationDate", "Registered"], ["legalAddress", "Address"]];
  const cell = (r, k) => (k === "legalAddress"
    ? [((r.legalAddress || {}).addressLines || []).join(", "), (r.legalAddress || {}).city, (r.legalAddress || {}).region,
      (r.legalAddress || {}).postalCode, (r.legalAddress || {}).country].filter(Boolean).join(", ")
    : r[k] || "");
  return h("div", { class: "table-scroll" }, h("table", { class: "data" },
    h("tr", {}, ...cols.map(([, label]) => h("th", {}, label))),
    ...sample.map((r) => h("tr", {}, ...cols.map(([k]) => h("td", k === "registrationNumber" ? { class: "mono" } : {}, cell(r, k)))))));
}

// Marked invalid after failing on several separate days (lc_core.validity): it stops until re-approved on Data sources.
function validityLine(v) {
  if (!v || v.state === "ok") return null;
  if (v.state === "invalid") {
    return h("p", { class: "held" }, h("strong", {}, `Invalid since ${v.at}. `), (v.reason || "") + (/[.!?]$/.test(v.reason || ".") ? "" : "."),
      " It no longer runs; re-approve it on the ", h("a", { href: `#/sources/invalid` }, "Data sources"), " tab to try again.");
  }
  if (v.state === "retrying") return h("p", { class: "muted" }, `Re-approved ${v.at}: it runs again from the next cycle.`);
  return h("p", { class: "held" }, `Failed on ${v.failedDays.length} of ${v.threshold} days (${v.failedDays.join(", ")}); `
    + `marked invalid on day ${v.threshold}.`);
}

function trialCard(t) {
  const q = t.report || {};
  const failure = t.health && t.health.lastFailure;
  const skipped = Object.values(q.skipped || {}).reduce((a, b) => a + b, 0);
  return h("section", { class: "card card-pad" },
    h("div", { class: "source-head" },
      h("span", { class: "flag big" }, t.country && t.country !== "*" ? flag(t.country) : "🌐"),
      h("div", {}, h("div", { class: "source-name" }, t.label, h("span", { class: "pill busy", style: { marginLeft: "8px" } }, "trial")),
        h("div", { class: "muted" }, `${t.name || t.id} · job ${t.job} · ${t.licence || "licence unstated"}`))),
    t.report ? h("p", { class: "muted" }, `Latest report: ${q.publish || "?"} (${q.at || "?"}). Files measured so far: ${fmt(t.measured)}. A trial keeps no records, only this report.`)
      : h("p", { class: "muted" }, t.reportError ? `The report can't be read right now (${t.reportError}).`
        : "No complete run yet: the job runs once on deploy, then in the cycle's last step whenever there is a new publication or new code to measure."),
    failure ? h("p", { class: "held" }, `Last failure ${failure.at}: ${failure.error}: ${failure.message}`) : null,
    validityLine(t.validity),
    h("div", { class: "kpis" },
      tile(fmt(q.records), "records mapped (latest file)", "t1"),
      tile(fmt(skipped), "rows skipped", skipped ? "t4" : "t6"),
      tile(fmt(q.statusUnmapped), "records with an unmapped status word", q.statusUnmapped ? "t8" : "t6"),
      tile(fmt(Object.values(q.anomalies || {}).reduce((a, b) => a + b, 0)), "anomalies", "t2")),
    h("h3", {}, "Checklist"),
    h("ul", { class: "feed compact" }, ...checks(t).map(([label, ok]) => h("li", {},
      h("span", { class: `pill ${ok ? "good" : "warn"}` }, ok ? "✓" : "…"), " ", label))),
    t.report ? h("div", { class: "grid two" },
      h("div", {}, h("div", { class: "action-label" }, "Fields filled"),
        pairs([...Object.entries(q.fill || {}).map(([k, v]) => [k, pct(v)]),
          ...Object.entries(q.addressFill || {}).map(([k, v]) => [`address ${k}`, pct(v)])], "No fields.")),
      h("div", {}, h("div", { class: "action-label" }, "Why rows were skipped"), pairs(Object.entries(q.skipped || {}), "None."),
        h("div", { class: "action-label" }, "Anomalies"), pairs(Object.entries(q.anomalies || {}), "None.")),
      h("div", {}, h("div", { class: "action-label" }, "Status words"), pairs(q.statusWords, "None."),
        h("div", { class: "action-label" }, "Registration number shapes"), pairs(q.numberShapes, "None.")),
      h("div", {}, h("div", { class: "action-label" }, "Legal forms"), pairs(q.legalForms, "None."),
        h("div", { class: "action-label" }, "Countries"), pairs(q.countries, "None."))) : null,
    t.report ? h("div", {}, h("h3", {}, `Sample of ${fmt((q.sample || []).length)} records`), sampleTable(q.sample)) : null);
}

// The web-asserted facts pilot (services/web_claims; owner approval 2026-11-23): measure-only, from Common Crawl's
// archived pages. Web claims are never shown on the public site unless a kind passes the 98% bar and the owner
// decides; there they would be a separate card saying they are web-crawled and not officially verified.
const rate = (v) => (v === undefined || v === null ? "–" : `${Math.round(v * 1000) / 10}%`);

function gateLine(label, g) {
  if (!g || !g.judged) return h("li", {}, h("span", { class: "pill warn" }, "…"), ` ${label}: nothing judged yet`);
  return h("li", {}, h("span", { class: `pill ${g.passes ? "good" : "warn"}` }, g.passes ? "✓" : "✗"),
    ` ${label}: ${rate(g.precision)} precision on ${fmt(g.judged)} judged (bar ${rate(g.bar)})`);
}

function webClaimsCard(w) {
  const q = (w && w.report) || null;
  const head = h("div", { class: "source-head" }, h("span", { class: "flag big" }, "🌐"),
    h("div", {}, h("div", { class: "source-name" }, "Web-asserted facts pilot", h("span", { class: "pill busy", style: { marginLeft: "8px" } }, "pilot")),
      h("div", { class: "muted" }, "job web-claims · Common Crawl archive · maker and checker models from different vendors · measure-only")));
  if (!q) {
    return h("section", { class: "card card-pad" }, head, h("p", { class: "muted" }, w && w.reportError
      ? `The report can't be read right now (${w.reportError}).`
      : "No report yet: the pilot starts once a full snapshot carries the website column, then works a few hours after each cycle."));
  }
  const withSite = Object.entries(q.withWebsite || {}).map(([g, v]) => [`${g}: in archive / evidence`, `${rate(v.inArchiveRate)} / ${rate(v.evidenceRate)} of ${fmt(v.processed)}`]);
  const without = Object.entries(q.withoutWebsite || {}).map(([g, v]) => [`${g}: websites found`, `${fmt(v.found)} of ${fmt(v.processed)} (${rate(v.yield)})`]);
  const p = q.parents || {};
  const sample = (q.sample || []).map((c) => h("tr", {}, h("td", {}, c.kind),
    h("td", { class: "mono" }, c.directoryId), h("td", {}, typeof c.value === "string" ? c.value : (c.value || {}).legalName || ""),
    h("td", {}, c.matchEvidence), h("td", {}, (c.capturedAt || "").slice(0, 10))));
  return h("section", { class: "card card-pad" }, head,
    h("p", { class: "muted" }, `Candidates from the snapshot of ${q.snapshot}; report ${q.generatedAt}. ${fmt(q.processed)} done, ${fmt(q.remaining)} to go. Model spend so far about $${((q.modelUsage || {}).usd || 0).toFixed(2)}.`),
    h("div", { class: "kpis" },
      tile(fmt((q.claims || {}).website || 0), "website claims (with evidence)", "t1"),
      tile(fmt((q.claims || {}).parent || 0), "parent claims (checked)", "t2"),
      tile(fmt(p.guardRejected || 0), "parents dropped as possibly a person", p.guardRejected ? "t4" : "t6"),
      tile(rate((q.discoveryTruth || {}).precision), "discovery precision (truth sample)", "t6")),
    h("h3", {}, "The 98% bar"),
    h("ul", { class: "feed compact" }, gateLine("Websites found from the name", (q.gates || {})["website-discovery"]),
      gateLine("Parents, against GLEIF", (q.gates || {}).parent)),
    h("div", { class: "grid two" },
      h("div", {}, h("div", { class: "action-label" }, "Records with a website"), pairs(withSite, "None yet.")),
      h("div", {}, h("div", { class: "action-label" }, "Records without one"), pairs(without, "None yet."))),
    sample.length ? h("div", {}, h("h3", {}, `Sample of ${fmt(sample.length)} claims`), h("div", { class: "table-scroll" },
      h("table", { class: "data" }, h("tr", {}, ...["Kind", "Record", "Value", "Evidence", "Captured"].map((x) => h("th", {}, x))), ...sample))) : null);
}

export async function render(root) {
  let d;
  try {
    d = await api(`${ADMIN_API}/trials`);
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Trials"),
      h("span", { class: "muted" }, "New sources run here first, kept out of the database, until their data checks out")),
    h("p", { class: "muted" }, "Each source on trial runs every day after the production cycle, into its own store. Nothing from it reaches the snapshot, the database or the site. A source moves to production only through a pull request you approve on the Approvals page, quoting these numbers."),
    d.trials.length ? h("div", { class: "grid", style: { gap: "18px" } }, ...d.trials.map(trialCard))
      : h("p", { class: "empty" }, "No source is on trial."),
    d.webClaims ? h("div", { style: { marginTop: "18px" } }, webClaimsCard(d.webClaims)) : null));
}

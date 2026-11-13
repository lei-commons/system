// The live system view. Everything here is drawn from the data: sources come from
// the registry (config/sources.yaml), so a new source appears without changes here.
// Status always comes with an icon and a word, never colour alone; motion stops for
// people who ask for reduced motion. Owner actions ask, inline, for a reason and a
// fresh authenticator code before the site acts (lc_site.actions).
import { actionBar, api, errorBox, flag, fmt, h, svg } from "../lib.js";

const KIND = { ok: "good", failed: "bad", running: "busy", idle: "idle", unknown: "idle" };
const ICON = { good: "✓", bad: "✕", busy: "●", idle: "○" };
const WORD = { good: "healthy", bad: "failed", busy: "running", idle: "idle" };
const W = 190, H = 50, ROW = 64, TOP = 16, GATE = 7;

const when = (t) => (t ? new Date(t).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "–");
const clock = (t) => (t ? new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC" : "–");
const minutes = (a, b) => (a && b ? Math.max(0, Math.round((new Date(b) - new Date(a)) / 60000)) : null);

function pill(status) {
  const kind = KIND[status] || "idle";
  return h("span", { class: `pill ${kind}` }, ICON[kind], " ", WORD[kind]);
}

function table(head, rows) {
  return h("table", { class: "data" }, h("tr", {}, ...head.map((c) => h("th", {}, c))), ...rows);
}

function unavailable(error) {
  return error ? h("p", { class: "muted" }, `Unavailable (${error}).`) : null;
}

// ---- the diagram: one row per source, sized to the number of sources ----
const fit = (text, n) => (text.length > n ? text.slice(0, n - 1) + "…" : text);

function layout(d) {
  const n = Math.max(d.sources.length, 2);
  const mid = TOP + ((n - 1) * ROW) / 2;
  const nodes = {};
  d.sources.forEach((s, i) => {
    nodes[`src:${s.store}`] = [30, TOP + i * ROW, s.label, "source", s.records ? "ok" : "unknown"];
    nodes[`job:${s.job}`] = [490, TOP + i * ROW, `Ingest ${s.label}`, s.refresh, s.jobStatus];
  });
  const svc = (n2) => (d.services.find((s) => s.name === n2) || {}).status || "unknown";
  const job = (n2) => (d.jobs.find((j) => j.name === n2) || {}).status || "unknown";
  const control = { running: "ok", paused: "idle", manual: "idle", stopped: "failed" }[d.control.state] || "unknown";
  Object.assign(nodes, {
    gateway: [260, mid, "Conduct gateway", "the only way out", svc("conduct-gateway")],
    egress: [260, mid + ROW * 1.6, "Egress check", "proves the seal", job("egress-check")],
    data: [720, mid, "Data bucket", "records and changes", d.sources.some((s) => s.records) ? "ok" : "unknown"],
    snapshot: [950, mid - ROW * 0.7, "Daily snapshot", "02:00 UTC, signed", job("build-snapshot")],
    site: [950, mid + ROW * 0.7, "Database and site", "private", svc("lei-commons-site")],
  });
  const bottom = TOP + n * ROW + 40;
  Object.assign(nodes, {
    console: [30, bottom, "Owner console", "two factors", svc("owner-console")],
    control: [260, bottom, "Control state", "owner only", control],
    guard: [490, bottom, "Budget guard", "pauses at the cap", svc("budget-guard")],
    agents: [720, bottom, "Agents", "off until you switch them on", "idle"],
  });
  const flows = [...d.sources.flatMap((s) => [[`src:${s.store}`, "gateway"], ["gateway", `job:${s.job}`], [`job:${s.job}`, "data"]]),
    ["data", "snapshot"], ["snapshot", "site"]];
  const links = [["egress", "gateway"], ["console", "control"], ["guard", "control"], ["control", "agents"]];
  return { nodes, flows, links, height: bottom + H + 20 };
}

function diagram(d) {
  const { nodes, flows, links, height } = layout(d);
  const centre = (k) => [nodes[k][0] + W / 2, nodes[k][1] + H / 2];
  const curve = ([a, b], cls) => {
    const [x1, y1] = centre(a), [x2, y2] = centre(b);
    const mx = (x1 + x2) / 2;
    return svg("path", { class: cls, d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` });
  };
  const running = d.control.state === "running";
  return svg("svg", { class: "sys-diagram", viewBox: `0 0 1170 ${height}`, width: "100%", role: "img", "aria-label": "System diagram" },
    ...flows.map((f) => curve(f, "edge")), ...links.map((f) => curve(f, "edge")),
    ...(running ? flows.map((f) => curve(f, "flow")) : []),
    ...Object.values(nodes).map(([x, y, label, sub, status]) => {
      const kind = KIND[status] || "idle";
      return svg("g", { class: `node ${kind}` },
        svg("rect", { x, y, width: W, height: H, rx: 10 }),
        svg("text", { x: x + 12, y: y + 21 }, `${ICON[kind]} ${fit(label, 22)}`, svg("title", {}, label)),
        svg("text", { class: "sub", x: x + 12, y: y + 38 }, `${sub} · ${WORD[kind]}`));
    }));
}

// ---- headline figures ----
function kpis(d, meta) {
  const cycle = d.lastCycle;
  const streak = Math.min(d.cleanStreak || 0, GATE);
  const ring = svg("svg", { class: "ring", viewBox: "0 0 44 44", width: 44, height: 44, "aria-hidden": "true" },
    svg("circle", { cx: 22, cy: 22, r: 18, class: "ring-track" }),
    svg("circle", { cx: 22, cy: 22, r: 18, class: "ring-fill", "stroke-dasharray": `${(streak / GATE) * 113.1} 113.1`,
      transform: "rotate(-90 22 22)" }));
  const tile = (value, label, extra) => h("div", { class: "kpi" }, extra || null, h("div", {},
    h("div", { class: "kpi-value num" }, value), h("div", { class: "kpi-label" }, label)));
  return h("div", { class: "kpis" },
    tile(fmt(meta.records), "records in the directory"),
    tile(fmt(meta.relationships), "relationships"),
    tile(fmt(d.sources.length), `sources: ${d.sources.map((s) => s.label).join(", ")}`),
    tile(`${d.cleanStreak || 0} of ${GATE}`, "clean daily cycles in a row (M2 gate)", ring),
    tile(cycle ? `${minutes(cycle.started, cycle.ended) ?? "–"} min` : "–",
      cycle ? `last cycle, ${clock(cycle.started)} · ${(cycle.state || "").toLowerCase()}` : "no cycle yet"));
}

// ---- one card per source ----
function sourceCards(d, meta) {
  const total = d.sources.reduce((a, s) => a + (s.records || 0), 0) || 1;
  return h("div", { class: "source-grid" }, ...d.sources.map((s) => h("section", { class: "card card-pad source-card" },
    h("div", { class: "source-head" },
      h("span", { class: "flag big" }, s.country && s.country !== "*" ? flag(s.country) : "🌐"),
      h("div", {}, h("div", { class: "source-name" }, s.label), h("div", { class: "muted" }, s.name))),
    h("div", { class: "source-records num" }, s.records ? fmt(s.records) : "–"),
    h("div", { class: "muted" }, s.relationships ? `records · ${fmt(s.relationships)} relationships` : "records"),
    h("div", { class: "share", title: `${Math.round(((s.records || 0) / total) * 100)}% of source records` },
      h("div", { class: "share-bar", style: { width: `${((s.records || 0) / total) * 100}%` } })),
    h("dl", { class: "mini" },
      h("div", {}, h("dt", {}, "Refresh"), h("dd", {}, s.refresh)),
      h("div", {}, h("dt", {}, "Last processed"), h("dd", { class: "mono" }, s.last || "–")),
      h("div", {}, h("dt", {}, "Job"), h("dd", {}, pill(s.jobStatus), " ", h("span", { class: "muted" }, when(s.lastRun)))),
      h("div", {}, h("dt", {}, "Licence"), h("dd", {}, s.licence || "–"))),
    unavailable(s.error))));
}

// ---- last night's cycle, step by step ----
function timeline(d) {
  const c = d.lastCycle;
  if (!c) return h("p", { class: "muted" }, "No cycle has run yet.");
  const t0 = new Date(c.started).getTime();
  const t1 = new Date(c.ended || Date.now()).getTime();
  const span = Math.max(t1 - t0, 60000);
  return h("div", { class: "timeline" },
    ...c.steps.map((s) => {
      const a = s.started ? new Date(s.started).getTime() : null;
      const b = s.finished ? new Date(s.finished).getTime() : a;
      const kind = KIND[s.status] || "idle";
      const left = a ? ((a - t0) / span) * 100 : 0;
      const width = a ? Math.max(((b - a) / span) * 100, 0.8) : 0;
      return h("div", { class: "tl-row" },
        h("div", { class: "tl-name mono" }, s.job),
        h("div", { class: "tl-track" }, a ? h("div", { class: `tl-bar ${kind}`, style: { left: `${left}%`, width: `${width}%` },
          title: `${clock(s.started)} – ${clock(s.finished)}` }) : null),
        h("div", { class: "tl-time muted num" }, a ? `${minutes(s.started, s.finished) ?? 0} min` : "not run"));
    }),
    h("div", { class: "tl-axis muted" }, h("span", {}, clock(c.started)), h("span", {}, c.ended ? clock(c.ended) : "running")));
}

// ---- recent decisions ----
const DECISION = { done: "good", published: "good", skipped: "idle", held: "warn", failed: "bad", paused: "idle", stopped: "bad", resumed: "good" };

function feed(d) {
  const events = [...(d.feed || []), ...(d.decisions || []).map((e) => ({ ...e, subject: e.component || "" }))]
    .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || ""))).slice(0, 14);
  if (!events.length) return h("p", { class: "muted" }, "No decisions recorded yet.");
  return h("ul", { class: "feed" }, ...events.map((e) => h("li", {}, h("span", { class: `dot ${DECISION[e.decision] || "idle"}` }),
    h("div", {}, h("strong", {}, `${e.actor} · ${e.action}`), " ", h("span", { class: `pill ${DECISION[e.decision] || "idle"}` }, e.decision),
      h("div", { class: "ink2" }, e.reason || ""), h("div", { class: "muted mono" }, `${when(e.ts)}${e.subject ? " · " + e.subject : ""}`)))));
}

// ---- owner actions: open the console, which asks for a fresh code ----
function actions(d, reload) {
  if (!d.actions || !d.actions.length) return null;
  const groups = {};
  for (const a of d.actions) (groups[a.group] = groups[a.group] || []).push(a);
  return h("section", { class: "card card-pad actions" }, h("h3", {}, "Owner actions"),
    h("p", { class: "muted", style: { marginTop: 0 } }, "Each asks, right here, for a reason and a fresh code from your authenticator before it acts, and is recorded in the decision log."),
    ...Object.entries(groups).map(([group, items]) => h("div", { class: "action-group" },
      h("div", { class: "action-label" }, group), actionBar(items, reload))));
}

function page(d, meta, updated, reload) {
  const c = d.control;
  return h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "System"),
      pill({ running: "ok", paused: "idle", manual: "idle", stopped: "failed" }[c.state] || "unknown"),
      h("span", { class: "ink2" }, `State: ${c.state || "unknown"}`),
      (c.pausedComponents || []).length ? h("span", { class: "pill warn" }, `paused: ${c.pausedComponents.join(", ")}`) : null,
      h("span", { class: "muted" }, `Updated ${updated.toLocaleTimeString("en-GB")} · refreshes every 30 seconds`)),
    kpis(d, meta),
    actions(d, reload),
    h("section", { class: "card card-pad" }, diagram(d), unavailable(c.error),
      h("p", { class: "muted", style: { marginBottom: 0 } },
        c.state === "running" ? "Moving lines show the data path while the system is running." : "The system is not running, so nothing moves.")),
    h("h2", { class: "section-title" }, "Sources"),
    sourceCards(d, meta),
    h("div", { class: "grid cols-2", style: { marginTop: "18px" } },
      h("section", { class: "card card-pad" }, h("h3", {}, "Last daily cycle"), timeline(d), unavailable(d.cyclesError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Latest decisions"), feed(d),
        h("p", { style: { marginBottom: 0 } }, h("a", { href: "#/log" }, "The full decision log →")), unavailable(d.feedError))),
    h("div", { class: "grid cols-3", style: { marginTop: "18px" } },
      h("section", { class: "card card-pad" }, h("h3", {}, "Recent cycles"),
        table(["Started", "Took", "Status"], d.cycles.slice(0, 7).map((x) => h("tr", {}, h("td", {}, when(x.started)),
          h("td", { class: "num" }, x.ended ? `${minutes(x.started, x.ended)} min` : "–"), h("td", {}, pill(x.status)))))),
      h("section", { class: "card card-pad" }, h("h3", {}, "Services"),
        table(["Service", "Access", "Status"], d.services.map((s) => h("tr", {}, h("td", { class: "mono" }, s.name),
          h("td", {}, s.iap ? "owner login" : s.ingress), h("td", {}, pill(s.status))))), unavailable(d.servicesError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Other jobs and agents"),
        table(["Name", "Status"], [
          ...d.jobs.filter((j) => !d.sources.some((s) => s.job === j.name)).map((j) => h("tr", {}, h("td", { class: "mono" }, j.name), h("td", {}, pill(j.status)))),
          ...d.agents.map((a) => h("tr", {}, h("td", {}, a.role), h("td", {}, pill(a.status)))),
        ]), unavailable(d.jobsError))));
}

export async function render(root) {
  let timer;
  const load = async (force) => {
    // Don't redraw under an open action form: the owner may be typing a code.
    if (!force && root.querySelector(".action-panel:not([hidden])")) return;
    try {
      const [d, meta] = await Promise.all([api("/api/v1/system"), api("/api/v1/meta")]);
      root.replaceChildren(page(d, meta, new Date(), () => load(true)));
    } catch (error) {
      root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    }
  };
  await load(true);
  timer = setInterval(load, 30000);
  return () => clearInterval(timer);
}

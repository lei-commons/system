// The live system view. Status always comes with an icon and a word, never
// colour alone; motion stops for people who ask for reduced motion.
import { api, errorBox, fmt, h, svg } from "../lib.js";

const KIND = { ok: "good", failed: "bad", running: "busy", idle: "idle", unknown: "idle" };
const ICON = { good: "✓", bad: "✕", busy: "●", idle: "○" };
const WORD = { good: "healthy", bad: "failed", busy: "running", idle: "idle" };

// One row per source: the source, then its ingestion job. Every job runs in the 02:00 UTC daily cycle;
// the sub-label says how often its source publishes something new.
const SOURCES = [
  ["gleif", "ingest-gleif", "GLEIF", "GLEIF ingestion", "new files daily"],
  ["gb", "ingest-gb", "Companies House", "UK ingestion", "monthly register"],
  ["br", "ingest-br", "Receita Federal", "Brazil ingestion", "monthly release"],
  ["us-ny", "ingest-ny", "New York DOS", "New York ingestion", "weekly"],
  ["sg", "ingest-sg", "ACRA Singapore", "Singapore ingestion", "monthly"],
];
const ROW = 65, TOP = 20;
const NODES = {
  ...Object.fromEntries(SOURCES.flatMap(([store, job, name, jobName, cadence], i) => [
    [store, [30, TOP + i * ROW, name, "source"]], [job, [490, TOP + i * ROW, jobName, cadence]]])),
  gateway: [260, TOP + 2 * ROW, "Conduct gateway", "the only way out"],
  egress: [260, TOP + 3.6 * ROW, "Egress check", "proves the seal"],
  data: [720, TOP + 2 * ROW, "Data bucket", "records and changes"],
  snapshot: [950, TOP + 1.3 * ROW, "Daily snapshot", "02:00 UTC, signed"],
  site: [950, TOP + 2.7 * ROW, "Site and API", "private"],
  console: [30, 380, "Owner console", "two factors"], control: [260, 380, "Control state", "owner only"],
  guard: [490, 380, "Budget guard", "pauses at the cap"], agents: [720, 380, "Agents", "off until you switch them on"],
};
const FLOWS = [...SOURCES.flatMap(([store, job]) => [[store, "gateway"], ["gateway", job], [job, "data"]]),
  ["data", "snapshot"], ["snapshot", "site"]];
const LINKS = [["egress", "gateway"], ["console", "control"], ["guard", "control"], ["control", "agents"]];
const W = 190, H = 50;

function statuses(d) {
  const job = (n) => (d.jobs.find((j) => j.name === n) || {}).status || "unknown";
  const svc = (n) => (d.services.find((s) => s.name === n) || {}).status || "unknown";
  const store = (p) => (d.stores[p] && d.stores[p].records ? "ok" : "unknown");
  const control = { running: "ok", paused: "idle", manual: "idle", stopped: "failed" }[d.control.state] || "unknown";
  return { ...Object.fromEntries(SOURCES.flatMap(([s, j]) => [[s, store(s)], [j, job(j)]])),
    gateway: svc("conduct-gateway"), egress: job("egress-check"),
    data: Object.values(d.stores).some((s) => s.records) ? "ok" : "unknown", snapshot: d.snapshot.date ? "ok" : "unknown",
    site: svc("lei-commons-site"), console: svc("owner-console"), guard: svc("budget-guard"), control, agents: "idle" };
}

function centre(key) {
  const [x, y] = NODES[key];
  return [x + W / 2, y + H / 2];
}

function diagram(d) {
  const st = statuses(d);
  const running = d.control.state === "running";
  const curve = ([a, b], cls) => {
    const [x1, y1] = centre(a), [x2, y2] = centre(b);
    const mx = (x1 + x2) / 2;
    return svg("path", { class: cls, d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` });
  };
  return svg("svg", { class: "sys-diagram", viewBox: "0 0 1170 450", width: "100%", role: "img", "aria-label": "System diagram" },
    ...FLOWS.map((f) => curve(f, "edge")), ...LINKS.map((f) => curve(f, "edge")),
    ...(running ? FLOWS.map((f) => curve(f, "flow")) : []),
    ...Object.entries(NODES).map(([key, [x, y, label, sub]]) => {
      const kind = KIND[st[key]] || "idle";
      return svg("g", { class: `node ${kind}` },
        svg("rect", { x, y, width: W, height: H, rx: 10 }),
        svg("text", { x: x + 12, y: y + 21 }, `${ICON[kind]} ${label}`),
        svg("text", { class: "sub", x: x + 12, y: y + 38 }, `${sub} · ${WORD[kind]}`));
    }));
}

function pill(status) {
  const kind = KIND[status] || "idle";
  return h("span", { class: `pill ${kind === "good" ? "good" : kind === "bad" ? "bad" : kind}` }, ICON[kind], " ", WORD[kind]);
}

const when = (t) => (t ? new Date(t).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "–");

function table(head, rows) {
  return h("table", { class: "data" }, h("tr", {}, ...head.map((c) => h("th", {}, c))), ...rows);
}

function unavailable(error) {
  return error ? h("p", { class: "muted" }, `Unavailable (${error}).`) : null;
}

function page(d, updated) {
  const c = d.control;
  return h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "System"),
      pill({ running: "ok", paused: "idle", manual: "idle", stopped: "failed" }[c.state] || "unknown"),
      h("span", { class: "ink2" }, `State: ${c.state || "unknown"}`),
      h("span", { class: "muted" }, `Updated ${updated.toLocaleTimeString("en-GB")} · refreshes every 30 seconds`)),
    h("section", { class: "card card-pad" }, diagram(d), unavailable(c.error),
      h("p", { class: "muted", style: { marginBottom: 0 } },
        d.control.state === "running" ? "Moving lines show the data path while the system is running." : "The system is not running, so nothing moves.")),
    h("div", { class: "grid cols-2", style: { marginTop: "18px" } },
      h("section", { class: "card card-pad" }, h("h3", {}, "Jobs"),
        table(["Job", "Status", "Last run"], d.jobs.map((j) => h("tr", {}, h("td", { class: "mono" }, j.name), h("td", {}, pill(j.status)), h("td", {}, when(j.lastRun))))),
        unavailable(d.jobsError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Daily cycle"),
        h("p", { class: "ink2", style: { marginTop: 0 } }, d.cycles.length
          ? `${d.cleanStreak} clean ${d.cleanStreak === 1 ? "day" : "days"} in a row. M2 needs 7.` : "No runs to show."),
        table(["Started", "Finished", "Status"], d.cycles.slice(0, 7).map((c) => h("tr", {}, h("td", {}, when(c.started)),
          h("td", {}, when(c.ended)), h("td", {}, pill(c.status))))),
        unavailable(d.cyclesError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Data"),
        table(["Source", "Records", "Last processed"], Object.values(d.stores).map((s) =>
          h("tr", {}, h("td", {}, s.label), h("td", { class: "n" }, s.records ? fmt(s.records) : s.error || "none"), h("td", { class: "mono" }, s.last || "–"))))),
      h("section", { class: "card card-pad" }, h("h3", {}, "Schedules"),
        table(["Schedule", "When", "Last attempt", "Status"], d.schedules.map((s) => h("tr", {}, h("td", { class: "mono" }, s.name),
          h("td", { class: "mono" }, s.schedule), h("td", {}, when(s.lastAttempt)), h("td", {}, pill(s.status))))),
        unavailable(d.schedulesError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Services"),
        table(["Service", "Access", "Status"], d.services.map((s) => h("tr", {}, h("td", { class: "mono" }, s.name),
          h("td", {}, s.iap ? "owner login (IAP)" : s.ingress), h("td", {}, pill(s.status))))),
        unavailable(d.servicesError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Decisions"),
        d.decisions.length ? h("ul", { class: "feed" }, ...d.decisions.map((e) => h("li", {}, h("span", { class: "dot" }),
          h("div", {}, h("strong", {}, `${e.actor} · ${e.action}`), " ", h("span", { class: "muted" }, e.decision),
            h("div", { class: "ink2" }, e.reason || ""), h("div", { class: "muted mono" }, e.ts || "")))))
          : h("p", { class: "muted" }, "No decisions recorded yet."), unavailable(d.decisionsError)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Agents"),
        table(["Role", "Status", ""], d.agents.map((a) => h("tr", {}, h("td", {}, a.role), h("td", {}, pill(a.status)), h("td", { class: "muted" }, a.note)))))));
}

export async function render(root) {
  let timer;
  const load = async () => {
    try {
      root.replaceChildren(page(await api("/api/v1/system"), new Date()));
    } catch (error) {
      root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    }
  };
  await load();
  timer = setInterval(load, 30000);
  return () => clearInterval(timer);
}

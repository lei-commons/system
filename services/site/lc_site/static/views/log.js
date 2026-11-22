// The decision log (admin area): everything that happened to the system, a day at a time. The jobs' decisions,
// every admin action (state, budget, sources, second factor, approvals, runs), every merge into main, deploy and
// Terraform apply, and the guards' actions (owner, 2026-11-22). A sealed day's chain is verified by the server
// from the previous day's head; a day not yet sealed is shown from its runs. The day's summary comes first.
// Status always has a word as well as a colour.
import { ADMIN_API, api, errorBox, fmt, h } from "../lib.js";

const DECISION = { done: "good", published: "good", approved: "good", merged: "good", skipped: "idle", held: "warn",
  paused: "idle", stopped: "bad", failed: "bad", rejected: "bad", resumed: "good", proposed: "busy", "kept-separate": "idle" };
const PROBLEM = new Set(["failed", "rejected", "stopped", "held"]);

// What kind of event: who acted tells. Admin actions are the owner's; changes come from GitHub (merges, deploys)
// or the owner's bootstrap rerun (Terraform); guards are services that act on their own (the budget guard).
export const KINDS = {
  admin: { label: "Admin actions", tint: "t1", one: "admin action" },
  change: { label: "Changes and deploys", tint: "t7", one: "change" },
  job: { label: "Data jobs", tint: "t3", one: "job decision" },
  guard: { label: "Guards", tint: "t4", one: "guard action" },
};

export function kindOf(e) {
  const actor = e.actor || "";
  if (e.action === "applied Terraform") return "change";
  if (actor === "owner" || actor.startsWith("owner:")) return "admin";
  if (actor.startsWith("github:")) return "change";
  if (actor.startsWith("service:")) return "guard";
  return "job";
}

const isProblem = (e) => PROBLEM.has(e.decision);
const plural = (n, word) => `${fmt(n)} ${n === 1 ? word : word.endsWith("y") ? word.slice(0, -1) + "ies" : word + "s"}`;

function decisionPill(decision) {
  return h("span", { class: `pill ${DECISION[decision] || "idle"}` }, decision);
}

// One sentence for the day: how many of each kind, the changes named.
export function summarySentence(events) {
  if (!events.length) return "Nothing was recorded on this day.";
  const count = (fn) => events.filter(fn).length;
  const parts = [
    plural(count((e) => kindOf(e) === "admin"), "admin action"),
    plural(count((e) => e.action === "merged into main"), "merge"),
    plural(count((e) => e.action === "deployed"), "deploy"),
    plural(count((e) => e.action === "applied Terraform"), "Terraform apply"),
    plural(count((e) => kindOf(e) === "job"), "job decision"),
  ];
  const guards = count((e) => kindOf(e) === "guard");
  if (guards) parts.push(plural(guards, "guard action"));
  const problems = count(isProblem);
  return `${parts.join(", ")}; ${problems ? plural(problems, "problem") + " (failed, rejected, held or stopped)" : "no problems"}.`;
}

function tile(value, label, tint) {
  return h("div", { class: `kpi tinted ${tint}` }, h("div", {},
    h("div", { class: "kpi-value num" }, value), h("div", { class: "kpi-label" }, label)));
}

function summaryCard(day, events) {
  const counts = Object.fromEntries(Object.keys(KINDS).map((k) => [k, events.filter((e) => kindOf(e) === k).length]));
  const problems = events.filter(isProblem).length;
  // The day's changes and admin actions, named: what someone checking the day wants first.
  const notable = events.filter((e) => kindOf(e) !== "job").slice(-12).reverse();
  return h("section", { class: "card card-pad" },
    h("h3", {}, `Summary of ${day.day}`),
    h("p", { class: "ink2", style: { marginTop: 0 } }, summarySentence(events)),
    h("div", { class: "kpis" },
      ...Object.entries(KINDS).map(([k, v]) => tile(fmt(counts[k]), v.label, v.tint)),
      tile(fmt(problems), "problems: failed, rejected, held or stopped", problems ? "t8" : "t6")),
    notable.length ? h("div", {},
      h("div", { class: "action-label" }, "Admin actions and changes"),
      h("ul", { class: "feed compact" }, ...notable.map((e) => h("li", {},
        h("span", { class: `dot ${DECISION[e.decision] || "idle"}` }),
        h("div", {}, h("span", { class: "mono muted" }, `${(e.ts || "").slice(11, 16)} UTC `),
          h("strong", {}, e.action), " ", h("span", {}, e.subject), " ", decisionPill(e.decision),
          h("div", { class: "muted" }, e.reason || "")))))) : null);
}

function sealCard(day) {
  if (!day.sealed) {
    return h("section", { class: "card card-pad" }, h("h3", {}, "Not sealed yet"),
      h("p", { class: "ink2", style: { margin: 0 } }, "The snapshot job seals each day the next morning and puts the day's head in the signed manifest. ",
        day.verified ? "Each run's own events check out." : "A run's events don't check out."),
      day.verified ? null : h("p", {}, h("span", { class: "pill bad" }, "✕ chain broken")));
  }
  const s = day.seal;
  return h("section", { class: "card card-pad" }, h("h3", {}, "Seal"),
    h("div", { class: "toolbar", style: { marginTop: 0 } },
      day.verified ? h("span", { class: "pill good" }, "✓ chain verified") : h("span", { class: "pill bad" }, "✕ chain broken"),
      h("span", { class: "ink2" }, `${fmt(s.events)} events`),
      h("span", { class: "muted" }, `sealed ${new Date(s.sealedAt).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })}`)),
    h("dl", { class: "facts" },
      h("div", {}, h("dt", {}, "Head"), h("dd", { class: "mono", style: { wordBreak: "break-all" } }, s.head)),
      h("div", {}, h("dt", {}, "Carries on from"), h("dd", { class: "mono", style: { wordBreak: "break-all" } },
        s.previousDay ? `${s.previousDay} · ${s.previousHead}` : "the start of the log"))));
}

function refsText(refs) {
  const keys = Object.keys(refs || {}).filter((k) => k !== "runObject");
  return keys.slice(0, 6).map((k) => `${k}: ${typeof refs[k] === "number" ? fmt(refs[k]) : refs[k]}`).join(" · ");
}

function eventsTable(events) {
  if (!events.length) return h("p", { class: "empty" }, "Nothing of this kind on this day.");
  return h("div", { class: "table-scroll" }, h("table", { class: "data" },
    h("tr", {}, ...["Time (UTC)", "Who", "What", "Decision", "Why"].map((c) => h("th", {}, c))),
    ...events.map((e) => h("tr", {},
      h("td", { class: "mono", style: { whiteSpace: "nowrap" } }, (e.ts || "").slice(11, 19)),
      h("td", { style: { whiteSpace: "nowrap" } }, h("span", { class: `kind-tag ${KINDS[kindOf(e)].tint}` }, KINDS[kindOf(e)].one),
        h("div", { class: "mono" }, e.actor)),
      h("td", {}, `${e.action} `, h("span", { class: "muted" }, e.subject)),
      h("td", {}, decisionPill(e.decision), e.chained === false
        ? h("div", { class: "muted", style: { fontSize: "12px" }, title: "Recorded before 22 Nov 2026 beside the chain, not in it" }, "not chained")
        : null),
      h("td", {}, h("div", {}, e.reason), refsText(e.refs) ? h("div", { class: "muted", style: { fontSize: "12px" } }, refsText(e.refs)) : null)))));
}

function eventsCard(day, events) {
  const filters = [["all", "All", () => true], ...Object.entries(KINDS).map(([k, v]) => [k, v.label, (e) => kindOf(e) === k]),
    ["problems", "Problems", isProblem]];
  const body = h("div", {});
  const chips = h("div", { class: "toolbar", role: "group", "aria-label": "Show events of one kind" });
  const show = (key) => {
    const [, , fn] = filters.find((f) => f[0] === key);
    for (const chip of chips.children) chip.classList.toggle("on", chip.dataset.key === key);
    body.replaceChildren(eventsTable(events.filter(fn)));
  };
  chips.append(...filters.map(([key, label, fn]) => h("button", { type: "button", class: "chip", "data-key": key, onclick: () => show(key) },
    label, h("span", { class: "count" }, fmt(events.filter(fn).length)))));
  show("all");
  return h("section", { class: "card card-pad" }, h("h3", {}, `Events on ${day.day}`), chips, body,
    day.truncated ? h("p", { class: "muted" }, "Only the first 2,000 events are shown.") : null);
}

export async function render(root, arg) {
  let days, day;
  try {
    days = (await api(`${ADMIN_API}/log`)).days;
    const chosen = arg || (days[0] && days[0].day);
    day = chosen ? await api(`${ADMIN_API}/log/${encodeURIComponent(chosen)}`) : null;
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Decision log"),
      h("span", { class: "muted" }, "Every job decision, admin action, merge, deploy and Terraform apply, hash-chained a day at a time")),
    days.length ? h("div", { class: "toolbar" }, ...days.slice(0, 30).map((d) => h("a", {
      class: `chip${day && d.day === day.day ? " on" : ""}`, href: `#/log/${d.day}`,
    }, d.day, d.sealed ? h("span", { class: "count" }, `✓ ${fmt(d.events)}`) : h("span", { class: "count" }, "open"))))
      : h("p", { class: "empty" }, "No decisions recorded yet."),
    day ? h("div", { class: "grid", style: { gap: "18px" } },
      summaryCard(day, day.events), sealCard(day), eventsCard(day, day.events)) : null));
}

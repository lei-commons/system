// The decision log: what every job decided and why, a day at a time.
// A sealed day's chain is verified by the server from the previous day's head.
// A day not yet sealed is shown from its runs. Status always has a word as well as a colour.
import { api, errorBox, fmt, h } from "../lib.js";

const DECISION = { done: "good", published: "good", approved: "good", merged: "good", skipped: "idle", held: "warn",
  paused: "idle", stopped: "bad", failed: "bad", rejected: "bad", resumed: "good", proposed: "busy", "kept-separate": "idle" };

function decisionPill(decision) {
  return h("span", { class: `pill ${DECISION[decision] || "idle"}` }, decision);
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

function eventsTable(day) {
  if (!day.events.length) return h("p", { class: "empty" }, "Nothing was recorded on this day.");
  return h("table", { class: "data" },
    h("tr", {}, ...["Time (UTC)", "Who", "What", "Decision", "Why"].map((c) => h("th", {}, c))),
    ...day.events.map((e) => h("tr", {},
      h("td", { class: "mono", style: { whiteSpace: "nowrap" } }, (e.ts || "").slice(11, 19)),
      h("td", { class: "mono", style: { whiteSpace: "nowrap" } }, e.actor),
      h("td", {}, `${e.action} `, h("span", { class: "muted" }, e.subject)),
      h("td", {}, decisionPill(e.decision)),
      h("td", {}, h("div", {}, e.reason), refsText(e.refs) ? h("div", { class: "muted", style: { fontSize: "12px" } }, refsText(e.refs)) : null))));
}

export async function render(root, arg) {
  let days, day;
  try {
    days = (await api("/api/v1/log")).days;
    const chosen = arg || (days[0] && days[0].day);
    day = chosen ? await api(`/api/v1/log/${encodeURIComponent(chosen)}`) : null;
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Decision log"),
      h("span", { class: "muted" }, "Every job's decisions, hash-chained a day at a time")),
    days.length ? h("div", { class: "toolbar" }, ...days.slice(0, 30).map((d) => h("a", {
      class: `chip${day && d.day === day.day ? " on" : ""}`, href: `#/log/${d.day}`,
    }, d.day, d.sealed ? h("span", { class: "count" }, `✓ ${fmt(d.events)}`) : h("span", { class: "count" }, "open"))))
      : h("p", { class: "empty" }, "No decisions recorded yet."),
    day ? h("div", { class: "grid", style: { gap: "18px" } },
      sealCard(day),
      h("section", { class: "card card-pad" }, h("h3", {}, `Events on ${day.day}`), eventsTable(day),
        day.truncated ? h("p", { class: "muted" }, "Only the first 2,000 events are shown.") : null)) : null));
}

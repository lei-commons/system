// The DeDi files card (the Data page): lei-commons described as signed DeDi files (docs/dedi.md), each one link
// away, with when each was published and checked. Status always comes with an icon and a word.
import { fmt, h } from "../lib.js";

const ICON = { good: "✓", bad: "✕", busy: "●", idle: "○" };
const when = (t) => (t ? new Date(t).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "–");

function table(head, rows) {
  return h("table", { class: "data" }, h("tr", {}, ...head.map((c) => h("th", {}, c))), ...rows);
}

const DEDI_STATE = { ok: ["good", "published and verified"], failing: ["bad", "needs attention"],
  unpublished: ["idle", "not published yet"] };

export function dediCard(dd) {
  if (!dd) return null;
  const [kind, word] = DEDI_STATE[dd.state] || ["idle", dd.state];
  const pub = dd.published || {}, chk = dd.check || {};
  const soon = (t) => t && new Date(t) - Date.now() < 12 * 3600 * 1000;
  const rows = dd.files.map((f) => h("tr", {},
    h("td", {}, f.present
      ? h("a", { href: f.path, target: "_blank", rel: "noopener" }, f.label)
      : h("span", { class: "muted" }, f.label)),
    h("td", { class: "mono" }, f.path),
    h("td", { class: "num" }, f.records != null ? fmt(f.records) : "–"),
    h("td", {}, when(f.updatedAt)),
    h("td", {}, f.nextUpdate ? h("span", { class: soon(f.nextUpdate) ? "pill warn" : "" }, when(f.nextUpdate)) : "–")));
  const notes = [];
  if (pub.ok) notes.push(`Last published ${when(pub.publishedAt)} for the ${pub.date} snapshot` + (pub.attempts > 1 ? ` (after ${pub.attempts} attempts)` : "") + ".");
  else if (pub.failedAt) notes.push(`Publishing failed ${when(pub.failedAt)}: ${pub.error}. ` + (pub.lastGood ? `The set published ${when(pub.lastGood)} stays valid until it goes stale.` : "Nothing has been published yet."));
  if (chk.checkedAt) notes.push(chk.ok ? `Independent check passed ${when(chk.checkedAt)}.` : `Independent check failed ${when(chk.checkedAt)}: ${(chk.problems || []).join("; ")}`);
  return h("section", { class: "card card-pad dedi" },
    h("div", { class: "toolbar", style: { marginBottom: "8px" } }, h("h3", { style: { margin: 0 } }, "DeDi files"),
      h("span", { class: `pill ${kind}` }, ICON[kind], " ", word),
      dd.domain ? h("span", { class: "muted" }, `namespace ${dd.domain}`) : null),
    h("p", { class: "muted", style: { marginTop: 0 } },
      "lei-commons described as signed DeDi files (Decentralized Directory protocol): its data sources, where every country's data is, "
      + "and its daily editions, plus the signed index that DeDi verifiers start from. Re-issued every day after the snapshot. Click to view."),
    // On a narrow screen the table scrolls sideways inside the card rather than running off it.
    dd.files.length ? h("div", { class: "table-scroll" }, table(["File", "Path", "Records", "Content changed", "Fresh until"], rows))
      : h("p", {}, "Nothing published yet."),
    ...notes.map((n) => h("p", { class: "muted", style: { marginBottom: 0 } }, n)));
}

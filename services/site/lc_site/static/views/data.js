// Downloads: the daily snapshots, their files and the fingerprints to check them by.
// Files stream from the private snapshots bucket through this site, behind the owner's login.
// A full snapshot's files are its parts (by country and sources); a changes day's files hold
// only what changed since the day before (docs/snapshots-v2.md). The DeDi files come first, then the chosen
// day's key figures as coloured cards.
import { api, errorBox, flag, fmt, h, sourceName } from "../lib.js";
import { dediCard } from "./dedi-card.js";

function size(bytes) {
  if (bytes === null || bytes === undefined) return "–";
  if (bytes < 1024 * 1024) return `${fmt(Math.max(1, Math.round(bytes / 1024)))} KB`;
  return `${(bytes / 1024 / 1024).toLocaleString("en-GB", { maximumFractionDigits: 1 })} MB`;
}

function fact(label, value, mono = false) {
  return h("div", {}, h("dt", {}, label), h("dd", { class: mono ? "mono" : "", style: mono ? { wordBreak: "break-all" } : {} }, value));
}

const ABOUT = { "manifest.json": "Manifest: counts, Merkle roots, file hashes",
  "manifest.json.sigstore.json": "The manifest's Sigstore signature", "NOTICE.txt": "Licences and attribution" };

function contents(f) {
  if (f.kind === "about") return ABOUT[f.name] || "";
  if (f.kind === "relationships") return "Relationships between entities";
  if (f.kind === "changes") return "Records added, changed or deleted that day";
  if (f.kind === "relationship-changes") return "Relationships added, changed or deleted that day";
  const sources = (f.name.split(".")[1] || "").split("_").filter(Boolean).map(sourceName).join(" + ");
  const where = f.country === "XX" ? "No country recorded" : h("span", {}, h("span", { class: "flag" }, flag(f.country)), " ", f.country);
  return sources ? h("span", {}, where, h("span", { class: "muted" }, ` · ${sources}`)) : where;
}

function tile(value, label, tint, sub) {
  return h("div", { class: `kpi tinted ${tint}` }, h("div", {},
    h("div", { class: "kpi-value num" }, value), h("div", { class: "kpi-label" }, label),
    sub ? h("div", { class: "kpi-sub" }, sub) : null));
}

// The day's key figures: the whole directory's size that day, and on a changes day what changed.
function figures(snap) {
  // What changed: on a changes day always; on a full snapshot only if it says something changed.
  const c = snap.changes && (snap.kind === "changes" || snap.changes.added || snap.changes.changed || snap.changes.deleted)
    ? snap.changes : null;
  const relChanges = c && (c.relationshipsAdded || c.relationshipsChanged || c.relationshipsDeleted)
    ? `${fmt(c.relationshipsAdded)} added, ${fmt(c.relationshipsChanged)} changed, ${fmt(c.relationshipsDeleted)} deleted that day` : null;
  const sources = Object.keys(snap.recordsPerSource || {}).length;
  return h("div", { class: "kpis" },
    tile(fmt(snap.records), "records: companies and other legal entities", "t1"),
    tile(fmt(snap.relationships), "relationships between them", "t7", relChanges),
    tile(fmt(snap.files.length), snap.kind === "changes" ? "files this day" : "files", "t3", size(snap.bytes)),
    sources ? tile(fmt(sources), sources === 1 ? "source" : "sources", "t4") : null,
    c ? tile(fmt(c.added), "records added", "t6", snap.base ? `since ${snap.base}` : null) : null,
    c ? tile(fmt(c.changed), "records changed", "t2") : null,
    c ? tile(fmt(c.deleted), "records deleted", "t8") : null);
}

function filesTable(snap) {
  const rows = [...snap.files].sort((a, b) => (a.kind === "about") - (b.kind === "about") || (b.records || 0) - (a.records || 0));
  return h("div", { class: "table-scroll" }, h("table", { class: "data" },
    h("tr", {}, ...["File", "Contents", "Records", "Size", ""].map((c) => h("th", {}, c))),
    ...rows.map((f) => h("tr", {},
      h("td", { class: "mono" }, f.name),
      h("td", {}, contents(f)),
      h("td", { class: "n" }, f.records === undefined || f.records === null ? "" : fmt(f.records)),
      h("td", { class: "n" }, size(f.bytes)),
      h("td", {}, h("a", { class: "btn", href: `/download/${snap.date}/${encodeURIComponent(f.name)}`, download: "" }, "Download"))))));
}

// A changes day's whole directory: its full snapshot's files, then each day's changes, in order.
async function wholeDay(snap) {
  const box = h("section", { class: "card card-pad" }, h("h3", {}, `The whole directory on ${snap.date}`),
    h("p", { class: "muted" }, "Working out which files make it up…"));
  try {
    const r = await api(`/api/v1/snapshots/${snap.date}/recipe`);
    const fileLink = (f) => h("li", {}, h("a", { href: f.url, download: "" }, f.name), h("span", { class: "muted" }, ` · ${size(f.bytes)}`));
    box.replaceChildren(h("h3", {}, `The whole directory on ${snap.date}`),
      h("p", { class: "muted" }, `${fmt(r.records)} records in ${fmt(r.anchor.files.length + r.steps.reduce((n, s) => n + s.files.length, 0))} files `
        + `(${size(r.bytes)}): the full snapshot of ${r.anchor.date}, then each day's changes in order. ${r.how}`),
      h("details", { class: "history" }, h("summary", {}, `1. Full snapshot ${r.anchor.date} (${r.anchor.files.length} files)`),
        h("ul", {}, ...r.anchor.files.map(fileLink))),
      ...r.steps.map((step, i) => h("details", { class: "history" }, h("summary", {}, `${i + 2}. Changes on ${step.date} (${step.files.length} files)`),
        h("ul", {}, ...step.files.map(fileLink)))),
      h("p", { class: "muted", style: { marginBottom: 0 } }, "The list as JSON, with each file's SHA-256: ",
        h("a", { href: `/api/v1/snapshots/${snap.date}/recipe` }, `/api/v1/snapshots/${snap.date}/recipe`)));
  } catch (error) {
    box.replaceChildren(h("h3", {}, `The whole directory on ${snap.date}`), errorBox(error));
  }
  return box;
}

export async function render(root, arg) {
  let list, dedi;
  try {
    // The DeDi card is extra: if its status can't be read, the rest of the page still shows.
    [list, dedi] = await Promise.all([api("/api/v1/snapshots").then((r) => r.snapshots), api("/api/v1/dedi").catch(() => null)]);
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  const snap = list.find((s) => s.date === arg) || list[0];
  const head = h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Data"),
    h("span", { class: "muted" }, "A full snapshot every few days, and each day's changes in between: Parquet, signed and recorded in Rekor"));
  if (!snap) {
    root.replaceChildren(h("div", { class: "wrap" }, head, dediCard(dedi), h("p", { class: "empty" }, "No snapshots yet.")));
    return;
  }
  const log = snap.decisionLog;
  root.replaceChildren(h("div", { class: "wrap" }, head,
    dediCard(dedi),
    h("h2", { class: "section-title" }, "Daily snapshots"),
    h("div", { class: "toolbar" }, ...list.map((s) => h("a", { class: `chip${s.date === snap.date ? " on" : ""}`, href: `#/data/${s.date}` },
      s.date, s.kind === "changes" ? h("span", { class: "count" }, "changes") : h("span", { class: "count" }, `full · ${fmt(s.records)}`)))),
    figures(snap),
    h("div", { class: "grid", style: { gap: "18px" } },
      h("section", { class: "card card-pad" }, h("h3", {}, snap.kind === "changes" ? `Changes on ${snap.date}` : `Full snapshot ${snap.date}`),
        snap.kind === "changes" ? h("p", { class: "muted" }, `The changes since ${snap.base}. That day's whole directory is the full snapshot of `,
          h("a", { href: `#/data/${snap.anchor}` }, snap.anchor), " plus each day's changes since; ",
          h("code", {}, "python -m lc_publish.rebuild"), " rebuilds it and checks it against the signed root below.") : null,
        h("dl", { class: "facts" },
          ...Object.entries(snap.recordsPerSource || {}).map(([k, v]) => fact(`From ${sourceName(k)}`, fmt(v))),
          fact("Snapshot root", snap.snapshotRoot, true),
          log ? fact(`Decision log through ${log.day}`, `${log.head} · ${fmt(log.events)} events that day`, true) : null),
        h("p", { class: "muted", style: { marginBottom: 0 } },
          "To check a file, compare its SHA-256 with the manifest. To check the manifest, verify its Sigstore signature ",
          "(listed here once the release has signed it, and attached to the draft release of the same date in lei-commons/releases). ",
          "The snapshot root covers the whole directory that day, on change days too.")),
      snap.kind === "changes" ? await wholeDay(snap) : null,
      h("section", { class: "card card-pad" }, h("h3", {}, snap.kind === "changes" ? "This day's files" : "Files"), filesTable(snap)))));
}

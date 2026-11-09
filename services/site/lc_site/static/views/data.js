// Downloads: the daily snapshots, their files and the fingerprints to check them by.
// Files stream from the private snapshots bucket through this site, behind the owner's login.
import { api, errorBox, flag, fmt, h, sourceName } from "../lib.js";

function size(bytes) {
  if (bytes === null || bytes === undefined) return "–";
  if (bytes < 1024 * 1024) return `${fmt(Math.max(1, Math.round(bytes / 1024)))} KB`;
  return `${(bytes / 1024 / 1024).toLocaleString("en-GB", { maximumFractionDigits: 1 })} MB`;
}

function fact(label, value, mono = false) {
  return h("div", {}, h("dt", {}, label), h("dd", { class: mono ? "mono" : "", style: mono ? { wordBreak: "break-all" } : {} }, value));
}

function filesTable(snap) {
  const rows = [...snap.files].sort((a, b) => (a.kind === "about") - (b.kind === "about") || (b.records || 0) - (a.records || 0));
  return h("table", { class: "data" },
    h("tr", {}, ...["File", "Contents", "Records", "Size", ""].map((c) => h("th", {}, c))),
    ...rows.map((f) => h("tr", {},
      h("td", { class: "mono" }, f.name),
      h("td", {}, f.kind === "relationships" ? "Relationships between entities"
        : f.kind === "about" ? (f.name === "manifest.json" ? "Manifest: counts, Merkle roots, file hashes" : "Licences and attribution")
          : f.country === "XX" ? "No country recorded" : h("span", {}, h("span", { class: "flag" }, flag(f.country)), " ", f.country)),
      h("td", { class: "n" }, f.records === undefined || f.records === null ? "" : fmt(f.records)),
      h("td", { class: "n" }, size(f.bytes)),
      h("td", {}, h("a", { class: "btn", href: `/download/${snap.date}/${encodeURIComponent(f.name)}`, download: "" }, "Download")))));
}

export async function render(root, arg) {
  let list;
  try {
    list = (await api("/api/v1/snapshots")).snapshots;
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  const snap = list.find((s) => s.date === arg) || list[0];
  const head = h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Data"),
    h("span", { class: "muted" }, "Daily snapshots as Parquet, one file per country, signed and recorded in Rekor"));
  if (!snap) {
    root.replaceChildren(h("div", { class: "wrap" }, head, h("p", { class: "empty" }, "No snapshots yet.")));
    return;
  }
  const log = snap.decisionLog;
  root.replaceChildren(h("div", { class: "wrap" }, head,
    h("div", { class: "toolbar" }, ...list.map((s) => h("a", { class: `chip${s.date === snap.date ? " on" : ""}`, href: `#/data/${s.date}` },
      s.date, h("span", { class: "count" }, fmt(s.records))))),
    h("div", { class: "grid", style: { gap: "18px" } },
      h("section", { class: "card card-pad" }, h("h3", {}, `Snapshot ${snap.date}`),
        h("dl", { class: "facts" },
          fact("Records", fmt(snap.records)), fact("Relationships", fmt(snap.relationships)),
          fact("Files", `${fmt(snap.files.length)} · ${size(snap.bytes)}`),
          ...Object.entries(snap.recordsPerSource || {}).map(([k, v]) => fact(`From ${sourceName(k)}`, fmt(v))),
          fact("Snapshot root", snap.snapshotRoot, true),
          log ? fact(`Decision log through ${log.day}`, `${log.head} · ${fmt(log.events)} events that day`, true) : null),
        h("p", { class: "muted", style: { marginBottom: 0 } },
          "To check a file, compare its SHA-256 with the manifest. To check the manifest, verify its Sigstore signature, ",
          "which is attached to the draft release of the same date in lei-commons/releases.")),
      h("section", { class: "card card-pad" }, h("h3", {}, "Files"), filesTable(snap)))));
}

import { api, errorBox, flag, fmt, h, REL, relColor, sourceChips, statusPill } from "../lib.js";
import { layoutOptions, makeGraph } from "./graph.js";

function address(a) {
  if (!a) return null;
  return [...(a.addressLines || []), [a.city, a.region].filter(Boolean).join(", "), a.postalCode, a.country]
    .filter(Boolean).map((line) => h("div", {}, line));
}

function fact(label, value, mono = false) {
  if (value === undefined || value === null || value === "") return null;
  return h("div", {}, h("dt", {}, label), h("dd", { class: mono ? "mono" : "" }, value));
}

function relationshipList(rel) {
  const rows = [...rel.up.map((r) => ({ ...r, dir: "up" })), ...rel.down.map((r) => ({ ...r, dir: "down" }))];
  if (!rows.length) return h("p", { class: "muted" }, "No relationships recorded.");
  return h("ul", { class: "rel-list" }, ...rows.map((r) => h("li", {},
    h("span", { class: "chip" }, h("span", { class: "swatch", style: { background: relColor(r.relationshipType) } }),
      REL[r.relationshipType] ? REL[r.relationshipType][r.dir] : r.role),
    r.otherId ? h("a", { href: `#/entity/${encodeURIComponent(r.otherId)}` }, h("span", { class: "flag" }, flag(r.otherCountry)), " ",
      r.otherName || r.otherLei)
      : h("span", { class: "muted" }, `${r.otherLei} (not in the directory)`),
    h("span", { class: "muted num" }, r.percentage ? `${r.percentage}%` : ""))),
    rel.downTotal > rel.down.length ? h("li", { class: "muted" }, `…and ${fmt(rel.downTotal - rel.down.length)} more below. Explore the graph to see them.`) : null);
}

export async function render(root, id) {
  let record, rel;
  try {
    [record, rel] = await Promise.all([api(`/api/v1/entities/${encodeURIComponent(id)}`),
      api(`/api/v1/entities/${encodeURIComponent(id)}/relationships`)]);
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  const mini = h("div", { class: "mini-graph" });
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "entity-head" },
      h("div", {},
        h("div", { class: "muted" }, h("span", { class: "flag" }, flag(record.country)), " ", record.country || "", " · ",
          h("span", { class: "mono" }, record.directoryId)),
        h("h1", {}, record.legalName),
        h("div", { class: "toolbar" }, statusPill(record.legalStatus), ...sourceChips(record.sources))),
      h("div", { class: "toolbar" },
        h("a", { class: "btn primary", href: `#/graph/${encodeURIComponent(record.directoryId)}` }, "Explore graph"),
        h("a", { class: "btn", href: `/api/v1/entities/${encodeURIComponent(record.directoryId)}`, target: "_blank" }, "JSON"))),
    h("div", { class: "grid cols-2" },
      h("section", { class: "card card-pad" }, h("h3", {}, "Identity"),
        h("dl", { class: "facts" },
          fact("LEI", record.lei, true), fact("Directory ID", record.directoryId, true),
          fact("Registration number", record.registrationNumber, true), fact("Registration authority", record.registrationAuthority),
          fact("Legal form", record.legalForm), fact("Jurisdiction", record.legalJurisdiction),
          fact("Incorporated", record.incorporationDate), fact("Dissolved", record.dissolutionDate),
          fact("Register", record.registryUrl ? h("a", { href: record.registryUrl, target: "_blank", rel: "noopener noreferrer" }, "View at the register") : null))),
      h("section", { class: "card card-pad" }, h("h3", {}, "Ownership and funds"), mini,
        h("p", { class: "muted", style: { marginBottom: 0 } }, `${rel.up.length} above · ${fmt(rel.downTotal)} below`)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Addresses"),
        h("div", { class: "grid cols-2" },
          record.legalAddress ? h("div", {}, h("div", { class: "muted" }, "Legal address"), address(record.legalAddress)) : null,
          record.headquartersAddress ? h("div", {}, h("div", { class: "muted" }, "Headquarters"), address(record.headquartersAddress)) : null)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Names and activity"),
        record.otherNames && record.otherNames.length ? [h("div", { class: "muted" }, "Other and previous names"),
          h("ul", {}, ...record.otherNames.map((n) => h("li", {}, n)))] : h("p", { class: "muted" }, "No other names recorded."),
        record.industryCodes && record.industryCodes.length ? [h("div", { class: "muted" }, "Industry codes (SIC)"),
          h("div", { class: "toolbar" }, ...record.industryCodes.map((c) => h("span", { class: "chip mono" }, c)))] : null)),
    h("section", { class: "card card-pad", style: { marginTop: "18px" } }, h("h3", {}, "Relationships"), relationshipList(rel))));

  if (!rel.up.length && !rel.down.length) {
    mini.replaceChildren(h("div", { class: "empty" }, "No relationships recorded."));
    return;
  }
  const full = await api(`/api/v1/graph/${encodeURIComponent(record.directoryId)}?depth=1`);
  // A preview: at most 12 connections, parents first. The explorer shows the rest.
  const edges = [...full.edges.filter((e) => e.source === full.focus), ...full.edges.filter((e) => e.source !== full.focus)].slice(0, 12);
  const keep = new Set([full.focus, ...edges.flatMap((e) => [e.source, e.target])]);
  const data = { ...full, edges, nodes: full.nodes.filter((n) => keep.has(n.id)) };
  const cy = makeGraph(mini, data, { interactive: true });
  cy.layout({ ...layoutOptions("radial", cy, data.focus), animate: false }).run();
  cy.on("tap", "node", (e) => { if (e.target.data("inDirectory")) location.hash = `#/entity/${encodeURIComponent(e.target.id())}`; });
  return () => cy.destroy();
}

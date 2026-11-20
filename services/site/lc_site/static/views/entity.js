import { api, categoryPill, errorBox, flag, fmt, h, REL, relColor, sourceChips } from "../lib.js";
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

// Every record says where it came from: each source's page for it (or its dataset), the licence,
// the attribution the licence asks for, and a citation to copy (owner requirement 2026-11-16).
function creditsCard(credits) {
  const link = (href, text) => (href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text, " ↗") : null);
  return h("section", { class: "card card-pad credits", style: { marginTop: "18px" } }, h("h3", {}, "Sources and attribution"),
    credits.length ? h("div", { class: "grid", style: { gap: "12px" } }, ...credits.map((c) => h("div", { class: "credit" },
      h("div", { class: "credit-head" }, h("strong", {}, c.publisher || c.name || c.id),
        c.name && c.publisher ? h("span", { class: "muted" }, ` · ${c.name}`) : null),
      h("div", { class: "toolbar", style: { margin: "4px 0" } }, link(c.url, "This record at the source"),
        c.licence ? (c.licenceUrl ? link(c.licenceUrl, `Licence: ${c.licence}`) : h("span", { class: "chip" }, `Licence: ${c.licence}`)) : null),
      c.attribution ? h("p", { class: "credit-attribution" }, c.attribution) : null,
      c.citation ? h("details", {}, h("summary", { class: "muted" }, "Citation"), h("p", { class: "mono credit-citation" }, c.citation)) : null)))
      : h("p", { class: "muted" }, "No source details for this record."));
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
  if (record.mergedInto) {
    // An old ID that merged into another (a tombstone): say so, and link to the record it is now.
    const into = `#/entity/${encodeURIComponent(record.mergedInto)}`;
    root.replaceChildren(h("div", { class: "wrap" },
      h("div", { class: "entity-head" },
        h("div", {},
          h("div", { class: "muted" }, h("span", { class: "flag" }, flag(record.country)), " ", record.country || "", " · ",
            h("span", { class: "mono" }, record.directoryId)),
          h("h1", {}, record.legalName),
          h("div", { class: "toolbar" }, categoryPill(record.statusCategory, null), ...sourceChips(record.sources))),
        h("div", { class: "toolbar" },
          h("a", { class: "btn primary", href: into }, "Go to the current record"),
          h("a", { class: "btn", href: `/api/v1/entities/${encodeURIComponent(record.directoryId)}`, target: "_blank" }, "JSON"))),
      h("section", { class: "card card-pad" }, h("h3", {}, "Merged"),
        h("p", {}, "This directory ID was merged into ", h("a", { class: "mono", href: into }, record.mergedInto),
          ". It stays here so links to it keep working; the record now lives under that ID."),
        h("dl", { class: "facts" },
          fact("Directory ID", record.directoryId, true), fact("Merged into", record.mergedInto, true),
          fact("Registration number", record.registrationNumber, true), fact("Registration authority", record.registrationAuthority),
          fact("Jurisdiction", record.legalJurisdiction))),
      creditsCard(record.credits || [])));
    return;
  }
  const mini = h("div", { class: "mini-graph" });
  root.replaceChildren(h("div", { class: "wrap" },
    h("div", { class: "entity-head" },
      h("div", {},
        h("div", { class: "muted" }, h("span", { class: "flag" }, flag(record.country)), " ", record.country || "", " · ",
          h("span", { class: "mono" }, record.directoryId)),
        h("h1", {}, record.legalName),
        h("div", { class: "toolbar" }, categoryPill(record.statusCategory, record.legalStatus), ...sourceChips(record.sources))),
      h("div", { class: "toolbar" },
        h("a", { class: "btn primary", href: `#/graph/${encodeURIComponent(record.directoryId)}` }, "Explore graph"),
        h("a", { class: "btn", href: `/api/v1/entities/${encodeURIComponent(record.directoryId)}`, target: "_blank" }, "JSON"))),
    h("div", { class: "grid cols-2" },
      h("section", { class: "card card-pad" }, h("h3", {}, "Identity"),
        h("dl", { class: "facts" },
          fact("LEI", record.lei, true), fact("Directory ID", record.directoryId, true),
          fact("Registration number", record.registrationNumber, true), fact("Registration authority", record.registrationAuthority),
          fact("Legal form", record.legalForm), fact("Jurisdiction", record.legalJurisdiction),
          fact("Status at the register", record.legalStatus),
          fact("Incorporated", record.incorporationDate), fact("Dissolved", record.dissolutionDate),
          fact("Source", record.registryUrl ? h("a", { href: record.registryUrl, target: "_blank", rel: "noopener noreferrer" }, "View at the source") : null))),
      h("section", { class: "card card-pad" }, h("h3", {}, "Ownership and funds"), mini,
        h("p", { class: "muted", style: { marginBottom: 0 } }, `${rel.up.length} above · ${fmt(rel.downTotal)} below`)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Addresses"),
        h("div", { class: "grid cols-2" },
          record.legalAddress ? h("div", {}, h("div", { class: "muted" }, "Legal address"), address(record.legalAddress)) : null,
          record.headquartersAddress ? h("div", {}, h("div", { class: "muted" }, "Headquarters"), address(record.headquartersAddress)) : null)),
      h("section", { class: "card card-pad" }, h("h3", {}, "Names and activity"),
        record.otherNames && record.otherNames.length ? [h("div", { class: "muted" }, "Other and previous names"),
          h("ul", {}, ...record.otherNames.map((n) => h("li", {}, n)))] : h("p", { class: "muted" }, "No other names recorded."),
        record.industryCodes && record.industryCodes.length ? [h("div", { class: "muted" }, "Industry codes (as the register classifies them)"),
          h("div", { class: "toolbar" }, ...record.industryCodes.map((c) => h("span", { class: "chip mono" }, c)))] : null)),
    h("section", { class: "card card-pad", style: { marginTop: "18px" } }, h("h3", {}, "Relationships"), relationshipList(rel)),
    creditsCard(record.credits || [])));

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

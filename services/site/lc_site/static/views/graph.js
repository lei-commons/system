import cytoscape from "../vendor/cytoscape-3.34.4.esm.min.js";
import { api, cssVar, errorBox, flag, fmt, h, REL, REL_TYPES, relColor, statusPill } from "../lib.js";
import { searchBox } from "./search.js";

const short = (s, n = 34) => (!s ? "" : s.length <= n ? s : `${s.slice(0, n - 1)}…`);

function elements(data) {
  const nodes = data.nodes.map((n) => ({ group: "nodes", data: { id: n.id, label: short(n.name || `LEI ${n.lei}`),
    name: n.name, lei: n.lei, country: n.country, status: n.status, inDirectory: n.inDirectory } }));
  const edges = data.edges.map((e) => ({ group: "edges", data: { id: e.id, source: e.source, target: e.target, type: e.type,
    status: e.status, percentage: e.percentage, label: (REL[e.type] ? REL[e.type].label : e.type) + (e.percentage ? ` · ${e.percentage}%` : "") } }));
  return [...nodes, ...edges];
}

function styles() {
  const v = (name) => cssVar(name);
  return [
    // Circles sized by how connected an entity is, labelled underneath.
    { selector: "node", style: { "shape": "ellipse", "width": "data(size)", "height": "data(size)",
      "background-color": v("--surface-2"), "border-width": 2, "border-color": v("--axis"), "label": "data(label)",
      "color": v("--ink"), "font-size": 10, "font-family": "system-ui, -apple-system, Segoe UI, sans-serif",
      "text-valign": "bottom", "text-halign": "center", "text-margin-y": 5, "text-wrap": "wrap", "text-max-width": 130,
      "text-background-color": v("--page"), "text-background-opacity": 0.7, "text-background-padding": 1,
      "transition-property": "border-color, border-width, opacity, background-color", "transition-duration": "150ms" } },
    // On the radial layout, labels sit outside the ring on their own side.
    { selector: "node.radial[side = 'left']", style: { "text-halign": "left", "text-valign": "center", "text-margin-x": -6, "text-margin-y": 0 } },
    { selector: "node.radial[side = 'right']", style: { "text-halign": "right", "text-valign": "center", "text-margin-x": 6, "text-margin-y": 0 } },
    { selector: "node[?inDirectory]", style: { "background-color": v("--accent-wash"), "border-color": v("--accent") } },
    { selector: "node[!inDirectory]", style: { "border-style": "dashed", "color": v("--ink-2") } },
    { selector: "node.focus", style: { "background-color": v("--accent"), "border-color": v("--raised"), "border-width": 4,
      "font-weight": 700, "font-size": 13, "text-max-width": 220, "z-index": 10 } },
    { selector: "node:selected", style: { "border-color": v("--accent"), "border-width": 3 } },
    { selector: "node.chain", style: { "border-color": v("--s2"), "border-width": 3 } },
    { selector: "edge", style: { "width": 2, "line-color": (e) => relColor(e.data("type")), "target-arrow-shape": "triangle",
      "target-arrow-color": (e) => relColor(e.data("type")), "arrow-scale": 0.9, "curve-style": "bezier",
      "font-size": 10, "color": v("--ink-2"), "text-rotation": "autorotate",
      "text-background-color": v("--page"), "text-background-opacity": 0.85, "text-background-padding": 2, "opacity": 0.85 } },
    // Labels only where they fit; the legend and the panel name every type.
    { selector: "edge.labelled, edge:selected, edge.hover", style: { "label": "data(label)" } },
    { selector: "edge.hover", style: { "width": 3.5, "opacity": 1 } },
    { selector: "edge.chain", style: { "width": 4, "opacity": 1, "label": "data(label)" } },
    { selector: ".faded", style: { "opacity": 0.15 } },
    { selector: ".hidden-type", style: { "display": "none" } },
  ];
}

// The focus at the centre and each step outward on an evenly spaced ring. Each
// ring is ordered by the angle of its neighbours on the ring inside it, so
// subsidiaries sit next to their parent. Deterministic: the same graph always
// looks the same.
function radialPositions(cy, focus) {
  const level = new Map([[focus, 0]]);
  const queue = [focus];
  while (queue.length) {
    const id = queue.shift();
    cy.$id(id).neighborhood("node").forEach((n) => {
      if (!level.has(n.id())) { level.set(n.id(), level.get(id) + 1); queue.push(n.id()); }
    });
  }
  const maxLevel = Math.max(0, ...level.values());
  cy.nodes().forEach((n) => { if (!level.has(n.id())) level.set(n.id(), maxLevel + 1); });
  const rings = new Map();
  for (const [id, l] of level) rings.set(l, [...(rings.get(l) || []), id]);
  const angle = new Map([[focus, 0]]);
  const pos = { [focus]: { x: 0, y: 0 } };
  let radius = 0;
  for (const l of [...rings.keys()].sort((x, y) => x - y)) {
    if (l === 0) continue;
    const ids = rings.get(l);
    const parentAngle = (id) => {
      const inner = cy.$id(id).neighborhood("node").filter((n) => level.get(n.id()) === l - 1 && angle.has(n.id()));
      return inner.length ? angle.get(inner[0].id()) : Math.PI;
    };
    ids.sort((x, y) => parentAngle(x) - parentAngle(y) || x.localeCompare(y));
    radius = Math.max(radius + 170, (ids.length * 54) / (2 * Math.PI));
    ids.forEach((id, i) => {
      const a = -Math.PI / 2 + (2 * Math.PI * i) / ids.length;
      angle.set(id, ((a % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI));
      pos[id] = { x: radius * Math.cos(a), y: radius * Math.sin(a) };
      cy.$id(id).data("side", Math.cos(a) < -0.2 ? "left" : Math.cos(a) > 0.2 ? "right" : "middle");
    });
  }
  return (node) => pos[node.id()];
}

export function layoutOptions(name, cy, focus) {
  cy.nodes().toggleClass("radial", name === "radial");
  if (name === "radial") return { name: "preset", positions: radialPositions(cy, focus), animate: true,
    animationDuration: 450, fit: true, padding: cy.scratch("pad") || 100 };
  if (name === "hierarchy") {
    const roots = cy.nodes().filter((n) => n.outgoers("edge:visible").length === 0);
    return { name: "breadthfirst", directed: false, roots: roots.length ? roots : cy.$id(focus), spacingFactor: 1.15,
      padding: 40, animate: true, animationDuration: 400 };
  }
  return { name: "cose", animate: true, animationDuration: 500, padding: 50, nodeRepulsion: () => 9000,
    idealEdgeLength: () => 110, nodeOverlap: 12, randomize: false, componentSpacing: 60 };
}

function labelIfSmall(cy) {
  cy.edges().toggleClass("labelled", cy.edges().length <= 15);
  cy.nodes().forEach((n) => n.data("size", n.hasClass("focus") ? 46 : 14 + Math.min(22, 3 * n.degree())));
}

export function makeGraph(container, data, { interactive = true } = {}) {
  const cy = cytoscape({ container, elements: elements(data), style: styles(), wheelSensitivity: 0.25,
    minZoom: 0.1, maxZoom: 3, userZoomingEnabled: interactive, userPanningEnabled: interactive, boxSelectionEnabled: false });
  cy.$id(data.focus).addClass("focus");
  labelIfSmall(cy);
  // Room for labels and overlays, scaled to the canvas: a preview card needs far less.
  const pad = Math.max(12, Math.min(100, Math.round(Math.min(container.clientWidth, container.clientHeight) * 0.09)));
  cy.scratch("pad", pad);
  cy.on("layoutstop", () => cy.fit(cy.elements(), pad));
  cy.on("mouseover", "edge", (e) => e.target.addClass("hover"));
  cy.on("mouseout", "edge", (e) => e.target.removeClass("hover"));
  cy.on("mouseover", "node", (e) => e.target.connectedEdges().addClass("hover"));
  cy.on("mouseout", "node", (e) => e.target.connectedEdges().removeClass("hover"));
  return cy;
}

function legend(onToggle, hidden) {
  return h("div", { class: "float legend", "aria-label": "Relationship types" },
    h("strong", {}, "Relationships"),
    ...REL_TYPES.map((t) => h("label", { style: { cursor: "pointer" } },
      h("input", { type: "checkbox", checked: !hidden.has(t.type), onchange: (e) => onToggle(t.type, e.target.checked) }), " ",
      h("span", { class: "legend-line", style: { background: relColor(t.type) } }), t.label)),
    h("span", { class: "muted" }, "Arrows point to the parent. Dashed: not in the directory."));
}

function panel(cy, node, focus, actions) {
  if (!node) {
    return [h("p", { class: "muted" }, "Click a company to see it here. Double-click to expand its connections."),
      h("p", { class: "muted" }, `${fmt(cy.nodes().length)} entities and ${fmt(cy.edges().length)} relationships on screen.`)];
  }
  const d = node.data();
  const ups = node.outgoers("edge").map((e) => ({ e, other: e.target() }));
  const downs = node.incomers("edge").map((e) => ({ e, other: e.source() }));
  const line = ({ e, other }, dir) => h("li", {}, h("span", { class: "chip" },
    h("span", { class: "swatch", style: { background: relColor(e.data("type")) } }),
    REL[e.data("type")] ? REL[e.data("type")][dir] : e.data("type")),
    h("span", {}, other.data("name") || `LEI ${other.data("lei")}`));
  return [
    h("div", {}, h("span", { class: "flag" }, flag(d.country)), " ", h("span", { class: "muted" }, d.country || "")),
    h("h2", {}, d.name || `LEI ${d.lei}`),
    d.inDirectory ? statusPill(d.status) : h("span", { class: "pill idle" }, "Not in the directory"),
    h("p", { class: "mono muted" }, d.lei ? `LEI ${d.lei}` : ""),
    h("div", { class: "actions" },
      d.inDirectory ? h("a", { class: "btn primary", href: `#/entity/${encodeURIComponent(d.id)}` }, "Open record") : null,
      d.inDirectory ? h("button", { class: "btn", type: "button", onclick: () => actions.expand(d.id) }, "Expand") : null,
      d.inDirectory && d.id !== focus ? h("button", { class: "btn", type: "button", onclick: () => actions.refocus(d.id) }, "Centre here") : null,
      h("button", { class: "btn", type: "button", onclick: () => actions.chain(node) }, "Trace to ultimate parent")),
    ups.length ? [h("h3", {}, "Above"), h("ul", { class: "rel-list narrow" }, ...ups.map((x) => line(x, "up")))] : null,
    downs.length ? [h("h3", {}, `Below (${downs.length})`), h("ul", { class: "rel-list narrow" }, ...downs.map((x) => line(x, "down")))] : null,
  ];
}

export async function render(root, arg, params) {
  const depth = Number(params.get("depth") || 1);
  const layoutName = params.get("layout") || "radial";
  const sideBody = h("div", {});
  const side = h("aside", { class: "panel" }, sideBody);
  const canvas = h("div", { id: "cy" });
  const note = h("div", { class: "float note hidden" });
  const hidden = new Set();

  const goTo = (id, extra = {}) => {
    const p = new URLSearchParams({ depth: String(extra.depth || depth), layout: extra.layout || layoutName });
    location.hash = `#/graph/${encodeURIComponent(id)}?${p}`;
  };
  const top = h("div", { class: "float top" },
    searchBox({ placeholder: "Find a company to explore", onPick: (r) => goTo(r.directoryId), onSubmit: () => {} }),
    h("select", { class: "btn", "aria-label": "Depth", onchange: (e) => arg && goTo(arg, { depth: e.target.value }) },
      ...[1, 2, 3].map((n) => h("option", { value: n, selected: n === depth }, `${n} step${n > 1 ? "s" : ""}`))),
    h("button", { class: `btn${layoutName === "radial" ? " primary" : ""}`, type: "button", onclick: () => arg && goTo(arg, { layout: "radial" }) }, "Radial"),
    h("button", { class: `btn${layoutName === "cose" ? " primary" : ""}`, type: "button", onclick: () => arg && goTo(arg, { layout: "cose" }) }, "Force"),
    h("button", { class: `btn${layoutName === "hierarchy" ? " primary" : ""}`, type: "button", onclick: () => arg && goTo(arg, { layout: "hierarchy" }) }, "Hierarchy"),
    h("span", { class: "spacer", style: { flex: 1 } }),
    h("button", { class: "btn", type: "button", id: "fit" }, "Fit"),
    h("button", { class: "btn", type: "button", id: "png" }, "Export PNG"));
  root.replaceChildren(h("div", { class: "explorer" }, h("div", { class: "canvas-wrap" }, canvas, top, note), side));

  if (!arg) {
    sideBody.replaceChildren(h("h2", {}, "Graph explorer"),
      h("p", { class: "ink2" }, "Find a company above to draw its parents, subsidiaries, fund managers and funds."),
      h("p", { class: "muted" }, "Tip: open any record and choose “Explore graph”."));
    return;
  }

  let data;
  try {
    data = await api(`/api/v1/graph/${encodeURIComponent(arg)}?depth=${depth}`);
  } catch (error) {
    sideBody.replaceChildren(errorBox(error));
    return;
  }
  const cy = makeGraph(canvas, data);
  // The first view appears at once; later changes, such as expanding a node, animate.
  const relayout = (animate = true) => cy.layout({ ...layoutOptions(layoutName, cy, data.focus), animate }).run();
  const setNote = (truncated) => {
    note.textContent = truncated ? "Large group: some relationships are left out. Expand a node to see more of it." : "";
    note.classList.toggle("hidden", !truncated);
  };
  setNote(data.truncated);

  const actions = {
    async expand(id) {
      const more = await api(`/api/v1/graph/${encodeURIComponent(id)}?depth=1`);
      const fresh = elements(more).filter((el) => cy.getElementById(el.data.id).empty());
      cy.add(fresh);
      labelIfSmall(cy);
      for (const t of hidden) cy.edges(`[type = "${t}"]`).addClass("hidden-type");
      setNote(more.truncated || data.truncated);
      relayout();
    },
    refocus: (id) => goTo(id),
    chain(node) {
      cy.elements().removeClass("chain faded");
      const path = node.successors().union(node);
      cy.elements().not(path).addClass("faded");
      path.addClass("chain");
    },
  };
  const select = (node) => sideBody.replaceChildren(...panel(cy, node, data.focus, actions).flat().filter(Boolean));
  select(cy.$id(data.focus));
  cy.on("tap", "node", (e) => select(e.target));
  cy.on("dbltap", "node", (e) => e.target.data("inDirectory") && actions.expand(e.target.id()));
  cy.on("tap", (e) => { if (e.target === cy) { cy.elements().removeClass("chain faded"); select(null); } });

  const toggleType = (type, on) => {
    if (on) hidden.delete(type); else hidden.add(type);
    cy.edges(`[type = "${type}"]`).toggleClass("hidden-type", !on);
  };
  root.querySelector(".canvas-wrap").append(legend(toggleType, hidden));
  root.querySelector("#fit").addEventListener("click", () => cy.animate({ fit: { padding: 50 } }, { duration: 300 }));
  root.querySelector("#png").addEventListener("click", async () => {
    const blob = await cy.png({ output: "blob-promise", full: true, scale: 2, bg: cssVar("--page") });
    const a = h("a", { href: URL.createObjectURL(blob), download: `lei-commons-${data.focus}.png` });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });
  const restyle = () => cy.style(styles());
  window.addEventListener("lc-theme", restyle);
  relayout(false);
  return () => { window.removeEventListener("lc-theme", restyle); cy.destroy(); };
}

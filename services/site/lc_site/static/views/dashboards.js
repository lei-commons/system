// Dashboards. Chart rules (dataviz skill): one axis, thin bars with a rounded
// data end anchored to the baseline, a 2px gap between bars, hover tooltips,
// a table view for every chart, and colour only where it carries identity.
import { api, cssVar, errorBox, flag, fmt, h, REL, REL_TYPES, sourcesLabel, svg, tooltip } from "../lib.js";


function barPath(x, y, w, hgt, r = 4) {
  if (w <= r) return `M${x},${y}h${w}v${hgt}h${-w}z`;
  return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${hgt - 2 * r}a${r},${r} 0 0 1 ${-r},${r}h${-(w - r)}z`;
}

function hbar(rows, { label, colour, labelWidth = 170, total, width = 560 }) {
  const rowH = 28, barH = 16, valueW = 90;
  const max = Math.max(...rows.map((r) => r.records), 1);
  const plotW = width - labelWidth - valueW;
  const height = rows.length * rowH;
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Bar chart" },
    svg("line", { x1: labelWidth, x2: labelWidth, y1: 0, y2: height, stroke: cssVar("--axis") }),
    ...rows.map((r, i) => {
      const y = i * rowH + (rowH - barH) / 2;
      const w = Math.max(2, (r.records / max) * plotW);
      const share = total ? ` · ${((100 * r.records) / total).toFixed(1)}%` : "";
      const hit = svg("rect", { x: 0, y: i * rowH, width, height: rowH, fill: "transparent" });
      const g = svg("g", {},
        svg("text", { x: labelWidth - 10, y: i * rowH + rowH / 2 + 4, "text-anchor": "end" }, label(r)),
        svg("path", { class: "bar", d: barPath(labelWidth + 1, y, w, barH), fill: colour(r) }),
        svg("text", { class: "value", x: labelWidth + 8 + w, y: i * rowH + rowH / 2 + 4 }, fmt(r.records)), hit);
      hit.addEventListener("mousemove", (e) => tooltip(e, [h("strong", {}, label(r)), h("div", { class: "num" }, `${fmt(r.records)}${share}`)]));
      hit.addEventListener("mouseleave", (e) => tooltip(e, null));
      return g;
    }));
  return chart;
}

function chartCard(title, rows, opts, note) {
  const holder = h("div", { class: "chart" });
  // Drawn at the card's real width, so text stays at its real size.
  const drawChart = () => holder.replaceChildren(hbar(rows, { ...opts, width: Math.max(320, holder.clientWidth || 560) }));
  requestAnimationFrame(drawChart);
  let asTable = false;
  const table = () => h("table", { class: "data" }, h("tr", {}, h("th", {}, "Value"), h("th", { class: "n" }, "Records")),
    ...rows.map((r) => h("tr", {}, h("td", {}, opts.label(r)), h("td", { class: "n" }, fmt(r.records)))));
  const toggle = h("button", { class: "linkish", type: "button", onclick: () => {
    asTable = !asTable;
    toggle.textContent = asTable ? "Show chart" : "Show table";
    if (asTable) holder.replaceChildren(table()); else drawChart();
  } }, "Show table");
  return h("section", { class: "card card-pad" }, h("div", { class: "chart-head" }, h("h3", {}, title), toggle),
    note ? h("p", { class: "muted", style: { marginTop: 0 } }, note) : null, holder);
}

function tile(value, label) {
  return h("div", { class: "card tile" }, h("div", { class: "value num" }, value), h("div", { class: "label" }, label));
}

export async function render(root) {
  let s;
  try {
    s = await api("/api/v1/stats");
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
    return;
  }
  const draw = () => {
    const both = s.bySources.find((r) => r.value.includes(",")) || { records: 0 };
    const countries = s.byCountry.length;
    const single = () => cssVar("--s1");
    const relRows = REL_TYPES.map((t) => s.relationshipTypes.find((r) => r.value === t.type) || { value: t.type, records: 0 })
      .filter((r) => r.records > 0);
    root.replaceChildren(h("div", { class: "wrap" },
      h("div", { class: "toolbar" }, h("h1", { style: { margin: 0, fontSize: "28px", letterSpacing: "-.02em" } }, "Dashboards"),
        h("span", { class: "muted" }, `Snapshot ${s.snapshot.date}`)),
      h("div", { class: "grid cols-4" },
        tile(fmt(s.records), "entities in the directory"),
        tile(fmt(s.relationships), "ownership and fund relationships"),
        tile(fmt(both.records), "held by more than one source"),
        tile(`${countries}${countries >= 30 ? "+" : ""}`, "countries in the top list")),
      h("div", { class: "grid charts", style: { marginTop: "18px" } },
        chartCard("Entities by country", s.byCountry.slice(0, 20),
          { label: (r) => `${flag(r.value)} ${r.value || "Unknown"}`, colour: single, total: s.records, labelWidth: 90 },
          "Top 20, by country of jurisdiction."),
        chartCard("Relationships by type", relRows,
          { label: (r) => (REL[r.value] ? REL[r.value].label : r.value), colour: (r) => cssVar(`--s${(REL[r.value] || { slot: 8 }).slot}`),
            total: s.relationships, labelWidth: 130 },
          "Colours match the graph explorer."),
        chartCard("Entities by status", s.byStatus.slice(0, 12), { label: (r) => r.value || "Unknown", colour: single, total: s.records, labelWidth: 210 }),
        chartCard("Entities by source", s.bySources,
          { label: (r) => sourcesLabel(r.value), colour: single, total: s.records, labelWidth: 150 },
          "A company in both sources is one merged record."))));
  };
  draw();
  window.addEventListener("lc-theme", draw);
  return () => window.removeEventListener("lc-theme", draw);
}

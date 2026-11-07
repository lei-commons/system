import { api, debounce, flag, fmt, h, sourceChips, statusPill, errorBox } from "../lib.js";

const PAGE = 25;

export function searchBox({ value = "", placeholder, onPick, onSubmit, autofocus = false }) {
  const input = h("input", { type: "search", value, placeholder, autocomplete: "off", spellcheck: "false",
    "aria-label": "Search companies", ...(autofocus ? { autofocus: true } : {}) });
  const list = h("div", { class: "suggest card hidden", role: "listbox" });
  let items = [];
  let index = -1;

  const close = () => { list.classList.add("hidden"); index = -1; };
  const show = (results) => {
    items = results;
    index = -1;
    list.replaceChildren(...results.map((r, i) => h("a", { href: `#/entity/${encodeURIComponent(r.directoryId)}`, role: "option",
      onmousedown: (e) => { e.preventDefault(); onPick(r); close(); } },
      h("span", { class: "flag" }, flag(r.country)), h("span", {}, r.legalName), h("span", { class: "muted mono" }, r.country || ""))));
    list.classList.toggle("hidden", results.length === 0);
  };
  const lookup = debounce(async (q) => {
    if (q.trim().length < 2) return close();
    try { show((await api(`/api/v1/suggest?q=${encodeURIComponent(q)}`)).results); } catch (_) { close(); }
  }, 180);

  input.addEventListener("input", () => lookup(input.value));
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    const links = [...list.querySelectorAll("a")];
    if (e.key === "ArrowDown" && links.length) { index = (index + 1) % links.length; e.preventDefault(); }
    else if (e.key === "ArrowUp" && links.length) { index = (index - 1 + links.length) % links.length; e.preventDefault(); }
    else if (e.key === "Escape") return close();
    else if (e.key === "Enter") {
      e.preventDefault();
      if (index >= 0 && items[index]) onPick(items[index]);
      else onSubmit(input.value.trim());
      return close();
    } else return;
    links.forEach((a, i) => a.classList.toggle("sel", i === index));
  });
  return h("div", { class: "searchbox" }, h("span", { class: "glass" }, "⌕"), input, list);
}

function go(params) {
  const p = new URLSearchParams(Object.entries(params).filter(([, v]) => v));
  location.hash = `#/search?${p}`;
}

function facetRow(label, key, rows, params, names = {}) {
  if (!rows || !rows.length) return null;
  return h("div", { class: "facet-row" }, h("span", { class: "label" }, label),
    ...rows.map((r) => {
      const on = params.get(key) === r.value;
      return h("button", { class: `chip${on ? " on" : ""}`, type: "button",
        onclick: () => go({ ...Object.fromEntries(params), [key]: on ? "" : r.value, page: "" }) },
        key === "country" ? h("span", { class: "flag" }, flag(r.value)) : null, names[r.value] || r.value || "none",
        h("span", { class: "count" }, fmt(r.records)));
    }));
}

function result(r) {
  return h("a", { class: "result", href: `#/entity/${encodeURIComponent(r.directoryId)}` },
    h("span", { class: "name" }, r.legalName),
    h("span", { class: "mono muted" }, r.lei || ""),
    h("span", { class: "meta" }, h("span", { class: "flag" }, flag(r.country)), r.country || "",
      r.registrationNumber ? h("span", { class: "mono" }, `No. ${r.registrationNumber}`) : null,
      statusPill(r.legalStatus), ...sourceChips((r.sources || "").split(",").filter(Boolean))));
}

export async function render(root, _arg, params) {
  const q = params.get("q") || "";
  const meta = await api("/api/v1/meta").catch(() => null);
  const hero = h("section", { class: "hero" }, h("div", { class: "wrap" },
    h("h1", {}, "Every legal entity, one open directory."),
    h("p", { class: "lede" }, "Search companies, funds and their ownership across GLEIF and national registers."),
    searchBox({ value: q, placeholder: "Company name, LEI, directory ID or registration number", autofocus: !q,
      onPick: (r) => { location.hash = `#/entity/${encodeURIComponent(r.directoryId)}`; },
      onSubmit: (value) => go({ q: value }) }),
    meta ? h("div", { class: "hero-stats" },
      h("div", {}, h("b", { class: "num" }, fmt(meta.records)), "entities"),
      h("div", {}, h("b", { class: "num" }, fmt(meta.relationships)), "relationships"),
      h("div", {}, h("b", {}, meta.snapshot.date || "–"), "snapshot")) : null));
  root.replaceChildren(hero);
  if (!q) return;

  const body = h("div", { class: "wrap" }, h("div", { class: "skeleton", style: { height: "220px" } }));
  root.append(body);
  let found;
  try {
    found = await api(`/api/v1/search?${new URLSearchParams({ q, country: params.get("country") || "",
      status: params.get("status") || "", source: params.get("source") || "", page: params.get("page") || "1", limit: PAGE })}`);
  } catch (error) {
    body.replaceChildren(errorBox(error));
    return;
  }
  const page = Number(params.get("page") || 1);
  const sourceNames = { "gb-coh": "Companies House", gleif: "GLEIF" };
  body.replaceChildren(
    h("div", { class: "toolbar" }, h("strong", { class: "num" }, `${fmt(found.total)}${found.totalCapped ? "+" : ""} matches`),
      found.totalCapped ? h("span", { class: "muted" }, "(counts and filters cover the first 10,000)") : null,
      h("span", { class: "muted" }, `for “${q}”`)),
    h("div", { class: "card card-pad facets" },
      facetRow("Country", "country", found.facets.country, params),
      facetRow("Status", "status", found.facets.status, params),
      facetRow("Source", "source", found.facets.source, params, sourceNames)),
    found.results.length ? h("div", { class: "results" }, ...found.results.map(result))
      : h("div", { class: "empty" }, "No matches. Try fewer words, or remove a filter."),
    h("div", { class: "pager" },
      h("button", { class: "btn", type: "button", disabled: page <= 1, onclick: () => go({ ...Object.fromEntries(params), page: page - 1 }) }, "← Previous"),
      h("span", { class: "muted", style: { alignSelf: "center" } }, `Page ${page}`),
      h("button", { class: "btn", type: "button", disabled: page * PAGE >= found.total,
        onclick: () => go({ ...Object.fromEntries(params), page: page + 1 }) }, "Next →")));
}

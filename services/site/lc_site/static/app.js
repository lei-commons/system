// The router: #/ search, #/entity/ID, #/graph/ID, #/dashboards, #/system.
import { h, errorBox } from "./lib.js";

const views = {
  search: () => import("./views/search.js"),
  entity: () => import("./views/entity.js"),
  graph: () => import("./views/graph.js"),
  dashboards: () => import("./views/dashboards.js"),
  system: () => import("./views/system.js"),
};

let cleanup = null;

function parse() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const name = parts[0] && views[parts[0]] ? parts[0] : "search";
  return { name, arg: parts[1] ? decodeURIComponent(parts[1]) : "", params: new URLSearchParams(query) };
}

async function route() {
  const { name, arg, params } = parse();
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.route === (name === "entity" ? "search" : name));
  if (cleanup) {
    try { cleanup(); } catch (_) { /* a view's cleanup must never block navigation */ }
    cleanup = null;
  }
  const root = document.getElementById("view");
  root.replaceChildren(h("div", { class: "wrap" }, h("div", { class: "skeleton", style: { height: "120px" } })));
  try {
    const module = await views[name]();
    cleanup = (await module.render(root, arg, params)) || null;
  } catch (error) {
    root.replaceChildren(h("div", { class: "wrap" }, errorBox(error)));
  }
  window.scrollTo(0, 0);
}

function setupTheme() {
  const saved = localStorage.getItem("lc-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  document.getElementById("theme").addEventListener("click", () => {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const current = document.documentElement.dataset.theme || (dark ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("lc-theme", next);
    window.dispatchEvent(new Event("lc-theme"));
  });
}

setupTheme();
window.addEventListener("hashchange", route);
route();

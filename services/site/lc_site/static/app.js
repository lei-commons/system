// The router: #/ search, #/entity/ID, #/graph/ID, #/dashboards, #/data/DATE, and in the admin area (/admin/)
// #/system, #/sources/FILTER, #/log/DAY, #/approvals and #/security. The admin area is the same app at another
// path: the load balancer sends /admin/ and /api/v1/admin/ to a backend where IAP asks for a recent 2-step
// sign-in. Its tabs show only to an admin.
import { api, h, errorBox, loadSourceNames, ADMIN } from "./lib.js";

const PUBLIC_VIEWS = {
  search: () => import("./views/search.js"),
  entity: () => import("./views/entity.js"),
  graph: () => import("./views/graph.js"),
  dashboards: () => import("./views/dashboards.js"),
  data: () => import("./views/data.js"),
};
const ADMIN_VIEWS = {
  system: () => import("./views/system.js"),
  sources: () => import("./views/sources.js"),
  trials: () => import("./views/trials.js"),
  log: () => import("./views/log.js"),
  security: () => import("./views/security.js"),
  approvals: () => import("./views/approvals.js"),
};
const views = ADMIN ? ADMIN_VIEWS : PUBLIC_VIEWS;
const HOME = ADMIN ? "system" : "search";

// Links into the other area leave this page: the admin pages live under /admin/, the rest at /.
function setupNav() {
  for (const a of document.querySelectorAll("#nav a")) {
    const admin = a.dataset.area === "admin";
    if (admin !== ADMIN) a.setAttribute("href", (admin ? "/admin/" : "/") + a.getAttribute("href"));
  }
  document.getElementById("admin-badge").hidden = !ADMIN;
  if (ADMIN) document.title = "lei-commons admin";
  // The admin tabs stay hidden (app.css) unless this viewer is an admin: in the admin area that's known (the
  // server let the page in); elsewhere the site says. Hiding is a courtesy: the server checks every request.
  if (ADMIN) document.body.classList.add("is-admin");
  else api("/api/v1/me").then((me) => document.body.classList.toggle("is-admin", !!me.admin)).catch(() => {});
}

let cleanup = null;
let routed = 0; // which navigation is the current one

function parse() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  // A page of the other area, reached by an old link, opens there.
  const other = ADMIN ? PUBLIC_VIEWS : ADMIN_VIEWS;
  if (parts[0] && other[parts[0]]) {
    location.replace((ADMIN ? "/" : "/admin/") + location.hash);
    return null;
  }
  const name = parts[0] && views[parts[0]] ? parts[0] : HOME;
  return { name, arg: parts[1] ? decodeURIComponent(parts[1]) : "", params: new URLSearchParams(query) };
}

async function route() {
  const parsed = parse();
  if (!parsed) return;
  const { name, arg, params } = parsed;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.route === (name === "entity" ? "search" : name));
  if (cleanup) {
    try { cleanup(); } catch (_) { /* a view's cleanup must never block navigation */ }
    cleanup = null;
  }
  // Each page draws into a container of its own: a page's late refresh (after an action, or a slow request)
  // lands in its own container, which is no longer shown once the owner has moved on, never over the next page.
  const root = h("div", { class: "view-page" });
  document.getElementById("view").replaceChildren(root);
  root.replaceChildren(h("div", { class: "wrap" }, h("div", { class: "skeleton", style: { height: "120px" } })));
  const mine = ++routed;
  try {
    const module = await views[name]();
    const done = (await module.render(root, arg, params)) || null;
    if (mine === routed) cleanup = done;
    else if (done) done(); // the owner moved on while this page was loading: stop its timers now
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

setupNav();
setupTheme();
window.addEventListener("hashchange", route);
loadSourceNames().then(route);

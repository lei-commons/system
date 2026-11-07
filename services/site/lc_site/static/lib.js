// Small helpers shared by every view. Pages are built with h(), which sets text
// through textContent, so data from the API is never parsed as HTML.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElementNS(tag.startsWith("svg:") ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml",
    tag.replace(/^svg:/, ""));
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function svg(tag, attrs = {}, ...children) {
  return h("svg:" + tag, attrs, ...children);
}

export async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" }, credentials: "same-origin" });
  if (!response.ok) {
    const error = new Error(`${response.status} for ${path}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export const fmt = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString("en-GB"));

export function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function flag(country) {
  if (!country || !/^[A-Z]{2}$/.test(country)) return "";
  return String.fromCodePoint(...[...country].map((c) => 0x1f1a5 + c.charCodeAt(0)));
}

// Relationship types in fixed categorical order (validated: dataviz skill).
export const REL_TYPES = [
  { type: "IS_DIRECTLY_CONSOLIDATED_BY", slot: 1, up: "direct parent", down: "direct subsidiary", label: "Direct parent" },
  { type: "IS_ULTIMATELY_CONSOLIDATED_BY", slot: 2, up: "ultimate parent", down: "ultimately owned", label: "Ultimate parent" },
  { type: "IS_FUND-MANAGED_BY", slot: 3, up: "fund manager", down: "managed fund", label: "Fund manager" },
  { type: "IS_SUBFUND_OF", slot: 4, up: "umbrella fund", down: "sub-fund", label: "Sub-fund of" },
  { type: "IS_FEEDER_TO", slot: 5, up: "master fund", down: "feeder fund", label: "Feeder to" },
  { type: "IS_INTERNATIONAL_BRANCH_OF", slot: 6, up: "head office", down: "branch", label: "Branch of" },
];
export const REL = Object.fromEntries(REL_TYPES.map((t) => [t.type, t]));

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function relColor(type) {
  const t = REL[type];
  return cssVar(t ? `--s${t.slot}` : "--muted");
}

export function statusPill(status) {
  const s = (status || "").toUpperCase();
  const kind = /^(ACTIVE)$/.test(s) ? "good" : /DISSOLVED|INACTIVE|LIQUIDATION|RETIRED|ANNULLED/.test(s) ? "bad"
    : /STRIKE|ADMINISTRATION|RECEIVER|LAPSED|PENDING/.test(s) ? "warn" : "idle";
  const icon = { good: "●", bad: "■", warn: "▲", idle: "○" }[kind];
  return h("span", { class: `pill ${kind}` }, icon, " ", status || "unknown");
}

export function sourceChips(sources) {
  const names = { "gb-coh": "Companies House", gleif: "GLEIF" };
  return (sources || []).map((s) => h("span", { class: "chip" }, names[s] || s));
}

let tip;
export function tooltip(event, content) {
  if (!tip) {
    tip = h("div", { class: "tooltip hidden", role: "tooltip" });
    document.body.append(tip);
  }
  if (content === null) {
    tip.classList.add("hidden");
    return;
  }
  tip.replaceChildren(...(Array.isArray(content) ? content : [content]));
  tip.classList.remove("hidden");
  const pad = 14;
  const x = Math.min(event.clientX + pad, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(event.clientY + pad, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

export function errorBox(error) {
  const why = error && error.status === 503 ? "The site is still loading the snapshot. Try again in a minute."
    : error && error.status === 404 ? "Not found." : `Something went wrong (${error && error.message}).`;
  return h("div", { class: "empty" }, why);
}

// Look of every board page, in two parts that the browser keeps:
//  - the mode: auto (follows the system), light or dark. The switch in the app bar sets it.
//  - the theme: the house look or Nachtschicht. Chosen on the settings page; every theme has a light and a dark side.
// Loaded in <head> without defer, so the saved choice is applied before the page paints.
(() => {
  const MODE = "sf-theme", STYLE = "sf-style";
  const MODES = ["auto", "light", "dark"];
  const STYLES = { standard: "Standard", night: "Nachtschicht" };
  const get = (k, fallback) => { try { return localStorage.getItem(k) || fallback; } catch (e) { return fallback; } };
  const put = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* the choice then lasts until reload */ } };

  // A browser that chose "Nacht" in the app bar before keeps that look: dark mode in the Nachtschicht theme.
  if (get(MODE, "") === "night") { put(MODE, "dark"); put(STYLE, "night"); }

  const mode = () => { const m = get(MODE, "auto"); return MODES.includes(m) ? m : "auto"; };
  const style = () => { const s = get(STYLE, "standard"); return s in STYLES ? s : "standard"; };

  // Nachtschicht sets its text in Inter Tight: the font is fetched only for that theme.
  // display=optional: no jump to a late font; once cached it shows from the first paint.
  const font = () => {
    if (document.getElementById("sf-night-font")) return;
    const l = document.createElement("link"); l.id = "sf-night-font"; l.rel = "stylesheet";
    l.href = "https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&display=optional";
    document.head.append(l);
  };
  const apply = () => {
    const root = document.documentElement, m = mode(), s = style();
    if (m === "auto") delete root.dataset.theme; else root.dataset.theme = m;
    if (s === "standard") delete root.dataset.style; else root.dataset.style = s;
    if (s === "night") font();
  };
  apply();

  const ICON = {
    auto: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.75" y="2.75" width="12.5" height="8.5" rx="1.5"/><path d="M5.5 13.75h5M8 11.25v2.5"/></svg>',
    light: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="2.75"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.06 1.06M11.54 11.54l1.06 1.06M3.4 12.6l1.06-1.06M11.54 4.46l1.06-1.06"/></svg>',
    dark: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M13.5 9.6A5.75 5.75 0 0 1 6.4 2.5a5.75 5.75 0 1 0 7.1 7.1Z"/></svg>',
  };
  const NAME = { auto: "Auto", light: "Hell", dark: "Dunkel" };
  const TITLE = { auto: "Wie das System", light: "Helle Variante des Themes", dark: "Dunkle Variante des Themes" };
  const mark = () => document.querySelectorAll(".themes button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.t === mode())));

  function mount() {
    const bar = document.querySelector(".appbar");
    if (!bar || bar.querySelector(".themes")) return;
    const box = document.createElement("div"); box.className = "themes"; box.setAttribute("role", "group"); box.setAttribute("aria-label", "Hell oder dunkel");
    for (const t of MODES) {
      const b = document.createElement("button"); b.type = "button"; b.dataset.t = t; b.title = TITLE[t];
      b.innerHTML = ICON[t] + "<span>" + NAME[t] + "</span>";
      b.onclick = () => { put(MODE, t); apply(); mark(); };
      box.append(b);
    }
    bar.append(box); mark();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
  // Another tab changed mode or theme: follow it.
  addEventListener("storage", e => { if (e.key === MODE || e.key === STYLE) { apply(); mark(); } });

  // For the settings page: read and set the theme.
  window.sfTheme = { styles: STYLES, style, setStyle: s => { if (s in STYLES) { put(STYLE, s); apply(); } } };
})();

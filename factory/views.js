// The project's line in three more views beside the kanban lanes. Loaded by board.html and drawn from its
// draw(); uses the page's helpers (el, cards, LIVE, key, selected, draw, item, dur, ago, SIDE).
//   Linie      – stations on a belt: what is worked on now, what waits in front of each station, what fell out.
//   Fahrplan   – time left to right: who held which runner when, and where each ticket spent its hours.
//   Stellwerk  – stations as a network: how many tickets went where in a time window, the owner as a station.
// The time data comes from each card: lanes [[lane, start, end]], end [how, ts], bays [[runner, start, end, station]].

const VIEWS = [["kanban", "Kanban"], ["line", "Linie"], ["timetable", "Fahrplan"], ["signal", "Stellwerk"]];
const LANE_NAME = { queue: "wartet auf einen Platz", plan: "Planung", build: "Umsetzung", rework: "Nacharbeit", verify: "Prüfung",
  wait: "wartet auf dich", pr: "Pull Request offen", stopped: "angehalten" };
const HOUR = 3600e3;
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; } };
let flowView = VIEWS.some(v => v[0] === store("board-flowview")) ? store("board-flowview") : "kanban";
let ttWindow = Number(store("board-tt-window")) || 36, ttFilter = "all", sigWindow = Number(store("board-sig-window")) || 24, sigNode = "you";

// The switch in the head of the line section.
function viewSwitch() {
  const box = el("div", "vswitch"); box.setAttribute("role", "group"); box.setAttribute("aria-label", "Ansicht des Fließbands");
  VIEWS.forEach(([id, label]) => {
    const b = el("button", null, label); b.type = "button"; b.setAttribute("aria-pressed", String(flowView === id));
    b.onclick = e => { e.stopPropagation(); flowView = id; store("board-flowview", id); draw(); };
    box.append(b);
  });
  return box;
}

// ---- shared reading of the cards -----------------------------------------------------------------------------

const T = ts => Date.parse(ts);
const isParent = c => Array.isArray(c.parts) && c.parts.length > 0 && c.column !== "done";
const youCols = ["waiting", "stopped", "proposed"];
// The station a lane belongs to on the line.
const STATION_OF = { queue: "queue", plan: "plan", build: "build", rework: "build", verify: "verify", pr: "review", wait: "you", stopped: "you" };
// Where a ticket that waits for the owner hangs on the line: the station it last worked in.
function hangsAt(c) {
  if (c.column === "proposed") return "queue";
  const work = (c.lanes || []).filter(l => ["plan", "build", "rework", "verify"].includes(l[0])).pop();
  return work ? STATION_OF[work[0]] : "queue";
}
// Every move of a ticket from one station to another, with its time; ends go to "done" or "asm" (split).
function moves(since) {
  const out = [];
  cards.forEach(c => {
    const ls = c.lanes || [];
    for (let i = 1; i < ls.length; i++) {
      const a = STATION_OF[ls[i - 1][0]], b = STATION_OF[ls[i][0]], t = T(ls[i][1]);
      if (a !== b && t >= since) out.push({ c, from: a, to: b, t, rw: ls[i][0] === "rework" && a === "verify" });
    }
    if (c.end && ls.length && T(c.end[1]) >= since) out.push({ c, from: STATION_OF[ls[ls.length - 1][0]], to: c.end[0] === "split" ? "asm" : "done", t: T(c.end[1]) });
  });
  return out;
}
// The typical time a ticket spends in a station, over stretches that ended in the window.
function typical(stationId, since) {
  const d = [];
  cards.forEach(c => {
    let run = null;
    (c.lanes || []).forEach(l => {
      if (STATION_OF[l[0]] === stationId) { run = run ? [run[0], l[2]] : [l[1], l[2]]; }
      else if (run) { if (T(run[1]) >= since) d.push((T(run[1]) - T(run[0])) / 1000); run = null; }
    });
  });
  if (!d.length) return null;
  d.sort((a, b) => a - b); return d[Math.floor(d.length / 2)];
}

// The runners of this project, from the machine view (fetched while a view needs it).
let MACHINE = null, machineAt = 0;
function machine() {
  if (Date.now() - machineAt > 10e3) {
    machineAt = Date.now();
    fetch("/api/machine").then(r => r.ok ? r.json() : null).then(d => { if (d) { MACHINE = d; if (flowView !== "kanban") draw(); } }).catch(() => {});
  }
  const mine = ((MACHINE && MACHINE.runners) || []).filter(r => here && r.repo === here.repo);
  return mine;
}
// A runner as the runs name it (RUNNER_NAME) → the short name the machine view uses.
function bayName(full) {
  const r = ((MACHINE && MACHINE.runners) || []).find(r => r.full === full || r.name === full);
  return r ? r.name : full;
}
const liveOf = c => LIVE[c.repo + "#" + c.number];
const vDur = s => s == null ? "–" : dur(s);

// ---- Linie -----------------------------------------------------------------------------------------------------

const LINE = [["queue", "Eingang"], ["plan", "Planung"], ["build", "Umsetzung"], ["verify", "Prüfung"], ["review", "Pull Request"], ["done", "Fertig"]];

function vChip(c) {
  const b = el("button", "vchip" + (c.round > 1 ? " r2" : "") + (key(c) === selected ? " sel" : ""), "#" + c.number);
  b.type = "button"; b.title = `#${c.number} ${c.title}\n${c.step}${c.round > 1 ? " · Runde " + c.round : ""}`; b.onclick = pick(c);
  return b;
}
function smallRow(c, meta) {
  const b = el("button", "vrow" + (key(c) === selected ? " sel" : "")); b.type = "button"; b.onclick = pick(c);
  b.title = c.title;
  b.append(el("span", "n", "#" + c.number), el("span", "t", c.detail || c.title));
  if (meta !== undefined) b.append(meta instanceof Node ? meta : el("span", "m", meta || ""));
  return b;
}
function job(c) {
  const t = liveOf(c);   // no job on this machine yet: the run waits for a runner
  const b = el("button", "vjob" + (t ? "" : " starting") + (key(c) === selected ? " sel" : "")); b.type = "button"; b.onclick = pick(c);
  b.title = c.title + (t ? "" : "\nDer Lauf ist angestoßen und wartet auf einen freien Runner.");
  const top = el("div", "top"); top.append(el("span", "pulse"), "#" + c.number); b.append(top);
  b.append(el("div", "bay", (t ? t.runners.join(", ") + " · " : "wartet auf Runner · ") + c.step));
  if (t) b.append(el("div", "ld", `CPU ${t.cpu} % · RAM ${t.ram} %`));
  return b;
}

function lineView(box) {
  const day = Date.now() - 24 * HOUR, week = Date.now() - 7 * 24 * HOUR, mv = moves(day);
  const at = id => cards.filter(c => c.column === id && !isParent(c));
  const wrap = el("div", "vline-wrap"), sc = el("div", "vscroll"), line = el("div", "vline"), chutes = el("div", "vchutes");
  LINE.forEach(([id, title]) => {
    const here_ = at(id), st = el("div", "vst"); st.dataset.st = id;
    const h = el("div", "vst-head"); h.append(el("b", null, title), el("span", null, String(here_.length))); st.append(h);
    if (id === "done") {
      const bin = el("div", "vbin"); bin.append(el("b", null, String(here_.length)), el("span", null, `${mv.filter(m => m.to === "done").length} in 24 h`)); st.append(bin);
      const belt = el("div", "vbelt"); const last = here_.slice(0, 3); belt.append(...last.map(vChip)); if (!last.length) belt.append(el("span", "idle", "noch nichts")); st.append(belt);
    } else {
      const waitingHere = here_.filter(c => !c.running), buf = el("div", "vbuf");
      waitingHere.slice(0, 18).forEach(c => buf.append(vChip(c)));
      if (waitingHere.length > 18) buf.append(el("span", "vmore", "+" + (waitingHere.length - 18)));
      const belt = el("div", "vbelt"), running = here_.filter(c => c.running);
      running.forEach(c => belt.append(job(c)));
      if (!running.length) belt.append(el("span", "idle", id === "review" ? "wartet auf Merge oder CI" : "gerade nichts in Arbeit"));
      st.append(buf, belt);
    }
    const out = mv.filter(m => m.from === id).length, typ = typical(id, week);
    const foot = el("div", "vfoot");
    const a = el("div"); a.append(id === "done" ? "Durchlauf Ø" : "raus in 24 h", el("b", null, id === "done" ? vDur(throughput(week)) : String(out)));
    const b = el("div"); if (id !== "done") b.append("typisch", el("b", null, vDur(typ)));
    foot.append(a, b); st.append(foot);
    line.append(st);
    // what waits for the owner falls out under the station it hangs at
    const fell = cards.filter(c => youCols.includes(c.column) && !isParent(c) && hangsAt(c) === id);
    const cell = el("div");
    if (fell.length) {
      const bad = fell.some(c => c.column === "stopped");
      const ch = el("div", "vchute" + (bad ? " bad" : ""));
      const hd = el("div", "hd"); hd.append(el("span", null, bad ? "Angehalten oder wartet auf dich" : "Wartet auf dich"), el("span", null, String(fell.length)));
      ch.append(hd); fell.slice(0, 6).forEach(c => ch.append(smallRow(c)));
      if (fell.length > 6) ch.append(el("span", "vmore", `und ${fell.length - 6} weitere unter „Braucht dich“`));
      cell.append(ch);
    }
    chutes.append(cell);
  });
  sc.append(line, chutes); wrap.append(sc); box.append(wrap);
  // rework: a dashed arc from Prüfung back to Umsetzung, with how many went back in 24 h
  const back = mv.filter(m => m.rw).length;
  if (back) requestAnimationFrame(() => {
    const from = line.querySelector('[data-st="verify"] .vbelt'), to = line.querySelector('[data-st="build"] .vbelt');
    if (!from || !to) return;
    const L = line.getBoundingClientRect(), a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
    const x1 = a.left - L.left + a.width * .5, x2 = b.left - L.left + b.width * .75, y = a.top - L.top, top = Math.max(6, y - 112);
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg"); svg.setAttribute("class", "varc"); svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = `<defs><marker id="varh" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--ln-rework)"/></marker></defs>` +
      `<path d="M${x1},${y} C${x1},${top} ${x2},${top} ${x2},${y}" fill="none" stroke="var(--ln-rework)" stroke-width="1.5" stroke-dasharray="4 4" marker-end="url(#varh)"/>` +
      `<rect x="${(x1 + x2) / 2 - 70}" y="${top + 12}" width="140" height="20" rx="10" fill="var(--surface)" stroke="var(--ln-rework)"/>` +
      `<text x="${(x1 + x2) / 2}" y="${top + 26}" text-anchor="middle" font-family="var(--mono)" font-size="12" font-weight="600" fill="var(--ln-rework)">Nacharbeit · ${back} in 24 h</text>`;
    line.append(svg);
  });

  const lower = el("div", "vlower");
  lower.append(baysPanel(), assemblies());
  box.append(lower);
}
function throughput(since) {
  const d = cards.filter(c => c.end && c.end[0] === "done" && T(c.end[1]) >= since && c.lanes && c.lanes.length)
    .map(c => (T(c.end[1]) - T(c.lanes[0][1])) / 1000).sort((a, b) => a - b);
  return d.length ? d[Math.floor(d.length / 2)] : null;
}

function baysPanel() {
  const p = el("section", "vpanel"), h = el("header");
  const runners = machine();
  h.append(el("h3", null, "Arbeitsplätze"), el("span", "vlabel", runners.length ? `${runners.filter(r => r.busy).length} von ${runners.length} belegt` : "Runner dieses Projekts"));
  p.append(h);
  if (!runners.length) p.append(el("p", "vnote", "Keine eigenen Runner für dieses Projekt gefunden. Läuft es auf GitHub-Runnern, gibt es hier keine festen Plätze."));
  runners.forEach(r => {
    const c = r.job && r.job.ticket ? cards.find(x => x.number === r.job.ticket) : null;
    const b = el("div", "vbay" + (r.busy ? "" : " free"));
    const id = el("div", "id"); id.append(el("b", null, r.name), el("span", "st" + (r.busy ? " on" : ""), r.busy ? "LÄUFT" : "FREI"));
    const what = el("div", "what");
    if (r.busy && r.job) {
      const btn = el("button"); btn.type = "button"; btn.append(el("b", null, "#" + r.job.ticket), r.job.title || ""); if (c) btn.onclick = pick(c);
      const g = el("div", "vgauges");
      const gauge = (label, pct) => { const x = el("div", "vg", label); const i = el("i"), s = el("span"); s.style.width = Math.min(100, Math.max(2, pct || 0)) + "%"; i.append(s); x.append(i); return x; };
      const t = c && liveOf(c);
      g.append(gauge("CPU " + (t ? t.cpu + " %" : "–"), t && t.cpu), gauge("RAM " + (t ? t.ram + " %" : "–"), t && t.ram),
               gauge(`${r.job.label || ""} · ${Math.round(r.job.minutes || 0)} min`, Math.min(100, (r.job.minutes || 0) / 90 * 100)));
      what.append(btn, el("span", "vnote", r.job.step || ""), g);
    } else what.append(el("span", "vnote", "wartet auf den nächsten Job"));
    b.append(id, what); p.append(b);
  });
  const next = cards.filter(c => c.column === "queue" && !c.running && /freien Platz/.test(c.step || ""));
  if (next.length) { const n = el("div", "vtools"); n.append(el("span", "vnote", "Warten auf einen Platz:"), ...next.slice(0, 12).map(vChip)); p.append(n); }
  return p;
}

function assemblies() {
  const p = el("section", "vpanel"), parents = cards.filter(isParent);
  const h = el("header"); h.append(el("h3", null, "Baugruppen"), el("span", "vlabel", `geteilte Tickets · ${parents.length}`)); p.append(h);
  if (!parents.length) { p.append(el("p", "vnote", "Kein Ticket ist gerade in Teil-Tickets aufgeteilt.")); return p; }
  p.append(el("p", "vnote", "Ein aufgeteiltes Ticket läuft nicht selbst über das Band. Jedes Kästchen ist ein Teil-Ticket: grün fertig, blau in Arbeit, gelb wartet auf dich, grau noch nicht dran."));
  const byNum = new Map(cards.map(c => [c.number, c]));
  parents.forEach(c => {
    const parts = c.parts.map(n => byNum.get(n)), done = parts.filter(x => x && x.column === "done").length;
    const b = el("button", "vasm"); b.type = "button"; b.onclick = pick(c); b.title = c.title;
    const bar = el("span", "vparts");
    parts.forEach((x, i) => { const s = el("i", !x ? "" : x.column === "done" ? "d" : x.running ? "a" : ATTN.includes(x.column) ? "w" : "");
      s.title = `#${c.parts[i]}` + (x ? ` · ${x.step}` : ""); bar.append(s); });
    b.append(el("span", "n", "#" + c.number), el("span", "t", c.title), bar, el("span", "f", `${done}/${parts.length}`));
    p.append(b);
  });
  return p;
}

// ---- Fahrplan --------------------------------------------------------------------------------------------------

function timetableView(box) {
  const span = ttWindow * HOUR, end = Date.now(), start = end - span;
  const x = t => Math.max(0, Math.min(100, (t - start) / span * 100));
  const tools = el("div", "vtools");
  const seg = (opts, now, set) => { const g = el("div", "vswitch"); g.style.marginLeft = "0";
    opts.forEach(([v, l]) => { const b = el("button", null, l); b.type = "button"; b.setAttribute("aria-pressed", String(now === v)); b.onclick = () => { set(v); draw(); }; g.append(b); }); return g; };
  const shown = cards.filter(c => !isParent(c) && (c.lanes || []).some(l => T(l[2]) >= start));
  const flt = { all: () => true, run: c => c.running, you: c => youCols.includes(c.column) || c.column === "review",
    rework: c => (c.lanes || []).some(l => l[0] === "rework" && T(l[2]) >= start) };
  const count = f => shown.filter(flt[f]).length;
  tools.append(seg([[12, "12 h"], [36, "36 h"], [168, "7 Tage"]], ttWindow, v => { ttWindow = v; store("board-tt-window", String(v)); }),
    seg([["all", `Alle ${count("all")}`], ["run", `Laufen ${count("run")}`], ["you", `Brauchen dich ${count("you")}`], ["rework", `Mit Nacharbeit ${count("rework")}`]], ttFilter, v => { ttFilter = v; }));
  const legend = el("div", "vlegend");
  ["plan", "build", "rework", "verify", "wait", "pr", "stopped", "queue"].forEach(l => { const s = el("span"); const i = el("i"); i.style.background = `var(--ln-${l})`; s.append(i, LANE_NAME[l]); legend.append(s); });
  tools.append(legend); box.append(tools);

  const panel = el("div", "vpanel"); panel.style.padding = "0"; const sc = el("div", "vscroll"), tt = el("div", "vtt"); sc.append(tt); panel.append(sc);
  // axis
  const ax = el("div", "vtt-row vtt-axis"), axl = el("div", "vtt-lab"); axl.append(el("span", "vlabel", "Zeit")); const ticks = el("div", "vtt-track");
  const step = ttWindow <= 12 ? 1 : ttWindow <= 36 ? 3 : 24, grid = el("div", "vtt-grid");
  const first = new Date(start); first.setMinutes(0, 0, 0); if (step === 24) first.setHours(0);
  for (let t = first.getTime(); t <= end; t += step * HOUR) {
    if (t < start) continue;
    const d = new Date(t), dayMark = d.getHours() === 0;
    const k = el("div", "vtt-tick" + (dayMark ? " day" : ""), dayMark ? `${d.getDate()}.${d.getMonth() + 1}.` : String(d.getHours()).padStart(2, "0") + ":00");
    k.style.left = x(t) + "%"; ticks.append(k);
    const g = el("i"); g.style.left = `calc(${x(t)}% )`; grid.append(g);
  }
  ax.append(axl, ticks); tt.append(ax);
  const gridWrap = el("div"); gridWrap.style.cssText = "position:relative";
  const bar = (lane, s, e, label, c, live) => {
    const b = el("button", "tseg" + (lane === "queue" ? " hatch" : "") + (live ? " live" : "")); b.type = "button";
    b.style.left = x(s) + "%"; b.style.width = Math.max(.25, x(e) - x(s)) + "%"; b.style.background = `var(--ln-${lane})`;
    b.title = `${c ? "#" + c.number + " · " : ""}${LANE_NAME[lane] || lane}${label && c && label !== "#" + c.number ? " · " + label : ""}\n` +
      `${new Date(s).toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" })} – ${live ? "jetzt" : new Date(e).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })} · ${dur((e - s) / 1000)}`;
    if (label) b.append(el("span", null, label));
    if (c) b.onclick = pick(c);
    return b;
  };
  // the runners: who held which place when
  const runners = machine(), names = new Map();
  runners.forEach(r => names.set(r.name, []));
  cards.forEach(c => (c.bays || []).forEach(([full, s, e, st]) => {
    if (T(e) < start) return; const n = bayName(full); if (!names.has(n)) names.set(n, []);
    names.get(n).push([st === "judge" ? "verify" : ["planner", "intake", "plan_check", "triage"].includes(st) ? "plan" : st === "rework" ? "rework" : ["implementer"].includes(st) ? "build" : "verify", T(s), T(e), c]);
  }));
  runners.forEach(r => { if (r.busy && r.job) { const c = cards.find(x => x.number === r.job.ticket); if (c) names.get(r.name).push(["build", end - (r.job.minutes || 0) * 60e3, end, c, true]); } });
  if (names.size) {
    const g = el("div", "vtt-group"), gl = el("div", "vtt-lab"); gl.append(el("b", null, "Arbeitsplätze"), el("span", "vlabel", String(names.size)));
    g.append(gl, el("div", "sum", "Lücken: der Runner stand leer")); tt.append(g);
    [...names.keys()].sort().forEach(n => {
      const row = el("div", "vtt-row vtt-bay"), lab = el("div", "vtt-lab"), tr = el("div", "vtt-track");
      lab.append(el("span", "t", n));
      names.get(n).forEach(([lane, s, e, c, live]) => tr.append(bar(lane, Math.max(s, start), e, "#" + c.number, c, live)));
      row.append(lab, tr); tt.append(row);
    });
  }
  // the tickets
  const rows = shown.filter(flt[ttFilter]).sort((a, b) => (b.running - a.running) || (youCols.includes(b.column) - youCols.includes(a.column)) ||
    (T((b.lanes.at(-1) || [])[2] || 0) - T((a.lanes.at(-1) || [])[2] || 0)));
  const hidden = cards.filter(isParent).length;
  const g2 = el("div", "vtt-group"), gl2 = el("div", "vtt-lab"); gl2.append(el("b", null, "Tickets"), el("span", "vlabel", `${Math.min(rows.length, 80)} gezeigt`));
  g2.append(gl2, el("div", "sum", hidden ? `${hidden} aufgeteilte Tickets stehen unter Baugruppen (Ansicht Linie)` : "")); tt.append(g2);
  rows.slice(0, 80).forEach(c => {
    const row = el("div", "vtt-row"), lab = el("div", "vtt-lab"), tr = el("div", "vtt-track");
    const btn = el("button"); btn.type = "button"; btn.onclick = pick(c); btn.title = c.title;
    btn.append(el("span", "n", "#" + c.number), el("span", "t", c.title)); lab.append(btn);
    if (c.running) lab.append(el("span", "vpill run", "LÄUFT")); else if (youCols.includes(c.column) || c.column === "review") lab.append(el("span", "vpill you", "DU"));
    const ls = c.lanes || [];
    ls.forEach((l, i) => { if (T(l[2]) < start) return; const live = i === ls.length - 1 && !c.end && c.running;
      tr.append(bar(l[0], Math.max(T(l[1]), start), T(l[2]), i === ls.length - 1 && !c.end ? c.step : "", c, live)); });
    row.append(lab, tr); tt.append(row);
  });
  if (!rows.length) tt.append(el("div", "vempty", "In diesem Zeitraum und mit diesem Filter gibt es keine Tickets."));
  const now = el("div", "vtt-now"); grid.append(now); tt.append(grid);
  box.append(panel);

  // what the hours say
  const sum = lane => cards.reduce((n, c) => n + (c.lanes || []).filter(l => l[0] === lane).reduce((m, l) => m + Math.max(0, Math.min(end, T(l[2])) - Math.max(start, T(l[1]))), 0), 0) / 1000;
  const busyBays = [...names.values()].reduce((n, segs) => n + segs.reduce((m, s) => m + Math.max(0, s[2] - Math.max(start, s[1])), 0), 0);
  const load = names.size ? Math.round(busyBays / (names.size * span) * 100) : null;
  const stats = el("div", "vstats");
  const stat = (label, value, text) => { const s = el("div", "vstat"); s.append(el("span", "vlabel", label), el("b", null, value), el("p", null, text)); return s; };
  stats.append(
    stat("Auslastung der Plätze", load == null ? "–" : load + " %", "Anteil der Zeit, in der die Runner dieses Projekts einen Job hatten."),
    stat("Wartezeit auf einen Platz", vDur(sum("queue")), "Summe der grauen Abschnitte im Zeitraum."),
    stat("Nacharbeit", vDur(sum("rework")), `${cards.filter(c => (c.lanes || []).some(l => l[0] === "rework" && T(l[2]) >= start)).length} Tickets hatten Nacharbeit.`),
    stat("Lag bei dir", vDur(sum("wait") + sum("pr")), "Rückfragen und offene Pull Requests, die auf deinen Merge warteten."));
  box.append(stats);
}

// ---- Stellwerk -------------------------------------------------------------------------------------------------

const NODES = { queue: ["Eingang", 70, 200], plan: ["Planung", 235, 200], build: ["Umsetzung", 420, 200], verify: ["Prüfung", 605, 200],
  review: ["Pull Request", 780, 200], done: ["Fertig", 935, 200], you: ["Du", 540, 385], asm: ["Baugruppen", 420, 62] };
function nodeCards(id) {
  if (id === "you") return cards.filter(c => youCols.includes(c.column) && !isParent(c));
  if (id === "asm") return cards.filter(isParent);
  return cards.filter(c => c.column === id && !isParent(c));
}

function signalView(box) {
  const since = Date.now() - sigWindow * HOUR, mv = moves(since), runners = machine();
  const tally = {}; mv.forEach(m => { const k = m.from + ">" + m.to + (m.rw ? ">rw" : ""); tally[k] = (tally[k] || 0) + 1; });
  // signals
  const sigs = el("div", "vsigs");
  const sig = (tone, title, text) => { const s = el("div", "vsignal"), i = el("i"); i.style.background = `var(--${tone})`; const d = el("div"); d.append(el("b", null, title), el("p", null, text)); s.append(i, d); return s; };
  const busy = runners.filter(r => r.busy).length, waitPlace = cards.filter(c => c.column === "queue" && /freien Platz/.test(c.step || "")).length;
  const you = nodeCards("you"), prs = cards.filter(c => c.column === "review");
  const doneN = mv.filter(m => m.to === "done").length, newN = cards.filter(c => c.lanes && c.lanes.length && T(c.lanes[0][1]) >= since).length;
  sigs.append(
    sig(runners.length && busy === runners.length && waitPlace ? "warn" : "ok", runners.length ? `Plätze: ${busy} von ${runners.length} belegt` : "Plätze",
        waitPlace ? `${waitPlace} Tickets warten auf einen freien Platz.` : "Kein Ticket wartet auf einen Platz."),
    sig(you.length + prs.length ? "warn" : "ok", `${you.length + prs.length} liegen bei dir`,
        `${prs.length} Pull Requests, ${you.filter(c => c.column === "waiting").length} Rückfragen, ${you.filter(c => c.column === "stopped").length} angehalten, ${you.filter(c => c.column === "proposed").length} Vorschläge.`),
    sig(doneN >= newN ? "ok" : "warn", "Abfluss", `${doneN} fertig und ${newN} neu im Zeitraum.`));
  box.append(sigs);

  const main = el("div", "vsig-main"), map = el("section", "vpanel vsig");
  const h = el("header"); h.append(el("h3", null, "Netz der Stationen"));
  const g = el("div", "vswitch"); g.style.marginLeft = "0";
  [[6, "6 h"], [24, "24 h"], [168, "7 Tage"]].forEach(([v, l]) => { const b = el("button", null, l); b.type = "button"; b.setAttribute("aria-pressed", String(sigWindow === v));
    b.onclick = () => { sigWindow = v; store("board-sig-window", String(v)); draw(); }; g.append(b); });
  h.append(g); map.append(h, el("p", "vnote", "Kreisgröße: Tickets, die dort liegen. Linienstärke: Tickets, die im Zeitraum von einer Station zur nächsten gingen. Orange: Nacharbeit nach der Prüfung, gelb: zu dir und von dir. Klick auf einen Knoten zeigt seine Tickets."));
  const sc = el("div", "vscroll"), NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 1000 470"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Stationen und Flüsse");
  const mk = (t, a, p = svg) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); p.append(e); return e; };
  const wip = id => nodeCards(id).length, rad = id => 16 + Math.sqrt(wip(id)) * 4;
  const order = ["queue", "plan", "build", "verify", "review", "done"];
  const most = Math.max(1, ...Object.values(tally));   // the busiest line is the thickest, whatever the window
  Object.entries(tally).forEach(([k, n]) => {
    const [a, b, rw] = k.split(">"); if (!NODES[a] || !NODES[b]) return;
    const [, ax, ay] = NODES[a], [, bx, by] = NODES[b];
    let d, cls = "vflow";
    if (a === "you" || b === "you") { cls += " you"; d = `M${ax},${ay} C${ax},${(ay + by) / 2 + 40} ${bx},${(ay + by) / 2} ${bx},${by}`; }
    else if (a === "asm" || b === "asm") { cls += " asm"; d = `M${ax},${ay} C${ax},${(ay + by) / 2} ${bx},${(ay + by) / 2} ${bx},${by}`; }
    else {
      const ia = order.indexOf(a), ib = order.indexOf(b);
      if (ib === ia + 1) d = `M${ax},${ay} L${bx},${by}`;
      else { const lift = (ib > ia ? -1 : -1) * (70 + Math.abs(ib - ia) * 22) * (ib > ia ? 0.6 : 1);
        d = `M${ax},${ay - rad(a) * .6} C${ax},${ay + lift} ${bx},${by + lift} ${bx},${by - rad(b) * .6}`; }
      if (rw) cls += " rw";
    }
    const p = mk("path", { d, class: cls, "stroke-width": 2 + 12 * n / most });
    p.append(Object.assign(document.createElementNS(NS, "title"), { textContent: `${NODES[a][0]} → ${NODES[b][0]}: ${n}` }));
    const L = p.getTotalLength ? p.getTotalLength() : 0;
    if (L) { const pt = p.getPointAtLength(L / 2); const tone = rw ? "var(--ln-rework)" : cls.includes("you") ? "var(--warn)" : "var(--text-2)";
      mk("rect", { x: pt.x - 13, y: pt.y - 9, width: 26, height: 18, rx: 9, fill: "var(--surface)", stroke: tone });
      mk("text", { x: pt.x, y: pt.y + 4, "text-anchor": "middle", "font-size": 10.5, "font-weight": 600, fill: tone }).textContent = n; }
  });
  Object.entries(NODES).forEach(([id, [name, x, y]]) => {
    const r = rad(id), g = mk("g", { class: "vnode" + (id === sigNode ? " sel" : ""), tabindex: 0, role: "button", "aria-label": `${name}: ${wip(id)} Tickets` });
    const col = id === "you" ? "var(--warn)" : id === "done" ? "var(--ok)" : id === "asm" ? "var(--line-3)" : "var(--accent)";
    mk("circle", { cx: x, cy: y, r: r + 5, fill: "var(--bg)", class: "ring", stroke: "var(--line-2)", "stroke-width": 1 }, g);
    mk("circle", { cx: x, cy: y, r, fill: "var(--surface-2)", stroke: col, "stroke-width": 2 }, g);
    mk("text", { x, y: y + 5, "text-anchor": "middle", "font-size": r > 30 ? 17 : 14, "font-weight": 700, fill: "var(--text)" }, g).textContent = wip(id);
    const above = id === "asm";
    mk("text", { x, y: above ? y - r - 12 : y + r + 21, "text-anchor": "middle", "font-size": 12, "font-weight": 600, fill: "var(--text)", style: "font-family: var(--sans)" }, g).textContent = name;
    const typ = ["queue", "plan", "build", "verify", "review", "you"].includes(id) ? typical(id, Date.now() - 7 * 24 * HOUR) : null;
    if (typ != null) mk("text", { x, y: y + r + 35, "text-anchor": "middle", "font-size": 10, fill: "var(--muted)" }, g).textContent = "typisch " + dur(typ);
    nodeCards(id).filter(c => c.running).slice(0, 4).forEach((c, i) => {
      const a = -Math.PI / 2 + (i - 0.5) * 0.8, px = x + Math.cos(a) * (r + 5), py = y + Math.sin(a) * (r + 5);
      mk("circle", { cx: px, cy: py, r: 5.5, fill: "var(--accent)", class: "vdot" }, g);
      mk("text", { x: px, y: py - 9, "text-anchor": "middle", "font-size": 9.5, "font-weight": 600, fill: "var(--accent)" }, g).textContent = "#" + c.number;
    });
    const go = () => { sigNode = id; draw(); };
    g.addEventListener("click", go); g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
  sc.append(svg); map.append(sc);

  // the selected station's tickets; at "Du" with the actions
  const side = el("aside", "vpanel"), [name] = NODES[sigNode], list = nodeCards(sigNode);
  const sh = el("header"); sh.append(el("h3", null, name), el("span", "vlabel", "Station")); side.append(sh);
  const facts = el("div", "vfacts");
  const fact = (v, l) => { const f = el("div", "vfact"); f.append(el("b", null, v), el("span", null, l)); return f; };
  const outN = mv.filter(m => m.from === sigNode).length, inN = mv.filter(m => m.to === sigNode).length;
  facts.append(fact(String(list.length), "liegen hier"), fact(`${inN} / ${outN}`, "rein / raus"), fact(vDur(typical(sigNode, Date.now() - 7 * 24 * HOUR)), "typisch"));
  side.append(facts);
  const lst = el("div", "vlist");
  if (sigNode === "you" || sigNode === "review") list.slice(0, 30).forEach(c => lst.append(item(c)));
  else list.slice(0, 40).forEach(c => lst.append(smallRow(c, c.running ? el("span", "vpill run", "LÄUFT") : c.round > 1 ? el("span", "vpill rw", "RUNDE " + c.round) : ago((c.last || {}).ts || new Date().toISOString()))));
  if (!list.length) lst.append(el("div", "vempty", "leer"));
  if (list.length > 40) lst.append(el("span", "vmore", `und ${list.length - 40} weitere`));
  side.append(lst);
  main.append(map, side); box.append(main);
}

// Called by draw() for every view but kanban.
function drawView(box) {
  const v = el("div", "vbox"); box.append(v);
  if (flowView === "line") lineView(v); else if (flowView === "timetable") timetableView(v); else if (flowView === "signal") signalView(v);
}

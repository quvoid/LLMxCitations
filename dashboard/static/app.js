"use strict";

const PLATFORM_LABEL = {
  chatgpt: "ChatGPT",
  chatgpt_anon: "ChatGPT (logged out)",
  gemini: "Gemini",
  perplexity: "Perplexity",
};
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];
const PAGE = 50;

const state = {
  data: null,
  platform: "all",
  date: "all",
  own: null,
  tab: "overview",
  promptPage: 0,
  promptSort: { key: "answers", dir: -1 },
  sourcePage: 0,
  sourceSort: { key: "answers", dir: -1 },
  openDomain: null,
};

const $ = (sel) => document.querySelector(sel);
const pct = (v) => (v * 100 >= 10 || v === 0 ? Math.round(v * 100) : (v * 100).toFixed(1)) + "%";
const fmt = (n) => n.toLocaleString("en-US");
const platformName = (p) => PLATFORM_LABEL[p] || p;

// Every string from the data (prompts, answers, URLs) is untrusted; build DOM with textContent only.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}
function safeLink(url, label) {
  return /^https?:\/\//i.test(url)
    ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label || url })
    : el("span", { text: label || url });
}
function store(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* storage unavailable */ }
}
function recall(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

// ---------- tooltip ----------
const tip = $("#tooltip");
function showTip(evt, title, rows) {
  tip.replaceChildren(el("div", { class: "t-title", text: title }));
  for (const r of rows) {
    tip.append(el("div", { class: "t-row" },
      r.color ? el("i", { style: `background:${r.color}` }) : null,
      el("b", { text: r.value }), el("span", { text: r.label })));
  }
  tip.hidden = false;
  const pad = 14;
  const { innerWidth: w, innerHeight: h } = window;
  const rect = tip.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + rect.width > w - 8) x = evt.clientX - rect.width - pad;
  if (y + rect.height > h - 8) y = evt.clientY - rect.height - pad;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
const hideTip = () => { tip.hidden = true; };

// ---------- data slicing ----------
function responses({ platform = state.platform, date = state.date } = {}) {
  return state.data.responses.filter((r) =>
    (platform === "all" || r.pl === platform) && (date === "all" || r.d === date));
}

function brandStats(resps) {
  const n = state.data.brands.length;
  const s = Array.from({ length: n }, (_, i) => ({ i, name: state.data.brands[i], answers: 0, mentions: 0, rankSum: 0, top: 0 }));
  let totalMentions = 0;
  for (const r of resps) {
    for (const [b, count, rank] of r.b) {
      s[b].answers++;
      s[b].mentions += count;
      s[b].rankSum += rank;
      if (rank === 1) s[b].top++;
      totalMentions += count;
    }
  }
  for (const b of s) {
    b.vis = resps.length ? b.answers / resps.length : 0;
    b.sov = totalMentions ? b.mentions / totalMentions : 0;
    b.avgRank = b.answers ? b.rankSum / b.answers : null;
  }
  return s;
}

function ownIndex() {
  return state.data.brands.indexOf(state.own);
}

// Colors follow the brand, not its rank under the current filter: your brand is
// slot 1, then competitors by visibility across the whole project.
function seriesBrands() {
  const all = brandStats(state.data.responses).filter((b) => b.answers).sort((a, b) => b.vis - a.vis);
  const own = all.find((b) => b.name === state.own);
  const rest = all.filter((b) => b !== own);
  return (own ? [own, ...rest] : rest).slice(0, SERIES.length).map((b, k) => ({ ...b, color: SERIES[k] }));
}

// ---------- boot ----------
async function boot() {
  const projects = await fetch("/api/projects").then((r) => r.json());
  const pick = $("#project");
  for (const p of projects) pick.append(el("option", { value: p.id, text: p.name }));
  const saved = recall("aiv.project");
  if (saved && projects.some((p) => p.id === saved)) pick.value = saved;
  pick.addEventListener("change", () => { store("aiv.project", pick.value); loadProject(pick.value); });

  document.querySelectorAll(".tabs button").forEach((btn) =>
    btn.addEventListener("click", () => setTab(btn.dataset.tab)));
  $("#date").addEventListener("change", (e) => { state.date = e.target.value; resetPages(); render(); });
  $("#own").addEventListener("change", (e) => {
    state.own = e.target.value;
    store(`aiv.own.${state.data.id}`, state.own);
    render();
  });
  $("#prompt-search").addEventListener("input", () => { state.promptPage = 0; renderPrompts(); });
  $("#prompt-filter").addEventListener("change", () => { state.promptPage = 0; renderPrompts(); });
  $("#source-search").addEventListener("input", () => { state.sourcePage = 0; renderSources(); });
  $("#source-cat").addEventListener("change", () => { state.sourcePage = 0; renderSources(); });
  $("#source-owner").addEventListener("change", () => { state.sourcePage = 0; renderSources(); });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => state.tab === "overview" && renderTrend(), 150); });

  if (pick.value) loadProject(pick.value);
  else $("#status").textContent = "No scraper output found. Add a project to dashboard/config.json.";
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== `tab-${tab}`; });
  render();
}

function resetPages() { state.promptPage = 0; state.sourcePage = 0; }

async function loadProject(id) {
  $("#status").textContent = "Loading… (a large CSV takes a few seconds the first time)";
  document.querySelectorAll(".tab").forEach((s) => { s.style.opacity = 0.4; });
  const data = await fetch(`/api/project/${encodeURIComponent(id)}`).then((r) => r.json());
  state.data = data;
  state.platform = "all";
  state.date = "all";
  resetPages();
  closeDrawer();

  const platforms = [...new Set(data.responses.map((r) => r.pl))].sort();
  const seg = $("#platforms");
  seg.replaceChildren();
  for (const p of ["all", ...platforms]) {
    seg.append(el("button", {
      "aria-pressed": String(p === "all"),
      text: p === "all" ? "All platforms" : platformName(p),
      onclick: (e) => {
        state.platform = p;
        seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
        resetPages();
        render();
      },
    }));
  }

  const dates = [...new Set(data.responses.map((r) => r.d))].sort().reverse();
  $("#date").replaceChildren(el("option", { value: "all", text: "All dates" }), ...dates.map((d) => el("option", { value: d, text: d })));

  const ranked = brandStats(data.responses).filter((b) => b.answers).sort((a, b) => b.vis - a.vis);
  const savedOwn = recall(`aiv.own.${data.id}`);
  state.own = [savedOwn, data.own_brand, ranked[0] && ranked[0].name].find((b) => b && data.brands.includes(b)) || data.brands[0];
  $("#own").replaceChildren(...data.brands.map((b) => el("option", { value: b, text: b })));
  $("#own").value = state.own;

  const cats = [...new Set(data.urls.map((u) => u.cat))].sort();
  $("#source-cat").replaceChildren(el("option", { value: "all", text: "All types" }), ...cats.map((c) => el("option", { value: c, text: c })));

  $("#source-note").textContent = `Source: ${data.source} · ${fmt(data.responses.length)} answers`;
  $("#status").textContent = "";
  document.querySelectorAll(".tab").forEach((s) => { s.style.opacity = ""; });
  render();
}

function render() {
  if (!state.data) return;
  if (state.tab === "overview") renderOverview();
  if (state.tab === "prompts") renderPrompts();
  if (state.tab === "sources") renderSources();
}

// ---------- overview ----------
function renderOverview() {
  const resps = responses();
  const stats = brandStats(resps);
  const own = stats[ownIndex()];
  const promptsSeen = new Set(resps.map((r) => r.p)).size;
  const domains = new Set();
  let citations = 0;
  for (const r of resps) for (const u of r.c) { domains.add(state.data.urls[u].dom); citations++; }

  const kpi = (label, value, sub) => el("div", { class: "kpi" },
    el("div", { class: "label", text: label }), el("div", { class: "value", text: value }), el("div", { class: "sub", text: sub || "" }));
  $("#kpis").replaceChildren(
    kpi("Prompts tracked", fmt(promptsSeen), `${fmt(resps.length)} AI answers`),
    kpi(`${state.own} visibility`, pct(own.vis), `in ${fmt(own.answers)} of ${fmt(resps.length)} answers`),
    kpi(`${state.own} avg. position`, own.avgRank ? "#" + own.avgRank.toFixed(1) : "—", "among brands named in an answer"),
    kpi(`${state.own} ranked #1`, fmt(own.top), resps.length ? `${pct(own.top / resps.length)} of answers` : ""),
    kpi(`${state.own} share of voice`, pct(own.sov), "of all brand mentions"),
    kpi("Citations", fmt(citations), `${fmt(domains.size)} unique domains`),
  );

  renderLeaderboard(stats);
  renderOwned(resps);
  renderTrend();
  renderByPlatform();
  renderDomains(resps);
  renderCategories(resps);
  renderQueries(resps);
}

function renderLeaderboard(stats) {
  const rows = stats.filter((b) => b.answers).sort((a, b) => b.vis - a.vis || b.top - a.top);
  const table = $("#leaderboard");
  if (!rows.length) { table.replaceChildren(el("tr", {}, el("td", { class: "empty", text: "No tracked brands mentioned in this selection." }))); return; }
  const maxVis = rows[0].vis || 1;
  table.replaceChildren(
    el("thead", {}, el("tr", {},
      el("th", { class: "num", text: "#" }), el("th", { text: "Brand" }), el("th", { text: "Visibility" }),
      el("th", { class: "num", text: "Share of voice" }), el("th", { class: "num", text: "Avg. position" }),
      el("th", { class: "num", text: "Ranked #1" }), el("th", { class: "num", text: "Mentions" }))),
    el("tbody", {}, rows.map((b, k) => el("tr", { class: b.name === state.own ? "you" : null },
      el("td", { class: "num", text: k + 1 }),
      el("td", {}, b.name, b.name === state.own ? el("span", { class: "tag", text: "You" }) : null),
      el("td", {}, el("div", { class: "barcell" },
        el("div", { class: "bar" }, el("i", { style: `width:${(b.vis / maxVis) * 100}%` })),
        el("span", { class: "num", text: pct(b.vis) }))),
      el("td", { class: "num", text: pct(b.sov) }),
      el("td", { class: "num", text: b.avgRank ? b.avgRank.toFixed(1) : "—" }),
      el("td", { class: "num", text: fmt(b.top) }),
      el("td", { class: "num", text: fmt(b.mentions) }),
    ))),
  );
}

// Which brands' own websites the AI uses as sources.
function renderOwned(resps) {
  const d = state.data;
  const card = $("#owned-card");
  card.hidden = !d.urls.some((u) => u.own != null);
  if (card.hidden) return;
  const per = new Map();
  for (const r of resps) {
    const seen = new Set();
    for (const u of r.c) {
      const b = d.urls[u].own;
      if (b == null) continue;
      const e = per.get(b) || { answers: 0, cites: 0, pages: new Set(), prompts: new Set() };
      e.cites++;
      e.pages.add(u);
      e.prompts.add(r.p);
      if (!seen.has(b)) { e.answers++; seen.add(b); }
      per.set(b, e);
    }
  }
  const own = ownIndex();
  if (!per.has(own)) per.set(own, { answers: 0, cites: 0, pages: new Set(), prompts: new Set() });
  const rows = [...per].sort((a, b) => b[1].answers - a[1].answers || b[1].cites - a[1].cites);
  const max = Math.max(...rows.map(([, e]) => e.answers), 1);
  $("#owned").replaceChildren(
    el("thead", {}, el("tr", {},
      el("th", { class: "num", text: "#" }), el("th", { text: "Brand website" }), el("th", { text: "Answers citing it" }),
      el("th", { class: "num", text: "Prompts" }), el("th", { class: "num", text: "Pages cited" }), el("th", { class: "num", text: "Citations" }))),
    el("tbody", {}, rows.map(([b, e], k) => el("tr", { class: b === own ? "you" : null },
      el("td", { class: "num", text: k + 1 }),
      el("td", {}, d.brands[b], b === own ? el("span", { class: "tag", text: "You" }) : null),
      el("td", {}, el("div", { class: "barcell" },
        el("div", { class: "bar" }, el("i", { style: `width:${(e.answers / max) * 100}%` })),
        el("span", { class: "num", text: `${fmt(e.answers)} · ${pct(resps.length ? e.answers / resps.length : 0)}` }))),
      el("td", { class: "num", text: fmt(e.prompts.size) }),
      el("td", { class: "num", text: fmt(e.pages.size) }),
      el("td", { class: "num", text: fmt(e.cites) }),
    ))),
  );
}

function ownerChip(b) {
  if (b == null) return null;
  const name = state.data.brands[b];
  return el("span", { class: "chip" + (name === state.own ? " you" : ""), text: name === state.own ? "Your site" : name });
}

function renderTrend() {
  const host = $("#trend");
  const dates = [...new Set(responses({ date: "all" }).map((r) => r.d))].sort();
  if (dates.length < 2) {
    host.replaceChildren(el("p", { class: "empty", text: `Only one scrape date so far (${dates[0] || "none"}). Run the scraper again on another day and the trend appears here.` }));
    return;
  }
  const series = seriesBrands().map((b) => ({
    ...b,
    values: dates.map((d) => {
      const rs = responses({ date: d });
      return rs.length ? rs.filter((r) => r.b.some(([i]) => i === b.i)).length / rs.length : 0;
    }),
  }));
  const W = Math.max(host.clientWidth, 280), H = 240;
  const m = { t: 12, r: 16, b: 26, l: 40 };
  const maxV = Math.min(1, Math.ceil((Math.max(...series.flatMap((s) => s.values)) || 0.1) * 10) / 10);
  const x = (k) => m.l + (k * (W - m.l - m.r)) / (dates.length - 1);
  const y = (v) => H - m.b - (v / maxV) * (H - m.t - m.b);

  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Visibility over time" });
  for (let k = 0; k <= 4; k++) {
    const v = (maxV * k) / 4;
    root.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: k ? "var(--grid)" : "var(--axis)", "stroke-width": 1 }));
    const t = svg("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = Math.round(v * 100) + "%"; root.append(t);
  }
  const tickEvery = Math.ceil(dates.length / Math.max(2, Math.floor((W - m.l - m.r) / 90)));
  dates.forEach((d, k) => {
    if (k % tickEvery && k !== dates.length - 1) return;
    const t = svg("text", { x: x(k), y: H - 8, "text-anchor": k === 0 ? "start" : k === dates.length - 1 ? "end" : "middle" });
    t.textContent = d.slice(5); root.append(t);
  });
  for (const s of series) {
    root.append(svg("polyline", {
      points: s.values.map((v, k) => `${x(k)},${y(v)}`).join(" "),
      fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    s.values.forEach((v, k) => root.append(svg("circle", { cx: x(k), cy: y(v), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 })));
  }
  const cross = svg("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" });
  root.append(cross);
  const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  hit.addEventListener("pointermove", (e) => {
    const box = root.getBoundingClientRect();
    const k = Math.max(0, Math.min(dates.length - 1, Math.round(((e.clientX - box.left - m.l) / (W - m.l - m.r)) * (dates.length - 1))));
    cross.setAttribute("x1", x(k)); cross.setAttribute("x2", x(k)); cross.setAttribute("visibility", "visible");
    const rows = series.map((s) => ({ color: s.color, value: pct(s.values[k]), label: s.name, v: s.values[k] })).sort((a, b) => b.v - a.v);
    showTip(e, dates[k], rows);
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  root.append(hit);

  const legend = el("div", { class: "legend" }, series.map((s) => el("span", {}, el("i", { style: `background:${s.color}` }), s.name)));
  host.replaceChildren(root, legend);
}

function renderByPlatform() {
  const table = $("#by-platform");
  const platforms = [...new Set(state.data.responses.map((r) => r.pl))].sort();
  const per = Object.fromEntries(platforms.map((p) => [p, brandStats(responses({ platform: p }))]));
  const overall = brandStats(responses({ platform: "all" })).filter((b) => b.answers).sort((a, b) => b.vis - a.vis).slice(0, 10);
  if (!overall.length) { table.replaceChildren(); return; }
  const step = (v) => (v <= 0 ? 0 : Math.min(6, Math.ceil(v * 6)));
  table.replaceChildren(
    el("thead", {}, el("tr", {}, el("th", { text: "Brand" }), platforms.map((p) => el("th", { class: "num", text: platformName(p) })))),
    el("tbody", {}, overall.map((b) => el("tr", { class: b.name === state.own ? "you" : null },
      el("td", {}, b.name, b.name === state.own ? el("span", { class: "tag", text: "You" }) : null),
      platforms.map((p) => {
        const v = per[p][b.i].vis, s = step(v);
        const cell = el("td", {
          class: "cell",
          text: pct(v),
          style: s ? `background:var(--q${s});color:var(--qi${s})` : "color:var(--muted)",
        });
        cell.addEventListener("pointermove", (e) => showTip(e, `${b.name} · ${platformName(p)}`, [
          { value: pct(v), label: "of answers mention it" },
          { value: fmt(per[p][b.i].top), label: "answers rank it #1" },
        ]));
        cell.addEventListener("pointerleave", hideTip);
        return cell;
      }),
    ))),
  );
}

function barList(host, rows, emptyText) {
  if (!rows.length) { host.replaceChildren(el("p", { class: "empty", text: emptyText })); return; }
  const max = Math.max(...rows.map((r) => r.v)) || 1;
  host.replaceChildren(...rows.map((r) => {
    const row = el("div", { class: "row" },
      el("span", { class: "name", title: r.name, text: r.name }),
      el("div", { class: "bar" }, el("i", { style: `width:${(r.v / max) * 100}%` })),
      el("span", { class: "num", text: pct(r.v) }));
    row.addEventListener("pointermove", (e) => showTip(e, r.name, r.tip));
    row.addEventListener("pointerleave", hideTip);
    return row;
  }));
}

function renderDomains(resps) {
  const byDom = new Map();
  for (const r of resps) {
    const seen = new Set();
    for (const u of r.c) {
      const d = state.data.urls[u].dom;
      const e = byDom.get(d) || { answers: 0, cites: 0 };
      e.cites++;
      if (!seen.has(d)) { e.answers++; seen.add(d); }
      byDom.set(d, e);
    }
  }
  const rows = [...byDom].sort((a, b) => b[1].answers - a[1].answers).slice(0, 12).map(([d, e]) => ({
    name: d, v: resps.length ? e.answers / resps.length : 0,
    tip: [{ value: fmt(e.answers), label: "answers cite it" }, { value: fmt(e.cites), label: "citations" }],
  }));
  barList($("#top-domains"), rows, "No citations in this selection.");
}

function renderCategories(resps) {
  const counts = new Map();
  let total = 0;
  for (const r of resps) for (const u of r.c) { const c = state.data.urls[u].cat; counts.set(c, (counts.get(c) || 0) + 1); total++; }
  const rows = [...counts].sort((a, b) => b[1] - a[1]).map(([c, n]) => ({
    name: c, v: total ? n / total : 0, tip: [{ value: fmt(n), label: "citations" }],
  }));
  barList($("#categories"), rows, "No citations in this selection.");
}

function renderQueries(resps) {
  const counts = new Map();
  for (const r of resps) for (const q of r.q) counts.set(q, (counts.get(q) || 0) + 1);
  const host = $("#queries");
  if (!counts.size) {
    host.replaceChildren(el("p", { class: "empty", text: "No search queries captured for this selection. They come from ChatGPT network payloads (the raw_payload column), which only newer runs save." }));
    return;
  }
  const top = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 30);
  host.replaceChildren(...top.map(([q, n]) => el("div", { class: "q" }, el("span", { text: q }), el("span", { text: n > 1 ? `×${n}` : "" }))));
}

// ---------- prompts ----------
function promptRows() {
  const own = ownIndex();
  const byPrompt = new Map();
  for (const r of responses()) {
    let e = byPrompt.get(r.p);
    if (!e) byPrompt.set(r.p, (e = { p: r.p, answers: 0, platforms: new Set(), urls: new Set(), ownRanks: [], brands: new Map() }));
    e.answers++;
    e.platforms.add(r.pl);
    r.c.forEach((u) => e.urls.add(u));
    for (const [b, , rank] of r.b) {
      const s = e.brands.get(b) || { n: 0, rankSum: 0, top: 0 };
      s.n++; s.rankSum += rank; if (rank === 1) s.top++;
      e.brands.set(b, s);
      if (b === own) e.ownRanks.push(rank);
    }
  }
  return [...byPrompt.values()].map((e) => {
    const ordered = [...e.brands].map(([b, s]) => ({ b, avg: s.rankSum / s.n, top: s.top, n: s.n })).sort((a, c) => a.avg - c.avg || c.n - a.n);
    const leader = [...ordered].sort((a, c) => c.top - a.top || a.avg - c.avg)[0];
    return {
      ...e,
      text: state.data.prompts[e.p],
      ordered,
      leader: leader && leader.top ? state.data.brands[leader.b] : null,
      ownAvg: e.ownRanks.length ? e.ownRanks.reduce((a, b) => a + b, 0) / e.ownRanks.length : null,
      cites: e.urls.size,
    };
  });
}

function sortRows(rows, sort, getters) {
  const get = getters[sort.key];
  return rows.sort((a, b) => {
    const va = get(a), vb = get(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sort.dir;
  });
}

function header(cols, sort, onSort) {
  return el("thead", {}, el("tr", {}, cols.map((c) => el("th", {
    class: [c.num ? "num" : "", c.key ? "sortable" : ""].join(" ").trim() || null,
    "aria-sort": c.key && sort.key === c.key ? (sort.dir > 0 ? "ascending" : "descending") : null,
    text: c.label,
    onclick: c.key ? () => onSort(c.key) : null,
  }))));
}

function pager(host, page, total, onPage) {
  const pages = Math.max(1, Math.ceil(total / PAGE));
  host.replaceChildren(
    el("span", { text: `Page ${page + 1} of ${pages}` }),
    el("button", { text: "Previous", disabled: page === 0, onclick: () => onPage(page - 1) }),
    el("button", { text: "Next", disabled: page >= pages - 1, onclick: () => onPage(page + 1) }),
  );
}

function renderPrompts() {
  const q = $("#prompt-search").value.trim().toLowerCase();
  const mode = $("#prompt-filter").value;
  let rows = promptRows().filter((r) => !q || r.text.toLowerCase().includes(q));
  if (mode === "own") rows = rows.filter((r) => r.ownAvg != null);
  if (mode === "own-top") rows = rows.filter((r) => r.ownRanks.includes(1));
  if (mode === "own-missing") rows = rows.filter((r) => r.ownAvg == null);
  sortRows(rows, state.promptSort, {
    prompt: (r) => r.text, answers: (r) => r.answers, own: (r) => r.ownAvg, cites: (r) => r.cites,
    leader: (r) => r.leader,
  });
  $("#prompt-count").textContent = `${fmt(rows.length)} prompts`;

  const onSort = (key) => {
    state.promptSort = state.promptSort.key === key ? { key, dir: -state.promptSort.dir } : { key, dir: key === "own" || key === "prompt" ? 1 : -1 };
    renderPrompts();
  };
  const own = ownIndex();
  const page = rows.slice(state.promptPage * PAGE, (state.promptPage + 1) * PAGE);
  $("#prompt-table").replaceChildren(
    header([
      { label: "Prompt", key: "prompt" }, { label: "Answers", key: "answers", num: true },
      { label: "Ranked #1", key: "leader" }, { label: `${state.own} position`, key: "own", num: true },
      { label: "Brands in the answer (by position)" }, { label: "Citations", key: "cites", num: true },
    ], state.promptSort, onSort),
    el("tbody", {}, page.map((r) => el("tr", { tabindex: 0, onclick: () => openPrompt(r.p), onkeydown: (e) => e.key === "Enter" && openPrompt(r.p) },
      el("td", { class: "prompt", text: r.text }),
      el("td", { class: "num", text: r.answers }),
      el("td", { text: r.leader || "—" }),
      el("td", { class: "num" }, r.ownAvg == null
        ? el("span", { class: "pos none", text: "Not mentioned" })
        : el("span", { class: "pos" + (r.ownAvg === 1 ? " first" : ""), text: "#" + (Number.isInteger(r.ownAvg) ? r.ownAvg : r.ownAvg.toFixed(1)) })),
      el("td", {}, el("div", { class: "chips" }, r.ordered.slice(0, 5).map((o) =>
        el("span", { class: "chip" + (o.b === own ? " you" : "") }, el("b", { text: Math.round(o.avg) }), state.data.brands[o.b])),
        r.ordered.length > 5 ? el("span", { class: "chip", text: `+${r.ordered.length - 5}` }) : null)),
      el("td", { class: "num", text: r.cites }),
    ))),
  );
  pager($("#prompt-pager"), state.promptPage, rows.length, (p) => { state.promptPage = p; renderPrompts(); window.scrollTo(0, 0); });
}

// ---------- prompt drawer ----------
function openPrompt(p) {
  const d = state.data;
  // Show every platform for the prompt so they can be compared; respect the date filter.
  const resps = d.responses.filter((r) => r.p === p && (state.date === "all" || r.d === state.date))
    .sort((a, b) => b.d.localeCompare(a.d) || a.pl.localeCompare(b.pl));
  $("#drawer-title").textContent = d.prompts[p];
  const body = $("#drawer-body");
  body.replaceChildren(el("p", { class: "count", text: `${resps.length} answer${resps.length === 1 ? "" : "s"}${state.date === "all" ? "" : " on " + state.date} across all platforms` }));

  for (const r of resps) {
    const brands = r.b.slice().sort((a, b) => a[2] - b[2]);
    const textBox = el("div", { class: "answer-text", text: "Loading…" });
    const details = el("details", {}, el("summary", { text: "Full answer" }), textBox);
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = "1";
      const res = await fetch(`/api/response?project=${encodeURIComponent(d.id)}&id=${encodeURIComponent(r.id)}`).then((x) => x.json());
      textBox.textContent = res.text || "(empty)";
    }, { once: false });

    body.append(el("section", { class: "answer" },
      el("div", { class: "answer-head" },
        el("span", { class: "platform", text: platformName(r.pl) }),
        el("span", { class: "meta", text: `${r.d} · ${fmt(r.len)} characters · ${r.c.length} citations` })),
      el("h3", { text: "Brands, in order of first mention" }),
      brands.length
        ? el("div", { class: "chips" }, brands.map(([b, count, rank]) =>
          el("span", { class: "chip" + (d.brands[b] === state.own ? " you" : ""), title: `${count} mention${count === 1 ? "" : "s"}` }, el("b", { text: rank }), d.brands[b])))
        : el("p", { class: "empty", text: "No tracked brands mentioned." }),
      r.c.length ? [el("h3", { text: "Cited sources" }), el("ol", {}, r.c.map((u) => {
        const url = d.urls[u];
        return el("li", {}, el("span", { class: "dom", text: url.dom }), ownerChip(url.own), " ", safeLink(url.u, url.u.replace(/^https?:\/\/(www\.)?/, "")));
      }))] : null,
      r.q.length ? [el("h3", { text: "Searches it ran" }), el("ul", {}, r.q.map((q) => el("li", { text: q })))] : null,
      r.rn.length ? el("details", {},
        el("summary", { text: `Read but not cited (${r.rn.length}${r.rc ? ` of ${r.rc} pages read` : ""})` }),
        el("ul", {}, r.rn.map((u) => el("li", {}, safeLink(u, u.replace(/^https?:\/\/(www\.)?/, "")))))) : null,
      details,
    ));
  }
  $("#drawer").hidden = false;
  $("#scrim").hidden = false;
  $("#drawer-close").focus();
}
function closeDrawer() { $("#drawer").hidden = true; $("#scrim").hidden = true; }

// ---------- sources ----------
function renderSources() {
  const resps = responses();
  const d = state.data;
  const byDom = new Map();
  const entry = (dom) => {
    let e = byDom.get(dom);
    if (!e) byDom.set(dom, (e = { dom, own: null, answers: 0, prompts: new Set(), urls: new Map(), cats: new Map(), platforms: new Set(), readNotCited: 0 }));
    return e;
  };
  for (const r of resps) {
    const seen = new Set();
    for (const u of r.c) {
      const url = d.urls[u];
      const e = entry(url.dom);
      if (url.own != null) e.own = url.own;
      if (!seen.has(url.dom)) { e.answers++; seen.add(url.dom); }
      e.prompts.add(r.p);
      e.platforms.add(r.pl);
      e.urls.set(url.u, (e.urls.get(url.u) || 0) + 1);
      e.cats.set(url.cat, (e.cats.get(url.cat) || 0) + 1);
    }
    const seenRn = new Set();
    for (const u of r.rn) {
      let dom;
      try { dom = new URL(u).hostname.replace(/^www\./, ""); } catch (_) { continue; }
      if (seenRn.has(dom)) continue;
      seenRn.add(dom);
      entry(dom).readNotCited++;
    }
  }
  const q = $("#source-search").value.trim().toLowerCase();
  const cat = $("#source-cat").value;
  const owner = $("#source-owner").value;
  const own = ownIndex();
  const ownerOk = (e) => owner === "all"
    || (owner === "brands" && e.own != null)
    || (owner === "competitors" && e.own != null && e.own !== own)
    || (owner === "own" && e.own === own)
    || (owner === "third" && e.own == null);
  let rows = [...byDom.values()].map((e) => ({
    ...e,
    cat: [...e.cats].sort((a, b) => b[1] - a[1])[0]?.[0] || "Not cited",
    share: resps.length ? e.answers / resps.length : 0,
  }));
  rows = rows.filter((e) => (!q || e.dom.includes(q)) && (cat === "all" || e.cat === cat) && ownerOk(e));
  sortRows(rows, state.sourceSort, {
    dom: (e) => e.dom, answers: (e) => e.answers, prompts: (e) => e.prompts.size, pages: (e) => e.urls.size, rn: (e) => e.readNotCited,
  });
  $("#source-count").textContent = `${fmt(rows.length)} domains`;
  const onSort = (key) => {
    state.sourceSort = state.sourceSort.key === key ? { key, dir: -state.sourceSort.dir } : { key, dir: key === "dom" ? 1 : -1 };
    renderSources();
  };
  const maxShare = Math.max(...rows.map((e) => e.share), 0) || 1;
  const page = rows.slice(state.sourcePage * PAGE, (state.sourcePage + 1) * PAGE);
  const tbody = el("tbody");
  for (const e of page) {
    const open = state.openDomain === e.dom;
    tbody.append(el("tr", {
      tabindex: 0,
      "aria-expanded": String(open),
      onclick: () => { state.openDomain = open ? null : e.dom; renderSources(); },
      onkeydown: (ev) => { if (ev.key === "Enter") { state.openDomain = open ? null : e.dom; renderSources(); } },
    },
      el("td", {}, e.dom),
      el("td", {}, ownerChip(e.own) || el("span", { class: "count", text: "Third-party" })),
      el("td", { text: e.cat }),
      el("td", {}, el("div", { class: "barcell" },
        el("div", { class: "bar" }, el("i", { style: `width:${(e.share / maxShare) * 100}%` })),
        el("span", { class: "num", text: `${fmt(e.answers)} · ${pct(e.share)}` }))),
      el("td", { class: "num", text: fmt(e.prompts.size) }),
      el("td", { class: "num", text: fmt(e.urls.size) }),
      el("td", { class: "num", text: e.readNotCited ? fmt(e.readNotCited) : "—" }),
    ));
    if (open) {
      const urls = [...e.urls].sort((a, b) => b[1] - a[1]).slice(0, 25);
      tbody.append(el("tr", {}, el("td", { colspan: 7 },
        urls.length ? el("ol", {}, urls.map(([u, n]) => el("li", {}, safeLink(u, u.replace(/^https?:\/\/(www\.)?/, "")), el("span", { class: "count", text: `  ×${n}` }))))
          : el("p", { class: "empty", text: "Read by ChatGPT but never cited in this selection." }))));
    }
  }
  $("#source-table").replaceChildren(
    header([
      { label: "Domain", key: "dom" }, { label: "Owner" }, { label: "Type" }, { label: "Answers citing it", key: "answers" },
      { label: "Prompts", key: "prompts", num: true }, { label: "Pages cited", key: "pages", num: true },
      { label: "Read, not cited", key: "rn", num: true },
    ], state.sourceSort, onSort),
    tbody,
  );
  pager($("#source-pager"), state.sourcePage, rows.length, (p) => { state.sourcePage = p; renderSources(); window.scrollTo(0, 0); });
}

boot().catch((err) => { $("#status").textContent = "Could not load data: " + err.message; });

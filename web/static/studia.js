const params = new URLSearchParams(location.search);
const MOCK = params.has("mock");
const TOKEN = params.get("t") || sessionStorage.getItem("studia-token") || "";
if (params.get("t")) {
  sessionStorage.setItem("studia-token", TOKEN);
  history.replaceState(null, "", location.pathname + (MOCK ? "?mock" : "") + location.hash);
}

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const store = {
  get(name, fallback) { try { return JSON.parse(localStorage.getItem(name)) ?? fallback; } catch { return fallback; } },
  set(name, value) { try { localStorage.setItem(name, JSON.stringify(value)); } catch { /* private mode */ } },
};

/* ---------------------------------------------------------------- icons */

const COLOR = { mooc: "var(--mooc)", zyk: "var(--zyk)", zhs: "var(--zhs)" };
function ring(percent, color = "var(--indigo)", size = 16) {
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  const r = size / 2 - 1.5, c = 2 * Math.PI * r, mid = size / 2;
  return `<svg class="ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><circle cx="${mid}" cy="${mid}" r="${r}" fill="none" stroke="rgb(var(--ink) / .12)" stroke-width="2.2"/>
    <circle cx="${mid}" cy="${mid}" r="${r}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="${(c * p) / 100} ${c}" transform="rotate(-90 ${mid} ${mid})"/></svg>`;
}
const svg = (body, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const ICON = {
  pulse: svg('<path d="M2 8h2.4l1.7-4.5 3.4 9 1.7-4.5H14"/>', 15),
  gear: svg('<circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2"/>', 15),
  moon: svg('<path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"/>', 15),
  sun: svg('<circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1"/>', 15),
  logout: svg('<path d="M6.5 2.5h-3v11h3M10 5l3 3-3 3M13 8H6"/>', 15),
  key: svg('<circle cx="5.5" cy="10.5" r="3"/><path d="m7.6 8.4 5.9-5.9M11 5l1.5 1.5"/>', 15),
  shield: svg('<path d="M8 1.8 13 3.6v4c0 3.2-2.1 5.4-5 6.6-2.9-1.2-5-3.4-5-6.6v-4z"/>', 15),
  login: svg('<path d="M9.5 2.5h3v11h-3M3 8h7M7.5 5l3 3-3 3"/>', 15),
  updown: svg('<path d="m5 6 3-3 3 3M5 10l3 3 3-3"/>', 12),
  play: '<svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor"><path d="M3 1.6v8.8L10.4 6z"/></svg>',
  repeat: svg('<path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"/>', 14),
  pen: svg('<path d="M10.5 2.5 13.5 5.5 6 13H3v-3z"/>', 14),
  stop: '<svg width="14" height="14" viewBox="0 0 16 16"><rect x="3.5" y="3.5" width="9" height="9" rx="2" fill="currentColor"/></svg>',
  tick: '<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m2.5 6.3 2.4 2.4L9.6 3.8"/></svg>',
  done: '<svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="var(--celadon)"/><path d="m4.8 8.2 2 2 4.2-4.4" fill="none" stroke="var(--card)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  error: '<svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="var(--seal)"/><path d="m5.5 5.5 5 5m0-5-5 5" stroke="var(--card)" stroke-width="1.8" stroke-linecap="round"/></svg>',
  waiting: '<svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="var(--gamboge)"/><path d="M8 4.5V8l2.2 1.6" fill="none" stroke="var(--card)" stroke-width="1.7" stroke-linecap="round"/></svg>',
  stopped: '<svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="none" stroke="var(--text-4)" stroke-width="1.5"/><rect x="5.6" y="5.6" width="4.8" height="4.8" rx="1" fill="var(--text-4)"/></svg>',
  running: '<svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="var(--indigo-soft)"/><circle cx="8" cy="8" r="3" fill="var(--indigo)"/></svg>',
};
ICON.queued = ICON.running;

/* ------------------------------------------------------------------ api */

// local: one person, token from the terminal URL; hosted: cookie session + CSRF header.
const session = { mode: "local", user: null, csrf: "" };
let mockModule = null;
async function api(path, { method = "GET", body } = {}) {
  if (MOCK) {
    mockModule = mockModule || (await import("./mock.js"));
    return mockModule.request(path, method, body);
  }
  const headers = body ? { "Content-Type": "application/json" } : {};
  if (session.mode === "hosted") { if (method !== "GET") headers["X-Studia-CSRF"] = session.csrf; }
  else headers["X-Studia-Token"] = TOKEN;
  const response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("/api/auth/") && path !== "/api/mode") {
    if (session.mode === "hosted") loadMe().then((ok) => { if (!ok) showAuth(); else refreshState(); });
    else if (path !== "/api/state") refreshState();
  }
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

const state = {
  platforms: [], courses: {}, papers: {}, units: {}, selected: {},
  jobs: [], runs: {}, focus: null, jobLimit: 4, history: [], view: "", loginToken: { cancelled: true }, lastDone: null,
  current: store.get("studia-platform", ""), menuOpen: false, meOpen: false, logFilter: "all", armed: null, editPassword: false,
};
function freshRun() { return { detail: "", overall: null, sub: null, lines: [], unit: null }; }
const runOf = (id) => (state.runs[id] = state.runs[id] || freshRun());
// Mirrors the server's rules (web/server.py _check_conflicts) so buttons say why before you click.
const GROUP = { study: "learn", review: "learn", answer: "answer" };
const jobType = (j) => j.kind || { 自动学习: "study", 每日复习: "review", "AI 答题": "answer" }[j.title];
function blocker(pid, cid, type) {
  for (const o of state.jobs) {
    if (GROUP[jobType(o)] !== GROUP[type] || o.platform !== pid) continue;
    if (o.course === cid) return o.state === "queued" ? `正在排队（${o.title}）` : `正在${o.title}…`;
    if (pid === "zhs" && GROUP[type] === "learn") return `智慧树一次只能播一个视频，等「${o.course_name}」的${o.title}结束`;
  }
  return "";
}
// The job the player and focus view follow: the one picked, else one on the open course, else the newest.
function focusedJob() {
  const [, pid, cid] = state.view.match(/^#\/course\/([^/]+)\/([^/]+)/) || [];
  return state.jobs.find((j) => j.id === state.focus)
    || state.jobs.find((j) => j.platform === pid && j.course === decodeURIComponent(cid || ""))
    || state.jobs[state.jobs.length - 1] || null;
}
const platformOf = (pid) => state.platforms.find((p) => p.id === pid);
const key = (pid, cid) => `${pid}:${cid}`;
const selectedOf = (k) => (state.selected[k] = state.selected[k] || new Set());
const courseOf = (pid, cid) => (state.courses[pid] || []).find((c) => c.id === cid);
const go = (hash) => { if (location.hash !== hash) location.hash = hash; else route(); };

let toastTimer = 0;
function toast(text) {
  const el = $(".toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}
function ago(seconds) {
  const diff = Date.now() / 1000 - seconds;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  const d = new Date(seconds * 1000);
  return diff < 86400 ? `今天 ${d.toTimeString().slice(0, 5)}` : `${d.getMonth() + 1}月${d.getDate()}日`;
}
const pad = (n) => String(n).padStart(2, "0");
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${pad(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${pad(Math.floor((sec % 3600) / 60))}m`;
}
const elapsed = (job) => (job.ended || Date.now() / 1000) - job.started;

function toggleTheme() {
  const night = document.documentElement.dataset.theme !== "night";
  document.documentElement.dataset.theme = night ? "night" : "";
  store.set("studia-theme", night ? "night" : "day");
  toast(night ? "夜读模式" : "日间模式");
  renderSidebar();
}

/* ---------------------------------------------------------------- sidebar */

const INITIAL = { mooc: "M", zyk: "资", zhs: "智" };
function currentPlatform() {
  return platformOf(state.current) || state.platforms.find((p) => p.logged_in) || state.platforms[state.platforms.length - 1];
}
function platformStatus(p) {
  if (!p.logged_in) return "未登录";
  return state.courses[p.id] ? `已登录 · ${state.courses[p.id].length} 门课程` : "已登录";
}
function homeOf(p) {
  if (!p.logged_in) return `#/login/${p.id}`;
  const first = (state.courses[p.id] || [])[0];
  return first ? `#/course/${p.id}/${first.id}` : `#/login/${p.id}`;
}
function switchPlatform(pid, navigate = true) {
  state.current = pid;
  state.menuOpen = false;
  store.set("studia-platform", pid);
  if (navigate) go(homeOf(platformOf(pid)));
  else renderSidebar();
}

function jobPill() {
  if (state.jobs.length) return state.jobs.every((j) => j.state === "queued") ? '<span class="live">排队中</span>' : `<span class="live">${state.jobs.length} 个进行中</span>`;
  if (state.history[0]?.state === "waiting") return '<span class="live waiting">等你操作</span>';
  return state.history.length ? `<span class="count">${state.history.length}</span>` : "";
}

function renderSidebar() {
  const p = currentPlatform();
  if (!p) return;
  const view = state.view;
  const item = (hash, icon, label, extra = "", cls = "") =>
    `<button class="nav_item ${cls}" data-go="${hash}" aria-current="${view === hash}">${icon}<span class="label">${esc(label)}</span>${extra}</button>`;

  patch($(".switcher_wrap"), `
    <button class="switch" data-menu aria-haspopup="menu" aria-expanded="${state.menuOpen}">
      <span class="plat ${p.id}">${INITIAL[p.id]}</span><span class="who"><b>${esc(p.name)}</b><small>${esc(platformStatus(p))}</small></span><span class="chev">${ICON.updown}</span></button>
    ${state.menuOpen ? `<div class="menu" role="menu">${state.platforms.map((x, i) => `
      <button class="menu_row" role="menuitem" data-switch="${x.id}"><span class="plat ${x.id}">${INITIAL[x.id]}</span>
        <span class="who"><b>${esc(x.name)}</b><small>${esc(platformStatus(x))}</small></span>
        ${state.jobs.some((j) => j.platform === x.id) ? '<span class="live">进行中</span>' : ""}
        ${x.id === p.id ? `<span class="tick">${ICON.tick}</span>` : `<span class="kbd">⌘${i + 1}</span>`}</button>`).join("")}</div>` : ""}`);

  let html = item("#/jobs", ICON.pulse, "任务", jobPill());
  if (session.user?.role === "admin") html += item("#/admin", ICON.shield, "管理");
  html += `<div class="nav_label">${p.logged_in ? "课程" : esc(p.short)}<span class="kbd">⌘K</span></div>`;
  if (!p.logged_in) html += item(`#/login/${p.id}`, ICON.login, `登录${/^[A-Za-z]/.test(p.short) ? " " : ""}${p.short}`);
  else if (!state.courses[p.id]) html += `<div class="nav_item quiet"><span class="label">正在读取课程…</span></div>`;
  else html += state.courses[p.id].map((c) => {
    const busy = state.jobs.some((j) => j.platform === p.id && j.course === c.id);
    return item(`#/course/${p.id}/${c.id}`, ring(c.progress, COLOR[p.id]), c.name,
      busy ? '<span class="live"></span>' : c.progress == null ? "" : `<span class="count">${esc(c.progress)}%</span>`);
  }).join("") || `<div class="nav_item quiet"><span class="label">没有课程</span></div>`;
  patch($(".nav"), html);

  const me = session.user;
  const night = document.documentElement.dataset.theme === "night";
  patch($(".side_foot"), (me
    ? `<button class="me" data-me aria-haspopup="menu" aria-expanded="${state.meOpen}"><span class="avatar">${esc(me.username.slice(0, 1).toUpperCase())}</span><span class="label">${esc(me.username)}</span></button>
       ${state.meOpen ? `<div class="menu me_menu" role="menu">
         <div class="me_head"><span class="avatar big">${esc(me.username.slice(0, 1).toUpperCase())}</span><span class="who"><b>${esc(me.username)}</b><small>${me.role === "admin" ? "管理员" : "成员"}</small></span></div>
         <button class="menu_row" role="menuitem" data-go="#/settings/account">${ICON.key}<span class="grow">账号与密码</span></button>
         ${me.role === "admin" ? `<button class="menu_row" role="menuitem" data-go="#/admin">${ICON.shield}<span class="grow">管理</span></button>` : ""}
         <div class="psep"></div>
         <button class="menu_row danger" role="menuitem" data-signout>${ICON.logout}<span class="grow">退出 Studia</span></button></div>` : ""}`
    : `<span class="me static" title="本机模式：只有这台电脑能访问"><span class="avatar">本</span><span class="label">本机</span></span>`)
    + `<button class="icon_btn" data-toggle-theme title="${night ? "日间模式" : "夜读模式"} ⌘J" aria-label="切换日间 / 夜读模式">${night ? ICON.sun : ICON.moon}</button>
       <button class="icon_btn" data-go="#/settings" title="设置" aria-label="设置">${ICON.gear}</button>`);
}

/* ---------------------------------------------------------------- page helpers */

const page = $(".page");
const scroller = $(".scroll");
// Live progress repaints several times a second. Patch the existing DOM instead of
// replacing it, so buttons, running animations and a finger mid-tap survive the update.
function patch(target, html) {
  const fresh = document.createElement("template");
  fresh.innerHTML = html;
  patchChildren(target, fresh.content);
}
function patchChildren(from, to) {
  const old = [...from.childNodes], next = [...to.childNodes];
  next.forEach((node, i) => {
    const have = old[i];
    if (!have) from.appendChild(node);
    else if (have.nodeType !== node.nodeType || have.nodeName !== node.nodeName) from.replaceChild(node, have);
    else if (node.nodeType === 1) {
      // A reused element may now mean something else; views rebind their handlers after each paint.
      have.onclick = null;
      for (const { name } of [...have.attributes]) if (!node.hasAttribute(name)) have.removeAttribute(name);
      for (const { name, value } of [...node.attributes]) if (have.getAttribute(name) !== value) have.setAttribute(name, value);
      patchChildren(have, node);
    } else if (have.nodeValue !== node.nodeValue) have.nodeValue = node.nodeValue;
  });
  for (let i = old.length - 1; i >= next.length; i -= 1) from.removeChild(old[i]);
}

// Entrance animation only when the user navigates; background repaints patch in place.
let quiet = false;
function setPage(html) {
  if (quiet) { patch(page, html); return; }
  page.innerHTML = html;
  page.classList.remove("enter");
  void page.offsetWidth;
  page.classList.add("enter");
}

const LABEL = { study: "自动学习", review: "每日复习", answer: "AI 答题" };
const TYPE_OF = Object.fromEntries(Object.entries(LABEL).map(([k, v]) => [v, k]));
const lastRun = (pid, cid, type) => state.history.find((j) => j.platform === pid && j.course === cid && j.title === LABEL[type]);
const STATE_WORD = { done: "完成", error: "失败", waiting: "等你操作", stopped: "已停止", running: "进行中", queued: "排队中" };

/* ---------------------------------------------------------------- units (course grid) */

const UNIT_WORD = { done: "已完成", queued: "待学", running: "正在学习", failed: "失败", skipped: "跳过（在答题里做或无法展开）", waiting: "等你操作" };
const KIND_WORD = { video: "视频", doc: "文档", ppt: "课件", audio: "音频", img: "图片", pdf: "文档", word: "文档", excel: "表格" };
function tally(chapters) {
  const t = { all: 0, done: 0, running: 0, queued: 0, failed: 0, skipped: 0, waiting: 0 };
  for (const ch of chapters) for (const u of ch.units) { t.all += 1; t[u.state] = (t[u.state] || 0) + 1; }
  return t;
}
const unitsOf = (k) => state.units[k]?.chapters || null;
function findUnit(k, id) {
  for (const ch of unitsOf(k) || []) for (const u of ch.units) if (u.id === id) return u;
  return null;
}

function gridHtml(k) {
  const data = state.units[k];
  if (!data) return `<div class="grid loading">正在读取学习单元…</div>`;
  if (data.error) return `<div class="grid loading">${esc(data.error)} · <button class="link" data-units-retry="${esc(k)}">重试</button></div>`;
  if (!data.chapters.length) return `<div class="grid loading">这门课没有可学习的单元</div>`;
  const t = tally(data.chapters);
  const legend = [["done", "完成"], ["running", "进行中"], ["queued", "待学"], ["waiting", "等你操作"], ["failed", "失败"], ["skipped", "跳过"]]
    .filter(([s]) => t[s] || s === "done" || s === "queued");
  return `<div class="grid" data-k="${esc(k)}">${data.chapters.map((ch, ci) => `<div class="chapter"><span class="name" title="${esc(ch.name)}">${esc(ch.name)}</span>
      <span class="cells">${ch.units.map((u, ui) => `<i class="cell ${u.state}" data-u="${ci}:${ui}"></i>`).join("")}</span>
      <span class="tally">${ch.units.filter((u) => u.state === "done").length}/${ch.units.length}</span></div>`).join("")}
    <div class="legend">${legend.map(([s, label]) => `<span><i class="cell ${s}"></i>${label} <b>${t[s]}</b></span>`).join("")}</div></div>`;
}

const loadingUnits = new Set();
async function loadUnits(pid, cid, force = false) {
  const k = key(pid, cid);
  if (loadingUnits.has(k) || (state.units[k] && !force)) return;
  loadingUnits.add(k);
  try { state.units[k] = { chapters: (await api(`/api/platforms/${pid}/units?course=${encodeURIComponent(cid)}`)).chapters }; }
  catch (error) { if (!state.units[k]?.chapters) state.units[k] = { error: error.message }; }
  finally { loadingUnits.delete(k); }
  repaint();
}

const TOUCH = matchMedia("(hover: none)").matches;
const tip = $(".tip");
// Touch screens fire mouseover on tap but never mouseout; hide the tip once the page moves.
document.addEventListener("scroll", () => tip.classList.remove("show"), true);
document.addEventListener("mouseover", (event) => {
  const cell = event.target.closest(".cell[data-u]");
  const grid = cell?.closest("[data-k]");
  const [ci, ui] = (cell?.dataset.u || "").split(":").map(Number);
  const u = grid && unitsOf(grid.dataset.k)?.[ci]?.units[ui];
  if (!u) { tip.classList.remove("show"); return; }
  const kind = KIND_WORD[u.kind] || u.kind;
  tip.innerHTML = `<b>${esc(u.name)}</b><br><span class="mono">${esc([kind, u.minutes ? `${u.minutes} 分钟` : "", UNIT_WORD[u.state]].filter(Boolean).join(" · "))}</span>`;
  const r = cell.getBoundingClientRect();
  tip.style.left = `${Math.max(8, Math.min(innerWidth - 270, r.left - 8))}px`;
  tip.style.top = `${r.bottom + 8}px`;
  tip.classList.add("show");
});

/* ---------------------------------------------------------------- views */

let adminTimer = 0;
const VIEWS = {
  async admin(refresh = false) {
    clearTimeout(adminTimer);
    const head = `<h1 class="title">管理</h1><p class="sub">全站任务、每人任务上限、账号和邀请码、日志导出。</p>`;
    let users, invites, memory, jobs, settings;
    try {
      [users, invites, memory, jobs, settings] = await Promise.all(["users", "invites", "memory", "jobs", "settings"].map((x) => api(`/api/admin/${x}`)));
    } catch (error) { if (!refresh) setPage(`${head}<div class="empty">${esc(error.message)}</div>`); return; }
    if (state.view !== "#/admin") return;
    users = users.users; invites = invites.invites; jobs = jobs.jobs;
    const nameOf = (uid) => users.find((u) => u.id === uid)?.username || "—";
    const inviteState = { open: ["blue", "可用"], used: ["", "已使用"], expired: ["", "已过期"] };
    const running = jobs.filter((j) => j.state === "running").length;
    const pad2 = (n) => String(n).padStart(2, "0");
    const local = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const kept = (name, fallback) => $(`[name="${name}"]`, page)?.value || fallback;
    quiet = refresh;
    setPage(`${head}
      <div class="section"><h3>全站任务</h3><span class="aside">${running} 个运行中 · ${jobs.length - running} 个排队 · 每 5 秒刷新</span></div>
      <div class="runs">${jobs.map((j) => `<div class="run">${j.state === "queued" ? ICON.waiting : ICON.running}
          <span class="t1">${esc(j.username)} · ${esc(j.title)} · ${esc(j.course_name)}<small>${esc(platformOf(j.platform)?.short || j.platform)} · 开始于 ${ago(j.started)}</small></span>
          <span class="fix"><button class="btn small danger" data-stop-job="${esc(j.user_id)}/${esc(j.id)}">停止</button></span>
          <span class="state ${j.state === "queued" ? "waiting" : "running"}">${j.state === "queued" ? "排队中" : "运行中"}<span class="mono">${duration(Date.now() / 1000 - j.started)}</span></span>
          <span class="when"></span></div>`).join("") || `<div class="run"><span></span><span class="t1"><small>现在没有任务在运行</small></span></div>`}</div>

      <div class="section"><h3>任务上限</h3></div>
      <p class="hint">每个人同时最多运行几个任务，多出的会排队，等自己前面的任务结束后自动开始。可以在下面的用户列表里给某个人单独设置。用户自己只能在设置里调学习和答题的并发数，不能改这里。</p>
      <div class="models"><div class="model plain"><span class="m1"><b>默认每人同时 <input class="inline_num" type="number" min="1" max="20" name="max_jobs" value="${esc(kept("max_jobs", settings.max_jobs))}"> 个任务</b>
          <small class="sans">4 核 4G 的服务器，每人 4 个、6 个人同时跑是可以的；答题多的话，让大家把答题并发调到 3–4。</small></span>
        <span class="row_btns"><button class="btn small primary" data-save-limit>保存</button></span></div></div>

      <div class="section"><h3>用户</h3><span class="aside">${users.length} 人</span></div>
      <div class="models">${users.map((u) => `<div class="model"><span class="avatar big">${esc(u.username.slice(0, 1).toUpperCase())}</span>
          <span class="m1"><b>${esc(u.username)}</b>${u.role === "admin" ? '<span class="pill blue">管理员</span>' : ""}${u.disabled ? '<span class="pill red">已停用</span>' : ""}<span class="pill">同时 ${u.job_limit} 个${u.max_jobs ? "（单独设置）" : ""}</span>
          <small class="sans">${u.jobs?.length ? u.jobs.map((j) => `${esc(j.title)} · ${esc(j.course_name)}${j.state === "queued" ? "（排队）" : ""}`).join("；") : u.last_login ? `上次登录 ${ago(u.last_login)}` : "还没登录过"}</small></span><span></span>
          <span class="row_btns">${u.jobs?.length ? `<button class="btn small danger" data-stop-user="${u.id}">停止全部任务</button>` : ""}
            <button class="btn small" data-user-limit="${u.id}" data-current="${u.max_jobs || ""}">任务上限</button>
            <button class="btn small" data-reset="${u.id}">重置密码</button>
            ${u.id === session.user.id ? "" : u.disabled ? `<button class="btn small" data-enable="${u.id}">启用</button>` : `<button class="btn small danger" data-disable="${u.id}">停用</button>`}</span></div>`).join("")}</div>

      <div class="section"><h3>邀请码</h3><button class="link" data-invite>＋ 生成邀请码</button></div>
      <p class="hint">把邀请码发给对方，对方在登录页选「用邀请码注册」。每个邀请码只能用一次，7 天后过期。</p>
      <div class="models">${invites.map((i) => `<div class="model plain"><span class="m1"><b><code>${esc(i.code)}</code></b><span class="pill ${inviteState[i.state][0]}">${inviteState[i.state][1]}</span>
          <small class="sans">${i.used_by ? `${esc(nameOf(i.used_by))} 已注册` : `创建于 ${ago(i.created)} · ${new Date(i.expires * 1000).toLocaleDateString()} 过期`}</small></span>
        <span class="row_btns">${i.state === "open" ? `<button class="btn small" data-copy="${esc(i.code)}">复制</button><button class="btn small danger" data-revoke="${esc(i.code)}">作废</button>` : ""}</span></div>`).join("")
        || `<div class="model plain"><span class="m1"><small class="sans">还没有邀请码</small></span></div>`}</div>

      <div class="section"><h3>共享答案记忆</h3><span class="aside">命中的题直接用，不再调用 AI</span></div>
      <div class="models"><div class="model plain"><span class="m1"><b>智慧树 · ${memory.questions} 题</b><span class="pill blue">${memory.confirmed} 题已确认正确</span>
        <small class="sans">交卷后平台判定的对错，在同一门课的所有试卷之间共享：对的直接用、错的避开。</small></span></div>
        <div class="model plain"><span class="m1"><b>MOOC · ${memory.mooc.questions} 题</b><span class="pill blue">平台公布的标准答案</span>
        <small class="sans">交卷后平台公布的标准答案，在同一门课（同一个班）的所有试卷之间共享；单选、多选、判断题。</small></span></div></div>

      <div class="section"><h3>日志导出</h3></div>
      <p class="hint">导出一段时间里的日志，下载为文本文件。运行日志是所有人任务的过程记录（已隐去密码和令牌）；操作记录是登录、注册和管理员操作。</p>
      <div class="models"><div class="model plain log_export"><span class="log_range">
          <label>从 <input type="datetime-local" name="log_from" value="${esc(kept("log_from", local(today)))}"></label>
          <label>到 <input type="datetime-local" name="log_to" value="${esc(kept("log_to", local(new Date())))}"></label>
          <label>类型 <select name="log_kind"><option value="app">运行日志</option><option value="audit" ${kept("log_kind", "") === "audit" ? "selected" : ""}>操作记录</option></select></label></span>
        <span class="row_btns"><button class="btn small primary" data-export-logs>导出</button></span></div></div>`);
    quiet = false;
    const act = async (path, body, message) => {
      try { await api(path, { method: "POST", body: body || {} }); toast(message); VIEWS.admin(true); } catch (e) { toast(e.message); }
    };
    $("[data-invite]", page).onclick = async () => {
      try { const { code } = await api("/api/admin/invites", { method: "POST", body: { days: 7 } }); navigator.clipboard?.writeText(code).catch(() => {}); toast(`邀请码 ${code} 已复制`); VIEWS.admin(true); }
      catch (e) { toast(e.message); }
    };
    $("[data-save-limit]", page).onclick = () => act("/api/admin/settings", { max_jobs: Number($('[name="max_jobs"]', page).value) }, "已保存，马上生效");
    $("[data-export-logs]", page).onclick = () => {
      const from = new Date($('[name="log_from"]', page).value).getTime() / 1000;
      const to = new Date($('[name="log_to"]', page).value).getTime() / 1000;
      if (!(from < to)) { toast("请选择正确的时间段"); return; }
      location.href = `/api/admin/logs?kind=${$('[name="log_kind"]', page).value}&from=${Math.floor(from)}&to=${Math.ceil(to)}`;
    };
    page.querySelectorAll("[data-stop-job]").forEach((el) => (el.onclick = () => act(`/api/admin/users/${el.dataset.stopJob.replace("/", "/jobs/")}/stop`, null, "已发出停止指令")));
    page.querySelectorAll("[data-user-limit]").forEach((el) => (el.onclick = () => {
      const value = window.prompt("这个人同时最多运行几个任务（1–20）？留空表示跟随默认值。", el.dataset.current);
      if (value !== null) act(`/api/admin/users/${el.dataset.userLimit}/limit`, { max_jobs: value.trim() }, "已保存，马上生效");
    }));
    page.querySelectorAll("[data-copy]").forEach((el) => (el.onclick = () => { navigator.clipboard?.writeText(el.dataset.copy).then(() => toast("已复制"), () => toast(el.dataset.copy)); }));
    page.querySelectorAll("[data-revoke]").forEach((el) => (el.onclick = () => act(`/api/admin/invites/${el.dataset.revoke}/revoke`, null, "邀请码已作废")));
    page.querySelectorAll("[data-stop-user]").forEach((el) => (el.onclick = () => act(`/api/admin/users/${el.dataset.stopUser}/stop`, null, "已发出停止指令")));
    page.querySelectorAll("[data-enable]").forEach((el) => (el.onclick = () => act(`/api/admin/users/${el.dataset.enable}/enable`, null, "已启用")));
    page.querySelectorAll("[data-disable]").forEach((el) => (el.onclick = () => {
      if (!el.classList.contains("armed")) { el.classList.add("armed"); el.textContent = "确认停用"; return; }
      act(`/api/admin/users/${el.dataset.disable}/disable`, null, "已停用，对方已被登出");
    }));
    page.querySelectorAll("[data-reset]").forEach((el) => (el.onclick = () => {
      const password = window.prompt("输入新密码（至少 8 位）。保存后对方需要用新密码重新登录。");
      if (password) act(`/api/admin/users/${el.dataset.reset}/password`, { password }, "密码已重置");
    }));
    // Live view of everyone's jobs; paused while the admin is typing or the tab is hidden.
    const tick = () => {
      if (state.view !== "#/admin") return;
      if (document.hidden || page.querySelector("input:focus, select:focus, .armed")) adminTimer = setTimeout(tick, 5000);
      else VIEWS.admin(true);
    };
    adminTimer = setTimeout(tick, 5000);
  },

  jobs() {
    const open = new Set([...page.querySelectorAll(".run_lines:not([hidden])")].map((el) => el.dataset.lines));
    const live = state.jobs.length ? `<div class="section"><h3>进行中</h3><span class="aside">每人最多同时运行 ${state.jobLimit} 个，多出的排队</span></div>
      <div class="runs">${state.jobs.map((job) => { const run = runOf(job.id); return `<div class="run">${ICON.running}
        <span class="t1">${esc(job.title)} · ${esc(job.course_name)}<small>${esc(platformOf(job.platform)?.short || "")} · ${esc(job.state === "queued" ? `排队中：你同时最多运行 ${state.jobLimit} 个任务` : run.unit?.name || run.detail || "准备中…")}</small></span>
        <span class="fix"><button class="btn small" data-open="${job.id}">展开</button><button class="btn small danger" data-stop="${job.id}">停止</button></span>
        <span class="state running">${STATE_WORD[job.state] || "进行中"}<span class="mono" data-elapsed="${job.id}">${duration(elapsed(job))}</span></span>
        <span class="when">${ago(job.started)}</span></div>`; }).join("")}</div>` : "";
    const rows = state.history.map((j, i) => `<div class="run" data-history="${i}">${ICON[j.state] || ICON.stopped}
        <span class="t1">${esc(j.title)} · ${esc(j.course_name)}<small>${esc(platformOf(j.platform)?.short || "")} · ${esc(j.summary)}</small></span>
        <span class="fix">${fixButtons(j, i)}</span>
        <span class="state ${j.state}">${STATE_WORD[j.state] || ""}<span class="mono">${j.ended ? duration(j.ended - j.started) : ""}</span></span>
        <span class="when">${ago(j.ended || j.started)}</span></div>
      <div class="run_lines" data-lines="${i}" ${open.has(String(i)) ? "" : "hidden"}>${logHtml(j.lines || [])}</div>`).join("");
    setPage(`<h1 class="title">任务</h1>
      <p class="sub">${session.mode === "hosted" ? "任务在服务器上运行，关掉网页也不会中断。" : "任务在这台电脑上运行，关掉网页不会中断，关掉终端才会停止。"}不同课程可以同时学习和答题，每人最多同时运行 ${state.jobLimit} 个，多出的会排队，前面的结束后自动开始。</p>
      ${live}
      <div class="section"><h3>历史</h3><span class="aside">只有失败和需要你处理的任务才有按钮，点一行看日志</span></div>
      ${rows ? `<div class="runs">${rows}</div>` : `<div class="empty">还没有运行过任务</div>`}`);
    page.querySelectorAll("[data-history]").forEach((el) => (el.onclick = (event) => {
      if (event.target.closest("button, a")) return;
      const lines = page.querySelector(`[data-lines="${el.dataset.history}"]`);
      lines.hidden = !lines.hidden;
    }));
    page.querySelectorAll("[data-retry]").forEach((el) => (el.onclick = () => {
      const j = state.history[el.dataset.retry];
      startJob({ platform: j.platform, course: j.course, type: TYPE_OF[j.title] });
    }));
  },

  login(pid) {
    const p = platformOf(pid);
    if (!p) return setPage(`<div class="empty">不支持这个平台</div>`);
    if (p.logged_in) return go(homeOf(p));
    const where = session.mode === "hosted" ? "登录状态保存在服务器上你自己的目录里，并且加密存放。" : "登录状态只保存在这台电脑上。";
    const head = `<div class="crumb"><span class="dot ${pid}"></span>${esc(p.name)}</div><h1 class="title">登录${/^[A-Za-z]/.test(p.short) ? " " : ""}${esc(p.short)}</h1>`;
    if (p.login === "qr") {
      setPage(`${head}<p class="sub">用智慧树 App 扫码，在手机上确认后，课程会出现在左侧。${where}</p>
        <div class="login_card"><div class="qr"><div class="qr_empty">正在获取二维码…</div></div>
          <div class="login_status"><div data-status><b>等待扫码</b><p>打开智慧树 App，点右上角的扫一扫。二维码过期会自动刷新。</p></div>
            <button class="btn" data-start-login>刷新二维码</button></div></div>`);
      $("[data-start-login]", page).onclick = () => qrLogin(pid);
      qrLogin(pid);
    } else {
      setPage(`${head}<p class="sub">${esc(p.name)} 使用职教云账号，通过浏览器授权登录。${where}</p>
        <div class="login_card"><div class="login_status"><div data-status><b>在浏览器中登录</b><p>点下面的按钮会打开职教云登录页，在那里登录完成后，这里会自动继续。</p></div>
          <button class="btn primary" data-start-login>打开登录页</button></div></div>`);
      $("[data-start-login]", page).onclick = () => browserLogin(pid);
    }
  },

  course(pid, cid, tab) {
    const p = platformOf(pid);
    if (!p || !p.logged_in) return go(p ? `#/login/${pid}` : homeOf(currentPlatform()));
    const c = courseOf(pid, cid);
    if (!c) return setPage(`<div class="empty">${state.courses[pid] ? "没有找到这门课程" : "正在读取课程…"}</div>`);
    renderCourse(p, c, tab === "answer" ? "answer" : "study", true);
  },

  async settings(tab = "models") {
    tab = ["account", "models", "study"].includes(tab) ? tab : "models";
    const tabs = `<div class="tabs" role="tablist">${[["account", "账号"], ["models", "AI 模型"], ["study", "偏好"]].map(([k, label]) =>
      `<button class="tab" role="tab" aria-selected="${k === tab}" data-go="#/settings/${k}">${label}</button>`).join("")}</div>`;
    const where = session.mode === "hosted" ? "设置只对你的账号生效，保存在服务器上你自己的目录里。" : "所有设置只保存在这台电脑上。";
    const head = `<h1 class="title">设置</h1><p class="sub">${where}</p>${tabs}`;
    if (tab === "account") {
      const me = session.user;
      const studia = me ? `<div class="section"><h3>Studia 账号</h3></div>
        <div class="models">
          <div class="model plain"><span class="account_who"><span class="avatar big">${esc(me.username.slice(0, 1).toUpperCase())}</span>
            <span class="m1"><b>${esc(me.username)}</b><span class="pill ${me.role === "admin" ? "blue" : ""}">${me.role === "admin" ? "管理员" : "成员"}</span>
            <small class="sans">${me.last_login ? `上次登录 ${ago(me.last_login)}` : ""}${me.created ? ` · ${new Date(me.created * 1000).toLocaleDateString()} 加入` : ""}</small></span></span></div>
          ${state.editPassword ? `<form class="model plain password_form" data-password>
            <span class="m1"><b>修改密码</b><small class="sans">保存后，其他设备上的登录会全部退出。</small></span>
            <span class="pw_fields">
              <label class="auth_field"><span>原密码</span><input name="old" type="password" autocomplete="current-password" required></label>
              <label class="auth_field"><span>新密码</span><input name="new" type="password" autocomplete="new-password" minlength="8" placeholder="至少 8 位" required></label>
              <label class="auth_field"><span>再输一次</span><input name="again" type="password" autocomplete="new-password" minlength="8" required></label>
              <span class="row_btns"><button type="button" class="btn" data-password-cancel>取消</button><button class="btn primary">保存新密码</button></span>
            </span></form>`
          : `<div class="model plain"><span class="m1"><b>密码</b><small class="sans">修改后，其他设备上的登录会全部退出</small></span>
              <span class="row_btns"><button class="btn small" data-password-edit>修改密码</button></span></div>`}
          <div class="model plain"><span class="m1"><b>退出登录</b><small class="sans">只退出这个浏览器</small></span>
            <span class="row_btns"><button class="btn small danger" data-signout>退出 Studia</button></span></div>
        </div>` : "";
      setPage(`${head}${studia}<div class="section"><h3>平台账号</h3></div>
        <div class="models">${state.platforms.map((p) => `<div class="model"><span class="plat ${p.id}">${INITIAL[p.id]}</span>
          <span class="m1"><b>${esc(p.name)}</b><small class="sans">${esc(platformStatus(p))} · ${p.login === "qr" ? "App 扫码登录" : "职教云浏览器授权"}</small></span><span></span>
          <span class="row_btns">${p.logged_in ? `<button class="btn small danger" data-logout="${p.id}">退出</button>` : `<button class="btn small primary" data-go="#/login/${p.id}">登录</button>`}</span></div>`).join("")}</div>`);
      page.querySelectorAll("[data-logout]").forEach((el) => (el.onclick = async () => {
        try { await api(`/api/platforms/${el.dataset.logout}/logout`, { method: "POST" }); } catch (e) { toast(e.message); return; }
        await refreshState();
        toast("已退出登录");
        VIEWS.settings("account");
      }));
      const edit = $("[data-password-edit]", page);
      if (edit) edit.onclick = () => { state.editPassword = true; quiet = true; VIEWS.settings("account"); quiet = false; $("[data-password] input", page)?.focus(); };
      const cancel = $("[data-password-cancel]", page);
      if (cancel) cancel.onclick = () => { state.editPassword = false; quiet = true; VIEWS.settings("account"); quiet = false; };
      const form = $("[data-password]", page);
      if (form) form.onsubmit = async (event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(form));
        if (data.new.length < 8) { toast("新密码至少 8 位"); return; }
        if (data.new !== data.again) { toast("两次输入的新密码不一致"); return; }
        try {
          await api("/api/auth/password", { method: "POST", body: { old: data.old, new: data.new } });
          await loadMe();
          state.editPassword = false;
          toast("密码已修改");
          VIEWS.settings("account");
        } catch (e) { toast(e.message); }
      };
      return;
    }
    setPage(`${head}<div class="empty">正在读取…</div>`);
    let data;
    try { data = await api("/api/settings"); } catch (error) { toast(error.message); return; }
    if (!location.hash.startsWith("#/settings")) return;
    if (tab === "study") {
      quiet = true;
      setPage(`${head}<div class="section"><h3>并发数量</h3></div>
        <p class="hint">同时处理的数量。智慧树的视频始终逐个播放，不受这里影响。</p>
        <div class="models"><div class="form_card" style="margin:0;box-shadow:none">
          <div class="field"><label>AI 答题</label><input type="number" min="1" max="8" name="ai" value="${data.concurrency.ai}"></div>
          <div class="field"><label>自动学习</label><input type="number" min="1" max="8" name="study" value="${data.concurrency.study}"></div>
          <div class="form_btns"><button class="btn primary" data-concurrency>保存</button></div></div></div>
        <div class="section"><h3>外观</h3></div>
        <div class="models"><div class="model plain"><span class="m1"><b>夜读模式</b><small class="sans">深色主题，只替换颜色，不改布局。只记在这个浏览器里。</small></span>
          <button class="btn" data-toggle-theme>切换 <span class="kbd">⌘J</span></button></div></div>`);
      quiet = false;
      $("[data-concurrency]", page).onclick = async () => {
        const body = { ai: Number($('input[name="ai"]', page).value), study: Number($('input[name="study"]', page).value) };
        try { await api("/api/settings/concurrency", { method: "POST", body }); toast("已保存"); } catch (e) { toast(e.message); }
      };
      return;
    }
    renderModels(head, data.models);
  },
};

function fixButtons(j, i) {
  const type = TYPE_OF[j.title];
  if (j.state === "waiting") {
    if (/登录/.test(j.summary)) return `<button class="btn small primary" data-go="#/login/${j.platform}">重新登录</button>`;
    if (/时长/.test(j.summary)) return "";
    if (type === "answer") return `<button class="btn small" data-go="#/course/${j.platform}/${j.course}/answer">验证后重新作答</button>`;
    return `<button class="btn small" data-retry="${i}">验证后重试</button>`;
  }
  if (j.state !== "error" || i !== state.history.findIndex((x) => x.course === j.course && x.title === j.title)) return "";
  if (type === "answer") return `<button class="btn small" data-go="#/course/${j.platform}/${j.course}/answer">重新选择试卷</button>`;
  return `<button class="btn small primary" data-retry="${i}">重试</button>`;
}

/* ---------------------------------------------------------------- settings: models */

const HEALTH = { ok: ["ok", "可用"], error: ["error", "出错"] };
function renderModels(head, models, editing = null) {
  const host = (url) => { try { return new URL(url).host; } catch { return url; } };
  const form = (m) => `<form class="form_card" data-form="${m ? esc(m.id) : "new"}">
      <h3>${m ? "编辑模型" : "添加模型"}</h3>
      <div class="field"><label>名称</label><input name="provider_name" value="${esc(m?.provider || "")}" placeholder="方便自己区分，例如 DeepSeek 官方"></div>
      <div class="field"><label>服务地址</label><input name="base_url" value="${esc(m?.base_url || "")}" placeholder="https://api.example.com/v1" required></div>
      <div class="field"><label>模型</label><input name="model" value="${esc(m?.model || "")}" placeholder="例如 deepseek-chat" required></div>
      <div class="field"><label>API Key</label><input name="api_key" type="password" autocomplete="off" ${m ? `placeholder="留空则不修改（当前 ${esc(m.key_hint)}）"` : "required"}></div>
      <div class="form_btns"><button type="button" class="btn" data-cancel>取消</button><button class="btn primary">${m ? "保存" : "保存并测试"}</button></div>
    </form>`;
  const row = (m) => {
    if (editing === m.id) return form(m);
    const [cls, label] = HEALTH[m.health] || ["", "未测试"];
    return `<div class="model ${m.active ? "on" : ""}" data-id="${esc(m.id)}">
      <button class="radio" data-activate title="${m.active ? "使用中" : "设为使用中"}" aria-label="设为使用中"></button>
      <span class="m1"><b>${esc(m.provider || "未命名服务")}</b>${m.deletable ? "" : '<span class="pill">内置</span>'}${m.active ? '<span class="pill blue">使用中</span>' : ""}
        <small>${esc(m.model)} · ${esc(host(m.base_url))} · ${esc(m.key_hint)}</small></span>
      <span class="health ${cls}" title="${esc(m.health_message)}">${label}</span>
      <span class="row_btns"><button class="btn small" data-test>测试</button><button class="btn small" data-edit>编辑</button>
        ${m.deletable ? `<button class="btn small danger" data-delete>删除</button>` : `<button class="btn small" disabled title="内置供应商不能删除，可以切换到别的模型使用">删除</button>`}</span></div>`;
  };
  quiet = true;
  setPage(`${head}<div class="section"><h3>AI 模型</h3><button class="link" data-add>＋ 添加模型</button></div>
    <p class="hint">答题、讨论和评价都用"使用中"的模型，点左边的圆圈切换。</p>
    ${editing === "new" ? form(null) : ""}<div class="models">${models.map(row).join("")}</div>`);
  quiet = false;
  const reload = async (edit = null) => {
    try { renderModels(head, (await api("/api/settings")).models, edit); } catch (e) { toast(e.message); }
  };
  $("[data-add]", page).onclick = () => renderModels(head, models, "new");
  page.querySelectorAll("[data-cancel]").forEach((el) => (el.onclick = () => renderModels(head, models)));
  page.querySelectorAll(".model[data-id]").forEach((rowEl) => {
    const id = rowEl.dataset.id;
    $("[data-activate]", rowEl).onclick = async () => {
      try { await api("/api/settings/models/activate", { method: "POST", body: { id } }); toast("已切换模型"); reload(); } catch (e) { toast(e.message); }
    };
    $("[data-edit]", rowEl).onclick = () => renderModels(head, models, id);
    $("[data-test]", rowEl).onclick = async () => {
      const chip = $(".health", rowEl);
      chip.className = "health busy"; chip.textContent = "测试中";
      try { const r = await api(`/api/settings/models/${id}/test`, { method: "POST" }); toast(r.health === "ok" ? "连接正常" : r.message); } catch (e) { toast(e.message); }
      reload();
    };
    const del = $("[data-delete]", rowEl);
    if (del) del.onclick = async () => {
      if (!del.classList.contains("armed")) { del.classList.add("armed"); del.textContent = "确认删除"; setTimeout(() => { if (del.isConnected) { del.classList.remove("armed"); del.textContent = "删除"; } }, 3000); return; }
      try { await api(`/api/settings/models/${id}/delete`, { method: "POST" }); toast("已删除"); reload(); } catch (e) { toast(e.message); }
    };
  });
  const formEl = $("[data-form]", page);
  if (formEl) {
    formEl.querySelector("input").focus();
    formEl.onsubmit = async (event) => {
      event.preventDefault();
      const body = Object.fromEntries(new FormData(formEl));
      const id = formEl.dataset.form;
      try {
        const r = await api(id === "new" ? "/api/settings/models" : `/api/settings/models/${id}`, { method: "POST", body });
        if (id === "new") {
          toast("已保存，正在测试连接…");
          const t = await api(`/api/settings/models/${r.id}/test`, { method: "POST" });
          toast(t.health === "ok" ? "模型可用，已设为使用中" : `已保存，但连接失败：${t.message}`);
        } else toast("已保存");
        reload();
      } catch (e) { toast(e.message); }
    };
  }
}

/* ---------------------------------------------------------------- course page */

function studyActions(p, c) {
  if (p.id === "zhs" && c.kind === "shared") return [
    ["study", ICON.play, "自动学习", "逐个播放还没看完的视频，约 1.5 倍速，视频里的弹题会自动作答。"],
    ["review", ICON.repeat, "每日复习", "从看完的视频里轮换着重看 30 分钟（1 倍速），用来攒学习习惯分，每天一次。"],
  ];
  if (p.id === "zhs") return [["study", ICON.play, "自动学习", "完成知识点下的文档、课件和视频，先做不用等的资源。"]];
  if (p.id === "mooc") return [["study", ICON.play, "自动学习", "按顺序完成还没学完的课件和视频，并按主题参与讨论、提交五星评价。需要先添加 AI 模型。"]];
  return [["study", ICON.play, "自动学习", "完成资源库课程里的学习资源。"]];
}
const STAMP = { 考试: "k", 测验: "c" };

function renderCourse(p, c, tab, full = false) {
  const k = key(p.id, c.id);
  const papers = state.papers[k];
  const open = (papers || []).filter((x) => x.available && !x.done).length;
  const chapters = unitsOf(k);
  const t = chapters && tally(chapters);
  const answerBlock = blocker(p.id, c.id, "answer");
  const last = state.history.find((j) => j.platform === p.id && j.course === c.id);
  const head = `
    <div class="crumb"><span class="dot ${p.id}"></span>${esc(p.name)}<span>›</span>${esc(c.name)}</div>
    <h1 class="title">${esc(c.name)}</h1>
    <p class="sub">${esc(c.type || p.short)}${t ? ` · ${t.all} 个学习单元` : ""}${last ? ` · 上次运行 ${ago(last.ended || last.started)}` : ""}</p>
    <dl class="ledger">
      <div><dt>学习进度</dt><dd>${c.progress == null ? '<span class="big">—</span>' : `<span class="big">${esc(c.progress)}<small>%</small></span>`}<span class="note">平台记录</span></dd></div>
      <div><dt>已完成</dt><dd><span class="big">${t ? t.done : "—"}</span><span class="note">/ ${t ? t.all : "—"} 单元</span></dd></div>
      <div><dt>待学</dt><dd><span class="big">${t ? t.queued + t.running + t.waiting + t.failed : "—"}</span><span class="note">单元</span></dd></div>
      <div><dt>可作答</dt><dd><span class="big red">${p.features.answer && papers ? open : "—"}</span><span class="note">${p.features.answer ? "份试卷" : "暂不支持"}</span></dd></div>
    </dl>
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" aria-selected="${tab === "study"}" data-go="#/course/${p.id}/${c.id}">学习</button>
      <button class="tab" role="tab" aria-selected="${tab === "answer"}" data-go="#/course/${p.id}/${c.id}/answer">答题${open ? `<span class="n">${open}</span>` : ""}</button>
    </div>`;
  let body;
  if (tab === "study") {
    body = `<div class="acts">${studyActions(p, c).map(([type, icon, label, text], i) => {
      const lastOf = lastRun(p.id, c.id, type);
      const block = blocker(p.id, c.id, type);
      const foot = block || (lastOf ? `上次 ${ago(lastOf.ended || lastOf.started)} · ${lastOf.summary}` : "还没有运行过");
      return `<button class="act" data-start="${type}" ${block ? "disabled" : ""}><span class="act_head"><span class="glyph">${icon}</span>${label}</span>
        <p>${text}</p><span class="foot"><span>${esc(foot)}</span>${i === 0 ? '<span class="kbd">S</span>' : ""}</span></button>`;
    }).join("")}</div>
      <div class="section"><h3>学习单元</h3><span class="aside">每格一个单元，${TOUCH ? "点一格" : "悬停"}查看</span></div>
      ${gridHtml(k)}`;
  } else if (!p.features.answer) {
    body = `<div class="notice">${esc(p.name)} 的网页版答题还在接入中。现在可以在终端里运行 <code>python StartWork.py</code>，选这门课后进入「AI 答题」。</div>`;
  } else if (!papers) {
    body = `<div class="empty">正在读取试卷…</div>`;
  } else {
    const selected = selectedOf(k);
    const row = (x) => {
      const on = selected.has(x.id);
      const pill = !x.available ? `<span class="pill">${esc(x.status)}</span>`
        : x.redo_left != null ? `<span class="pill blue">可重做 · 剩 ${esc(x.redo_left)} 次</span>`
        : x.done ? `<span class="pill">已提交</span>` : "";
      const note = x.reason || x.hint || (x.available ? x.status : "");
      return `<button class="paper ${on ? "on" : ""} ${x.available ? "" : "off"}" data-paper="${esc(x.id)}">
        <span class="box">${on ? ICON.tick : ""}</span><span class="stamp ${STAMP[x.category] || ""}">${esc((x.category || "作")[0])}</span>
        <span class="pname"><b>${esc(x.name)}</b><small title="${esc(note)}">${esc(x.category)}${note ? ` · ${esc(note)}` : ""}</small></span>
        <span>${pill}</span>
        <span class="score ${x.score == null ? "none" : ""}">${x.score == null ? "—" : esc(x.score)}</span></button>`;
    };
    const order = ["作业", "附件作业", "测验", "考试"];
    const rank = (label) => (order.includes(label) ? order.indexOf(label) : order.length);
    const categories = [...new Set(papers.map((x) => x.category))].sort((a, b) => rank(a) - rank(b));
    const n = selected.size;
    body = categories.map((label) => {
      const list = papers.filter((x) => x.category === label);
      return `<div class="section"><h3>${esc(label)}</h3><span class="aside">${list.filter((x) => x.available).length} / ${list.length} 份可选</span></div>
        <div class="papers">${list.map(row).join("")}</div>`;
    }).join("") || `<div class="empty">这门课没有作业、测验或考试</div>`;
    if (categories.length) body += `<div class="dock"><span class="info">${answerBlock ? esc(answerBlock) : n ? `已选 <b>${n}</b> 份 · 交卷前会再确认一次` : "勾选要作答的试卷"}</span>
      <button class="btn" data-save ${n && !answerBlock ? "" : "disabled"}>仅生成答案</button>
      <button class="btn primary ${state.armed === k ? "armed" : ""}" data-submit ${n && !answerBlock ? "" : "disabled"}>${state.armed === k ? `再按一次，交 ${n} 份卷` : "AI 作答并交卷"}</button></div>`;
  }
  if (full) setPage(head + body); else { quiet = true; setPage(head + body); quiet = false; }

  page.querySelectorAll("[data-start]").forEach((el) => (el.onclick = () => startJob({ platform: p.id, course: c.id, type: el.dataset.start })));
  page.querySelectorAll("[data-paper]").forEach((el) => (el.onclick = () => {
    const x = papers.find((y) => String(y.id) === el.dataset.paper);
    if (!x.available) { toast(x.reason || "这份试卷暂不可选"); return; }
    const selected = selectedOf(k);
    selected.has(x.id) ? selected.delete(x.id) : selected.add(x.id);
    renderCourse(p, c, tab);
  }));
  const tasks = () => [...selectedOf(k)];
  const save = $("[data-save]", page);
  if (save) save.onclick = () => startJob({ platform: p.id, course: c.id, type: "answer", tasks: tasks(), auto_submit: false });
  const submit = $("[data-submit]", page);
  if (submit) submit.onclick = () => {
    if (state.armed === k) { state.armed = null; startJob({ platform: p.id, course: c.id, type: "answer", tasks: tasks(), auto_submit: true }); return; }
    state.armed = k;
    renderCourse(p, c, tab);
    setTimeout(() => { if (state.armed === k) { state.armed = null; repaint(); } }, 4000);
  };
  if (tab === "study") loadUnits(p.id, c.id);
  if (tab === "answer" && p.features.answer && !papers) loadPapers(p, c);
}

const loadingPapers = new Set();
async function loadPapers(p, c) {
  const k = key(p.id, c.id);
  if (loadingPapers.has(k)) return;
  loadingPapers.add(k);
  try {
    const papers = (await api(`/api/platforms/${p.id}/tasks?course=${encodeURIComponent(c.id)}`)).tasks;
    state.papers[k] = papers;
    const selected = selectedOf(k);
    if (!selected.size) papers.filter((x) => x.available && !x.done).forEach((x) => selected.add(x.id));
  } catch (error) {
    state.papers[k] = [];
    toast(error.message);
  } finally {
    loadingPapers.delete(k);
  }
  repaint();
}

/* ---------------------------------------------------------------- player & focus sheet */

const LOG_LEVEL = { error: ["err", "ERR"], warn: ["warn", "WARN"] };
function logHtml(lines, filter = "all") {
  const shown = lines.filter((l) => filter === "all" || (filter === "warn" ? l.level : l.level === "error")).slice(0, 120);
  return shown.map((l) => {
    const [cls, lv] = LOG_LEVEL[l.level] || (l.done ? ["ok", "OK"] : ["", "·"]);
    return `<div class="line ${cls}"><time>${esc(l.time)}</time><span class="lv">${lv}</span><span class="msg">${esc(l.text)}</span></div>`;
  }).join("") || `<div class="line none">${lines.length ? "没有符合条件的日志" : "等待第一条进度…"}</div>`;
}

function progressOf(run) {
  const part = run.sub?.total ? run.sub : run.overall?.total ? run.overall : null;
  if (!part) return { pct: null, label: "" };
  return { pct: Math.min(100, Math.round((part.done / part.total) * 100)), label: `${part.done}/${part.total} ${part.unit || ""}`.trim() };
}

function recentCells(k) {
  const all = (unitsOf(k) || []).flatMap((ch) => ch.units);
  if (!all.length) return "";
  let at = all.findIndex((u) => u.state === "running");
  if (at < 0) at = all.findIndex((u) => u.state === "queued");
  if (at < 0) at = all.length - 1;
  const start = Math.max(0, Math.min(at - 8, all.length - 14));
  return `<span class="mini">${all.slice(start, start + 14).map((u) => `<i class="cell ${u.state}"></i>`).join("")}</span>`;
}

const player = $(".player"), sheet = $(".sheet");
const LIVE_WORD = { 自动学习: "正在学习", 每日复习: "正在复习", "AI 答题": "正在答题" };
const recentlyDone = () => (!state.jobs.length && state.lastDone && Date.now() - state.lastDone.shownAt < 60000 ? state.lastDone : null);

function renderPlayer() {
  const job = focusedJob(), done = recentlyDone();
  if (!job && !done) { player.hidden = true; return; }
  if (player.hidden) { player.hidden = false; player.classList.remove("enter"); void player.offsetWidth; player.classList.add("enter"); }
  if (job) {
    const run = runOf(job.id), queued = job.state === "queued";
    const { pct, label } = progressOf(run);
    const others = state.jobs.length - 1;
    player.classList.remove("ended");
    patch(player, `
      <div class="np"><span class="art">${job.title === "AI 答题" ? ICON.pen : job.title === "每日复习" ? ICON.repeat : ICON.play}</span>
        <span class="what"><small><span class="live">${queued ? "排队中" : LIVE_WORD[job.title] || "进行中"}</span>${esc(job.course_name)}</small>
        <b>${esc(queued ? `排队中，等你前面的任务结束（最多同时 ${state.jobLimit} 个）` : run.unit?.name || run.detail || "准备中…")}</b></span></div>
      <div class="scrub"><span class="track ${pct == null ? "unknown" : ""}"><i style="width:${pct ?? 0}%"></i></span><span class="t">${esc(label || clock(elapsed(job)))}</span></div>
      <div class="np_meta">${recentCells(key(job.platform, job.course))}
        ${others ? `<button class="btn" data-go="#/jobs" title="查看全部任务">另有 ${others} 个</button>` : ""}
        <button class="btn" data-open="${job.id}">展开 <span class="kbd">F</span></button><button class="icon_btn" data-stop="${job.id}" title="停止 ⇧S" aria-label="停止">${ICON.stop}</button></div>`);
  } else {
    player.classList.add("ended");
    patch(player, `
      <div class="np"><span class="art ${done.state === "waiting" ? "waiting" : "still"}">${ICON[done.state] || ICON.stopped}</span>
        <span class="what"><small>${esc(done.title)} · ${esc(done.course_name)} · ${STATE_WORD[done.state] || ""}</small><b>${esc(done.summary || "已结束")}</b></span></div>
      <div class="np_meta"><button class="btn" data-go="#/jobs">查看</button><button class="btn" data-dismiss>关闭</button></div>`);
  }
  if (sheet.classList.contains("open")) renderFocus();
}

function renderFocus() {
  const live = focusedJob();
  const job = live || state.lastDone;
  const body = $(".sheet_body", sheet);
  // With several jobs, the bar doubles as a switcher between them.
  patch($(".sheet_tag", sheet), state.jobs.length > 1
    ? state.jobs.map((j) => `<button class="chip" data-focus="${j.id}" aria-pressed="${j.id === live.id}">${esc(j.title)} · ${esc(j.course_name)}</button>`).join("")
    : live ? `<span class="live">${LIVE_WORD[live.title] || "进行中"}</span>` : "");
  const stop = $("[data-stop]", sheet);
  stop.hidden = !live;
  if (live) stop.dataset.stop = live.id;
  if (!job) { patch(body, `<div class="focus"><div><div class="clock">—</div><p class="of">现在没有任务。</p></div></div>`); return; }
  const run = live ? runOf(live.id) : freshRun();
  const { pct, label } = progressOf(run);
  const k = key(job.platform, job.course);
  const grid = job.title !== "AI 答题" && unitsOf(k)
    ? `<div class="section" style="margin-top:34px"><h3>整门课</h3><span class="aside mono">${tally(unitsOf(k)).done}/${tally(unitsOf(k)).all}</span></div>${gridHtml(k)}` : "";
  const headline = live
    ? `<div class="clock" data-clock="${live.id}">${clock(elapsed(job))}<small>已用时</small></div>
       <div class="now">${esc(run.unit?.name || run.detail || "准备中…")}</div><div class="of">${esc(run.detail && run.unit ? run.detail : job.course_name)}</div>
       <div class="track ${pct == null ? "unknown" : ""}"><i style="width:${pct ?? 0}%"></i></div>
       <div class="ticks"><span>开始</span><span>${esc(label)}</span><span>结束</span></div>`
    : `<div class="clock">${STATE_WORD[job.state] || "结束"}<small>${duration(elapsed(job))}</small></div>
       <div class="now">${esc(job.summary || "")}</div><div class="of">${esc(job.title)} · ${esc(job.course_name)}</div>`;
  patch(body, `<div class="focus"><div>${headline}${grid}</div>
    <div class="log"><div class="log_head"><span class="grow">日志</span>
      ${[["all", "全部"], ["warn", "提醒"], ["err", "错误"]].map(([f, l]) => `<button class="chip" data-filter="${f}" aria-pressed="${state.logFilter === f}">${l}</button>`).join("")}</div>
      <div class="log_body">${logHtml(live ? run.lines : job.lines || [], state.logFilter)}</div></div></div>`);
}
function openSheet(id) {
  if (id) state.focus = id;
  if (!focusedJob() && !state.lastDone) return;
  renderFocus();
  renderPlayer();
  sheet.classList.add("open");
}
function closeSheet() { sheet.classList.remove("open"); }

setInterval(() => {
  const byId = (id) => state.jobs.find((j) => j.id === id);
  document.querySelectorAll("[data-clock]").forEach((el) => { const j = byId(el.dataset.clock); if (j) el.firstChild.textContent = clock(elapsed(j)); });
  document.querySelectorAll("[data-elapsed]").forEach((el) => { const j = byId(el.dataset.elapsed); if (j) el.textContent = duration(elapsed(j)); });
}, 1000);

/* ---------------------------------------------------------------- jobs */

async function startJob(body) {
  try {
    const started = await api("/api/jobs", { method: "POST", body });
    if (started.id) state.focus = started.id;
    if (body.type === "answer") { delete state.papers[key(body.platform, body.course)]; state.selected[key(body.platform, body.course)] = new Set(); }
  } catch (error) { toast(error.message); }
}
async function stopJob(id) {
  if (!id) return;
  try { await api("/api/jobs/stop", { method: "POST", body: { id } }); toast("正在停止…"); } catch (error) { toast(error.message); }
}

const STATUS = { success: "完成", skipped: "跳过", error: "出错", submitted: "已交卷", saved: "已保存答案", preview: "已生成答案", paused: "已暂停", ai_error: "AI 出错", submission_unknown: "交卷结果未知", waiting: "等你操作" };
function pushLine(run, text, level = "", done = false) {
  run.lines.unshift({ text, level, done, time: new Date().toTimeString().slice(0, 5) });
  run.lines.length = Math.min(run.lines.length, 200);
}

function finishUnits(job) {
  const k = key(job.platform, job.course);
  for (const ch of unitsOf(k) || []) for (const u of ch.units) if (u.state === "running") u.state = job.state === "waiting" ? "waiting" : "queued";
  // The platform is the source of truth once the job is over; a waiting job keeps its marks until the user acts.
  if (job.state !== "waiting" && jobType(job) !== "answer") loadUnits(job.platform, job.course, true);
}

function handleEvent(event) {
  if (event.type === "job") {
    const at = state.jobs.findIndex((j) => j.id === event.id);
    if (event.state === "running" || event.state === "queued") {
      if (at >= 0) state.jobs[at] = event;
      else {
        state.jobs.push(event);
        runOf(event.id);
        state.lastDone = null;
        if (jobType(event) !== "answer") loadUnits(event.platform, event.course);
      }
    } else {
      if (at >= 0) state.jobs.splice(at, 1);
      delete state.runs[event.id];
      if (state.focus === event.id) state.focus = null;
      state.history = [event, ...state.history.filter((j) => j.id !== event.id)].slice(0, 20);
      state.lastDone = Object.assign({}, event, { shownAt: Date.now() });
      toast(`${event.title} · ${event.summary || STATE_WORD[event.state] || "结束"}`);
      finishUnits(event);
      delete state.papers[key(event.platform, event.course)];
      loadCourses(event.platform);
      setTimeout(repaint, 61000);
    }
  } else if (event.type === "progress" || event.type === "log") {
    const job = state.jobs.find((j) => j.id === event.job);
    if (!job) return;
    const run = runOf(job.id);
    if (event.type === "log") pushLine(run, event.text, event.level);
    else if (event.kind === "begin") run.overall = { name: event.name, total: event.total || 0, done: 0, unit: event.unit };
    else if (event.kind === "add_total" && run.overall) run.overall.total += event.total || 0;
    else if (event.kind === "detail") run.detail = event.name;
    else if (event.kind === "unit") {
      const u = findUnit(key(job.platform, job.course), event.key);
      if (u) u.state = event.status;
      if (event.status === "running") run.unit = { name: event.name, key: event.key };
    } else if (event.kind === "result") {
      if (run.overall) run.overall.done += 1;
      pushLine(run, `${event.name} · ${STATUS[event.status] || event.status || ""}`, event.status === "error" || event.status === "ai_error" ? "error" : event.status === "waiting" ? "warn" : "", true);
    } else if (event.kind === "resources" || event.kind === "questions") {
      run.sub = { name: event.name, done: event.completed || 0, total: event.total || 0, unit: event.unit === "项资源" ? "项" : event.unit };
    }
  }
  repaint();
}

let paintQueued = false;
function repaint() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => {
    paintQueued = false;
    renderSidebar();
    renderPlayer();
    const [, name, a, b, c] = parseHash(location.hash);
    quiet = true;
    try {
      if (name === "jobs") VIEWS.jobs();
      else if (name === "course") {
        const p = platformOf(a), course = courseOf(a, decodeURIComponent(b || ""));
        if (p && course) renderCourse(p, course, c === "answer" ? "answer" : "study");
        else if (p) VIEWS.course(a, decodeURIComponent(b || ""), c);
      }
    } finally { quiet = false; }
  });
}

function connectEvents() {
  if (MOCK) { import("./mock.js").then((m) => m.subscribe(handleEvent)); return; }
  const source = new EventSource(session.mode === "hosted" ? "/api/events" : `/api/events?t=${encodeURIComponent(TOKEN)}`);
  source.onmessage = (message) => handleEvent(JSON.parse(message.data));
  // Events sent while the stream was down are lost; catch up from the server on reconnect.
  let opened = false;
  source.onopen = () => { if (opened) refreshState().then(repaint); opened = true; };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshState().then(repaint); });
}

/* ---------------------------------------------------------------- login */

async function qrLogin(pid) {
  state.loginToken.cancelled = true;
  const token = (state.loginToken = { cancelled: false });
  const box = $(".qr", page), status = $("[data-status]", page);
  const say = (title, text) => { status.innerHTML = `<b>${esc(title)}</b><p>${esc(text)}</p>`; };
  const setImage = (src) => { box.innerHTML = `<img alt="登录二维码" src="${src}">`; };
  try { setImage((await api(`/api/platforms/${pid}/login`, { method: "POST" })).qr); }
  catch (error) { say("获取二维码失败", error.message); return; }
  while (!token.cancelled) {
    await sleep(1500);
    if (token.cancelled || !box.isConnected) return;
    let result;
    try { result = await api(`/api/platforms/${pid}/login`); } catch (error) { say("网络异常", error.message); continue; }
    if (result.qr) setImage(result.qr);
    if (result.status === "scanned") say("已扫码", "请在手机上确认登录。");
    else if (result.status === "refreshed") say("二维码已刷新", "旧二维码过期了，请扫描新的。");
    else if (result.status === "failed") { say("登录未完成", result.message || "请点击刷新二维码重试。"); return; }
    else if (result.status === "done") { token.cancelled = true; await afterLogin(pid); return; }
  }
}

async function browserLogin(pid) {
  state.loginToken.cancelled = true;
  const token = (state.loginToken = { cancelled: false });
  const status = $("[data-status]", page), button = $("[data-start-login]", page);
  const say = (title, text) => { status.innerHTML = `<b>${esc(title)}</b><p>${esc(text)}</p>`; };
  button.disabled = true;
  // Opened synchronously inside the click so popup blockers allow it; pointed at the SSO page below.
  const tab = session.mode === "hosted" ? window.open("", "_blank") : null;
  try {
    const started = await api(`/api/platforms/${pid}/login`, { method: "POST" });
    if (started.open && tab) tab.location = started.open; else if (started.open) location.href = started.open; else tab?.close();
    say("等待职教云登录", started.message);
  } catch (error) { tab?.close(); say("无法开始登录", error.message); button.disabled = false; return; }
  while (!token.cancelled) {
    await sleep(1500);
    if (token.cancelled || !status.isConnected) return;
    let result;
    try { result = await api(`/api/platforms/${pid}/login`); } catch (error) { say("网络异常", error.message); continue; }
    if (result.status === "failed") { say("登录未完成", result.message); button.disabled = false; button.textContent = "重新打开登录页"; return; }
    if (result.status === "done") { token.cancelled = true; await afterLogin(pid); return; }
  }
}

async function afterLogin(pid) {
  toast("登录成功");
  await refreshState();
  await loadCourses(pid);
  switchPlatform(pid);
}

async function loadCourses(pid) {
  try { state.courses[pid] = (await api(`/api/platforms/${pid}/courses`)).courses; }
  catch (error) { toast(error.message); state.courses[pid] = state.courses[pid] || []; }
  repaint();
  const p = platformOf(pid);
  if (p?.features.answer) for (const c of state.courses[pid] || []) if (!state.papers[key(pid, c.id)]) loadPapers(p, c);
}

async function refreshState() {
  try {
    const data = await api("/api/state");
    state.platforms = data.platforms;
    state.history = data.history || [];
    state.jobLimit = data.job_limit || state.jobLimit;
    const live = data.jobs || [];
    for (const job of live) {
      if (state.jobs.some((j) => j.id === job.id)) continue;
      runOf(job.id).lines = job.lines || [];
      if (jobType(job) !== "answer") loadUnits(job.platform, job.course);
    }
    // Jobs that ended while we were away: drop them and refresh what they changed.
    for (const gone of state.jobs.filter((j) => !live.some((x) => x.id === j.id))) {
      delete state.runs[gone.id];
      const ended = state.history.find((h) => h.id === gone.id);
      if (ended) finishUnits(ended);
      delete state.papers[key(gone.platform, gone.course)];
    }
    state.jobs = live;
    for (const p of state.platforms) if (!p.logged_in) delete state.courses[p.id];
  } catch (error) { toast(error.message); }
  renderSidebar();
}

/* ---------------------------------------------------------------- palette */

const overlay = $("[data-overlay]");
const finderInput = $("input", overlay);
const finderList = $(".rows", overlay);
let finderItems = [], finderIndex = 0;
function openFinder() { overlay.classList.add("open"); finderInput.value = ""; finderIndex = 0; paintFinder(); finderInput.focus(); }
function closeFinder() { overlay.classList.remove("open"); }
function paintFinder() {
  const q = finderInput.value.trim().toLowerCase();
  const match = (text) => !q || text.toLowerCase().includes(q);
  const pages = [["#/jobs", ICON.pulse, "任务"], ["#/settings", ICON.gear, "设置"], ...(session.user?.role === "admin" ? [["#/admin", ICON.shield, "管理"]] : []),
    ...state.platforms.map((p) => [homeOf(p), `<span class="dot ${p.id}"></span>`, `切换到 ${p.name}`])]
    .filter(([, , label]) => match(label)).map(([hash, icon, label]) => ({ hash, icon, label, meta: "页面" }));
  const courses = state.platforms.flatMap((p) => (state.courses[p.id] || []).filter((c) => match(c.name))
    .map((c) => ({ hash: `#/course/${p.id}/${c.id}`, icon: ring(c.progress, COLOR[p.id]), label: c.name, meta: `${p.short} 课程` })));
  const papers = q ? state.platforms.flatMap((p) => (state.courses[p.id] || []).flatMap((c) => (state.papers[key(p.id, c.id)] || [])
    .filter((x) => match(x.name)).map((x) => ({ hash: `#/course/${p.id}/${c.id}/answer`, icon: `<span class="stamp ${STAMP[x.category] || ""}">${esc((x.category || "作")[0])}</span>`, label: x.name, meta: c.name })))).slice(0, 8) : [];
  finderItems = [...courses, ...pages, ...papers];
  finderIndex = Math.min(finderIndex, Math.max(0, finderItems.length - 1));
  let i = 0;
  const groups = [courses, pages, papers].filter((g) => g.length).map((g) => g.map(() => {
    const it = finderItems[i];
    const html = `<button class="prow" data-i="${i}" aria-selected="${i === finderIndex}">${it.icon}<span>${esc(it.label)}</span><span class="meta">${esc(it.meta)}</span></button>`;
    i += 1;
    return html;
  }).join(""));
  finderList.innerHTML = groups.join('<div class="psep"></div>') || `<div class="prow" style="color:var(--text-4)">没有找到</div>`;
  finderList.querySelectorAll("[data-i]").forEach((el) => (el.onclick = () => { closeFinder(); go(finderItems[el.dataset.i].hash); }));
  finderList.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
}
finderInput.addEventListener("input", () => { finderIndex = 0; paintFinder(); });
overlay.addEventListener("click", (event) => { if (event.target === overlay) closeFinder(); });

/* ---------------------------------------------------------------- global input */

document.addEventListener("click", (event) => {
  const t = event.target;
  if (state.menuOpen && !t.closest(".menu, [data-menu]")) { state.menuOpen = false; renderSidebar(); }
  if (state.meOpen && !t.closest("[data-me]")) { state.meOpen = false; renderSidebar(); }
  if (t.closest("[data-me]")) { state.meOpen = !state.meOpen; renderSidebar(); return; }
  if (t.closest("[data-signout]")) { signOut(); return; }
  if (t.closest("[data-drawer]")) { document.body.classList.toggle("drawer"); return; }
  if (t.closest("[data-go], [data-find]")) document.body.classList.remove("drawer");
  if (t.closest("[data-menu]")) { state.menuOpen = !state.menuOpen; renderSidebar(); return; }
  const sw = t.closest("[data-switch]"); if (sw) { switchPlatform(sw.dataset.switch); return; }
  const target = t.closest("[data-go]"); if (target) { closeSheet(); go(target.dataset.go); }
  if (t.closest("[data-find]")) openFinder();
  if (t.closest("[data-toggle-theme]")) toggleTheme();
  const open = t.closest("[data-open]"); if (open) openSheet(open.dataset.open);
  if (t.closest("[data-close]")) closeSheet();
  const stop = t.closest("[data-stop]"); if (stop) stopJob(stop.dataset.stop);
  const focus = t.closest("[data-focus]"); if (focus) { state.focus = focus.dataset.focus; renderFocus(); renderPlayer(); }
  if (t.closest("[data-dismiss]")) { state.lastDone = null; repaint(); }
  const filter = t.closest("[data-filter]"); if (filter) { state.logFilter = filter.dataset.filter; renderFocus(); }
  const retry = t.closest("[data-units-retry]");
  if (retry) { const [pid, ...rest] = retry.dataset.unitsRetry.split(":"); delete state.units[retry.dataset.unitsRetry]; loadUnits(pid, rest.join(":")); repaint(); }
});

window.addEventListener("keydown", (event) => {
  const mod = event.metaKey || event.ctrlKey;
  if (overlay.classList.contains("open")) {
    if (event.key === "Escape" || (mod && event.key.toLowerCase() === "k")) { event.preventDefault(); closeFinder(); }
    else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      finderIndex = (finderIndex + (event.key === "ArrowDown" ? 1 : -1) + finderItems.length) % Math.max(1, finderItems.length);
      paintFinder();
    } else if (event.key === "Enter" && finderItems[finderIndex]) { closeFinder(); go(finderItems[finderIndex].hash); }
    return;
  }
  if (mod && ["1", "2", "3"].includes(event.key) && state.platforms[event.key - 1]) { event.preventDefault(); switchPlatform(state.platforms[event.key - 1].id); return; }
  if (mod && event.key.toLowerCase() === "k") { event.preventDefault(); openFinder(); return; }
  if (mod && event.key.toLowerCase() === "j") { event.preventDefault(); toggleTheme(); return; }
  if (event.key === "Escape" && document.body.classList.contains("drawer")) { document.body.classList.remove("drawer"); return; }
  if (event.key === "Escape" && state.meOpen) { state.meOpen = false; renderSidebar(); return; }
  if (event.key === "Escape") { if (state.menuOpen) { state.menuOpen = false; renderSidebar(); } else closeSheet(); return; }
  if (mod || event.altKey || event.target.closest("input, textarea, select") || document.body.classList.contains("signed-out")) return;
  if (event.key === "S" && event.shiftKey) { stopJob(focusedJob()?.id); return; }
  if (event.key === "f") { openSheet(); return; }
  if (event.key === "s") $('[data-start]:not([disabled])', page)?.click();
});

/* ---------------------------------------------------------------- router */

const parseHash = (hash) => (hash || "").match(/^#\/(\w+)(?:\/([^/]+))?(?:\/([^/]+))?(?:\/([^/]+))?/) || [];

function route() {
  const [, name, a, b, c] = parseHash(location.hash);
  if (!name || name === "all") { go(homeOf(currentPlatform())); return; }
  if ((name === "course" || name === "login") && platformOf(a) && state.current !== a) { state.current = a; store.set("studia-platform", a); }
  if (name !== "login") state.loginToken.cancelled = true;
  state.view = name === "course" ? `#/course/${a}/${b}` : name === "login" ? `#/login/${a}` : `#/${name}`;
  renderSidebar();
  renderPlayer();
  scroller.scrollTop = 0;
  if (name === "settings") { state.editPassword = false; VIEWS.settings(a); }
  else if (name === "course") VIEWS.course(a, decodeURIComponent(b || ""), c);
  else if (name === "login") VIEWS.login(a);
  else if (name === "admin" && session.user?.role !== "admin") go(homeOf(currentPlatform()));
  else if (VIEWS[name]) VIEWS[name]();
  else go(homeOf(currentPlatform()));
}
window.addEventListener("hashchange", route);

// The sign-in page's left half: a course grid quietly studying itself.
function authGrid() {
  const rows = [9, 7, 11, 8, 10, 6];
  let n = 0;
  return rows.map((count) => `<span class="cells">${Array.from({ length: count }, () => {
    n += 1;
    const state = n % 13 === 0 ? "skipped" : n < 34 ? "done" : "queued";
    return `<i class="cell ${state}"></i>`;
  }).join("")}</span>`).join("");
}
let authTimer = 0;
function animateAuthGrid(box) {
  clearInterval(authTimer);
  authTimer = setInterval(() => {
    if (!box.isConnected) { clearInterval(authTimer); return; }
    const running = $(".auth_grid .cell.running", box);
    if (running) running.className = "cell done";
    const next = $(".auth_grid .cell.queued", box);
    if (next) next.className = "cell running";
    else box.querySelectorAll(".auth_grid .cell.done").forEach((c, i) => { if (i > 20) c.className = "cell queued"; });
  }, 900);
}

const AUTH = {
  login: { title: "登录", lead: "欢迎回来。", button: "登录", switch: ["还没有账号？", "用邀请码注册", "register"] },
  register: { title: "注册", lead: "邀请码向管理员要，每个只能用一次。", button: "注册并登录", switch: ["已经有账号？", "去登录", "login"] },
  setup: { title: "创建管理员", lead: "这是新部署的 Studia。设置码在服务器日志里，Docker 部署可以运行 docker compose logs studia 查看。", button: "创建并进入" },
};
function showAuth(mode = "login") {
  document.body.classList.add("signed-out");
  let box = $(".auth");
  if (!box) { box = document.createElement("div"); box.className = "auth"; document.body.appendChild(box); }
  const text = AUTH[mode];
  const field = (name, label, attrs) => `<label class="auth_field"><span>${label}</span>${attrs.includes("password")
    ? `<span class="pw"><input name="${name}" ${attrs}><button type="button" class="pw_eye" data-reveal aria-label="显示密码">显示</button></span>`
    : `<input name="${name}" ${attrs}>`}</label>`;
  box.innerHTML = `
    <section class="auth_art" aria-hidden="true">
      <div class="brand"><span class="seal">S</span><b>Studia</b></div>
      <div class="auth_pitch"><h1>课程、视频和作业，<br>摊在一张书桌上。</h1>
        <p>自动学习还没学完的单元，AI 帮你作答试卷；每一格都看得见进度。</p></div>
      <div class="auth_grid">${authGrid()}</div>
      <p class="auth_foot">MOOC · 智慧树</p>
    </section>
    <section class="auth_side">
      <form class="auth_card" data-auth novalidate>
        <div class="brand auth_mobile_brand"><span class="seal">S</span><b>Studia</b></div>
        <h2>${text.title}</h2><p class="auth_lead">${esc(text.lead)}</p>
        ${mode === "setup" ? field("code", "设置码", 'required autocomplete="off" autocapitalize="characters" placeholder="例如 2593-2D9E-D41F"') : ""}
        ${mode === "register" ? field("invite", "邀请码", 'required autocomplete="off" autocapitalize="characters" placeholder="例如 3F2A-91BC-04DE"') : ""}
        ${field("username", mode === "setup" ? "管理员用户名" : "用户名", `required autocomplete="username" autocapitalize="off" ${mode === "login" ? "" : 'minlength="2" maxlength="32"'}`)}
        ${field("password", "密码", `type="password" required autocomplete="${mode === "login" ? "current-password" : "new-password"}" ${mode === "login" ? "" : 'minlength="8" placeholder="至少 8 位"'}`)}
        ${mode === "setup" ? field("again", "再输一次密码", 'type="password" required autocomplete="new-password" minlength="8"') : ""}
        <p class="auth_error" role="alert"></p>
        <button class="btn primary auth_submit">${text.button}</button>
        ${text.switch ? `<p class="auth_note">${text.switch[0]}<button type="button" class="link" data-auth-mode="${text.switch[2]}">${text.switch[1]}</button></p>` : ""}
      </form>
    </section>`;
  animateAuthGrid(box);
  box.querySelectorAll("[data-auth-mode]").forEach((el) => (el.onclick = () => showAuth(el.dataset.authMode)));
  box.querySelectorAll("[data-reveal]").forEach((el) => (el.onclick = () => {
    const input = el.previousElementSibling, show = input.type === "password";
    input.type = show ? "text" : "password";
    el.textContent = show ? "隐藏" : "显示";
    el.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  }));
  const form = $("[data-auth]", box);
  const error = $(".auth_error", form);
  if (!TOUCH) form.querySelector("input").focus();  // on a phone this would pop the keyboard over the page
  form.oninput = () => { error.textContent = ""; };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const missing = [...form.querySelectorAll("input")].find((input) => !input.checkValidity());
    if (missing) {
      const label = missing.closest("label").firstElementChild.textContent;
      error.textContent = missing.validity.tooShort ? `${label}至少 ${missing.minLength} 位` : `请填写${label}`;
      missing.focus();
      return;
    }
    const body = Object.fromEntries(new FormData(form));
    if (mode === "setup" && body.password !== body.again) { error.textContent = "两次输入的密码不一致"; return; }
    const button = $(".auth_submit", form);
    button.disabled = true;
    button.textContent = "请稍候…";
    try {
      await api(`/api/auth/${mode}`, { method: "POST", body });
      location.reload();
    } catch (failure) {
      error.textContent = failure.message;
      button.disabled = false;
      button.textContent = text.button;
    }
  };
}

async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already gone */ }
  location.reload();
}

async function loadMe() {
  try {
    const me = await api("/api/auth/me");
    Object.assign(session, { user: me.user, csrf: me.csrf });
    return true;
  } catch { return false; }
}

(async function boot() {
  let setup = false;
  if (!MOCK) { try { ({ mode: session.mode, setup } = await api("/api/mode")); } catch { /* old server: local */ } }
  if (MOCK && params.has("auth")) { showAuth(params.get("auth") || "login"); return; }
  if (setup) { showAuth("setup"); return; }
  if (session.mode === "hosted" && !(await loadMe())) { showAuth(); return; }
  await refreshState();
  connectEvents();
  await Promise.all(state.platforms.filter((p) => p.logged_in).map((p) => loadCourses(p.id)));
  route();
})();

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
function pie(percent, size = 16, color = "var(--blue)") {
  const p = Math.max(0, Math.min(100, Number(percent) || 0)) / 100;
  const r = size / 2, inner = r - 3, angle = p * Math.PI * 2;
  const x = r + inner * Math.sin(angle), y = r - inner * Math.cos(angle);
  const wedge = p >= 1 ? `<circle cx="${r}" cy="${r}" r="${inner}" fill="${color}"/>`
    : p > 0 ? `<path d="M${r} ${r}V${r - inner}A${inner} ${inner} 0 ${angle > Math.PI ? 1 : 0} 1 ${x} ${y}Z" fill="${color}"/>` : "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><circle cx="${r}" cy="${r}" r="${r - 1}" fill="none" stroke="${color}" stroke-width="1.4"/>${wedge}</svg>`;
}
const svg = (body, size = 16, extra = 'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"') =>
  `<svg width="${size}" height="${size}" viewBox="0 0 16 16" ${extra}>${body}</svg>`;
const ICON = {
  all: svg('<rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/><rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/>'),
  jobs: svg('<path d="M2.5 8h2.2l1.6-4.5 3.2 9 1.7-4.5h2.3"/>'),
  gear: svg('<circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2"/>'),
  chev: svg('<path d="m4.5 6 3.5 3.5L11.5 6"/>', 12),
  play: svg('<path d="M5 3.2v9.6L12.6 8z" fill="currentColor" stroke="none"/>'),
  repeat: svg('<path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3"/>'),
  login: svg('<path d="M9.5 2.5h3v11h-3M3 8h7M7.5 5l3 3-3 3"/>'),
  logout: svg('<path d="M6.5 2.5h-3v11h3M10 5l3 3-3 3M13 8H6"/>', 14),
  stop: svg('<rect x="3.5" y="3.5" width="9" height="9" rx="2" fill="currentColor" stroke="none"/>', 11),
  check: '<svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="m3.8 7.8 2.4 2.4 5-5.2" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  shield: svg('<path d="M8 1.8 13 3.6v4c0 3.2-2.1 5.4-5 6.6-2.9-1.2-5-3.4-5-6.6v-4z"/>'),
  tick: svg('<path d="m3.5 8.5 3 3 6-7"/>'),
  search: svg('<circle cx="7" cy="7" r="5"/><path d="m11 11 3.5 3.5"/>', 15),
  ok: svg('<circle cx="8" cy="8" r="6.5" fill="#4cb36c" stroke="none"/><path d="m5.2 8.2 1.9 1.9 3.8-4" stroke="#fff" stroke-width="1.7"/>'),
  err: svg('<circle cx="8" cy="8" r="6.5" fill="#f94876" stroke="none"/><path d="M8 4.8v3.6M8 10.8v.1" stroke="#fff" stroke-width="1.8"/>'),
  stopped: svg('<circle cx="8" cy="8" r="6.5" fill="#b8babd" stroke="none"/><rect x="5.6" y="5.6" width="4.8" height="4.8" rx="1" fill="#fff" stroke="none"/>'),
};

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
  platforms: [], courses: {}, papers: {}, selected: {}, collapsed: store.get("studia-collapsed", {}),
  job: null, run: freshRun(), history: [], view: "", loginToken: { cancelled: true }, lastDone: null,
  current: store.get("studia-platform", ""), menuOpen: false,
};
function freshRun() { return { detail: "", overall: null, sub: null, lines: [] }; }
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

/* ---------------------------------------------------------------- sidebar */

const INITIAL = { mooc: "M", zyk: "资", zhs: "智" };
const KEYS = ["mooc", "zyk", "zhs"];
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

function renderSidebar() {
  const p = currentPlatform();
  if (!p) return;
  const view = state.view;
  const item = (hash, icon, label, extra = "", cls = "") =>
    `<button class="nav_item ${cls}" data-go="${hash}" aria-current="${view === hash}">${icon}<span class="label">${esc(label)}</span>${extra}</button>`;
  const done = state.lastDone;
  const jobPill = state.job ? `<span class="pill">进行中</span>`
    : done ? `<span class="pill ${done.state === "done" ? "ok" : done.state === "error" ? "err" : "stop"}">${{ done: "完成", error: "出错", stopped: "已停止" }[done.state] || ""}</span>` : "";

  $(".switcher_wrap").innerHTML = `
    <button class="switcher" data-menu aria-haspopup="menu" aria-expanded="${state.menuOpen}">
      <span class="avatar ${p.id}">${INITIAL[p.id]}</span><span class="who"><b>${esc(p.name)}</b><small>${esc(platformStatus(p))}</small></span><span class="chev">${ICON.chev}</span></button>
    <button class="find_icon" data-find title="快速查找 ⌘K" aria-label="快速查找">${ICON.search}</button>
    <div class="menu" role="menu" ${state.menuOpen ? "" : "hidden"}>${state.platforms.map((x, i) => `
      <button class="menu_row" role="menuitem" data-switch="${x.id}"><span class="avatar ${x.id}">${INITIAL[x.id]}</span>
        <span class="who"><b>${esc(x.name)}</b><small>${esc(platformStatus(x))}</small></span>
        ${state.job && state.job.platform === x.id ? '<span class="live" title="任务进行中"></span>' : ""}
        ${x.id === p.id ? `<span class="tick">${ICON.tick}</span>` : `<span class="key">⌘${i + 1}</span>`}</button>`).join("")}</div>`;

  const courses = state.courses[p.id] || [];
  let html = item("#/jobs", ICON.jobs, "任务", jobPill);
  if (session.user?.role === "admin") html += item("#/admin", ICON.shield, "管理");
  html += `<div class="nav_label">${p.logged_in ? "课程" : esc(p.short)}</div>`;
  if (!p.logged_in) html += item(`#/login/${p.id}`, ICON.login, `登录${/^[A-Za-z]/.test(p.short) ? " " : ""}${p.short}`, "", "login");
  else if (!state.courses[p.id]) html += `<div class="nav_item"><span class="label" style="color:var(--faint)">正在读取课程…</span></div>`;
  else html += courses.map((c) => {
    const open = (state.papers[key(p.id, c.id)] || []).filter((x) => x.available && !x.done).length;
    return item(`#/course/${p.id}/${c.id}`, pie(c.progress, 16, COLOR[p.id]), c.name,
      open ? `<span class="count" title="${open} 份试卷可作答">${open}</span>` : "");
  }).join("") || `<div class="nav_item"><span class="label" style="color:var(--faint)">没有课程</span></div>`;
  $(".sidebar_nav").innerHTML = html;
  $(".sidebar_foot").innerHTML = session.user
    ? `<button class="foot_btn account" data-go="#/settings/account" title="账号与密码"><span class="me">${esc(session.user.username.slice(0, 1).toUpperCase())}</span>${esc(session.user.username)}</button>
       <button class="foot_btn icon" data-go="#/settings" aria-label="设置" title="设置">${ICON.gear}</button>`
    : `<button class="foot_btn" data-go="#/settings" aria-current="${view === "#/settings"}">${ICON.gear}设置</button>`;

  $("[data-menu]").onclick = (event) => { event.stopPropagation(); state.menuOpen = !state.menuOpen; renderSidebar(); };
  document.querySelectorAll("[data-switch]").forEach((el) => (el.onclick = () => switchPlatform(el.dataset.switch)));
}
document.addEventListener("click", (event) => {
  if (state.menuOpen && !event.target.closest(".menu, [data-menu]")) { state.menuOpen = false; renderSidebar(); }
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-go]");
  if (target) go(target.dataset.go);
  if (event.target.closest("[data-find]")) openFinder();
});

/* ---------------------------------------------------------------- page helpers */

const page = $(".page");
const toolbar = $(".toolbar");
const jobbar = $(".jobbar");
// Entrance animation only when the user navigates; background repaints must not replay it.
let quiet = false;
function setPage(html) {
  const scroll = $(".main_scroll").scrollTop;
  page.innerHTML = html;
  page.classList.remove("enter");
  if (quiet) { $(".main_scroll").scrollTop = scroll; return; }
  void page.offsetWidth;
  page.classList.add("enter");
}
function setToolbar(html) { toolbar.innerHTML = html; toolbar.hidden = !html; }

const LABEL = { study: "自动学习", review: "每日复习", answer: "AI 答题" };
const lastRun = (pid, cid, type) => state.history.find((j) => j.platform === pid && j.course === cid && j.title === LABEL[type]);

/* ---------------------------------------------------------------- views */

const VIEWS = {
  async admin() {
    setToolbar("");
    const head = `<h1 class="title">管理</h1><p class="meta">邀请新用户、管理账号，查看所有人正在运行的任务。</p>`;
    let users, invites;
    try { [users, invites] = await Promise.all([api("/api/admin/users"), api("/api/admin/invites")]); }
    catch (error) { setPage(`${head}<div class="empty">${esc(error.message)}</div>`); return; }
    if (state.view !== "#/admin") return;
    users = users.users; invites = invites.invites;
    const nameOf = (uid) => users.find((u) => u.id === uid)?.username || "—";
    const inviteState = { open: ["blue", "可用"], used: ["", "已使用"], expired: ["", "已过期"] };
    setPage(`${head}
      <div class="heading">邀请码<button class="link" data-invite>+ 生成邀请码</button></div>
      <p class="meta" style="margin:0 0 8px">把邀请码发给对方，对方在登录页选「用邀请码注册」。每个邀请码只能用一次，7 天后过期。</p>
      ${invites.map((i) => `<div class="model"><span class="model_body"><span class="model_name"><code class="code">${esc(i.code)}</code>
          <span class="tag ${inviteState[i.state][0]}">${inviteState[i.state][1]}</span></span>
          <span class="model_sub">${i.used_by ? `${esc(nameOf(i.used_by))} 已注册` : `创建于 ${ago(i.created)} · ${new Date(i.expires * 1000).toLocaleDateString()} 过期`}</span></span>
        <span class="model_actions">${i.state === "open" ? `<button class="button" data-copy="${esc(i.code)}">复制</button><button class="button danger" data-revoke="${esc(i.code)}">作废</button>` : ""}</span></div>`).join("")
        || `<div class="empty" style="margin-top:20px">还没有邀请码</div>`}
      <div class="heading">用户<span class="hint">${users.length} 人</span></div>
      ${users.map((u) => `<div class="model"><span class="me big">${esc(u.username.slice(0, 1).toUpperCase())}</span>
          <span class="model_body"><span class="model_name">${esc(u.username)}${u.role === "admin" ? '<span class="tag blue">管理员</span>' : ""}${u.disabled ? '<span class="tag">已停用</span>' : ""}</span>
          <span class="model_sub">${u.job ? `${esc(u.job.title)} · ${esc(u.job.course_name)} · ${u.job.state === "queued" ? "排队中" : "运行中"}` : u.last_login ? `上次登录 ${ago(u.last_login)}` : "还没登录过"}</span></span>
          <span class="model_actions">${u.job ? `<button class="button danger" data-stop-user="${u.id}">停止任务</button>` : ""}
            <button class="button" data-reset="${u.id}">重置密码</button>
            ${u.id === session.user.id ? "" : u.disabled ? `<button class="button" data-enable="${u.id}">启用</button>` : `<button class="button danger" data-disable="${u.id}">停用</button>`}</span></div>`).join("")}`);
    const act = async (path, body, message) => {
      try { await api(path, { method: "POST", body: body || {} }); toast(message); VIEWS.admin(); } catch (e) { toast(e.message); }
    };
    $("[data-invite]", page).onclick = async () => {
      try { const { code } = await api("/api/admin/invites", { method: "POST", body: { days: 7 } }); navigator.clipboard?.writeText(code).catch(() => {}); toast(`邀请码 ${code} 已复制`); VIEWS.admin(); }
      catch (e) { toast(e.message); }
    };
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
  },

  jobs() {
    setToolbar("");
    const rows = state.history.map((j, i) => {
      const icon = j.state === "done" ? ICON.ok : j.state === "error" ? ICON.err : ICON.stopped;
      return `<button class="history_row" data-history="${i}">${icon}<span class="item_body"><span class="item_line">${esc(j.title)} · ${esc(j.course_name)}</span>
        <span class="item_sub">${esc(platformOf(j.platform)?.short || "")} · ${esc(j.summary)}</span></span><span class="when">${ago(j.ended || j.started)}</span></button>
        <div class="history_lines" data-lines="${i}" hidden>${linesHtml(j.lines || [])}</div>`;
    }).join("");
    setPage(`<h1 class="title">任务</h1><p class="meta">任务在这台电脑上运行，关掉网页不会中断，关掉终端才会停止。</p>
      ${state.job ? `<div class="heading">进行中</div>${runCard()}` : ""}
      <div class="heading">历史<span class="hint">最近 ${state.history.length} 次，点开看详情</span></div>
      ${rows || `<div class="empty">还没有运行过任务</div>`}`);
    bindRunCard();
    page.querySelectorAll("[data-history]").forEach((el) => (el.onclick = () => {
      const lines = page.querySelector(`[data-lines="${el.dataset.history}"]`);
      lines.hidden = !lines.hidden;
    }));
  },

  login(pid) {
    setToolbar("");
    const p = platformOf(pid);
    if (!p) return setPage(`<div class="empty">不支持这个平台</div>`);
    if (p.logged_in) return go(homeOf(p));
    const head = `<div class="crumbs"><span class="dot ${pid}"></span>${esc(p.name)}</div><h1 class="title">登录${/^[A-Za-z]/.test(p.short) ? " " : ""}${esc(p.short)}</h1>`;
    if (p.login === "qr") {
      setPage(`${head}<p class="meta">用智慧树 App 扫描二维码，在手机上确认后，课程会出现在左侧。${session.mode === "hosted" ? "登录状态保存在服务器上你自己的目录里。" : "登录状态只保存在这台电脑上。"}</p>
        <div class="card"><div class="qr_card"><div class="qr_empty">正在获取二维码…</div>
          <p class="qr_status"><b>等待扫码</b>打开智慧树 App，点右上角的扫一扫。二维码过期会自动刷新。</p></div>
          <div class="card_actions"><button class="button" data-start-login>刷新二维码</button></div></div>`);
      $("[data-start-login]", page).onclick = () => qrLogin(pid);
      qrLogin(pid);
    } else {
      setPage(`${head}<p class="meta">${esc(p.name)} 使用职教云账号，通过浏览器授权登录。${session.mode === "hosted" ? "登录状态保存在服务器上你自己的目录里。" : "登录状态只保存在这台电脑上。"}</p>
        <div class="card"><p class="qr_status" data-status><b>在浏览器中登录</b>点击下面的按钮会打开职教云登录页，在那里登录完成后，这里会自动继续。MOOC 和资源库需要分别授权一次。</p>
          <div class="card_actions"><button class="button primary" data-start-login>打开登录页</button></div></div>`);
      $("[data-start-login]", page).onclick = () => browserLogin(pid);
    }
  },

  course(pid, cid, tab) {
    const p = platformOf(pid);
    if (!p || !p.logged_in) return go(p ? `#/login/${pid}` : homeOf(currentPlatform()));
    const c = courseOf(pid, cid);
    if (!c) { setToolbar(""); return setPage(`<div class="empty">${state.courses[pid] ? "没有找到这门课程" : "正在读取课程…"}</div>`); }
    renderCourse(p, c, tab === "answer" ? "answer" : "study", true);
  },

  async settings(tab = "models") {
    setToolbar("");
    tab = ["account", "models", "study"].includes(tab) ? tab : "models";
    const tabs = `<div class="tabs" role="tablist">${[["account", "账号"], ["models", "AI 模型"], ["study", "学习"]].map(([k, label]) =>
      `<button class="tab" role="tab" aria-selected="${k === tab}" data-go="#/settings/${k}">${label}</button>`).join("")}</div>`;
    const where = session.mode === "hosted" ? "设置只对你的账号生效，保存在服务器上你自己的目录里。" : "所有设置只保存在这台电脑上。";
    const head = `<h1 class="title">设置</h1><p class="meta">${where}</p>${tabs}`;
    if (tab === "account") {
      const studia = session.user ? `<div class="heading">Studia 账号</div>
        <div class="model"><span class="me big">${esc(session.user.username.slice(0, 1).toUpperCase())}</span>
          <span class="model_body"><span class="model_name">${esc(session.user.username)}${session.user.role === "admin" ? '<span class="tag blue">管理员</span>' : ""}</span>
          <span class="model_sub">修改密码后，其他设备上的登录会全部失效</span></span>
          <button class="button" data-signout>${ICON.logout}退出 Studia</button></div>
        <form class="form_card" data-password style="margin-top:6px"><h3>修改密码</h3>
          <div class="field"><label>原密码</label><input name="old" type="password" autocomplete="current-password" required></div>
          <div class="field"><label>新密码</label><input name="new" type="password" autocomplete="new-password" minlength="8" required></div>
          <div class="field"><label>再输一次</label><input name="again" type="password" autocomplete="new-password" minlength="8" required></div>
          <div class="row_buttons"><button class="button primary">保存新密码</button></div></form>` : "";
      setPage(`${head}${studia}<div class="heading">平台账号</div>
        ${state.platforms.map((p) => `<div class="model"><span class="avatar ${p.id}" style="width:24px;height:24px;border-radius:6px;display:grid;place-items:center;color:#fff;font-size:12px;font-weight:700">${INITIAL[p.id]}</span>
          <span class="model_body"><span class="model_name">${esc(p.name)}</span><span class="model_sub">${esc(platformStatus(p))} · ${p.login === "qr" ? "App 扫码登录" : "职教云浏览器授权"}</span></span>
          ${p.logged_in ? `<button class="button danger" data-logout="${p.id}">${ICON.logout}退出</button>` : `<button class="button primary" data-go="#/login/${p.id}">登录</button>`}</div>`).join("")}`);
      page.querySelectorAll("[data-logout]").forEach((el) => (el.onclick = async () => {
        try { await api(`/api/platforms/${el.dataset.logout}/logout`, { method: "POST" }); } catch (e) { toast(e.message); return; }
        await refreshState();
        toast("已退出登录");
        VIEWS.settings("account");
      }));
      const signout = $("[data-signout]", page);
      if (signout) signout.onclick = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already gone */ } location.reload(); };
      const form = $("[data-password]", page);
      if (form) form.onsubmit = async (event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(form));
        if (data.new !== data.again) { toast("两次输入的新密码不一致"); return; }
        try { await api("/api/auth/password", { method: "POST", body: { old: data.old, new: data.new } }); await loadMe(); form.reset(); toast("密码已修改"); }
        catch (e) { toast(e.message); }
      };
      return;
    }
    setPage(`${head}<div class="empty">正在读取…</div>`);
    let data;
    try { data = await api("/api/settings"); } catch (error) { toast(error.message); return; }
    if (!location.hash.startsWith("#/settings")) return;
    if (tab === "study") {
      quiet = true;
      setPage(`${head}<div class="heading">并发数量</div>
        <p class="meta" style="margin:0 0 10px">同时处理的数量。智慧树的视频始终逐个播放，不受这里影响。</p>
        <div class="field"><label>AI 答题</label><input type="number" min="1" max="8" name="ai" value="${data.concurrency.ai}"></div>
        <div class="field"><label>自动学习</label><input type="number" min="1" max="8" name="study" value="${data.concurrency.study}"></div>
        <div class="field"><span></span><span><button class="button primary" data-concurrency>保存</button></span></div>`);
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
      <div class="row_buttons"><button type="button" class="button" data-cancel>取消</button><button class="button primary">${m ? "保存" : "保存并测试"}</button></div>
    </form>`;
  const row = (m) => {
    if (editing === m.id) return form(m);
    const [cls, label] = HEALTH[m.health] || ["none", "未测试"];
    return `<div class="model ${m.active ? "active" : ""}" data-id="${esc(m.id)}">
      <button class="radio" data-activate title="${m.active ? "使用中" : "设为使用中"}" aria-label="设为使用中"></button>
      <span class="model_body"><span class="model_name">${esc(m.provider || "未命名服务")}${m.deletable ? "" : '<span class="tag">内置</span>'}${m.active ? '<span class="tag blue">使用中</span>' : ""}</span>
        <span class="model_sub">${esc(m.model)} · ${esc(host(m.base_url))} · Key ${esc(m.key_hint)}</span></span>
      <span class="health ${cls}" title="${esc(m.health_message)}">${label}</span>
      <span class="model_actions"><button class="button" data-test>测试</button><button class="button" data-edit>编辑</button>
        ${m.deletable ? `<button class="button danger" data-delete>删除</button>` : `<button class="button" disabled title="内置供应商不能删除，可以切换到别的模型使用">删除</button>`}</span></div>`;
  };
  quiet = true;
  setPage(`${head}<div class="heading">AI 模型<button class="link" data-add>+ 添加模型</button></div>
    <p class="meta" style="margin:0 0 8px">答题、讨论和评价都用"使用中"的模型，点左边的圆圈切换。内置供应商不能删除。</p>
    ${editing === "new" ? form(null) : ""}${models.map(row).join("")}`);
  quiet = false;
  const reload = async (edit = null) => {
    try { renderModels(head, (await api("/api/settings")).models, edit); } catch (e) { toast(e.message); }
  };
  $("[data-add]", page).onclick = () => renderModels(head, models, "new");
  page.querySelectorAll("[data-cancel]").forEach((el) => (el.onclick = () => renderModels(head, models)));
  page.querySelectorAll(".model").forEach((rowEl) => {
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
    ["review", ICON.repeat, "每日复习", "从看完的视频里轮换着重看 30 分钟（1 倍速），用来攒学习习惯分，每天运行一次。"],
  ];
  if (p.id === "zhs") return [["study", ICON.play, "自动学习", "完成知识点下的文档、课件和视频，先做非视频资源。"]];
  if (p.id === "mooc") return [["study", ICON.play, "自动学习", "完成课件和视频，并按主题自动参与讨论、提交五星评价。需要先在设置里添加 AI 模型。"]];
  return [["study", ICON.play, "自动学习", "完成资源库课程里的学习资源。"]];
}

function renderCourse(p, c, tab, full = false) {
  const k = key(p.id, c.id);
  const papers = state.papers[k];
  const open = (papers || []).filter((x) => x.available && !x.done).length;
  const jobHere = state.job && state.job.platform === p.id && state.job.course === c.id;
  const head = `
    <div class="crumbs"><span class="dot ${p.id}"></span><span>${esc(p.name)}</span><span>›</span><span>${esc(c.name)}</span></div>
    <h1 class="title">${pie(c.progress, 28, COLOR[p.id])}${esc(c.name)}</h1>
    <p class="meta">${esc(c.type || p.short)} · ${c.progress == null ? "进度未知" : `学习进度 ${c.progress}%`}</p>
    <div class="tabs" role="tablist">
      <button class="tab" role="tab" aria-selected="${tab === "study"}" data-go="#/course/${p.id}/${c.id}">学习</button>
      <button class="tab" role="tab" aria-selected="${tab === "answer"}" data-go="#/course/${p.id}/${c.id}/answer">答题${open ? `<span class="n">${open}</span>` : ""}</button>
    </div>
    ${jobHere ? runCard() : ""}`;
  let body;
  if (tab === "study") {
    body = `<div class="actions">${studyActions(p, c).map(([type, icon, label, text]) => {
      const last = lastRun(p.id, c.id, type);
      return `<div class="action"><div class="action_head"><span class="action_icon">${icon}</span>${label}</div><p>${text}</p>
        <span class="last">${last ? `上次：${ago(last.ended || last.started)} · ${esc(last.summary)}` : "还没有运行过"}</span>
        <button class="button primary" data-start="${type}" ${state.job ? "disabled" : ""}>${state.job ? "有任务在运行" : "开始" + label}</button></div>`;
    }).join("")}</div>`;
  } else if (!p.features.answer) {
    body = `<div class="notice">${esc(p.name)} 的网页版答题还在接入中。现在可以在终端里运行 <code>python StartWork.py</code>，选这门课后进入「AI 答题」。</div>`;
  } else if (!papers) {
    body = `<div class="empty">正在读取试卷…</div>`;
  } else {
    const selected = selectedOf(k);
    const row = (x) => {
      const on = selected.has(x.id);
      const tag = !x.available ? `<span class="tag">${esc(x.status)}</span>`
        : x.redo_left != null ? `<span class="tag blue">可重做 · 剩 ${esc(x.redo_left)} 次</span>`
        : x.done ? `<span class="tag">已提交</span>` : "";
      return `<button class="item ${on ? "selected" : ""} ${x.available ? "" : "off"}" data-paper="${esc(x.id)}" title="${esc(x.reason || x.hint || "")}">
        <span class="box ${on ? "checked" : ""} ${x.available ? "" : "disabled"}">${ICON.check}</span>
        <span class="item_body"><span class="item_line">${esc(x.name)}${tag}</span></span>
        ${x.score != null ? `<span class="item_right"><span class="chip">${esc(x.score)} 分</span></span>` : ""}</button>`;
    };
    const section = (label, list) => `<div class="heading">${label}<span class="hint">${list.filter((x) => x.available).length} / ${list.length} 份可选</span></div>
      ${list.map(row).join("") || `<div class="item off"><span class="item_body">没有${label}</span></div>`}`;
    const order = ["作业", "附件作业", "测验", "考试"];
    const rank = (label) => (order.includes(label) ? order.indexOf(label) : order.length);
    const categories = [...new Set(papers.map((x) => x.category))].sort((a, b) => rank(a) - rank(b));
    body = categories.map((label) => section(label, papers.filter((x) => x.category === label))).join("")
      || `<div class="empty">这门课没有作业、测验或考试</div>`;
  }
  if (full) setPage(head + body); else { quiet = true; setPage(head + body); quiet = false; }

  bindRunCard();
  page.querySelectorAll("[data-start]").forEach((el) => (el.onclick = () => startJob({ platform: p.id, course: c.id, type: el.dataset.start })));
  page.querySelectorAll("[data-paper]").forEach((el) => (el.onclick = () => {
    const x = papers.find((y) => String(y.id) === el.dataset.paper);
    if (!x.available) { toast(x.reason || "这份试卷暂不可选"); return; }
    const selected = selectedOf(k);
    selected.has(x.id) ? selected.delete(x.id) : selected.add(x.id);
    renderCourse(p, c, tab);
  }));
  if (tab === "answer" && p.features.answer) renderAnswerToolbar(p, c); else setToolbar("");
  if (tab === "answer" && p.features.answer && !papers) loadPapers(p, c);
}

function renderAnswerToolbar(p, c) {
  const k = key(p.id, c.id);
  const n = selectedOf(k).size;
  const busy = Boolean(state.job);
  setToolbar(`<span class="info">${busy ? "有任务在运行，结束后才能作答" : n ? `已选 ${n} 份试卷` : "勾选要作答的试卷"}</span><span class="spacer"></span>
    <button class="tool" data-save ${n && !busy ? "" : "disabled"}>仅生成答案</button>
    <button class="tool primary" data-submit ${n && !busy ? "" : "disabled"}>AI 作答并交卷${n ? `（${n}）` : ""}</button>`);
  const tasks = () => [...selectedOf(k)];
  $("[data-save]", toolbar).onclick = () => startJob({ platform: p.id, course: c.id, type: "answer", tasks: tasks(), auto_submit: false });
  const submit = $("[data-submit]", toolbar);
  submit.onclick = () => {
    if (submit.classList.contains("armed")) { startJob({ platform: p.id, course: c.id, type: "answer", tasks: tasks(), auto_submit: true }); return; }
    submit.classList.add("armed");
    submit.textContent = `再点一次确认交卷（${n}）`;
    setTimeout(() => { if (submit.isConnected) { submit.classList.remove("armed"); submit.textContent = `AI 作答并交卷（${n}）`; } }, 4000);
  };
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

/* ---------------------------------------------------------------- run card & job bar */

function percent() {
  const run = state.run;
  if (run.overall?.total) return Math.round((run.overall.done / run.overall.total) * 100);
  if (run.sub?.total) return Math.round((run.sub.done / run.sub.total) * 100);
  return null;
}
const linesHtml = (lines) => `<div class="checklist">${lines.slice(0, 40).map((l) =>
  `<div class="check_row"><span class="ring ${l.level === "error" ? "err" : l.done ? "done" : ""}"></span><span>${esc(l.text)}</span><time>${esc(l.time)}</time></div>`).join("")
  || `<div class="check_row" style="color:var(--faint)">等待第一条进度…</div>`}</div>`;

function runCard() {
  const job = state.job;
  if (!job) return "";
  const run = state.run;
  const pct = percent();
  return `<div class="card">
    <div class="card_head"><span class="dot ${job.platform}"></span><span class="card_title">${esc(job.title)} · ${esc(job.course_name)}</span><span class="tag blue">${job.state === "queued" ? "排队中" : "进行中"}</span></div>
    <p class="card_text">${esc(run.detail || "准备中…")}</p>
    <div class="progress"><i style="width:${pct ?? 4}%"></i></div>
    ${run.sub ? `<p class="card_text" style="margin-top:6px;font-size:12.5px;color:var(--muted)">${esc(run.sub.name)} · ${run.sub.done}/${run.sub.total || "?"} ${esc(run.sub.unit || "")}</p>` : ""}
    ${linesHtml(run.lines)}
    <div class="card_actions"><button class="button danger" data-stop>${ICON.stop}停止</button></div></div>`;
}
function bindRunCard() { page.querySelectorAll("[data-stop]").forEach((el) => (el.onclick = stopJob)); }

function renderJobbar() {
  const job = state.job;
  const done = !job && state.lastDone && Date.now() - state.lastDone.shownAt < 60000 ? state.lastDone : null;
  if (!job && !done) { jobbar.hidden = true; return; }
  jobbar.hidden = false;
  if (job) {
    const pct = percent();
    jobbar.innerHTML = `<span class="dot ${job.platform}"></span><span class="title">${esc(job.title)} · ${esc(job.course_name)}</span>
      <span class="detail">${esc(job.state === "queued" ? "服务器繁忙，排队等待中…" : state.run.detail || "准备中…")}</span>
      <span class="track ${pct == null ? "indeterminate" : ""}"><i style="width:${pct ?? 0}%"></i></span><span class="pct">${pct == null ? "" : pct + "%"}</span>
      <button class="tool" data-go="#/jobs">查看</button><button class="tool" data-stop-bar>${ICON.stop}停止</button>`;
    $("[data-stop-bar]", jobbar).onclick = stopJob;
  } else {
    const icon = done.state === "done" ? ICON.ok : done.state === "error" ? ICON.err : ICON.stopped;
    jobbar.innerHTML = `${icon}<span class="title">${esc(done.title)} · ${esc(done.course_name)}</span><span class="detail">${esc(done.summary)}</span>
      <button class="tool" data-go="#/jobs">查看</button><button class="tool" data-dismiss>关闭</button>`;
    $("[data-dismiss]", jobbar).onclick = () => { state.lastDone = null; renderJobbar(); renderSidebar(); };
  }
}

/* ---------------------------------------------------------------- jobs */

async function startJob(body) {
  try {
    await api("/api/jobs", { method: "POST", body });
    if (body.type === "answer") { delete state.papers[key(body.platform, body.course)]; state.selected[key(body.platform, body.course)] = new Set(); }
    $(".main_scroll").scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) { toast(error.message); }
}
async function stopJob() {
  try { await api("/api/jobs/stop", { method: "POST" }); } catch (error) { toast(error.message); }
}

const STATUS = { success: "完成", skipped: "跳过", error: "出错", submitted: "已交卷", saved: "已保存答案", preview: "已生成答案", paused: "已暂停", ai_error: "AI 出错", submission_unknown: "交卷结果未知" };
function pushLine(text, level = "", done = false) {
  state.run.lines.unshift({ text, level, done, time: new Date().toTimeString().slice(0, 5) });
  state.run.lines.length = Math.min(state.run.lines.length, 80);
}

function handleEvent(event) {
  const run = state.run;
  if (event.type === "job") {
    if (event.state === "queued" || (event.state === "running" && state.job?.id === event.id)) {
      state.job = event;
    } else if (event.state === "running") {
      state.job = event;
      state.run = freshRun();
      state.lastDone = null;
    } else {
      state.job = null;
      state.history = [event, ...state.history.filter((j) => j.id !== event.id)].slice(0, 20);
      state.lastDone = Object.assign({}, event, { shownAt: Date.now() });
      toast(`${event.title} · ${event.summary || "结束"}`);
      delete state.papers[key(event.platform, event.course)];
      loadCourses(event.platform);
      setTimeout(renderJobbar, 61000);
    }
  } else if (event.type === "progress") {
    if (event.kind === "begin") run.overall = { name: event.name, total: event.total || 0, done: 0, unit: event.unit };
    else if (event.kind === "add_total" && run.overall) run.overall.total += event.total || 0;
    else if (event.kind === "detail") run.detail = event.name;
    else if (event.kind === "result") {
      if (run.overall) run.overall.done += 1;
      pushLine(`${event.name} · ${STATUS[event.status] || event.status || ""}`, event.status === "error" || event.status === "ai_error" ? "error" : "", true);
    } else if (event.kind === "resources" || event.kind === "questions") {
      run.sub = { name: event.name, done: event.completed || 0, total: event.total || 0, unit: event.unit };
    }
  } else if (event.type === "log") pushLine(event.text, event.level);
  repaint();
}

let paintQueued = false;
function repaint() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => {
    paintQueued = false;
    renderSidebar();
    renderJobbar();
    const [, name, a, b, c] = parseHash(location.hash);
    quiet = true;
    try {
      if (name === "jobs") VIEWS.jobs();
      else if (name === "course") {
        const p = platformOf(a), course = courseOf(a, b);
        if (p && course) renderCourse(p, course, c === "answer" ? "answer" : "study");
        else if (p) VIEWS.course(a, b, c);
      }
    } finally { quiet = false; }
  });
}

function connectEvents() {
  if (MOCK) { import("./mock.js").then((m) => m.subscribe(handleEvent)); return; }
  const source = new EventSource(session.mode === "hosted" ? "/api/events" : `/api/events?t=${encodeURIComponent(TOKEN)}`);
  source.onmessage = (message) => handleEvent(JSON.parse(message.data));
}

/* ---------------------------------------------------------------- login */

async function qrLogin(pid) {
  state.loginToken.cancelled = true;
  const token = (state.loginToken = { cancelled: false });
  const box = $(".qr_card", page), status = $(".qr_status", page);
  const say = (title, text) => { status.innerHTML = `<b>${esc(title)}</b>${esc(text)}`; };
  const setImage = (src) => { box.querySelector(".qr_empty, img")?.remove(); box.insertAdjacentHTML("afterbegin", `<img alt="登录二维码" src="${src}">`); };
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
  const say = (title, text) => { status.innerHTML = `<b>${esc(title)}</b>${esc(text)}`; };
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
  state.collapsed[pid] = false;
  store.set("studia-collapsed", state.collapsed);
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
    if (data.job && !state.job) { state.job = data.job; state.run.lines = data.job.lines || []; }
    for (const p of state.platforms) if (!p.logged_in) delete state.courses[p.id];
  } catch (error) { toast(error.message); }
  renderSidebar();
}

/* ---------------------------------------------------------------- quick find */

const overlay = $("[data-overlay]");
const finderInput = $(".finder_input input");
const finderList = $(".finder_list");
let finderItems = [], finderIndex = 0;
function openFinder() { overlay.classList.add("open"); finderInput.value = ""; finderIndex = 0; paintFinder(); finderInput.focus(); }
function closeFinder() { overlay.classList.remove("open"); }
function paintFinder() {
  const q = finderInput.value.trim().toLowerCase();
  const match = (text) => !q || text.toLowerCase().includes(q);
  const pages = [["#/jobs", ICON.jobs, "任务"], ["#/settings", ICON.gear, "设置"], ...(session.user?.role === "admin" ? [["#/admin", ICON.shield, "管理"]] : []),
    ...state.platforms.map((p) => [homeOf(p), `<span class="dot ${p.id}"></span>`, `切换到 ${p.name}`])]
    .filter(([, , label]) => match(label)).map(([hash, icon, label]) => ({ hash, icon, label, meta: "" }));
  const courses = state.platforms.flatMap((p) => (state.courses[p.id] || []).filter((c) => match(c.name))
    .map((c) => ({ hash: `#/course/${p.id}/${c.id}`, icon: pie(c.progress, 16, COLOR[p.id]), label: c.name, meta: p.short })));
  const papers = q ? state.platforms.flatMap((p) => (state.courses[p.id] || []).flatMap((c) => (state.papers[key(p.id, c.id)] || [])
    .filter((x) => match(x.name)).map((x) => ({ hash: `#/course/${p.id}/${c.id}/answer`, icon: '<span class="box" style="margin:0"></span>', label: x.name, meta: c.name })))).slice(0, 8) : [];
  finderItems = [...pages, ...courses, ...papers];
  finderIndex = Math.min(finderIndex, Math.max(0, finderItems.length - 1));
  let i = 0;
  const groups = [pages, courses, papers].filter((g) => g.length).map((g) => g.map(() => {
    const it = finderItems[i];
    const html = `<button class="finder_row" data-i="${i}" aria-selected="${i === finderIndex}">${it.icon}<span>${esc(it.label)}</span><span class="meta">${esc(it.meta)}</span></button>`;
    i += 1;
    return html;
  }).join(""));
  finderList.innerHTML = groups.join('<div class="finder_sep"></div>') || `<div class="finder_row" style="color:var(--faint)">没有找到</div>`;
  finderList.querySelectorAll("[data-i]").forEach((el) => (el.onclick = () => { closeFinder(); go(finderItems[el.dataset.i].hash); }));
}
finderInput.addEventListener("input", () => { finderIndex = 0; paintFinder(); });
overlay.addEventListener("click", (event) => { if (event.target === overlay) closeFinder(); });
window.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && ["1", "2", "3"].includes(event.key) && state.platforms[event.key - 1]) {
    event.preventDefault(); switchPlatform(state.platforms[event.key - 1].id); return;
  }
  if (event.key === "Escape" && state.menuOpen) { state.menuOpen = false; renderSidebar(); return; }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); overlay.classList.contains("open") ? closeFinder() : openFinder(); return; }
  if (!overlay.classList.contains("open")) return;
  if (event.key === "Escape") closeFinder();
  else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    finderIndex = (finderIndex + (event.key === "ArrowDown" ? 1 : -1) + finderItems.length) % Math.max(1, finderItems.length);
    paintFinder();
  } else if (event.key === "Enter" && finderItems[finderIndex]) { closeFinder(); go(finderItems[finderIndex].hash); }
});

/* ---------------------------------------------------------------- router */

const parseHash = (hash) => (hash || "").match(/^#\/(\w+)(?:\/([^/]+))?(?:\/([^/]+))?(?:\/([^/]+))?/) || [];

function route() {
  const [, name, a, b, c] = parseHash(location.hash);
  if (!name || name === "all") { go(homeOf(currentPlatform())); return; }
  if ((name === "course" || name === "login") && platformOf(a) && state.current !== a) { state.current = a; store.set("studia-platform", a); }
  if (name !== "login") state.loginToken.cancelled = true;
  state.view = name === "course" ? `#/course/${a}/${b}` : name === "login" ? `#/login/${a}` : `#/${name}`;
  if (name === "settings") { renderSidebar(); renderJobbar(); $(".main_scroll").scrollTop = 0; VIEWS.settings(a); return; }
  renderSidebar();
  renderJobbar();
  $(".main_scroll").scrollTop = 0;
  if (name === "course") VIEWS.course(a, decodeURIComponent(b || ""), c);
  else if (name === "login") VIEWS.login(a);
  else if (name === "admin" && session.user?.role !== "admin") go(homeOf(currentPlatform()));
  else if (VIEWS[name]) VIEWS[name]();
  else go(homeOf(currentPlatform()));
}
window.addEventListener("hashchange", route);

function showAuth(tab = "login") {
  document.body.classList.add("signed-out");
  let box = $(".auth");
  if (!box) { box = document.createElement("div"); box.className = "auth"; document.body.appendChild(box); }
  box.innerHTML = `<form class="auth_card" data-auth>
      <div class="auth_brand"><span class="avatar zhs" style="background:var(--blue-fill)">${ICON.tick}</span>Studia</div>
      <div class="tabs" role="tablist">
        <button type="button" class="tab" role="tab" aria-selected="${tab === "login"}" data-auth-tab="login">登录</button>
        <button type="button" class="tab" role="tab" aria-selected="${tab === "register"}" data-auth-tab="register">用邀请码注册</button></div>
      ${tab === "register" ? `<div class="field"><label>邀请码</label><input name="invite" placeholder="例如 3F2A-91BC-04DE" required autocomplete="off"></div>` : ""}
      <div class="field"><label>用户名</label><input name="username" required autocomplete="username" autofocus></div>
      <div class="field"><label>密码</label><input name="password" type="password" required minlength="${tab === "register" ? 8 : 1}" autocomplete="${tab === "register" ? "new-password" : "current-password"}"></div>
      <p class="auth_error" role="alert"></p>
      <button class="button primary auth_submit">${tab === "register" ? "注册并登录" : "登录"}</button>
      <p class="auth_note">${tab === "register" ? "没有邀请码？请向管理员索取。" : "还没有账号？请向管理员要一个邀请码。"}</p></form>`;
  box.querySelectorAll("[data-auth-tab]").forEach((el) => (el.onclick = () => showAuth(el.dataset.authTab)));
  const form = $("[data-auth]", box);
  form.querySelector("input").focus();
  form.onsubmit = async (event) => {
    event.preventDefault();
    const button = $(".auth_submit", form);
    button.disabled = true;
    try {
      await api(tab === "register" ? "/api/auth/register" : "/api/auth/login", { method: "POST", body: Object.fromEntries(new FormData(form)) });
      location.reload();
    } catch (error) {
      $(".auth_error", form).textContent = error.message;
      button.disabled = false;
    }
  };
}

async function loadMe() {
  try {
    const me = await api("/api/auth/me");
    Object.assign(session, { user: me.user, csrf: me.csrf });
    return true;
  } catch { return false; }
}

(async function boot() {
  if (!MOCK) { try { session.mode = (await api("/api/mode")).mode; } catch { /* old server: local */ } }
  if (session.mode === "hosted" && !(await loadMe())) { showAuth(); return; }
  await refreshState();
  connectEvents();
  await Promise.all(state.platforms.filter((p) => p.logged_in).map((p) => loadCourses(p.id)));
  route();
})();

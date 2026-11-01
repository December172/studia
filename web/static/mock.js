// Offline fixtures for previewing the interface with ?mock (no server, no account).
const listeners = new Set();
const emit = (event) => listeners.forEach((listener) => listener(event));
const platforms = [
  { id: "mooc", name: "MOOC", short: "MOOC", login: "browser", logged_in: true, features: { answer: true, review: false } },
  { id: "zhs", name: "智慧树", short: "智慧树", login: "qr", logged_in: false, features: { answer: true, review: true } },
];
const courses = {
  mooc: [{ id: "m1", platform: "mooc", name: "大学英语（二）", type: "MOOC", kind: "mooc", progress: 62 }],
  zyk: [{ id: "z1", platform: "zyk", name: "Python 程序设计", type: "资源库", kind: "zyk", progress: 35 }],
  zhs: [
    { id: "shared-1", platform: "zhs", name: "新一代信息技术导论", type: "共享课", kind: "shared", progress: 100 },
    { id: "ai-1", platform: "zhs", name: "通信基础与计算机网络", type: "智慧课程", kind: "ai", progress: 84 },
  ],
};
const tasks = [
  { id: "t1", name: "第三章单元测试", category: "作业", status: "已提交", score: 8, available: true, hint: "重做成绩会覆盖当前成绩（当前 8/10），剩余重做 1 次。", redo_left: 1 },
  { id: "t2", name: "第五章单元测试", category: "作业", status: "已提交", score: 8, available: true, hint: "重做成绩会覆盖当前成绩（当前 8/10），剩余重做 1 次。", redo_left: 1 },
  { id: "t3", name: "第一章单元测试", category: "作业", status: "已满分", score: 10, available: false, reason: "已满分，无需重做。" },
  { id: "t5", name: "第十章单元测试", category: "作业", status: "已提交", score: 8, available: true, hint: "剩余重做 3 次", redo_left: 3 },
  { id: "t4", name: "新一代信息技术导论教程考试", category: "考试", status: "未开考", score: null, available: false, reason: "考试需要先在网页上手动开考" },
];
let job = null, polls = 0;
const models = [
  { id: "builtin-default", provider: "默认供应商", model: "gemini-3.8-flash", base_url: "https://mochif.com/v1", key_hint: "sk-…gTjW", active: true, health: "ok", health_message: "", deletable: false },
  { id: "legacy", provider: "mochif", model: "gemini-3.8-flash", base_url: "https://mochif.com/v1", key_hint: "sk-…PJlj", active: false, health: "", health_message: "", deletable: true },
  { id: "p2", provider: "DeepSeek 官方", model: "deepseek-v4.1-flash", base_url: "https://api.deepseek.com/v1", key_hint: "sk-…77c1", active: false, health: "error", health_message: "401 鉴权失败", deletable: true },
];
const history = [
  { id: "h1", title: "自动学习", course: "z1", course_name: "Python 程序设计", platform: "zyk", state: "done", summary: "学习完成",
    started: Date.now() / 1000 - 7200, ended: Date.now() / 1000 - 5400, lines: [{ text: "第 3 章 · 完成", done: true, time: "08:12" }] },
];

function fakeJob(body) {
  const course = courses[body.platform].find((c) => c.id === body.course);
  const title = { study: "自动学习", review: "每日复习", answer: "AI 答题" }[body.type];
  const steps = body.type === "answer" ? ["第三章单元测试", "第五章单元测试"] : ["信道的基本概念", "数据通信方式", "传输介质"];
  job = { type: "job", id: "j" + Date.now(), title, course: body.course, course_name: course.name, platform: body.platform,
    state: "running", summary: "", started: Date.now() / 1000, ended: null, lines: [] };
  emit(job);
  emit({ type: "progress", kind: "begin", name: title, total: steps.length, unit: "项" });
  let i = 0;
  const timer = setInterval(() => {
    if (!job) { clearInterval(timer); return; }
    const step = steps[i];
    emit({ type: "progress", kind: "detail", name: `${course.name} · ${step}` });
    emit({ type: "log", text: `开始：${step}` });
    let done = 0;
    const inner = setInterval(() => {
      done += 1;
      emit({ type: "progress", kind: "resources", name: step, completed: done, total: 6, unit: "项资源" });
      if (done === 6) { clearInterval(inner); emit({ type: "progress", kind: "result", name: step, status: "success" }); }
    }, 280);
    i += 1;
    if (i === steps.length) {
      clearInterval(timer);
      setTimeout(() => {
        const finished = Object.assign({}, job, { state: "done", summary: `${title}完成`, ended: Date.now() / 1000 });
        job = null;
        history.unshift(finished);
        emit(finished);
      }, 2200);
    }
  }, 2000);
}

export function subscribe(listener) { listeners.add(listener); }

export async function request(path, method, body) {
  await new Promise((resolve) => setTimeout(resolve, 200));
  let m;
  if (path === "/api/state") return { platforms, job, history };
  if ((m = path.match(/^\/api\/platforms\/(\w+)\/login$/))) {
    const p = platforms.find((x) => x.id === m[1]);
    if (method === "POST") {
      polls = 0;
      return p.login === "qr" ? { qr: "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29" shape-rendering="crispEdges"><rect width="29" height="29" fill="#fff"/><path d="M1 1h7v7H1zM21 1h7v7h-7zM1 21h7v7H1z" fill="none" stroke="#111"/><path d="M3 3h3v3H3zM23 3h3v3h-3zM3 23h3v3H3zM10 2h2v2h-2zM13 5h3v2h-3zM10 10h4v3h-4zM17 11h3v2h-3zM12 16h5v2h-5zM20 18h4v3h-4zM10 22h2v4h-2zM15 24h4v2h-4z" fill="#111"/></svg>') }
        : { status: "waiting", message: "已在浏览器打开职教云登录页" };
    }
    polls += 1;
    if (polls < 3) return { status: "waiting", message: "等待扫码" };
    if (polls < 4 && p.login === "qr") return { status: "scanned", message: "已扫码" };
    p.logged_in = true;
    return { status: "done", message: "登录成功" };
  }
  if ((m = path.match(/^\/api\/platforms\/(\w+)\/logout$/))) { platforms.find((x) => x.id === m[1]).logged_in = false; return {}; }
  if ((m = path.match(/^\/api\/platforms\/(\w+)\/courses$/))) return { courses: courses[m[1]] };
  if (path.match(/^\/api\/platforms\/(\w+)\/tasks/)) return { tasks };
  if (path === "/api/jobs" && method === "POST") { fakeJob(body); return { ok: true }; }
  if (path === "/api/jobs/stop") {
    const stopped = Object.assign({}, job, { state: "stopped", summary: "已停止", ended: Date.now() / 1000 });
    job = null; history.unshift(stopped); emit(stopped); return {};
  }
  if (path === "/api/settings") return { models, concurrency: { ai: 8, study: 8 } };
  if (path === "/api/settings/models/activate") { models.forEach((x) => (x.active = x.id === body.id)); return {}; }
  if ((m = path.match(/^\/api\/settings\/models\/([\w-]+)\/test$/))) {
    const x = models.find((y) => y.id === m[1]);
    x.health = x.model.includes("bad") ? "error" : "ok"; x.health_message = x.health === "ok" ? "" : "401 鉴权失败";
    return { health: x.health, message: x.health_message || "连接正常" };
  }
  if ((m = path.match(/^\/api\/settings\/models\/([\w-]+)\/delete$/))) { models.splice(models.findIndex((y) => y.id === m[1]), 1); return {}; }
  if (path === "/api/settings/models") {
    const id = "p" + Date.now();
    models.forEach((x) => (x.active = false));
    models.push({ id, provider: body.provider_name, model: body.model, base_url: body.base_url, key_hint: "sk-…new1", active: true, health: "", health_message: "", deletable: true });
    return { id };
  }
  if ((m = path.match(/^\/api\/settings\/models\/([\w-]+)$/))) {
    Object.assign(models.find((y) => y.id === m[1]), { provider: body.provider_name, model: body.model, base_url: body.base_url });
    return { id: m[1] };
  }
  return { ok: true };
}

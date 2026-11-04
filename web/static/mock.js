// Offline fixtures for previewing the interface with ?mock (no server, no account).
const listeners = new Set();
const emit = (event) => listeners.forEach((listener) => listener(event));
const platforms = [
  { id: "mooc", name: "MOOC", short: "MOOC", login: "browser", logged_in: true, features: { answer: true, review: false } },
  { id: "zhs", name: "智慧树", short: "智慧树", login: "qr", logged_in: true, features: { answer: true, review: true, qa: true } },
  { id: "cx", name: "超星", short: "超星", login: "password", logged_in: true, features: { study: false, answer: true, review: false } },
];
const courses = {
  mooc: [{ id: "m1", platform: "mooc", name: "大学英语（二）", type: "第 7 期开课", kind: "mooc", progress: 62 }, { id: "m2", platform: "mooc", name: "中外音乐与民族技艺实践", type: "第 3 期开课", kind: "mooc", progress: 100 }],
  zyk: [{ id: "z1", platform: "zyk", name: "Python 程序设计", type: "资源库", kind: "zyk", progress: 35 }],
  cx: [{ id: "cx-1", platform: "cx", name: "高等数学", type: "张老师", kind: "cx", progress: null,
      features: { study: false, answer: true, review: false } }],
  zhs: [
    { id: "shared-1", platform: "zhs", name: "新一代信息技术导论", type: "共享课", kind: "shared", progress: 100,
      features: { answer: true, review: true, qa: true }, shares: { progress: 20, habit: 20, interactive: 10, knowledge: 0, test: 10, meet: 0, final: 40 } },
    { id: "shared-2", platform: "zhs", name: "通信基础与计算机网络", type: "共享课", kind: "shared", progress: 64,
      features: { answer: true, review: false, qa: false }, shares: { progress: 40, habit: 0, interactive: 0, knowledge: 0, test: 20, meet: 0, final: 40 } },
    { id: "ai-1", platform: "zhs", name: "通信基础与计算机网络2026年秋冬学期", type: "智慧课程", kind: "ai", progress: 84,
      features: { answer: true, review: false, qa: false }, shares: null },
  ],
};
const tasks = [
  { id: "t1", name: "第三章单元测试", category: "作业", status: "已提交", score: 8, available: true, hint: "重做成绩会覆盖当前成绩（当前 8/10），剩余重做 1 次。", redo_left: 1 },
  { id: "t2", name: "第五章单元测试", category: "作业", status: "已提交", score: 8, available: true, hint: "重做成绩会覆盖当前成绩（当前 8/10），剩余重做 1 次。", redo_left: 1 },
  { id: "t3", name: "第一章单元测试", category: "作业", status: "已满分", score: 10, available: false, reason: "已满分，无需重做。" },
  { id: "t5", name: "第十章单元测试", category: "作业", status: "已提交", score: 8, available: true, hint: "剩余重做 3 次", redo_left: 3 },
  { id: "t4", name: "新一代信息技术导论教程考试", category: "考试", status: "未开考", score: null, available: false, reason: "考试需要先在网页上手动开考" },
];
// 超星不提供作业提交状态，预览里保留一条「状态未知」。
const cxTasks = [
  { id: "work-a", name: "1.1 章节测验", category: "作业", status: "状态未知", score: null, available: true, hint: "平台不提供这份作业的提交状态；已完成的作业请不要再勾选" },
  { id: "work-b", name: "1.2 章节作业", category: "作业", status: "状态未知", score: null, available: true, hint: "平台不提供这份作业的提交状态；已完成的作业请不要再勾选" },
];
const jobs = {};
let polls = 0;
const models = [
  { id: "builtin-default", provider: "默认供应商", model: "gemini-3.8-flash", base_url: "https://mochif.com/v1", key_hint: "sk-…gTjW", active: true, health: "ok", health_message: "", deletable: false },
  { id: "legacy", provider: "mochif", model: "gemini-3.8-flash", base_url: "https://mochif.com/v1", key_hint: "sk-…PJlj", active: false, health: "", health_message: "", deletable: true },
  { id: "p2", provider: "DeepSeek 官方", model: "deepseek-v4.1-flash", base_url: "https://api.deepseek.com/v1", key_hint: "sk-…77c1", active: false, health: "error", health_message: "401 鉴权失败", deletable: true },
];
const history = [
  { id: "h4", title: "AI 答题", course: "shared-1", course_name: "新一代信息技术导论", platform: "zhs", state: "done", summary: "处理完成 7/7 份试卷",
    started: Date.now() / 1000 - 700, ended: Date.now() / 1000 - 629, lines: [{ text: "新一代信息技术导论 · 第三章单元测试 · 已交卷", done: true, time: "14:32" }] },
  { id: "h3", title: "AI 答题", course: "shared-1", course_name: "新一代信息技术导论", platform: "zhs", state: "waiting", summary: "智慧树要求先完成滑块验证",
    started: Date.now() / 1000 - 900, ended: Date.now() / 1000 - 840, lines: [{ text: "第三章单元测试 · 等你操作", level: "warn", done: true, time: "14:20" }] },
  { id: "h2", title: "AI 答题", course: "m1", course_name: "大学英语（二）", platform: "mooc", state: "error", summary: "第 4 题：AI 返回的答案格式不符",
    started: Date.now() / 1000 - 4000, ended: Date.now() / 1000 - 3815, lines: [{ text: "第四单元测验 · 出错", level: "error", done: true, time: "13:40" }] },
  { id: "h1", title: "自动学习", course: "m1", course_name: "大学英语（二）", platform: "mooc", state: "done", summary: "学习完成",
    started: Date.now() / 1000 - 9000, ended: Date.now() / 1000 - 2900, lines: [{ text: "第 3 章 · 完成", done: true, time: "08:12" }] },
];

// Course grid: chapters of units; the first `done` units are already finished.
function chapters(spec, done) {
  let n = 0;
  return spec.map(([name, count], ci) => ({ name, units: Array.from({ length: count }, (_, ui) => {
    n += 1;
    const kind = ["video", "video", "ppt", "video", "doc", "作业"][(ui + ci) % 6];
    return { id: `u${ci}-${ui}`, name: `${ci + 1}.${ui + 1} ${{ video: "课文精讲", ppt: "课件", doc: "阅读材料", 作业: "单元作业" }[kind]}`, kind,
      minutes: kind === "video" ? 6 + ((ui * 5 + ci * 11) % 14) : 0, state: kind === "作业" ? "skipped" : n <= done ? "done" : "queued" };
  }) }));
}
const units = {
  m1: chapters([["第 1 章 自我介绍", 7], ["第 2 章 校园生活", 8], ["第 3 章 职场沟通", 9], ["第 4 章 产品说明", 8], ["第 5 章 安全规范", 7]], 22),
  "shared-1": chapters([["第一章 信息技术概述", 6], ["第二章 云计算", 6], ["第三章 人工智能", 7]], 99),
  "shared-2": chapters([["第一章 通信概述", 8], ["第二章 数据通信", 10]], 7),
  "ai-1": chapters([["广义的通信概念", 2], ["通信的发展", 5], ["现代通信的概念", 2], ["贝尔和贝尔实验室", 6], ["通信基本模型", 5], ["信息、消息与信号", 5],
    ["信息的度量", 4], ["信号基本概念和分类", 4], ["信号的变换", 4], ["信号的时域和频域特性", 4], ["通信系统一般模型", 4], ["调制与解调", 6],
    ["复用技术", 5], ["计算机网络的发展历程", 6], ["网络体系结构", 7], ["物理层", 6], ["数据链路层", 8], ["网络层", 9], ["传输层", 7], ["应用层", 6]], 66),
};

const GROUP = { study: "learn", review: "learn", answer: "answer", qa: "answer", ask: "answer" };
const qaPosts = [{ kind: "answer", question: "云计算的三种服务模式有什么区别？", text: "IaaS 提供虚拟机等基础设施，PaaS 提供运行平台，SaaS 直接提供软件。", at: Date.now() / 1000 - 7200 }];
function fakeJob(body) {
  const course = courses[body.platform].find((c) => c.id === body.course);
  for (const o of Object.values(jobs)) {
    if (GROUP[o.kind] !== GROUP[body.type] || o.platform !== body.platform) continue;
    if (o.course === body.course) throw new Error(`这门课正在${o.title}，请等它结束或先停止`);
    if (body.platform === "zhs" && GROUP[body.type] === "learn") throw new Error(`智慧树同一账号同时只能播放一个视频，请等「${o.course_name}」的${o.title}结束`);
  }
  const title = { study: "自动学习", review: "每日复习", answer: "AI 答题", qa: "问答区答题", ask: "问答区提问" }[body.type];
  const job = { type: "job", kind: body.type, id: "j" + Date.now().toString(16), title, course: body.course, course_name: course.name, platform: body.platform,
    state: "running", summary: "", started: Date.now() / 1000, ended: null, lines: [] };
  jobs[job.id] = job;
  emit(Object.assign({}, job));
  const alive = () => jobs[job.id];
  const send = (event) => alive() && emit(Object.assign(event, { job: job.id }));
  const finish = (state, summary) => {
    if (!alive()) return;
    const finished = Object.assign({}, alive(), { state, summary, ended: Date.now() / 1000 });
    delete jobs[job.id];
    history.unshift(finished);
    emit(finished);
  };
  if (body.type === "ask") {
    send({ type: "progress", kind: "begin", name: title, total: body.texts.length, unit: "条" });
    body.texts.forEach((q, i) => setTimeout(() => {
      qaPosts.push({ kind: "ask", question: q, text: "", at: Date.now() / 1000 });
      send({ type: "progress", kind: "result", name: q.slice(0, 20), status: "success" });
      if (i === body.texts.length - 1) setTimeout(() => finish("done", `发布了 ${body.texts.length} 条提问`), 600);
    }, 1200 * (i + 1)));
    return job.id;
  }
  if (body.type === "qa") {
    const asked = ["TCP 和 UDP 各适合什么场景？", "交换机和路由器的区别是什么？", "为什么要分层设计网络协议？"];
    send({ type: "progress", kind: "begin", name: title, total: asked.length, unit: "条" });
    asked.forEach((q, i) => setTimeout(() => {
      send({ type: "progress", kind: "detail", name: `第 ${i + 1} 条 · ${q}` });
      qaPosts.push({ kind: "answer", question: q, text: "这是 AI 写的回答示例：先讲清概念，再举一个课程里的例子说明适用场景。", at: Date.now() / 1000 });
      send({ type: "progress", kind: "result", name: q.slice(0, 20), status: "success" });
      if (i === asked.length - 1) setTimeout(() => finish("done", `回答了 ${asked.length} 条提问`), 800);
    }, 1500 * (i + 1)));
    return job.id;
  }
  if (body.type === "answer") {
    const steps = (body.tasks || []).map((id) => tasks.find((t) => t.id === id)?.name || "试卷");
    send({ type: "progress", kind: "begin", name: title, total: steps.length, unit: "份试卷" });
    steps.forEach((step, i) => setTimeout(() => {
      send({ type: "progress", kind: "detail", name: step });
      send({ type: "progress", kind: "questions", name: step, completed: 10, total: 10, unit: "题" });
      send({ type: "progress", kind: "result", name: step, status: body.auto_submit ? "submitted" : "preview" });
      if (i === steps.length - 1) setTimeout(() => finish("done", `处理完成 ${steps.length}/${steps.length} 份试卷`), 800);
    }, 1500 * (i + 1)));
    return job.id;
  }
  const todo = (units[body.course] || []).flatMap((ch) => ch.units).filter((u) => u.state === "queued" || u.state === "failed");
  const total = (units[body.course] || []).flatMap((ch) => ch.units).length;
  send({ type: "progress", kind: "begin", name: title, total: 1, unit: "门课程" });
  let i = 0, ticks = 0;
  const step = () => {
    if (!alive()) { clearInterval(timer); return; }
    const u = todo[i];
    if (!u) { clearInterval(timer); send({ type: "progress", kind: "result", name: course.name, status: "success" }); finish("done", "学习完成"); return; }
    if (ticks === 0) {
      u.state = "running";
      send({ type: "progress", kind: "unit", key: u.id, name: u.name, status: "running" });
      send({ type: "log", text: `${course.name} · 开始播放：${u.name}`, level: "" });
    }
    ticks += 1;
    const length = u.minutes ? 6 : 1, secs = (u.minutes || 1) * 60, mmss = (v) => `${String(Math.floor(v / 60)).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
    send({ type: "progress", kind: "detail", name: `${course.name} · ${u.name} · ${mmss(Math.round((secs * ticks) / length))}/${mmss(secs)}` });
    if (ticks >= length) {
      u.state = Math.random() < 0.08 ? "failed" : "done";
      send({ type: "progress", kind: "unit", key: u.id, name: u.name, status: u.state });
      send({ type: "log", text: u.state === "done" ? `${course.name} · 播放完成：${u.name}` : `${course.name} · ${u.name}：上报失败`, level: u.state === "done" ? "" : "error" });
      if (Math.random() < 0.12) send({ type: "log", text: "讨论区发帖频率限制，等待 60 秒", level: "warn" });
      i += 1; ticks = 0;
      const doneCount = total - todo.length + i;
      send({ type: "progress", kind: "resources", name: `${course.name} · ${u.name}`, completed: doneCount, total, unit: "项资源" });
    }
  };
  const timer = setInterval(step, 700);
  return job.id;
}

export function subscribe(listener) { listeners.add(listener); }

export async function request(path, method, body) {
  await new Promise((resolve) => setTimeout(resolve, 200));
  let m;
  if (path === "/api/state") return { platforms, jobs: Object.values(jobs), history };
  if ((m = path.match(/^\/api\/platforms\/(\w+)\/login(?:\?.*)?$/))) {
    const p = platforms.find((x) => x.id === m[1]);
    if (method === "POST") {
      polls = 0;
      if (p.login === "password") { p.logged_in = true; return { status: "done", message: "登录成功" }; }
      return p.login === "qr" ? { id: "mock", qr: "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29" shape-rendering="crispEdges"><rect width="29" height="29" fill="#fff"/><path d="M1 1h7v7H1zM21 1h7v7h-7zM1 21h7v7H1z" fill="none" stroke="#111"/><path d="M3 3h3v3H3zM23 3h3v3h-3zM3 23h3v3H3zM10 2h2v2h-2zM13 5h3v2h-3zM10 10h4v3h-4zM17 11h3v2h-3zM12 16h5v2h-5zM20 18h4v3h-4zM10 22h2v4h-2zM15 24h4v2h-4z" fill="#111"/></svg>') }
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
  if ((m = path.match(/^\/api\/platforms\/(\w+)\/tasks/))) return { tasks: m[1] === "cx" ? cxTasks : tasks };
  if ((m = path.match(/^\/api\/platforms\/\w+\/units\?course=(.+)$/))) return { chapters: units[decodeURIComponent(m[1])] || [] };
  if ((m = path.match(/^\/api\/platforms\/\w+\/qa\?course=(.+)$/))) return { posts: qaPosts.slice().reverse() };
  if (path.match(/^\/api\/platforms\/\w+\/qa\/draft$/)) {
    await new Promise((r) => setTimeout(r, 900));
    return { questions: ["边缘计算会不会取代云计算？各自适合什么场景？", "RPA 和传统的脚本自动化有什么本质区别？", "企业上云时数据安全的责任应该怎么划分？", "物联网设备越来越多，个人隐私该靠谁来保护？", "大模型写的代码出了事故，责任算谁的？"].filter((q) => !(body.have || []).includes(q)).slice(0, 1) };
  }
  if ((m = path.match(/^\/api\/platforms\/\w+\/score\?course=(.+)$/))) return { earned: {
    "shared-1": { progress: 20, habit: 9, interactive: 2, test: 9, meet: 0, final: 0, usual: 31, total: 40, qa: { answers: 3, questions: 1, received: 2 } },
    "shared-2": { progress: 28, habit: 0, interactive: 0, test: 20, meet: 0, final: 0, usual: 28, total: 48 } }[decodeURIComponent(m[1])] };
  if (path === "/api/jobs" && method === "POST") return { ok: true, id: fakeJob(body) };
  if (path === "/api/jobs/stop") {
    const job = jobs[body?.id];
    if (!job) return {};
    Object.values(units).forEach((list) => list.forEach((ch) => ch.units.forEach((u) => { if (u.state === "running") u.state = "queued"; })));
    delete jobs[job.id];
    const stopped = Object.assign({}, job, { state: "stopped", summary: "已停止", ended: Date.now() / 1000 });
    history.unshift(stopped); emit(stopped); return {};
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

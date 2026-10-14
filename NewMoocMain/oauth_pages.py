"""Minimal, self-contained callback pages with no credentials or external assets."""
from string import Template


_STATES = {
    "success": {
        "title": "登录成功",
        "description": "请返回终端，继续选择课程。",
        "detail": "助手正在验证账号并读取课程，此页可以关闭。",
        "icon": '<path d="m8 16 5 5L24 10"/>',
    },
    "waiting": {
        "title": "等待登录",
        "description": "请在智慧职教页面完成扫码或短信验证。",
        "detail": "完成后会自动返回结果，请保留终端窗口。",
        "icon": '<circle cx="16" cy="16" r="11"/><path d="M16 9v8l5 3"/>',
    },
    "error": {
        "title": "登录未完成",
        "description": "未收到有效的登录信息，请返回终端重试。",
        "detail": "若终端仍在等待，可按 Ctrl+C 退出后重新启动助手。",
        "icon": '<circle cx="16" cy="16" r="11"/><path d="M16 10v7m0 5h.01"/>',
    },
    "not_found": {
        "title": "页面不存在",
        "description": "请返回终端，使用助手打开的登录页面。",
        "detail": "此页可以关闭。",
        "icon": '<path d="m10 10 12 12m0-12L10 22"/>',
    },
}

_PAGE = Template('''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>${title} · ICVE AI Assistant</title>
  <script>
    // The callback query belongs to the local handler, never to the rendered UI.
    try { history.replaceState(null, '', location.pathname); } catch (_) {}
  </script>
  <style>
    :root {
      color-scheme: light;
      --background: #f5f4ed; --surface: #ffffff; --ink: #141413;
      --secondary: #3d3d3a;
      --primary: #141413; --primary-hover: #3d3d3a;
      --brand: #d97757; --brand-soft: #faf9f5;
      --line: rgba(31, 30, 29, 0.15);
      font-family: "Anthropic Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; min-height: 100svh; padding: 32px 20px;
      display: grid; place-items: center; background: var(--background); color: var(--ink);
    }
    main { width: 100%; max-width: 460px; }
    .brand { margin: 0 0 20px; color: var(--secondary); font-size: 14px; font-weight: 500; text-align: center; }
    .card { padding: 36px 32px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); }
    .icon { display: grid; place-items: center; width: 48px; height: 48px; margin-bottom: 24px;
      border-radius: 12px; border: 1px solid var(--line); color: var(--brand); background: var(--brand-soft); }
    .icon svg { width: 32px; height: 32px; fill: none; stroke: currentColor;
      stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    h1 { margin: 0 0 12px; font-size: 26px; line-height: 1.4; font-weight: 600; }
    .description { margin: 0; font-size: 16px; line-height: 1.75; }
    .detail { margin: 10px 0 28px; color: var(--secondary); font-size: 14px; line-height: 1.75; }
    button { width: 100%; min-height: 46px; padding: 12px 20px; border: 0; border-radius: 8px;
      background: var(--primary); color: #ffffff; font: inherit; font-size: 15px; font-weight: 500; cursor: pointer; }
    button:hover { background: var(--primary-hover); }
    button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
    .close-help { margin: 16px 0 0; color: var(--secondary); font-size: 14px; line-height: 1.75; }
    @media (max-width: 420px) {
      body { padding: 24px 16px; }
      .card { padding: 28px 24px; }
      h1 { font-size: 24px; }
    }
  </style>
</head>
<body>
  <main>
    <p class="brand">ICVE AI Assistant · 智慧职教助手</p>
    <section class="card" aria-labelledby="heading">
      <div class="icon" aria-hidden="true"><svg viewBox="0 0 32 32">${icon}</svg></div>
      <h1 id="heading">${title}</h1>
      <p class="description">${description}</p>
      <p class="detail">${detail}</p>
      <button type="button" id="close-page">关闭此页</button>
      <p id="close-help" class="close-help" role="status" aria-live="polite" hidden></p>
      <noscript><p class="close-help">请手动关闭此标签页，然后返回终端。</p></noscript>
    </section>
  </main>
  <script>
    document.getElementById('close-page').addEventListener('click', function () {
      const help = document.getElementById('close-help');
      help.hidden = false;
      help.textContent = '若页面未关闭，请手动关闭此标签页，然后返回终端。';
      window.close();
    });
  </script>
</body>
</html>
''')


def render_oauth_page(state):
    """Only fixed state copy is interpolated; tokens are never template inputs."""
    return _PAGE.substitute(_STATES[state])

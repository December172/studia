"""Offline interactive preview of the real native keyboard controls.

``python -m tools.ui_demo --native`` (or ``python -m tools.native_demo``) walks
through the production ``TerminalUI`` pages — platform menu, course picker,
single/multi selection, confirm, model picker, concurrency form and the AI/export
summary and result action pages — using only synthetic data.

Nothing here touches the platform, the AI service or the user's saved settings:
no login, no HTTP request, no ``SettingsStore`` write. The only reason it exists
is to let a human inspect the real keyboard interaction on a real terminal.
"""
import sys

from ai.client import AIConfig
from ui import Back, Cancelled, TerminalUI

COURSES = [
    {'id': 'demo1', 'courseName': '中外音乐与民族技艺实践', 'courseInfoName': '2026 春 · 示范课程', 'studySpeed': 36},
    {'id': 'demo2', 'courseName': '大学英语（综合）', 'courseInfoName': '2026 春 · 公共基础课', 'studySpeed': 82},
    {'id': 'demo3', 'courseName': '计算机应用基础', 'courseInfoName': '2026 春 · 公共基础课', 'studySpeed': 100},
    {'id': 'demo4', 'courseName': '思想道德与法治', 'courseInfoName': '2026 秋 · 公共基础课', 'studySpeed': 12},
]
TASKS = [
    {'id': 't1', 'name': '第一章 单元作业', 'status': '未提交', 'score': '--', 'left': '不限', 'kind': '作业'},
    {'id': 't2', 'name': '期中测验', 'status': '已提交', 'score': '92.5', 'left': '2', 'kind': '测验'},
    {'id': 't3', 'name': '附件作业 · 实践报告', 'status': '未作答', 'score': '--', 'left': '1', 'kind': '附件作业'},
    {'id': 't4', 'name': '第二章 单元作业', 'status': '未作答', 'score': '待批', 'left': '0', 'kind': '作业'},
]
MODEL_ROWS = [
    ('builtin-default', '默认供应商', 'gemini-3.8-flash', '未测试'),
    ('custom-1', '我的兼容服务', 'deepseek-chat', '✓ 上次通过'),
    ('custom-2', '备用供应商', 'qwen-max', '× 失败'),
]
CONFIG = AIConfig(base_url='https://example.org/v1', model='demo-model',
                  api_key='synthetic-key', provider_name='演示供应商')


def _wait(prompt):
    """Hold the current page until the user is ready for the next one."""
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)


def _page(prompt, action):
    """Preview one page; Esc / back / quit just skips to the next page."""
    _wait(prompt)
    try:
        return action()
    except (Back, Cancelled):
        return None


def main():
    ui = TerminalUI(dev=False)
    if not ui.native_enabled():
        print('原生预览需要真实终端（stdin 为 TTY 且未重定向输入）。', file=sys.stderr)
        print('非交互环境请使用：python -m tools.ui_demo', file=sys.stderr)
        return 1
    selected = TASKS[:2]
    state = {}
    try:
        with ui.session():
            ui.heading = '原生控件预览 · 合成数据'
            ui.account_name = '演示姓名'
            ui.header('4.2.0')
            print('\n以下页面均为真实原生控件，只使用合成数据，不访问平台 / AI / 配置。\n')

            _page('回车预览【平台菜单】… ', lambda: ui.choose_platform(0))

            _page('回车预览【课程选择】… ', lambda: ui.library(COURSES, 0, state))

            _page('回车预览【单选：任务】… ', lambda: ui.select(
                '选择一份任务', TASKS, lambda t: t['name'], multiple=False,
                status=lambda t: t['status'], selectable=lambda t: t['kind'] != '附件作业',
                state={}, actions={'r': '刷新'}))

            choice = _page('回车预览【多选：任务】… ', lambda: ui.select(
                '勾选任务', TASKS, lambda t: t['name'], multiple=True,
                status=lambda t: t['status'], state={},
                actions={'r': '刷新'}, selected=selected,
                selectable=lambda t: t['kind'] != '附件作业'))
            if isinstance(choice, list):
                selected = choice

            _page('回车预览【确认框】… ',
                  lambda: ui.confirm('开始处理所选任务（合成预览）？', False))

            _page('回车预览【模型配置】… ',
                  lambda: ui.model_picker(MODEL_ROWS, 'builtin-default',
                                          locked_ids=('builtin-default',)))

            _page('回车预览【并发设置】… ',
                  lambda: ui.concurrency_editor({'ai': 8, 'study': 4}))

            _page('回车预览【AI 执行摘要】… ', lambda: ui.review_ai(
                [(COURSES[0], task, 1) for task in selected], CONFIG, {}))

            _page('回车预览【导出摘要】… ', lambda: ui.review_export(
                [(COURSES[0], task, 1) for task in selected], {}))

            _wait('回车预览【执行结果页】… ')
            ui.results = [(task['name'], 'preview') for task in selected]
            ui.counts['preview'] = len(selected)
            ui.operation_result(['tiku/中外音乐与民族技艺实践/第一章 单元作业.md'])
    except (KeyboardInterrupt, SystemExit):
        return 0
    finally:
        print('\n原生预览结束。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

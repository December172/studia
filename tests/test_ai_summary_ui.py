"""Compact AI execution summary: no confidence, manual input or runtime footer."""
import io
import unittest

from rich.console import Console
from rich.text import Text

import theme
from ai.client import AIConfig
from reporting import ProgressEvent
from ui import TerminalUI


def make_view(answers, width=100):
    output = io.StringIO()
    values = iter(answers)

    def read():
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    view = TerminalUI(console=Console(file=output, width=width, theme=theme.THEME), input_fn=read)
    return view, output


class CompactSummaryTests(unittest.TestCase):
    def config(self):
        return AIConfig("https://example.org/v1", "model", "secret")

    def test_summary_keeps_connection_and_execution_mode_without_confidence_or_concurrency(self):
        view, output = make_view([""])
        options = {"connection_status": "连接测试通过 · 2026-10-10 12:34:56", "auto_submit": False}
        self.assertIs(view.review_ai([], self.config(), options), False)
        self.assertEqual(options["auto_submit"], False)
        self.assertNotIn("threshold", options)
        rendered = output.getvalue()
        for shown in ("供应商", "模型", "连接状态", "执行方式", "通过", "仅生成答案"):
            self.assertIn(shown, rendered)
        for removed in ("最低置信度", "答题并发", "置信度", "完成即补", "secret"):
            self.assertNotIn(removed, rendered)

    def test_report_only_mode_is_the_only_answer_setting(self):
        view, output = make_view(["s", "1", ""])
        self.assertIs(view.review_ai([], self.config(), {}), False)
        rendered = output.getvalue()
        self.assertIn("仅生成答案", rendered)
        self.assertIn("不交卷，无平台成绩", rendered)
        view, output = make_view(["s", "2", ""])
        self.assertIs(view.review_ai([], self.config(), {"auto_submit": False}), True)
        self.assertIn("答题并自动交卷", output.getvalue())

    def test_no_confidence_or_manual_input_methods_remain(self):
        view, output = make_view(["2"])
        for removed in ("confidence", "manual_answer"):
            self.assertFalse(hasattr(view, removed), removed)
        # The legacy entry point keeps its name but only chooses the mode.
        self.assertIs(view.answer_settings(), True)
        self.assertNotIn("置信度", output.getvalue())

    def test_connection_test_stays_available_without_verbose_summary(self):
        view, output = make_view(["t", ""])
        checks = []

        def check():
            checks.append(True)
            return "连接测试通过 · 2026-10-10 12:34:56"

        self.assertEqual(view.review_ai([], self.config(), {}, check_connection=check), True)
        self.assertEqual(checks, [True])
        rendered = output.getvalue()
        for shortcut in ("回车开始", "s 答题设置", "t 测试连接", "Esc 返回选择", "q 退出"):
            self.assertIn(shortcut, rendered)
        for clutter in ("ai_results/", "12:34:56", "题干和选项将发送", "开始前自动测试"):
            self.assertNotIn(clutter, rendered)

    def test_ai_plan_states_scope_without_implementation_details(self):
        view, output = make_view([])
        view.ai_plan([(object(), {}, 3)], True)
        rendered = output.getvalue()
        self.assertIn("自动交卷", rendered)
        self.assertIn("报告位置", rendered)
        for removed in ("并发", "置信度", "完成即"):
            self.assertNotIn(removed, rendered)

    def test_study_confirmation_omits_concurrency_explanation(self):
        view, output = make_view(["开始"])
        view.review_study([{"courseName": "课程"}], max_workers=3)
        rendered = output.getvalue()
        self.assertIn("完成课程学习任务", rendered)
        self.assertNotIn("并发", rendered)


class RuntimeRegionTests(unittest.TestCase):
    def test_runtime_frame_has_no_footer_or_startup_concurrency_log(self):
        for width in (120, 52):
            with self.subTest(width=width):
                output = io.StringIO()
                view = TerminalUI(console=Console(file=output, width=width, theme=theme.THEME))
                view.screen.active = True
                view.heading = "ICVE AI Assistant · test"
                view.log("AI 作答 1 / 1：题干")
                view.console.print(view.render())
                plain = Text.from_ansi(output.getvalue()).plain
                self.assertIn("运行日志", plain)
                self.assertNotIn("Ctrl+C", plain)
                self.assertNotIn("无需按回车", plain)
                self.assertNotIn("同时处理", plain)
                for line in plain.splitlines():
                    self.assertLessEqual(len(line), width)

    def test_header_hint_keeps_shortcuts_without_runtime_footer_wording(self):
        view, output = make_view([])
        view.header("test")
        rendered = output.getvalue()
        self.assertIn("Esc 返回", rendered)
        self.assertNotIn("无需按回车", rendered)

    def test_automatic_failure_result_is_labeled_without_manual_wording(self):
        view, output = make_view([""], width=120)
        view.begin_operation()
        view.on_progress(ProgressEvent("result", "第一章", status="needs_review"))
        self.assertEqual(view.operation_result(), 1)
        rendered = output.getvalue()
        self.assertIn("未完成", rendered)
        self.assertNotIn("待人工", rendered)
        self.assertNotIn("补答", rendered)
        self.assertIn("失败 1", rendered)


if __name__ == "__main__":
    unittest.main()

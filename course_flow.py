"""Course workspace navigation; platform operations remain in Application."""
from pathlib import Path

from AIMoocMain.export_questions import QuizExportHandler, paper_exam_id
from ai.runner import report_path
from ui import Back, Cancelled

REVIEW_MINUTES = 30


def task_identity(task):
    course, item, category = task
    zhs = course.get("zhs")
    if zhs is not None:
        return (str(zhs.get("courseId")), str(zhs.get("classId", "")),
                paper_exam_id(item), category)
    return (str(course.get("id") or course.get("courseInfoId") or ""),
            str(course.get("courseId") or ""), paper_exam_id(item), category)


class CourseWorkspace:
    def __init__(self, application):
        self.app = application
        self.view = application.view
        self.store = application.store
        self.platform = None
        self.client = None
        self.rows = []
        self.library_state = {}
        self.shares = {}  # 智慧树课程 id -> 考核规则各项满分

    def run(self):
        # Import here so the legacy entry points and dependency injection share
        # the same client factories.
        from app import AIMoocApi, ZYKMoocApi, ZHSApi

        self.view.workspace = True
        while True:
            try:
                if self.platform is None:
                    saved = [index for index, name in enumerate(("mooc", "zyk", "zhs", "cx"))
                             if self.store.session(name)]
                    self.platform = saved[0] if len(saved) == 1 else self.view.choose_platform(0)
                if self.client is None:
                    try:
                        if self.platform == 3:
                            self.client, self.rows = self.app.cx_client_and_courses(choose=False)
                        else:
                            cls = {1: ZYKMoocApi, 2: ZHSApi}.get(self.platform, AIMoocApi)
                            self.client, self.rows = self.app.client_and_courses(cls, choose=False)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        action = self.view.connection_problem(exc)
                        self.root_action(action)
                        continue
                selected = self.view.library(self.rows, self.platform, self.library_state)
                if isinstance(selected, str):
                    self.root_action(selected)
                    continue
                if selected:
                    self.course_loop(selected)
            except Back:
                continue

    def root_action(self, action):
        if action == "s":
            if self.app.manage_settings(current_platform={1: "zyk", 2: "zhs", 3: "cx"}.get(self.platform, "mooc")):
                self.view.set_user()
                self.client, self.rows = None, []
                self.library_state.clear()
        elif action == "t":
            platform = self.view.choose_platform(self.platform)
            if platform != self.platform:
                self.view.set_user()
                self.platform = platform
                self.client, self.rows = None, []
                self.library_state.clear()
        elif action == "r":
            self.view.set_user()
            self.client = None

    def course_loop(self, courses):
        task_state = {}
        offers = self.zhs_offers(courses) if self.platform == 2 else None
        while True:
            try:
                mode = self.view.course_menu(courses, self.platform, offers)
            except Back:
                return
            try:
                if mode == 1:
                    self.view.review_study(
                        courses, max_workers=self.store.concurrency()['study'],
                        activities=self.platform == 0,
                        notice=("逐个视频实时播放，耗时约为视频时长的三分之二；"
                                "平台限制同一账号同时只能看一个视频。") if self.platform == 2 else "")
                    result = self.execute(lambda: self.app.study(self.platform, self.client, courses))
                elif mode == 3 and self.platform == 2:
                    targets = offers["review"]  # 只复习考核里有学习习惯分的课
                    self.view.review_study(
                        targets, title="每日复习",
                        notice=("共享课从已看完的视频里按日期轮换，1 倍速实时重看 %d 分钟；"
                                "智慧课程会跳过。每天运行一次。" % REVIEW_MINUTES))
                    result = self.execute(lambda: self.app.study(
                        self.platform, self.client, targets, review_minutes=REVIEW_MINUTES))
                elif mode == 4 and self.platform == 2:
                    result = self.qa_area(offers["qa"])  # 只去考核里有问答互动分的课
                else:
                    result = self.tasks(courses, mode, task_state.setdefault(mode, {}))
                if result == 2:
                    return
            except Back:
                continue
            except (Cancelled, EOFError):
                raise
            except Exception as exc:
                self.view.begin_operation()
                self.view.pending_error = str(exc)
                if self.view.operation_result() == 2:
                    return

    def zhs_offers(self, courses):
        """所选课程里哪些用得上每日复习、问答区（看各门课的考核规则），{"review": [...], "qa": [...]}。
        某门课的规则读不到就不收窄，照旧可选。"""
        from ZHSMain.api import course_offers
        found = {"review": [], "qa": []}
        with self.view.activity("读取考核规则"):
            for row in courses:
                z = row.get("zhs") or {}
                shares = None
                if z.get("kind") == "shared":
                    try:
                        shares = self.client.score_shares(z["courseId"], z["recruitId"])
                    except Exception:  # 规则只是用来收窄菜单，读不到不该挡住学习
                        shares = None
                    if not isinstance(shares, dict):
                        shares = None
                    self.shares[z.get("courseId")] = shares or {}
                offer = course_offers(row, shares)
                for name in found:
                    if offer[name]:
                        found[name].append(row)
        return found

    def qa_area(self, courses):
        """问答区，和网页一样两件事：回答别人的提问；让 AI 出提问，你改好再发。只选条数。"""
        while True:
            action = self.view.qa_menu(self.qa_scores(courses))  # Esc 回课程菜单
            try:
                if action == 1:
                    saved = self.store.get("qa", {})
                    limit = self.view.qa_size("回答提问 / 每门课几条", (5, 10, 20),
                                              saved.get("limit") if isinstance(saved, dict) else None)
                    self.app.remember(lambda: self.store.set("qa", {"limit": limit}))
                    config = self.app.ai_configuration()

                    def answer():
                        # 统计只进汇总页；execute() 的返回值是文件路径，不能直接回传字典。
                        self.app.execute_zhs_qa(self.client, courses, config, {"limit": limit})
                        return []

                    result = self.execute(answer, "问答区答题")
                else:
                    result = self.ask_questions(courses)
            except Back:
                continue
            if result == 2:
                return 2

    def qa_scores(self, courses):
        """每门课一行：互动分和「我的互动」三个数，和网页问答区顶上那行一样。读不到的课不写。"""
        lines = []
        with self.view.activity("读取问答得分"):
            for row in courses:
                z = row.get("zhs") or {}
                try:
                    got = self.client.score_earned(z["courseId"], z["recruitId"])
                except Exception:  # 只是展示，读不到不挡操作
                    continue
                full = (self.shares.get(z["courseId"]) or {}).get("interactive")
                q = got["qa"]
                lines.append(f"{row.get('courseName', '')}：互动分 {got['interactive']:g}{f'/{full:g}' if full else ''}"
                             f" · 有效回答 {q['answers']} · 有效提问 {q['questions']} · 收获回答 {q['received']}")
        return lines

    def ask_questions(self, courses):
        """发起提问：一门课一批，AI 照这门课问答区里回答少的提问出题。"""
        course = courses[0]
        if len(courses) > 1:
            course = courses[self.view.choice("发起提问 / 哪门课", [
                (i, row.get("courseName", ""), "") for i, row in enumerate(courses, 1)]) - 1]
        saved = self.store.get("qa_ask", {})
        count = self.view.qa_size("发起提问 / 出几条", (1, 3, 5),
                                  saved.get("count") if isinstance(saved, dict) else None)
        self.app.remember(lambda: self.store.set("qa_ask", {"count": count}))
        texts = self.prepare_questions([course], {"count": count})
        if not texts:
            return 1

        def ask():
            # 正文已经确认过，这里只负责发布；提问不产生文件。
            self.app.execute_zhs_ask(self.client, [course], texts)
            return []

        return self.execute(ask, "问答区提问")

    def prepare_questions(self, courses, options):
        """AI 出题，再让用户删改；r 换一批就重出。返回要发的提问，不发返回空列表。

        失败要说清原因并允许重试：写日志的那一行回到菜单就看不见了。
        """
        while True:
            try:
                config = self.app.ai_configuration()
                with self.view.activity("生成提问"):
                    texts = self.app.generate_zhs_questions(self.client, courses, config, options)
            except (Cancelled, EOFError, Back):
                raise
            except KeyboardInterrupt:  # 生成阶段还能中断，不必退出整个工具
                self.view.note("已取消生成提问。", "warning")
                return []
            except Exception as exc:  # AI 或格式问题：说清楚，别把用户留在坏状态
                self.view.note(f"生成提问失败：{exc}", "error")
                if not self.view.confirm("重试生成", default=True):
                    return []
                continue
            if not texts:
                self.view.note("这次没出成可用的提问。", "warning")
                if self.view.confirm("换一批", default=True):
                    continue
                return []
            reviewed = self.view.qa_drafts(texts)
            if reviewed is None:  # 换一批
                continue
            if not reviewed:
                self.view.note("没有发布任何提问。", "warning")
            return reviewed

    def catalog_for(self, courses):
        if self.platform == 2:
            return self.app.zhs_task_catalog(self.client, courses)
        if self.platform == 3:
            return self.app.cx_task_catalog(self.client, courses)
        return self.app.task_catalog(self.client, courses)

    def tasks(self, courses, mode, state):
        try:
            catalog = self.catalog_for(courses)
        except Exception as exc:
            self.view.begin_operation()
            self.view.pending_error = str(exc)
            return self.view.operation_result()
        if not catalog:
            self.view.empty_tasks()
            return 1
        saved = self.store.get("export", {})
        export_options = {key: saved[key] for key in ("export_format", "quiz_mode", "include_my_answer")
                          if isinstance(saved, dict) and key in saved}
        answer_options = {"auto_submit": True, "save_to_platform": self.platform == 3}
        selected = state.get("selected", [])
        while True:
            # History/status fields can change between reads; selection belongs
            # to the task identity, not the entire response dictionary.
            selected_ids = {task_identity(task) for task in selected}
            selected = [task for task in catalog if task_identity(task) in selected_ids]
            choice = self.view.papers(catalog, selected=selected, state=state, refresh=True)
            if choice == "r":
                selected = state.get("selected", selected)
                try:
                    catalog = self.catalog_for(courses)
                except Exception as exc:
                    self.view.begin_operation()
                    self.view.pending_error = str(exc)
                    return self.view.operation_result()
                if not catalog:
                    self.view.empty_tasks()
                    return 1
                continue
            selected = choice
            state["selected"] = selected
            if not selected:
                return 1
            try:
                if mode == 3:
                    options = self.view.review_export(selected, export_options)
                    self.app.remember(lambda: self.store.set("export", options))
                    def export():
                        handler = QuizExportHandler(client=self.client, course_rows=courses,
                                                    selected_papers=selected, **options,
                                                    dev_mode=self.view.dev, on_progress=self.view.on_progress)
                        return [row[key] for row in handler.summary_rows for key in ("markdown", "docx") if row.get(key)]
                    result = self.execute(export, "导出题目")
                else:
                    config = self.app.ai_configuration()
                    answer_options["connection_status"] = self.app.ai_status_text(config)
                    auto_submit = self.view.review_ai(
                        selected, config, answer_options,
                        check_connection=lambda: self.app.test_ai_connection(answer_options.get("config")),
                        manage_models=self.app.manage_models,
                        connection_status=self.app.ai_status_text)
                    config = answer_options["config"]
                    def answer():
                        if self.platform == 2:
                            # 智慧树没有本地报告文件，执行器已记录结果与错误。
                            self.app.execute_zhs_ai(self.client, selected, config, auto_submit)
                            return []
                        if self.platform == 3:
                            # 超星同样只在平台侧保存答案或交卷，不写本地报告。
                            self.app.execute_cx_ai(self.client, selected, config, auto_submit)
                            return []
                        results = self.app.execute_ai(self.client, selected, config, auto_submit)
                        for report in results:
                            if report.get("status") in ("submitted", "preview"):
                                continue
                            if report.get("message"):
                                self.view.log(report["message"], "warning")
                            for entry in report.get("answers", []):
                                if entry.get("error"):
                                    self.view.log(f"{report['paper']}：{entry['error']}", "warning")
                        paths = []
                        for (course, item, _), report in zip(selected, results):
                            path = report_path("ai_results", self.client.user_id, course, paper_exam_id(item)).with_suffix(".md")
                            if path.exists():
                                paths.append(str(path))
                        return paths
                    result = self.execute(answer, "AI 答题")
                state.clear()
                return result
            except Back:
                continue

    def execute(self, operation, title=None):
        self.view.begin_operation()
        paths = []
        try:
            if title:
                with self.view.activity(title):
                    paths = operation() or []
            else:
                operation()
        except KeyboardInterrupt:
            self.view.cancelled = True
            self.view.log("操作已中断；未确认的交卷结果请到平台核对。", "warning")
        except Exception as exc:
            self.view.pending_error = str(exc)
        return self.view.operation_result([str(Path(path).resolve()) for path in paths])

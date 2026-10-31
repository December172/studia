"""Application orchestration. Menus/rendering are provided by the view (ui.py)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from MoocMain.log import Logger

from AIMoocMain.api import AIMoocApi
from AIMoocMain.main import AIMoocHandler
from AIMoocMain.exam_api import ExamAPI, ExamError, ExamServiceError, TaskUnavailableError
from AIMoocMain.export_questions import QuizExportHandler, collect_quiz_catalog, paper_display_name, paper_exam_id
from ZYKMoocMain.api import ZYKMoocApi
from ZYKMoocMain.main import ZYKMoocHandler
from ZHSMain.api import CaptchaRequired, StudyTimeLimit, ZHSApi
from ZHSMain.main import ZHSHandler
import ZHSMain.answer
from ZHSMain.answer import ZHSAnswerRunner
from ZHSMain.memory import AnswerMemory
from ZHSMain.login import qr_login
from NewMoocMain.oauth_login import oauth_login
from ai.client import AIClient, AIError
from ai.runner import AnswerRunner
from reporting import Reporter
from base.auth import AuthenticationExpired, PlatformUnavailable
from base.concurrency import INTERACTIVE_WORKERS
from settings import SettingsStore, SettingsError
from task_capabilities import task_availability

logger = Logger(__name__).get_log()


class Application:
    def __init__(self, view, store=None):
        self.view = view
        self.progress = Reporter(view.on_progress)
        self.store = store if store is not None else SettingsStore()
        # 显式退出后，下一次浏览器登录先走官方 /loginOut 清掉共享 cookie；
        # 成功拿到 token 才消费，失败/取消保留退出意图。
        self.browser_logout_pending = False

    def remember(self, operation):
        try:
            operation()
            return True
        except SettingsError as exc:
            self.view.note(str(exc) + "；本次改动未持久化。", "warning")
            return False

    def login(self):
        self.view.login_hint()
        while True:
            try:
                with self.view.activity("等待浏览器登录"):
                    token = oauth_login(timeout=300, force_logout=self.browser_logout_pending)
                if token:
                    self.browser_logout_pending = False
                    return "", "", token
                self.view.note("登录失败或超时。", "warning")
            except Exception as exc:
                logger.exception("OAuth 登录异常")
                self.view.note(f"登录失败：{exc}", "error")
            if not self.view.confirm("重试登录", default=True):
                from ui import Cancelled
                raise Cancelled()

    def zhs_login(self):
        from ui import Cancelled
        while True:
            self.view.note("请用智慧树 App 扫描浏览器中打开的二维码。")
            client = None
            try:
                with self.view.activity("等待智慧树扫码"):
                    client = ZHSApi()
                    if qr_login(client, on_status=lambda message: self.view.note(message)):
                        return "", "", client.access_token
                self.view.note("扫码未完成或已超时。", "warning")
            except Exception as exc:
                logger.exception("智慧树扫码登录异常")
                self.view.note(f"登录失败：{exc}", "error")
            if client is not None:
                client.close()
            if not self.view.confirm("重试扫码登录", default=True):
                raise Cancelled()

    def client_and_courses(self, api_class, login=None, *, choose=True):
        self.view.set_user()
        platform = {ZYKMoocApi: "zyk", ZHSApi: "zhs"}.get(api_class, "mooc")
        saved = self.store.session(platform) if login is None else None
        if saved:
            try:
                with self.view.activity("恢复已保存的登录状态"):
                    client = api_class(access_token=saved["access_token"])
                    rows = self.read_courses(client)
                self.view.note("已复用本机登录状态。")
                self.view.set_user(getattr(client, "user_info", None))
                return client, self.view.courses(rows) if choose else rows
            except AuthenticationExpired:
                self.store.clear_session(platform)
                self.view.note("保存的登录已过期，请重新登录；成功后自动保存。", "warning")
        if login is None and platform == "zhs":
            login = self.zhs_login
        username, password, token = (login or self.login)()
        with self.view.activity("读取课程列表"):
            client = api_class(token=token, username=username, password=password)
            if not client.access_token:
                raise PlatformUnavailable("未获取有效的平台登录状态，请重新登录或稍后重试")
            rows = self.read_courses(client)
        self.remember(lambda: self.store.save_session(platform, client))
        self.view.set_user(getattr(client, "user_info", None))
        return client, self.view.courses(rows) if choose else rows

    @staticmethod
    def read_courses(client):
        result = client.my_course_list(page_size=9999, strict=True)
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            raise PlatformUnavailable("课程列表读取失败，登录缓存未清除，请稍后重试")
        return result["rows"]

    def ai_configuration(self):
        config = self.store.ai_config()
        if config is not None:
            self.view.note(f"AI：{config.provider_name or '自定义供应商'} · {config.model} · {self.ai_status_text(config)}")
            return config
        config = self.manage_models()
        if config is None:
            from ui import Back
            raise Back()
        return config

    def manage_models(self):
        """One picker from settings and summaries; only saved configs can activate."""
        from ui import Back
        from ai.defaults import DEFAULT_PROVIDER_NAME
        cursor_id, message = None, ""
        while True:
            profiles = self.store.ai_profiles()
            active_id = next((profile.id for profile in profiles if profile.active), None)
            locked_ids = {profile.id for profile in profiles if profile.builtin}
            rows = []
            for profile in profiles:
                health = self.store.ai_health(profile.config)
                status = {"ok": "✓ 上次通过", "error": "× 失败"}.get(health.get("status"), "? 未测试")
                name = DEFAULT_PROVIDER_NAME if profile.builtin else profile.config.provider_name or "自定义供应商"
                rows.append((profile.id, name, profile.config.model, status))
            try:
                choice = self.view.model_picker(rows, active_id, cursor_id=cursor_id,
                                                message=message, locked_ids=locked_ids,
                                                test_connection=self.test_profile_connection)
                if choice.action == "back":
                    return self.store.ai_config()
                cursor_id = choice.profile_id
                if choice.action == "select":
                    self.store.activate_ai_profile(choice.profile_id)
                    return self.store.ai_config()
                if choice.profile_id in locked_ids:
                    raise SettingsError("默认供应商不可修改或删除")
                current = next((p.config for p in profiles if p.id == choice.profile_id), None)
                while True:
                    result = self.view.model_editor(current)
                    if result.action == "delete":
                        try:
                            if self.view.confirm("删除这份模型配置", default=False):
                                self.store.delete_ai_profile(choice.profile_id)
                                cursor_id, message = None, "已删除"
                        except Back:
                            continue
                    elif result.action == "save":
                        cursor_id = self.store.save_ai_profile(result.config, choice.profile_id)
                        message = self.test_ai_connection(result.config)
                    break
                if message:
                    self.view.note(message)
            except Back:
                continue
            except SettingsError as exc:
                message = str(exc)
                self.view.note(message, "error")

    def ai_status_text(self, config=None):
        try:
            config = config or self.store.ai_config()
            if config is None:
                return "未配置"
            health = self.store.ai_health(config)
        except SettingsError:
            return "配置无效，请重新配置"
        if not isinstance(health, dict) or not health:
            return "已配置 · 未测试"
        if health.get("status") == "ok":
            return "上次连接通过 · " + str(health.get("checked_at", "")).replace("T", " ")
        return "连接失败 · " + str(health.get("message", "请重新测试"))

    def test_profile_connection(self, profile_id):
        """Backend-only test for the picker; no rendering or profile activation."""
        from dataclasses import replace
        from MoocMain.log import redact
        try:
            config = next((p.config for p in self.store.ai_profiles() if p.id == profile_id), None)
            if config is None:
                return '连接失败：配置已不存在，请重新打开列表'
            previous = replace(config)
            try:
                AIClient(config).check_connection()
            except AIError as exc:
                reason = ' '.join(str(redact(str(exc))).split()).replace(config.api_key, '***')
                self.store.save_ai_health(config, 'error', reason)
                return '连接失败：' + reason
            self.store.save_ai_health(config, 'ok', previous_config=previous)
            return '连接通过'
        except SettingsError:
            return '测试结果无法保存，请检查配置文件权限后重试'
        except Exception:
            return '连接测试失败，请检查配置后重试'

    def verify_ai(self, config, client=None):
        from dataclasses import replace
        previous_config = replace(config)
        owns_client = client is None
        client = client or AIClient(config)
        try:
            with self.view.activity("测试 AI 连接"):
                client.check_connection()
            self.remember(lambda: self.store.save_ai_health(config, "ok", previous_config=previous_config))
            return client
        except BaseException as exc:
            if owns_client:
                client.session.close()
            if isinstance(exc, AIError):
                self.remember(lambda: self.store.save_ai_health(config, "error", str(exc)))
            raise

    def test_ai_connection(self, config=None):
        try:
            config = config or self.store.ai_config()
            if config is None:
                self.view.note("请先配置 AI 服务。", "warning")
                return "未配置"
            self.verify_ai(config)
        except (AIError, SettingsError) as exc:
            self.view.note(str(exc), "error")
        return self.ai_status_text(config)

    def study(self, version, client=None, selected=None):
        api_class, handler = {1: (ZYKMoocApi, ZYKMoocHandler),
                              2: (ZHSApi, ZHSHandler)}.get(version, (AIMoocApi, AIMoocHandler))
        if client is None:
            client, selected = self.client_and_courses(api_class)
        if not selected:
            self.view.cancelled = True
            return
        max_workers = self.store.concurrency()['study']
        inject = {}
        activity_ai = None
        if version == 0:
            # Only MOOC learning posts AI discussions and a 5-star evaluation; a
            # failure to obtain a healthy AI client aborts instead of silently
            # reporting a successful study run.
            activity_ai = self.verify_ai(self.ai_configuration())
            inject['activity_ai'] = activity_ai
        try:
            self.view.plan("学习管理", selected)
            if version == 2:
                note = ("智慧树学习：逐个视频实时播放（约 1.5 倍速），同一账号不能同时观看多个视频；"
                        "遇到验证码或当日学习时长上限会停止。")
            else:
                note = f"自动学习：最多 {max_workers} 项同时处理，完成即更新进度"
            if inject:
                note += "；并按主题自动参与课程讨论、提交五星课程评价，无需逐条确认"
            self.view.note(note)
            with self.view.activity("处理课程"):
                handler(client=client, course_rows=selected, on_progress=self.view.on_progress,
                        max_workers=max_workers, **inject)
        finally:
            if activity_ai is not None:
                activity_ai.session.close()

    def export(self):
        client, selected = self.client_and_courses(AIMoocApi)
        if not selected:
            self.view.cancelled = True
            return
        options = self.view.export_settings(self.store.get("export", {}))
        self.remember(lambda: self.store.set("export", options))
        selected_ids = []
        if options["quiz_mode"] == "single" and 3 in options["category_ids"]:
            with self.view.activity("读取章节测验"):
                catalog = collect_quiz_catalog(client, selected, category_id=3)
            quizzes = self.view.quizzes(catalog)
            selected_ids = [item["exam_id"] for item in quizzes]
            if not selected_ids and options["category_ids"] == [3]:
                self.view.cancelled = True
                return
        self.view.plan("题目导出", selected, options)
        with self.view.activity("导出题目"):
            return QuizExportHandler(client=client, course_rows=selected,
                                     selected_exam_ids=selected_ids, output_dir="tiku",
                                     dev_mode=self.view.dev, on_progress=self.view.on_progress, **options)

    def ai(self, version=0, login=None):
        if version != 0:
            self.view.note("AI 答题支持常规 MOOC / AI 优课。", "warning")
            return []
        client, courses = self.client_and_courses(AIMoocApi, login)
        if not courses:
            self.view.cancelled = True
            return []
        api = ExamAPI(client)
        categories = self.view.categories()
        if not categories:
            self.view.cancelled = True
            return []
        catalog = []
        with self.view.activity("读取可选任务"):
            for course in courses:
                for category in categories:
                    for item in api.catalog(course, category):
                        catalog.append((course, item, category))
        selected = self.view.papers(catalog)
        if not selected:
            self.view.cancelled = True
            return []
        config = self.ai_configuration()
        auto_submit = self.view.answer_settings()
        self.view.ai_plan(selected, auto_submit)
        return self.execute_ai(client, selected, config, auto_submit, api=api)

    def task_catalog(self, client, courses):
        api = ExamAPI(client)

        def status(exam_id):
            try:
                return api.completion_status(exam_id)
            except ExamError:
                return "unknown"

        # Read-only GETs, so they run side by side; order still follows courses/categories.
        with self.view.activity("读取课程任务"), ThreadPoolExecutor(INTERACTIVE_WORKERS) as pool:
            pairs = [(course, category) for course in courses for category in (1, 2, 3)]
            listed = [(course, item, category) for (course, category), items
                      in zip(pairs, pool.map(lambda pair: api.catalog(*pair), pairs)) for item in items]
            open_ids = list(dict.fromkeys(paper_exam_id(item) for _, item, category in listed
                                          if task_availability(item, category).available))
            statuses = dict(zip(open_ids, pool.map(status, open_ids)))
        return [(course, dict(item, _completion_status=statuses[paper_exam_id(item)]
                              if task_availability(item, category).available else "unsupported"), category)
                for course, item, category in listed]

    def execute_ai(self, client, selected, config, auto_submit, api=None):
        max_workers = self.store.concurrency()['ai']
        # Verify with synthetic material before paper() can start an attempt.
        runner = None
        results = []
        with self.view.activity("AI 答题"):
            self.progress.begin("AI 答题", len(selected), "份试卷")
            for course, item, category in selected:
                name = paper_display_name(item)
                availability = task_availability(item, category)
                if not availability.available:
                    message = availability.reason
                    logger.warning("%s：%s", name, message)
                    results.append({"paper": name, "status": "skipped", "message": message})
                    self.progress.result(name, "skipped")
                    continue
                try:
                    if runner is None:
                        ai_client = self.verify_ai(config)
                        api = api or ExamAPI(client)
                        runner = AnswerRunner(api, ai_client, auto_submit=auto_submit,
                                              on_progress=self.view.on_progress, max_workers=max_workers,
                                              manual_answer=getattr(self.view, "manual_answer", None),
                                              confirm_submit=getattr(self.view, "confirm_submit", None))
                    result = runner.run_paper(course, item, category)
                    results.append(result)
                except TaskUnavailableError as exc:
                    results.append({"paper": name, "status": "skipped", "message": str(exc)})
                    logger.warning("%s：%s", name, exc)
                    self.progress.result(name, "skipped")
                    continue
                except ExamError as exc:
                    result = {"paper": name, "status": "error", "message": str(exc),
                              "stop_queue": isinstance(exc, ExamServiceError)}
                    results.append(result)
                    logger.error("%s：%s", name, exc)
                status = result["status"]
                if status == "ai_error":
                    self.remember(lambda: self.store.save_ai_health(config, "error", result.get("message", "AI 服务失败")))
                self.progress.result(name, status)
                # A human pause ends the run locally: the queued papers are not
                # started and the draft stays resumable on the next selection.
                if status == "paused":
                    logger.warning("已暂停补答：%s；其余试卷未执行，可稍后继续。", name)
                    break
                if result.get("stop_queue") or status in ("submission_unknown", "submitting", "ai_error"):
                    # The message states why this run ended; do not guess a
                    # service failure when a human stopped a submission.
                    logger.warning("队列停止：%s；其余试卷未执行。", result.get("message") or "本次处理已停止")
                    break
        return results

    def zhs_task_catalog(self, client, courses):
        catalog = []
        with self.view.activity("读取课程任务"):
            for course in courses:
                catalog.extend(ZHSMain.answer.catalog(client, course))
        return catalog

    def execute_zhs_ai(self, client, selected, config, auto_submit):
        results = []
        ai_client = None
        with self.view.activity("AI 答题"):
            self.progress.begin("AI 答题", len(selected), "份试卷")
            try:
                for course, item, category in selected:
                    name = paper_display_name(item)
                    if ai_client is None:
                        ai_client = self.verify_ai(config)
                        runner = ZHSAnswerRunner(
                            client, ai_client, auto_submit=auto_submit,
                            on_progress=self.view.on_progress,
                            max_workers=self.store.concurrency()["ai"],
                            memory=AnswerMemory(
                                Path(self.store.path).parent / "zhs-answers.json"))
                    try:
                        result = runner.run_paper(course, item, category)
                        results.append(result)
                    except (CaptchaRequired, StudyTimeLimit) as exc:
                        results.append({"paper": name, "status": "error", "message": str(exc)})
                        self.progress.result(name, "error")
                        logger.warning("队列停止：%s；其余试卷未执行。", exc)
                        break
                    status = result["status"]
                    if status == "ai_error":
                        self.remember(lambda: self.store.save_ai_health(
                            config, "error", result.get("message", "AI 服务失败")))
                    self.progress.result(name, status)
                    if status in ("ai_error", "submission_unknown"):
                        logger.warning("队列停止：%s；其余试卷未执行。",
                                       result.get("message") or "本次处理已停止")
                        break
            finally:
                if ai_client is not None:
                    ai_client.session.close()
        return results

    def run(self):
        from course_flow import CourseWorkspace
        self.remember(self.store.ensure_default_model)
        return CourseWorkspace(self).run()

    def manage_concurrency(self):
        draft, message = self.store.concurrency(), ''
        while True:
            edited = self.view.concurrency_editor(draft, message=message)
            if edited is None:
                return False
            draft = edited
            try:
                self.store.save_concurrency(draft['ai'], draft['study'])
            except SettingsError as exc:
                message = str(exc)
                continue
            return True

    def manage_settings(self, current_platform="mooc"):
        from ui import Back
        message = ""
        while True:
            try:
                action = self.view.settings_menu(self.store.path, self.ai_status_text(), message=message,
                                                 concurrency=self.store.concurrency())
                message = ""
            except Back:
                return False
            if action == 0:
                return False
            if action == 1:
                try:
                    self.manage_models()
                except SettingsError as exc:
                    self.view.note(str(exc), "error")
            elif action == 2:
                if self.manage_concurrency():
                    message = '并发设置已保存，下次运行生效。'
            elif action in (3, 4, 5):
                platform = {3: "mooc", 4: "zyk", 5: "zhs"}[action]
                if self.remember(lambda: self.store.clear_session(platform)):
                    if platform != "zhs":
                        self.browser_logout_pending = True
                    label = {"mooc": "MOOC", "zyk": "资源库", "zhs": "智慧树"}[platform]
                    if platform == current_platform:
                        self.view.set_user()
                        self.view.note(f"已退出{label}登录，正在打开浏览器重新登录。", "success")
                        return True
                    message = f"已退出{label}登录。"
                else:
                    message = "退出失败，登录状态未更改，请重试。"

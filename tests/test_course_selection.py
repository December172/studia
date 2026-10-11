import unittest
from unittest.mock import Mock, patch

import io
from rich.console import Console
from ui import TerminalUI, Back
from app import Application
import theme
from AIMoocMain.main import AIMoocHandler
from ZYKMoocMain.main import ZYKMoocHandler


class CourseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.courses = [
            {"id": "a", "courseInfoId": "a", "courseName": "音乐", "courseInfoName": "第1期"},
            {"id": "b", "courseInfoId": "b", "courseName": "音乐", "courseInfoName": "第2期"},
        ]

    def view(self):
        return TerminalUI(console=Console(file=io.StringIO(), theme=theme.THEME))

    def test_choose_by_number_not_course_name(self):
        with patch('builtins.input', return_value='2'):
            self.assertEqual(self.view().courses(self.courses), [self.courses[1]])

    def test_back_returns_and_all_selects_all(self):
        with patch('builtins.input', return_value='b'):
            with self.assertRaises(Back):
                self.view().courses(self.courses)
        with patch('builtins.input', return_value='all'):
            self.assertEqual(self.view().courses(self.courses), self.courses)

    def test_study_fetches_before_selection_and_reuses_client(self):
        client = Mock()
        client.my_course_list.return_value = {"rows": self.courses}
        api_class, handler = Mock(return_value=client), Mock()
        def choose(rows):
            client.my_course_list.assert_called_once_with(page_size=9999, strict=True)
            self.assertEqual(rows, self.courses)
            return [rows[1]]
        view = self.view()
        store = Mock()
        store.session.return_value = None
        store.concurrency.return_value = {'ai': 1, 'study': 1}
        app = Application(view, store=store)
        with patch.object(app, 'login', return_value=('', '', 'token')), \
             patch.object(view, 'courses', side_effect=choose), \
             patch('app.AIMoocApi', api_class), patch('app.AIMoocHandler', handler):
            app.study(0)
        handler.assert_called_once_with(client=client, course_rows=[self.courses[1]], on_progress=view.on_progress,
                                        max_workers=1)

    def test_cancel_never_starts_processing(self):
        client, handler = Mock(), Mock()
        client.my_course_list.return_value = {"rows": self.courses}
        view = self.view()
        store = Mock()
        store.session.return_value = None
        app = Application(view, store=store)
        with patch.object(app, 'login', return_value=('', '', 'token')), \
             patch.object(view, 'courses', return_value=[]), \
             patch('app.AIMoocApi', return_value=client), patch('app.AIMoocHandler', handler):
            app.study(0)
        handler.assert_not_called()

    def test_both_handlers_process_only_selected_rows_without_refetch(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            with self.subTest(handler=cls.__name__):
                client = Mock()
                with patch.object(cls, 'process_course') as process:
                    cls(client=client, course_rows=[self.courses[1]])
                    process.assert_called_once_with(self.courses[1])
                    client.my_course_list.assert_not_called()
                with patch.object(cls, 'process_course') as process:
                    cls(client=client, course_rows=[])
                    process.assert_not_called()
                    client.my_course_list.assert_not_called()

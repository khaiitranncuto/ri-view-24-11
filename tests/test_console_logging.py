import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from process_utils import configure_utf8_console, console_print


class ConsoleLoggingTests(unittest.TestCase):
    def test_cp1252_console_falls_back_without_stopping(self):
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding='cp1252')
        with patch('sys.stdout', console):
            console_print('Tốc độ +20%; Bắt buộc đúng 281 câu.', flush=True)
        self.assertIn(b'281', buffer.getvalue())
        self.assertIn(b'\\u1ed1', buffer.getvalue())

    def test_redirected_console_uses_utf8(self):
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding='cp1252')
        with patch('sys.stdout', console), patch('sys.stderr', console):
            configure_utf8_console()
            console_print('Tốc độ đọc', flush=True)
        self.assertEqual(buffer.getvalue().decode('utf-8').strip(), 'Tốc độ đọc')

    def test_background_task_keeps_vietnamese_in_file_and_api(self):
        message = '[NARRATION_BUDGET] Tốc độ +20%; Bắt buộc đúng 281 câu.'
        details = 'Video nguồn: rì view 1411; giọng Ngọc Huyền.'
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding='cp1252')
        task_id = 'console-regression-test'
        errors = []
        app.TASKS[task_id] = {'status': 'running', 'persistent': False}
        def write_log():
            try:
                app.append_task_log(task_id, 'INFO', message, details)
            except Exception as exc:
                errors.append(exc)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'task.log'
                with patch.object(app, '_task_log_path', return_value=str(path)), \
                        patch('sys.stdout', console):
                    worker = threading.Thread(target=write_log)
                    worker.start()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertIn(message, path.read_text(encoding='utf-8'))
                self.assertIn(details, path.read_text(encoding='utf-8'))
                response = app.app.test_client().get('/api/task-status/' + task_id)
                self.assertEqual(response.status_code, 200)
                log = response.get_json()['logs'][-1]
                self.assertEqual(log['message'], message)
                self.assertEqual(log['details'], details)
        finally:
            app.TASKS.pop(task_id, None)

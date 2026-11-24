import os
import tempfile
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import task_store


class PersistentTaskStoreTests(unittest.TestCase):
    def test_large_fields_are_reused_during_progress_patch(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(task_store, 'JOB_DIR', directory):
            task_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            evidence = {'facts': ['source fact'] * 20000}
            task_store.save_task_state(task_id, {'status': 'running', 'evidence': evidence})
            path = task_store.task_state_path(task_id)
            with open(path, encoding='utf-8') as source:
                before = json.load(source)
            with mock.patch.object(task_store, '_atomic_write_json', wraps=task_store._atomic_write_json) as write:
                task_store.save_task_patch(task_id, {'progress': 47})
                self.assertEqual(write.call_count, 1)
            with open(path, encoding='utf-8') as source:
                after = json.load(source)
            self.assertEqual(before['_task_store_fields'], after['_task_store_fields'])
            self.assertLess(os.path.getsize(path), 1000)
            self.assertEqual(task_store.load_task_state(task_id)['evidence'], evidence)
            task_store.save_task_patch(task_id, {'evidence': {}})
            self.assertEqual(task_store.load_task_state(task_id)['evidence'], {})

    def test_legacy_state_migrates_and_concurrent_patches_preserve_fields(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(task_store, 'JOB_DIR', directory):
            task_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            task_store._atomic_write_json(task_store.task_state_path(task_id),
                                          {'status': 'running', 'archive': 'x' * 100000})
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda number: task_store.save_task_patch(task_id,
                    {str(number): number}), range(6)))
            state = task_store.load_task_state(task_id)
            self.assertEqual(state['archive'], 'x' * 100000)
            self.assertTrue(all(state[str(number)] == number for number in range(6)))

    def test_state_and_payload_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(task_store, "JOB_DIR", temp_dir):
            task_id = "11111111-1111-1111-1111-111111111111"
            task_store.save_task_state(task_id, {"status": "running", "progress": 25})
            task_store.save_task_payload(task_id, {"sentences": ["Cau mot"]})

            state = task_store.load_task_state(task_id)
            payload = task_store.load_task_payload(task_id)
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["progress"], 25)
            self.assertEqual(payload["sentences"], ["Cau mot"])
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(temp_dir)))

    def test_invalid_task_id_cannot_escape_job_directory(self):
        with self.assertRaises(ValueError):
            task_store.task_state_path("../outside")

    def test_failed_serialization_keeps_previous_checkpoint_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(task_store, 'JOB_DIR', directory):
            task_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            task_store.save_task_state(task_id, {'status': 'running'})
            with self.assertRaises(TypeError):
                task_store.save_task_state(task_id, {'invalid': object()})
            self.assertEqual(task_store.load_task_state(task_id)['status'], 'running')
            self.assertFalse(any(name.endswith('.tmp') for name in os.listdir(directory)))

    def test_failed_replace_keeps_previous_checkpoint_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(task_store, 'JOB_DIR', directory):
            task_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            task_store.save_task_state(task_id, {'status': 'running'})
            with mock.patch('task_store.os.replace', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    task_store.save_task_state(task_id, {'status': 'success'})
            self.assertEqual(task_store.load_task_state(task_id)['status'], 'running')
            self.assertFalse(any(name.endswith('.tmp') for name in os.listdir(directory)))

    def test_worker_startup_failure_is_persisted_before_heartbeat(self):
        import render_worker
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(task_store, 'JOB_DIR', directory), \
             mock.patch.object(render_worker, 'JOB_DIR', directory), \
             mock.patch('render_worker._run_render', side_effect=ValueError('Invalid render payload')):
            task_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            task_store.save_task_state(task_id, {'status': 'queued', 'worker_pid': os.getpid()})
            with self.assertRaisesRegex(ValueError, 'Invalid render payload'):
                render_worker.main(task_id)
            state = task_store.load_task_state(task_id)
            self.assertEqual(state['status'], 'failed')
            self.assertIn('Invalid render payload', state['message'])


if __name__ == "__main__":
    unittest.main()

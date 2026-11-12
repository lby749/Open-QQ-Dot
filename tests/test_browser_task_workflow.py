import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from browser_automation import IsolatedBrowserSession
from notification_manager import NotificationManager
from task_manager import TaskManager, TaskStatus


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html;charset=utf-8')
        self.end_headers()
        self.wfile.write(b'''<html><head><title>Workflow test</title></head><body>
            <input id="query"><button id="run" onclick="document.querySelector('#result').textContent=document.querySelector('#query').value">Run</button>
            <p id="result"></p></body></html>''')


@unittest.skipUnless(importlib.util.find_spec('playwright') and Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').is_file(),
                     'requires optional Playwright and installed Edge')
class BrowserWorkflowTests(unittest.TestCase):
    def test_task_observes_plans_interacts_and_persists_receipts_in_real_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            friend = root / 'friends/10001'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'computer_tools': {'enabled': True, 'project_path': str(root)}}))
            manager = TaskManager(root / 'tasks.db', root)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f'http://127.0.0.1:{server.server_port}'
            session = IsolatedBrowserSession('10001', base_dir=root / 'sessions',
                                             allowed_local_origins=[origin], use_headless_chromium=True)
            steps = [{'action': 'input', 'selector': '#query', 'text': 'AUTONOMY_OK'},
                     {'action': 'click', 'selector': '#run'}, {'action': 'read', 'selector': '#result'}, {'action': 'done'}]
            try:
                tid = manager.create_task('Browser workflow', '把 AUTONOMY_OK 输入并显示在结果里', category='browser',
                    owner_id='10001', max_retries=0, initial_checkpoint={
                        'url': origin, 'browser_autoplan': True, 'expected_text': 'AUTONOMY_OK'})
                with patch('browser_automation.get_browser_session', return_value=session), \
                        patch('multi_agent_orchestrator.call_single_agent', side_effect=[(json.dumps(step), None) for step in steps]) as model, \
                        patch.object(NotificationManager, 'get_instance', return_value=NotificationManager(root / 'notices.db')):
                    manager._process_single_task(manager.get_task(tid))
                task = manager.get_task(tid)
                self.assertTrue(model.call_args_list)
                self.assertTrue(all('"html":' not in call.args[1] for call in model.call_args_list))
                self.assertEqual(task.status, TaskStatus.COMPLETED, task.error)
                self.assertEqual([step['action'] for step in task.checkpoint['browser_history']], ['input', 'click'])
                self.assertNotIn('browser_inflight', task.checkpoint)
                self.assertTrue(Path(task.artifacts[0]).read_bytes().startswith(b'\x89PNG'))
                self.assertIn('AUTONOMY_OK', task.result)
            finally:
                session.close()
                manager.stop_worker()
                server.shutdown()
                server.server_close()

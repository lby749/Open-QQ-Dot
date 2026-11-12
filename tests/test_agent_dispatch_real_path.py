"""call_single_agent / orchestrate against a fake Codex *executable* (not a monkeypatched function).

Everything between the orchestrator and the OS process is the production path: ModelDispatcher's
gate, main.codex_text routing, _codex_text argument building, run_codex_cli and JSONL event parsing.
Only the model is fake: a script that echoes the prompt as `codex exec --json` events. A previous
version of call_single_agent called the backends with the wrong signatures, and the tests mocked it,
so every multi-agent call failed in production while the suite stayed green.
"""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE_CODEX = textwrap.dedent('''
    import json, sys
    sys.stdin.reconfigure(encoding="utf-8")  # the bridge writes UTF-8, like the real Codex CLI expects
    sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    mode = open(sys.argv[1], encoding="utf-8").read().strip() if len(sys.argv) > 1 else "ok"
    if mode == "fail":
        print("fatal: simulated backend failure", file=sys.stderr)
        sys.exit(3)
    events = [
        {"type": "thread.started", "thread_id": "fake-thread-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ECHO:" + prompt.strip()[:40]}},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
    ]
    for event in events:
        print(json.dumps(event))
''')


class RealDispatchPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.workdir = self.root / 'friend'
        self.workdir.mkdir()
        script = self.root / 'fake_codex.py'
        script.write_text(FAKE_CODEX, encoding='utf-8')
        self.mode_file = self.root / 'mode.txt'
        self.mode_file.write_text('ok', encoding='utf-8')
        # cli_path() accepts any existing file; a .cmd wrapper makes it executable on Windows.
        if os.name == 'nt':
            self.codex = self.root / 'codex.cmd'
            self.codex.write_text(f'@chcp 65001 >nul\r\n@"{sys.executable}" "{script}" "{self.mode_file}" %*\r\n', encoding='utf-8')
        else:
            self.codex = self.root / 'codex'
            self.codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{self.mode_file}" "$@"\n', encoding='utf-8')
            self.codex.chmod(0o755)
        self.profile = {'qq_id': '10001', 'model_api': {'provider': 'codex', 'codex_path': str(self.codex)}}
        # The fake CLI never leaves the machine; the production overseas-service guard
        # still requires a configured proxy, so provide a deterministic loopback value.
        self._proxy = os.environ.get('HTTPS_PROXY')
        os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9'
        os.environ['HTTP_PROXY'] = 'http://127.0.0.1:9'

    def tearDown(self):
        if self._proxy is None:
            os.environ.pop('HTTPS_PROXY', None)
            os.environ.pop('HTTP_PROXY', None)
        else:
            os.environ['HTTPS_PROXY'] = self._proxy
            os.environ['HTTP_PROXY'] = self._proxy
        self._tmp.cleanup()

    def test_call_single_agent_returns_real_backend_output(self):
        import multi_agent_orchestrator as mao
        text, thread = mao.call_single_agent('codex', '请总结这段代码', self.workdir, self.profile,
                                             ephemeral=True, raise_on_error=True)
        self.assertEqual(text, 'ECHO:请总结这段代码')
        self.assertEqual(thread, 'fake-thread-1')

    def test_backend_failure_raises_instead_of_masquerading_as_output(self):
        import multi_agent_orchestrator as mao
        from agent_errors import AgentError
        self.mode_file.write_text('fail', encoding='utf-8')
        with self.assertRaises(AgentError):
            mao.call_single_agent('codex', 'x', self.workdir, self.profile, ephemeral=True, raise_on_error=True)
        # Without raise_on_error (chat), the failure is reported as text, never as the agent's answer.
        text, thread = mao.call_single_agent('codex', 'x', self.workdir, self.profile, ephemeral=True)
        self.assertNotIn('ECHO:', text)
        self.assertIsNone(thread)

    def test_single_provider_orchestrate_goes_through_the_same_path(self):
        import multi_agent_orchestrator as mao
        profile = dict(self.profile, model_api=dict(self.profile['model_api'], multi_agent={'enabled': True}))
        text, _ = mao.orchestrate('hello agents', self.workdir, ephemeral=True, profile=profile)
        self.assertIn('ECHO:hello agents', text)

    def test_dispatcher_slot_wait_honours_cancellation(self):
        import threading
        from model_dispatcher import ModelDispatcher
        dispatcher = ModelDispatcher.get_instance()
        sem = dispatcher._semaphores.setdefault('fake_provider', threading.Semaphore(2))
        sem.acquire(); sem.acquire()
        try:
            calls = []

            def stop():
                calls.append(1)
                raise InterruptedError('cancelled while waiting')
            with self.assertRaises(InterruptedError):
                dispatcher.execute_with_budget_and_gate('fake_provider', lambda: ('never', None),
                                                        timeout_sec=30, should_stop=stop)
            self.assertEqual(len(calls), 1)
        finally:
            sem.release(); sem.release()


if __name__ == '__main__':
    unittest.main()

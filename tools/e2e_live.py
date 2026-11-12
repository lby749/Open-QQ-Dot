"""Opt-in end-to-end smoke run against the real agent CLIs installed on this machine.

Every step is labelled LIVE (a real model CLI / real process did it) or MOCK (a fake stood in), so a
report can never pass off a faked step as verified execution. Everything runs in a disposable home
(QQ_BRIDGE_HOME is a fresh temp folder), so the real friends, tasks and approvals are not touched.
The real CLIs use your own installed credentials and spend a few short turns of quota.

    python tools/e2e_live.py                    # mailbox + task scenarios, real CLIs where installed
    python tools/e2e_live.py --scenario mailbox
    python tools/e2e_live.py --mock             # fake CLIs only (no quota, for checking the harness)

Scenarios:
  mailbox  Two independent agents (builder, reviewer) exchange a request and a reply through the
           persistent mailbox; the reviewer is woken by a TaskManager task and answers with its CLI.
  task     A QQ-style code task ("fix the failing test") runs through TaskManager with the agent CLI;
           TaskVerifier re-runs the project's tests independently and the owner is notified.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS: list[tuple[str, str, str]] = []  # (label, step, detail)


def step(label: str, name: str, detail: str = ''):
    STEPS.append((label, name, detail))
    print(f'[{label:4}] {name}' + (f' :: {detail}' if detail else ''), flush=True)


FAKE_CODEX = textwrap.dedent('''
    import json, sys
    sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    for event in ({"type": "thread.started", "thread_id": "mock"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "MOCK reply to: " + prompt[-80:]}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}):
        print(json.dumps(event))
''')


def fake_cli(home: Path) -> Path:
    script = home / 'fake_codex.py'
    script.write_text(FAKE_CODEX, encoding='utf-8')
    path = home / ('codex.cmd' if os.name == 'nt' else 'codex')
    path.write_text(f'@"{sys.executable}" "{script}" %*\r\n' if os.name == 'nt'
                    else f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
    if os.name != 'nt':
        path.chmod(0o755)
    return path


def wait_for(predicate, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def setup(home: Path, mock: bool):
    """Fresh home with one friend (QQ 10001) configured for Codex; returns (label, managers...)."""
    os.environ['QQ_BRIDGE_HOME'] = str(home)
    sys.path.insert(0, str(ROOT))
    from approval_manager import ApprovalManager
    from notification_manager import NotificationManager
    from task_manager import TaskManager
    from agent_messaging import AgentMailbox
    real = shutil.which('codex')
    codex_path = str(fake_cli(home)) if mock or not real else real
    label = 'MOCK' if mock or not real else 'LIVE'
    friend = home / 'friends' / '10001'
    (friend / 'workspace').mkdir(parents=True)
    profile = {'qq_id': '10001', 'model_api': {'provider': 'codex', 'codex_path': codex_path},
               'computer_tools': {'enabled': True, 'commands': True, 'project_path': str(friend / 'workspace')}}
    (friend / 'friend.json').write_text(json.dumps(profile), encoding='utf-8')
    tasks = TaskManager(db_path=home / 'data' / 'tasks.sqlite3', workspace=home)
    TaskManager._instance = tasks
    ApprovalManager._instance = ApprovalManager(db_path=home / 'data' / 'approvals.sqlite3')
    NotificationManager._instance = NotificationManager(home / 'data' / 'notifications.sqlite3')
    AgentMailbox._instance = AgentMailbox(home / 'data' / 'agent_mail.sqlite3')
    sent = []
    NotificationManager._instance.register_channel_sink(lambda m: sent.append(m) or True, name='capture')
    step('MOCK', 'QQ transport', 'notices are captured locally instead of sent through the QQ link')
    step(label, 'agent CLI', codex_path)
    return label, tasks, AgentMailbox._instance, sent, friend


def scenario_mailbox(label, tasks, mailbox, sent, friend) -> bool:
    builder = mailbox.register_agent('10001', 'demo', 'builder', 'codex', capabilities=['code'])
    reviewer = mailbox.register_agent('10001', 'demo', 'reviewer', 'codex', capabilities=['review'], wake=True,
                                      description='You review short Python snippets and answer in one sentence.')
    step('LIVE', 'mailbox', f'registered {builder} and {reviewer} (SQLite, this process)')
    ask = mailbox.send(builder, reviewer, 'Review this function in one sentence: def add(a, b): return a - b',
                       subject='add()')
    step('LIVE', 'send request', f'{ask.message_id} in {ask.thread_id}; reviewer woken through TaskManager')
    reply = mailbox.wait_for_reply(ask.message_id, timeout=240)
    if reply is None:
        step('FAIL', 'reply', 'no reply within 240 s; ' + '; '.join(
            f'{t.task_id}:{t.status.value}:{t.error[:120]}' for t in tasks.list_tasks(owner_id='10001')))
        return False
    step(label, 'reviewer answered', reply.body[:300].replace('\n', ' '))
    receipt = mailbox.receipt(ask.message_id, reviewer)
    step('LIVE', 'receipt', f'original message {receipt}; reply in thread {reply.thread_id}')
    return receipt == 'acked'


def scenario_task(label, tasks, mailbox, sent, friend) -> bool:
    from task_manager import TaskStatus
    import task_spec
    workspace = friend / 'workspace'
    (workspace / 'tests').mkdir(exist_ok=True)
    (workspace / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    (workspace / 'tests' / 'test_calc.py').write_text(
        'import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n'
        'from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n', encoding='utf-8')
    text = f'修复 calc.py 里 add 的 bug；测试: "{sys.executable}" -m pytest -q；文件: calc.py'
    spec = task_spec.build_task_spec(text, 'code', 'codex')
    if label == 'MOCK':
        # Inside tasks Codex speaks the app-server protocol, which the exec-only fake cannot; a mock
        # executor makes the edit instead. Verification and notification below still run for real.
        def mock_fix(task, log):
            (workspace / 'calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
            return {'output': 'MOCK: fixed add()', 'modified_files': ['calc.py'],
                    'target_files': task.checkpoint.get('target_files', []),
                    'test_command': task.checkpoint.get('test_command', '')}
        tasks.register_custom_executor('code', mock_fix)
        step('MOCK', 'code agent', 'mock executor edits calc.py')
    tid = tasks.create_task(title='Fix add()', description=text, category='code', owner_id='10001',
                            initial_checkpoint=spec, max_duration_seconds=600, max_retries=0)
    step('LIVE', 'QQ task spec', json.dumps(spec, ensure_ascii=False))
    done = wait_for(lambda: (t := tasks.get_task(tid)) and t.status in (
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.WAITING_APPROVAL,
        TaskStatus.WAITING_RESOURCE) and t, timeout=480)
    task = tasks.get_task(tid)
    if not done:
        step('FAIL', 'task', f'still {task.status.value} after 480 s')
        return False
    step(label, 'agent run', f'status {task.status.value}; {task.error[:200] or "no error"}')
    info = task.verification_info or {}
    step('LIVE', 'independent verification', f"{info.get('verifier_type')}: {str(info.get('details'))[:200]}")
    notice = wait_for(lambda: [m.title for m in sent if tid in m.content or tid in str(m.metadata)], timeout=10)
    step('LIVE', 'owner notice', ', '.join(notice or ['none']))
    return task.status == TaskStatus.COMPLETED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scenario', choices=('mailbox', 'task', 'all'), default='all')
    parser.add_argument('--mock', action='store_true', help='use fake CLIs (no quota)')
    parser.add_argument('--keep', action='store_true', help='keep the temp home for inspection')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    home = Path(tempfile.mkdtemp(prefix='openqqdot-e2e-'))
    ok = False
    try:
        ok = run_scenarios(args, home)
    except Exception as exc:
        step('FAIL', 'harness', f'{type(exc).__name__}: {exc}')
    finally:
        live = sum(1 for s in STEPS if s[0] == 'LIVE')
        mocked = sum(1 for s in STEPS if s[0] == 'MOCK')
        print(f'=== {"PASS" if ok else "FAIL"}: {live} LIVE steps, {mocked} MOCK steps, home {home}')
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)
    return 0 if ok else 1


def run_scenarios(args, home: Path) -> bool:
    label, tasks, mailbox, sent, friend = setup(home, args.mock)
    if not tasks.start_worker():
        step('FAIL', 'worker', 'could not take the worker lock')
        return False
    ok = True
    try:
        for name, run in (('mailbox', scenario_mailbox), ('task', scenario_task)):
            if args.scenario in (name, 'all'):
                print(f'--- scenario: {name}', flush=True)
                ok = run(label, tasks, mailbox, sent, friend) and ok
    finally:
        tasks.stop_worker()
    return ok


if __name__ == '__main__':
    sys.exit(main())

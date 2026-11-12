"""Opt-in local Codex CLI smoke test. Requires legitimate installed credentials.

Run inside a new authorized test directory; never automates account login.
"""
import argparse
from pathlib import Path
from uuid import uuid4

from codex_app_server import CodexAppServerClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True, help='Authorized disposable test workspace')
    parser.add_argument('--owner-id', required=True, help='Local authenticated test user id')
    parser.add_argument('--model', default=None)
    args = parser.parse_args()
    wd = Path(args.workspace).resolve()
    if not wd.is_dir():
        parser.error('--workspace must be an existing directory; refuse to create arbitrary directories')
    if not args.owner_id.strip():
        parser.error('--owner-id must be non-empty')
    client = CodexAppServerClient()
    task_id = f'smoke_{uuid4().hex}'
    try:
        thread = client.start_thread(cwd=wd, task_id=task_id, owner_id=args.owner_id,
                                     model=args.model, sandbox_policy={'mode': 'read-only'})
        result = client.execute_turn(thread, 'Reply with only the string OPEN_QQ_DOT_SMOKE_OK. Do not modify any files.',
                                     task_id=task_id, owner_id=args.owner_id, timeout_sec=120)
        print('result_event:', result['event'])
        print('result_status:', result['status'])
        print('output:', result.get('output', '')[:2000])
        print('tokens_used:', result.get('tokens_used'))
        if result['event'] != 'COMPLETED' or result['status'] != 'completed':
            raise SystemExit(2)
    finally:
        client.close()


if __name__ == '__main__':
    main()

"""Trusted *local operator* provisioning for external CLI/MCP/Python agents.

This is an administration command, NOT an API endpoint or an MCP tool. The
operator must run it on the Open QQ Dot host with access to its shared data
folder. Never expose this script to untrusted agent tools or web callers.

Credentials are persistent bearer tokens. Protect the files and use separate
OS accounts/workspaces when running untrusted third-party code.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_messaging import AgentMailbox, agent_id_for, issue_agent_token, verify_agent_token  # noqa: E402
from house_settings import data_dir, root  # noqa: E402


def provision(argv=None):
    p = argparse.ArgumentParser(description='Locally provision a generic Agent Bridge identity')
    p.add_argument('--owner', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--provider', default='external')
    p.add_argument('--capability', action='append', default=[])
    p.add_argument('--description', default='')
    p.add_argument('--wake', action='store_true', help='Only supported by built-in Codex/Claude TaskManager workers')
    p.add_argument('--replace-file', action='store_true', help='Recreate an unreadable local credential file')
    args = p.parse_args(argv)
    agent_id = agent_id_for(args.owner, args.project, args.name)
    if args.wake and args.provider not in ('codex', 'claude'):
        p.error('External providers must poll their own inbox; only supported native workers can use --wake')
    # Separate per-agent credentials. This does not give OS process isolation on Windows.
    folder = data_dir() / 'agent_bridge_credentials' / args.owner / args.project
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == 'posix':
        for directory in (data_dir() / 'agent_bridge_credentials', folder.parent, folder):
            directory.chmod(0o700)
    token_path = folder / (args.name + '.token')
    token = issue_agent_token(agent_id)
    if token_path.exists():
        if token_path.is_symlink():
            raise PermissionError('Refusing to overwrite a symlinked token file')
        if not args.replace_file:
            actual = token_path.read_text(encoding='utf-8').strip()
            if verify_agent_token(actual) != agent_id:
                raise PermissionError('Existing credential file does not match this agent; use --replace-file')
    # Create with mode 0600 on POSIX; Windows must additionally enforce ACLs.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if args.replace_file else os.O_EXCL)
    if not token_path.exists() or args.replace_file:
        fd = os.open(token_path, flags, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(token + '\n')
    if os.name == 'posix':
        token_path.chmod(0o600)
    mailbox = AgentMailbox.get_instance()
    mailbox.register_agent(args.owner, args.project, args.name, args.provider,
                           capabilities=args.capability, description=args.description, wake=args.wake)
    # No secrets in stdout or process arguments. Copy this environment into a trusted client.
    return {'agent_id': agent_id, 'credential_file': str(token_path.resolve()),
            'environment': {'AGENT_MAIL_TOKEN_FILE': str(token_path.resolve()),
                            'QQ_BRIDGE_HOME': str(root().resolve())},
            'mcp_stdio': {'command': sys.executable,
                          'args': ['-X', 'utf8', '-u', str(ROOT / 'agent_mailbox_mcp.py')],
                          'env': {'AGENT_MAIL_TOKEN_FILE': str(token_path.resolve()),
                                  'QQ_BRIDGE_HOME': str(root().resolve())}},
            'cli': [sys.executable, str(ROOT / 'agent_bridge_cli.py')],
            'warning': 'Only run under a trusted OS identity. This is not isolated from same-user processes.'}


if __name__ == '__main__':
    try:
        print(json.dumps(provision(), ensure_ascii=False, indent=2))
    except (PermissionError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
